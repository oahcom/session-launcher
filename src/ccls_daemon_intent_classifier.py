#!/usr/bin/env python3
"""
CCS 输入意图判定 + 重复提醒 daemon (R70 Round 1 — deliverable #1, v2)

User spec:
  "deamon 当用户输入给 ccs 时判断这些是不是疑问或者任务，
   deamon 需要重复提醒 ccs（你真的解决的用户的问题了吗？你真的有严格按用户的提示去完成任务吗？)"

设计决策（v2 修复 Codex 20 维审查发现的 P0/P1）：
- P0#2 死去重：原 hash(constant_string) 使第二次起永远 False → 改为纯冷却门控。
- D3 误判 ccs 自身输出：daemon **不分类 ccs 输出**，仅靠 pane 无新产出判定停滞；
       提醒框架为「停滞自检」（复用 _CORE_QUESTIONS 两问），不声称收到用户指令
       （user 意图判定属 core.send→inject_reminder，daemon 不负该责，避免虚假前提）。
- X1 缺单实例锁：加 fcntl.flock pidfile（对齐架构红线 3）。
- C1 任务动词只看前 12 字：改为全串扫描。
- C2 英文任务漏判：加英文任务动词前缀。
- D1 send-keys 多行：用 send-keys -l 逐行 literal 发送。
- P1 _states 无回收：每轮剔除已不存在的 session。

ponytail: 轮询 10s，覆盖 ~50 个 ccs-* tmux session；并发 >100 改 event-driven。
"""
from __future__ import annotations
import os, time, subprocess, sys, fcntl, threading
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
BUS_CLIENT = os.path.join(HERMES_HOME, "scripts", "bus_client.py")
# 漂移告警时效：检测器每 3600s 重跑一次（routing_daemon.DRIFT_INTERVAL）。
# 关键：不再漂移的角色【不会被更新】，其旧行的 ts 停留在最后一次漂移时刻，
# 因此 TTL 必须【略大于一个检测周期】—— 否则本轮刚报的告警会在下轮检测前
# 就被判过期；取 1.5 倍既覆盖调度抖动，又能在「上轮报过、本轮没报」后
# 于约 1.5~2.5 小时内自然过期。实测：detect_drift 修复时间窗后 scout 已
# 不再漂移，但其 17:15 的旧告警仍会持续注入 pane。
_DRIFT_ALERT_TTL = 5400  # 1.5 × DRIFT_INTERVAL
PID_FILE = Path(HERMES_HOME) / "run" / "ccls_intent_daemon.pid"


# 意图判定单一真相源：复用 user_intent_reminder 的 classify_intent / Intent / _CORE_QUESTIONS，
# 不再各自维护副本（v3 收敛：消除审查发现的重复定义与符号覆盖冲突）。
try:
    from user_intent_reminder import classify_intent, Intent, _CORE_QUESTIONS  # type: ignore
except Exception:  # pragma: no cover - 同目录兜底
    from pathlib import Path as _P
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "user_intent_reminder",
        str(_P(__file__).resolve().parent / "user_intent_reminder.py"))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    classify_intent = _mod.classify_intent
    Intent = _mod.Intent
    _CORE_QUESTIONS = _mod._CORE_QUESTIONS

# 生命周期加载（ponytail: 复用 survival_monitor 的 _load_role_lifecycles，
# 通过 realpath 规范化防路径遍历；ondemand 角色不主动唤醒，避免无谓轰击）。
try:
    _PIPE_SRC = os.path.realpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "session-pipeline", "src"))
    if _PIPE_SRC not in sys.path:
        sys.path.insert(0, _PIPE_SRC)
    from routing.survival_monitor import _load_role_lifecycles as _load_role_lifecycles  # type: ignore
except Exception:  # pragma: no cover - 跨项目路径不可用时回退
    def _load_role_lifecycles() -> dict[str, str]:
        return {}


# 同目录复用 tmux 投递（脚本以本目录为 sys.path[0] 启动，直接 import 即可）
try:
    from tmux_ops import _tmux_send  # type: ignore
except Exception:  # pragma: no cover - 加载失败时降级为不可注入
    def _tmux_send(tmux_name: str, message: str, **_kw) -> None:
        raise RuntimeError("tmux_ops 不可用")


def _inject_text(session: str, text: str) -> None:
    """把多行文本作为**一条**消息注入 pane。

    不可逐行 send-keys + 逐行 Enter：那样每行独立成一条消息（实测
    coordinator 收到 22 字符的裸首行「【停滞自检 · 每轮必对齐】第1次重复自检：」），
    角色为每行各起一个回合应答。_tmux_send 整条投递（chunk + 单个 Enter），
    payload 内嵌换行在 CLI 输入框中是换行而非提交。
    """
    _tmux_send(session, text)


def _tmux_sessions() -> list[str]:
    """枚举 ccs-* tmux sessions。"""
    try:
        out = subprocess.check_output(["tmux", "ls", "-F", "#{session_name}"],
                                      text=True, timeout=3)
        return [s for s in out.splitlines() if s.startswith("ccs-")]
    except Exception:
        return []

def _capture_pane_end(session: str) -> str:
    """Read last ~500 chars of a tmux pane (active window/pane)."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", "-500"],
            text=True, timeout=3, stderr=subprocess.DEVNULL)
        return out
    except Exception:
        return ""

@dataclass
class _SessionState:
    """Per-session reminder state."""
    last_snippet: str = ""
    last_remind_ts: float = 0.0
    remind_count: int = 0


class IntentReminderDaemon:
    """后台轮询：检测 ccs 是否有疑问未答/任务未尽 → 重复提醒。

    启动: python3 ccls_daemon_intent_classifier.py --daemon
    """
    _INTERVAL = 10.0        # 轮询间隔
    _REMIND_COOLDOWN = 30.0  # 同 session 冷却（纯冷却门控，修复 P0#2）

    def __init__(self, interval: float = _INTERVAL):
        self._interval = interval
        self._states: dict[str, _SessionState] = {}
        self._stop = threading.Event()

    def _push_reminder(self, session: str, round_no: int = 1) -> bool:
        """向 tmux pane 逐行 literal 注入「停滞自检」提醒。返回 True 表示已推送。"""
        st = self._states.setdefault(session, _SessionState())
        now = time.monotonic()
        if now - st.last_remind_ts < self._REMIND_COOLDOWN:  # 纯冷却门控 (P0#2)
            return False
        # 本 daemon 在「pane 无新产出」(停滞) 时触发，而非「收到用户指令」时触发；
        # 因此只复用 user_intent_reminder._CORE_QUESTIONS 这一单一真相源的两问，
        # 改用「停滞自检」框架——绝不声称"你收到用户直接指令"(那是 core.send→inject_reminder 的职责)，
        # 否则会向未收到任何用户输入的 /loop 角色注入虚假前提，制造空转答案
        # (实证：ccs-coordinator 曾回 "已完成。不再回应重复自检。"，对不存在的用户任务作答)。
        reminder = self._stagnation_reminder(round_no)
        try:
            _inject_text(session, reminder)
            # G1 生长：把 conversation_monitor 已检测但仅写 bus 的越界提案
            # 回灌给角色自身（只读提示，不自动执行），闭合治理回路。
            vsum = self._recent_violations_summary(session)
            if vsum:
                _inject_text(session, vsum)
            st.last_remind_ts = now
            st.remind_count += 1
            return True
        except Exception as e:
            # 不静默：注入失败会让角色永久停在停滞态而无人知晓（本轮实测
            # AttributeError 被此处吞掉，排查多轮）。降级仍在，但留下痕迹。
            print(f"[ccls_daemon] 停滞自检注入失败 {session}: {e}",
                  file=sys.stderr, flush=True)
            return False

    def _stagnation_reminder(self, round_no: int = 1) -> str:
        """停滞自检提醒文本：诚实对齐当前任务，不复述不存在的用户指令。"""
        q, t = _CORE_QUESTIONS[0], _CORE_QUESTIONS[1]
        return (
            f"【停滞自检 · 每轮必对齐】第{round_no}次重复自检：\n"
            "你的会话已一段时间没有新产出（可能卡住或空转）。在继续前先诚实对齐当前任务：\n"
            f"1. {q}\n2. {t}\n"
            "若当前没有可推进的真实任务，停留在职责上，不要编造待办作答。"
        )

    def _recent_violations_summary(self, session: str) -> str:
        """读取 conversation_monitor 写入 bus 的 role_drift 越界统计。

        返回紧凑提示（最多 2 行），无越界返回空串。
        任何异常静默降级（不阻塞提醒主流程）。
        """
        role = session[len("ccs-"):] if session.startswith("ccs-") else session
        try:
            out = subprocess.run(
                [BUS_CLIENT, "read", "--cat", "architecture", "--limit", "30", "--json"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            import json
            # bus read --json 输出为对象信封 {"facts": [...]}，与 search --json 同约定
            # （partner.py:210）。直接取 facts；非 dict 时回退为自身。
            _data = json.loads(out)
            facts = _data.get("facts", _data) if isinstance(_data, dict) else _data
            # 时效门：检测器每小时重跑，不再漂移的角色【不会覆写】其旧告警，
            # 旧条会长期留在最近 30 条内并被反复注入（幽灵告警）。
            # 实测 scout 在时间窗修复后已不再漂移，但其 0.9h 前的旧告警仍会被注入。
            # 此处只认最近一轮检测周期（2×DRIFT_INTERVAL=2h）内的告警。
            _cutoff = time.time() - _DRIFT_ALERT_TTL
            hits = [f for f in facts
                    if "role_drift" in (f.get("tags") or "")
                    and f.get("title", "").startswith(f"[role_drift] {role} ")
                    and float(f.get("timestamp") or 0) >= _cutoff]
            if not hits:
                return ""
            last = hits[0]["title"]
            entries = last.split(":", 1)[-1].strip()
            return (f"⚠️ 监测到越界操作统计（来自 conversation_monitor）："
                    f"{role} 漂移 {entries[:120]}")
        except Exception:
            return ""

    def _tick(self) -> int:
        """One poll cycle. Returns # reminders pushed."""
        pushed = 0
        live = set(_tmux_sessions())
        # P1 修复：剔除已不存在的 session
        for dead in set(self._states) - live:
            del self._states[dead]
        for sess in live:
            st = self._states.setdefault(sess, _SessionState())
            # lifecycle 过滤：ondemand 角色被动接收信号，idle 是正常态，不主动唤醒
            # （ponytail: 防止 daemon 对被动角色无谓轰击形成噪声循环）。
            role_key = sess[len("ccs-"):] if sess.startswith("ccs-") else sess
            if "-" in role_key:
                role_key = role_key.rsplit("-", 1)[0]
            if _load_role_lifecycles().get(role_key, "infinite") == "ondemand":
                continue
            text = _capture_pane_end(sess)
            if not text:
                continue
            # 仅当 pane 快照未变（ccs 无新产出）→ 视为停滞 → 自检提醒
            # D3 修复：不分类 ccs 自身输出，只靠"无新产出"判断停滞
            if text == st.last_snippet:
                if self._push_reminder(sess, st.remind_count + 1):
                    pushed += 1
                    self._bus_trace(sess)
            else:
                st.last_snippet = text  # 存完整 pane 快照（非截断尾）；与下次整段比较才能正确判定"无新产出"
        return pushed

    def _bus_trace(self, session: str) -> None:
        # 停滞自检属监控/健康事件，写入 monitor_audit（语义正确），
        # 不再污染 code_fix（角色据该 cat 找真实修复方案，噪声会稀释信号）。
        try:
            subprocess.run([
                BUS_CLIENT, "write", "monitor_audit",
                f"[ccls_daemon] 停滞自检提醒 {session} (无新产出→重复自检)",
                "--evidence", f"round=R70 session={session}",
                "--src", "ccls_daemon",
            ], capture_output=True, timeout=5)
        except Exception:
            pass

    def run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self._interval)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="IntentReminderDaemon")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def _acquire_pid_lock() -> int | None:
    """单实例锁（架构红线 3）。返回 fd 或 None（已被占用）。"""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(PID_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except OSError:
        os.close(fd)
        return None


# ── CLI self-check ─────────────────────────────────────────────────────────
def _selfcheck() -> int:
    """Assert-based self-check for classification rules."""
    assert classify_intent("请帮我写一个排序函数") == Intent.TASK
    assert classify_intent("什么是 YAGNI 原则？") == Intent.QUESTION
    assert classify_intent("能否解释下 tmux socket 隔离") == Intent.QUESTION
    assert classify_intent("分析下三项目架构") == Intent.TASK
    assert classify_intent("hello") == Intent.OTHER
    assert classify_intent("") == Intent.OTHER
    assert classify_intent("修改 core.py 中的注入逻辑") == Intent.TASK
    # C1 修复：长上下文前的任务动词
    assert classify_intent("blah " * 50 + "分析下架构") == Intent.TASK
    # C2 修复：英文任务
    assert classify_intent("please fix this bug") == Intent.TASK
    assert classify_intent("please implement the feature") == Intent.TASK
    # 其他
    assert classify_intent("今天天气真好") == Intent.OTHER
    print("selfcheck: 11/11 PASS")
    return 0

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true", help="Run as background daemon")
    ap.add_argument("--selfcheck", action="store_true", help="Run assert self-check")
    args = ap.parse_args()
    if args.selfcheck:
        raise SystemExit(_selfcheck())
    if args.daemon:
        lock = _acquire_pid_lock()
        if lock is None:
            print("ccls_daemon already running (pid lock held). Exiting.")
            raise SystemExit(1)
        IntentReminderDaemon().run()
    else:
        print("Run with --daemon or --selfcheck")
