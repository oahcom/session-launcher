#!/usr/bin/env python3
"""
CCS 输入意图判定 + 重复提醒 daemon (R70 Round 1 — deliverable #1, v2)

User spec:
  "deamon 当用户输入给 ccs 时判断这些是不是疑问或者任务，
   deamon 需要重复提醒 ccs（你真的解决的用户的问题了吗？你真的有严格按用户的提示去完成任务吗？)"

设计决策（v3 触发源纠偏 — 见 ~/.hermes/workspace/ccs-intent-daemon-realign/REQUIREMENT.md）：
- 触发源：pane 无新产出 → **该角色存在未对齐用户请求**。前者对 infinite 常驻角色
  等价于「角色空闲」（其设计上的正常态），会形成 注入→应答→pane变→重置预算→再注入
  的自激循环（实测 pm 250 轮 / coordinator 193 轮 / engineer 175 轮）。改后
  无用户请求即零注入，且回到用户原始要求「当用户输入信息给 ccs 时候…重复提醒」。
- 请求证据复用 core.send 既有写入，不新增状态文件（bus ON CONFLICT(t) 天然去重）。
- 提醒文案改回用户要求的「你收到的是用户直接指令」——前提已由 bus 痕迹确证。
- 删除 _recent_violations_summary 回灌（drift 告警语义另有缺陷，且属纯噪音）。
- P0#2 死去重：原 hash(constant_string) 使第二次起永远 False → 改为纯冷却门控。
- X1 缺单实例锁：加 fcntl.flock pidfile（对齐架构红线 3）。
- C1 任务动词只看前 12 字：改为全串扫描。
- C2 英文任务漏判：加英文任务动词前缀。
- D1 send-keys 多行：用 send-keys -l 逐行 literal 发送。
- P1 _states 无回收：每轮剔除已不存在的 session。

ponytail: 轮询 10s，覆盖 ~50 个 ccs-* tmux session；并发 >100 改 event-driven。
"""
from __future__ import annotations
import os, time, subprocess, sys, fcntl, threading
from dataclasses import dataclass
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
BUS_CLIENT = os.path.join(HERMES_HOME, "scripts", "bus_client.py")
PID_FILE = Path(HERMES_HOME) / "run" / "ccls_intent_daemon.pid"


# 意图判定单一真相源：复用 user_intent_reminder 的 classify_intent / Intent /
# repeat_reminder，不再各自维护副本（v3 收敛：消除重复定义与符号覆盖冲突）。
try:
    from user_intent_reminder import (  # type: ignore
        classify_intent, Intent, repeat_reminder)
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
    repeat_reminder = _mod.repeat_reminder

# 同目录复用 tmux 投递（脚本以本目录为 sys.path[0] 启动，直接 import 即可）
try:
    from tmux_ops import _tmux_send, parse_tmux_name  # type: ignore
except Exception:  # pragma: no cover - 加载失败时降级为不可注入
    def _tmux_send(tmux_name: str, message: str, **_kw) -> None:
        raise RuntimeError("tmux_ops 不可用")

    def parse_tmux_name(name: str):  # type: ignore
        return (name, 0)


def _inject_text(session: str, text: str) -> bool | None:
    """把多行文本作为**一条**消息注入 pane。返回 _tmux_send 的结果。

    不可逐行 send-keys + 逐行 Enter：那样每行独立成一条消息（实测
    coordinator 收到 22 字符的裸首行「【停滞自检 · 每轮必对齐】第1次重复自检：」），
    角色为每行各起一个回合应答。_tmux_send 整条投递（chunk + 单个 Enter），
    payload 内嵌换行在 CLI 输入框中是换行而非提交。

    **返回值必须逐层上传**：_tmux_send 失败时返回 False 而非抛异常，
    这里若丢弃它就等于把投递失败伪装成成功。
    """
    return _tmux_send(session, text)


def _tmux_sessions() -> list[str]:
    """枚举 ccs-* tmux sessions。"""
    try:
        out = subprocess.check_output(["tmux", "ls", "-F", "#{session_name}"],
                                      text=True, timeout=3)
        return [s for s in out.splitlines() if s.startswith("ccs-")]
    except Exception:
        return []

@dataclass
class _SessionState:
    """Per-session reminder state."""
    request_ts: float = 0.0   # 该角色最近一次用户请求时刻（bus 注入痕迹 timestamp）
    last_remind_ts: float = 0.0
    remind_count: int = 0


class IntentReminderDaemon:
    """后台轮询：用户向 ccs 提出请求后，重复提醒其自检是否真解决了用户问题。

    触发源 = 该角色存在**未对齐的用户请求**，取自 bus 里 core.send 写下的注入
    痕迹（title 前缀 `[user_intent_reminder] 已注入意图提醒 → <role>`，其
    timestamp 即「最近一次用户请求」）。复用 core.send 既有写入，无需新状态；
    bus 的 ON CONFLICT(t) 保证每角色恒只留最新一条，天然免去去重维护。

    曾用「pane 快照无变化」判停滞 —— 该判据对 infinite 常驻角色等价于「角色空闲」，
    而空闲是其设计上的正常态：被判停滞 → 每 30s 注入 → 角色回一句"是/是/待机" →
    pane 变化 → 预算重置 → 再注入，形成自激循环（实测 pm 250 轮、coordinator 193 轮、
    engineer 175 轮；coordinator 应答"告警同前。待机。"逐字重复 98 次，每次消耗一个
    完整 LLM 回合）。改用请求驱动后，无用户请求即零注入，空转从根上消失，
    同时回到用户原始要求：「当用户输入信息给 ccs 时候…重复提醒」。

    启动: python3 ccls_daemon_intent_classifier.py --daemon
    """
    _INTERVAL = 10.0        # 轮询间隔
    _REMIND_COOLDOWN = 30.0  # 同 session 冷却（纯冷却门控，修复 P0#2）
    # 单次用户请求的重复提醒预算。冷却只限频率不限总量；同一请求超过此数仍未
    # 对齐（应答恒为同一句"是/是/待命"）说明角色是**选择空闲**而非卡住，转静默
    # 避免空转消耗。3 次跨 90s 已足够让真跑偏的角色察觉。
    # 新一轮用户请求到达 → 预算重置 → 可再次提醒。
    _MAX_REMINDERS = 3
    # 用户请求的提醒有效期：超过则视为已过期，不再重复提醒（旧请求不该无限期
    # 挂着消耗回合；也不该在新请求到来时被复活）。
    _REQUEST_TTL = 1800.0
    # 连续空枚举多少轮才认定"会话真的都没了"（此前不回收状态）。对齐架构红线
    # 「3 次连续失败才标 down」：单次 tmux 抖动不算，连续则是真退出。
    _EMPTY_POLL_THRESHOLD = 3
    # 本文件路径，供 run() 检测源码更新（见 run 内注释）。
    _SRC_FILE = os.path.abspath(__file__)
    # 由 supervisor 托管（--daemon）时置 True：源码更新后退出，交由
    # systemd Restart=always 以新代码拉起。线程模式下无人重拉，故不退出。
    _supervised = False
    # 待确认的源码 mtime：非 0 表示上一轮见到过变化，本轮再相同才认定稳定（防抖）。
    _pending_mtime = 0.0

    def __init__(self, interval: float = _INTERVAL):
        self._interval = interval
        self._states: dict[str, _SessionState] = {}
        self._empty_polls = 0
        self._stop = threading.Event()

    def _push_reminder(self, session: str, round_no: int = 1) -> bool:
        """向 tmux pane 注入「重复自检」提醒。返回 True 表示已推送。"""
        st = self._states.setdefault(session, _SessionState())
        now = time.monotonic()
        if now - st.last_remind_ts < self._REMIND_COOLDOWN:  # 纯冷却门控 (P0#2)
            return False
        # 触发前提已成立（该角色确有未对齐的用户请求），故用用户要求的原始措辞。
        # 这与 core.send 的首轮注入同源（_REMINDER_HEADER / repeat_reminder），
        # 不再需要 v2 的「停滞自检」中性框架——那是为了在「不知有无用户输入」时
        # 避免虚假前提而设的妥协，现已有 bus 痕迹作为确证。
        reminder = repeat_reminder(Intent.TASK, round_no)
        try:
            # 必须看返回值：_tmux_send 重试耗尽时 **return False 而不抛异常**
            # （tmux_ops.py:220/270，其 docstring 明写"调用方必须检查返回值"）。
            # 旧写法丢弃返回值 → 投递全失败也计成功，三重后果：用户请求一次没送到
            # 预算却已耗尽（此后永久静默）、bus 留下"已提醒"的假痕迹（把"没送到"
            # 伪装成"已对齐"）、daemon 侧无任何失败信号。
            # 判据用 `is not True` 而非 `is False`：只认明确的成功，任何其他返回
            # （None/0/""/异常值）都算失败。_tmux_send 的实现历史上有过"超限后静默
            # 返回 None"的版本（见其 docstring），`is False` 会在那种实现下重新
            # 静默失效——这是防御性判据，不是对当前实现的断言。
            delivered = _inject_text(session, reminder)
            if delivered is not True:
                print(f"[ccls_daemon] 重复自检投递失败 {session}（返回 {delivered!r}）",
                      file=sys.stderr, flush=True)
                return False
            st.last_remind_ts = now
            st.remind_count += 1
            return True
        except Exception as e:
            # 不静默：注入失败会让角色永久停在未对齐态而无人知晓。
            print(f"[ccls_daemon] 重复自检注入失败 {session}: {e}",
                  file=sys.stderr, flush=True)
            return False

    @staticmethod
    def _role_of(session: str) -> str:
        """tmux 会话名 → 角色名。复用 tmux_ops.parse_tmux_name 的权威解析。

        不可用 `role.rsplit('-', 1)[0]`：那只对 `ccs-<role>-<N>` 成立，
        对角色名**本身含连字符**的会话会把真名砍掉（`ccs-ccs-monitor` → `ccs`、
        `ccs-codex-dev` → `codex`），与痕迹标题永不相等 → 这些角色静默零注入。
        parse_tmux_name 只在后缀为纯数字时才剥离实例号。
        """
        try:
            return parse_tmux_name(session)[0]
        except Exception:
            return session[len("ccs-"):] if session.startswith("ccs-") else session

    @staticmethod
    def _read_request_traces() -> dict[str, float]:
        """一次性读回全部用户请求痕迹：{角色名: 最近请求时刻}。

        每轮只读一次 bus，而非每个 session 各读一次——查询与 session 无关
        （只有标题里的角色名不同）。逐 session 读会形成 N+1 子进程：实测单次
        read 79ms，10 个会话 × 每 10s = 每分钟 ~4.7s CPU，且 bus_client 每次
        构造 Blackboard 都会触发全表 FTS 回填。整轮读一次的代价降到 ~0.5s/分。

        用 `search` 而非 `read --cat code_fix`：痕迹是各角色各自的最新一条，而 read
        按分类取整批返回，与全部 code_fix 写入（含 contract_updater 等高频源）共享
        同一个 limit 窗口。实测该窗口约 53 天（50 条 / 近 30 分钟新增 9 条），
        当前速率下离饥饿还很远，但代价为零而语义更精确——search 按标题关键词命中，
        实测只返回痕迹本身，无窗口竞争、不受其他写入速率影响。
        任何异常返回空 dict（宁可不提醒，也不凭推测注入），但留日志不静默。
        """
        try:
            out = subprocess.run(
                [BUS_CLIENT, "search", "已注入意图提醒", "--limit", "50", "--json"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            import json
            data = json.loads(out)
            facts = data.get("facts", data) if isinstance(data, dict) else data
            prefix = "[user_intent_reminder] 已注入意图提醒 → "
            traces: dict[str, float] = {}
            for f in facts:
                title = f.get("title") or ""
                if not title.startswith(prefix):
                    continue
                role = title[len(prefix):].strip()
                try:
                    ts = float(f.get("timestamp") or 0.0)
                except (TypeError, ValueError):
                    continue
                if role and ts > traces.get(role, 0.0):
                    traces[role] = ts
            return traces
        except Exception as e:
            # 不静默：读不到痕迹 = 所有角色的提醒静默停发，必须留下痕迹。
            print(f"[ccls_daemon] 读取用户请求痕迹失败: {e}",
                  file=sys.stderr, flush=True)
            return {}

    def _tick(self) -> int:
        """One poll cycle. Returns # reminders pushed."""
        pushed = 0
        live = set(_tmux_sessions())
        # 空枚举几乎总是 tmux 抖动（`tmux ls` 3s timeout / socket 重建），不是
        # "所有 session 都消失了"。若照常回收 _states，下一轮 setdefault 会把
        # remind_count 归零 → 同一用户请求再拿满额预算（实测单次抖动使注入翻倍 3→6）。
        # 故按架构红线「3 次连续失败才标 down」的同一判据：连续为空才认定真退出，
        # 那时照常回收状态（否则全部 session 真退出时 _states 永久泄漏）。
        # 每轮空都留日志：否则"抖动"与"daemon 卡死"在观感上无法区分。
        if not live:
            self._empty_polls += 1
            print(f"[ccls_daemon] 未枚举到任何 ccs-* 会话（连续第 {self._empty_polls} 轮）",
                  file=sys.stderr, flush=True)
            if self._empty_polls < self._EMPTY_POLL_THRESHOLD:
                return pushed
        else:
            self._empty_polls = 0
        # P1 修复：剔除已不存在的 session
        for dead in set(self._states) - live:
            del self._states[dead]
        # 每轮读一次请求痕迹，供所有 session 复用（避免 N+1 子进程）
        traces = self._read_request_traces()
        for sess in live:
            st = self._states.setdefault(sess, _SessionState())
            role_key = self._role_of(sess)
            if not traces:
                continue  # 无任何用户请求 → 零注入（infinite 角色的空闲是正常态）
            # 有未对齐用户请求时**不做 lifecycle 过滤**：该过滤的意图是"不主动唤醒
            # 被动角色"，而此刻用户明确找过它、它也确实在 tmux 里跑着——提醒是响应
            # 用户请求，不是主动唤醒。过滤掉会让"用户问了 qa，qa 永远收不到重复提醒"。
            # 实证：qa 的 persona 声明 ondemand 但运行时哨兵是 infinite，被静默跳过。
            req = traces.get(role_key, 0.0)
            if not req:
                continue
            if time.time() - req > self._REQUEST_TTL:
                continue  # 请求已过期，不再重复提醒
            if req != st.request_ts:
                # 新一轮用户请求 → 预算重置，可重新提醒
                st.request_ts = req
                st.remind_count = 0
            if st.remind_count >= self._MAX_REMINDERS:
                continue  # 同请求预算耗尽：角色选择空闲而非卡住，转静默
            if self._push_reminder(sess, st.remind_count + 1):
                pushed += 1
                self._bus_trace(sess)
                if st.remind_count == self._MAX_REMINDERS:
                    # 预算耗尽转静默时留痕一次：否则"角色已对齐"与"daemon 挂了"
                    # 在观感上无法区分（journald 侧可查）。
                    print(f"[ccls_daemon] {sess} 重复自检预算耗尽 "
                          f"({self._MAX_REMINDERS}次) → 转静默，等下一轮用户请求",
                          file=sys.stderr, flush=True)
        return pushed

    def _bus_trace(self, session: str) -> None:
        # 重复提醒属监控/健康事件，写入 monitor_audit（语义正确），
        # 不再污染 code_fix（角色据该 cat 找真实修复方案，噪声会稀释信号）。
        try:
            subprocess.run([
                BUS_CLIENT, "write", "monitor_audit",
                f"[ccls_daemon] 重复自检提醒 {session} (存在未对齐用户请求)",
                "--evidence", f"round=R70 session={session}",
                "--src", "ccls_daemon",
            ], capture_output=True, timeout=5)
        except Exception:
            pass

    def run(self) -> None:
        """轮询主循环。

        WHY 每轮查源码 mtime：本 daemon 由 systemd(ccs-intent-guardian)常驻托管，
        进程启动后永不再读源文件——main 分支上的修复对运行中的实例完全不生效
        （实测 2026-10-16：18:23 与 20:37 两次修复提交后，运行实例仍在执行修复前
        的判据，继续以旧逻辑每 30s 轰击角色，coordinator 累计 193+ 轮）。
        supervised 时主动退出，靠单元既有的 Restart=always 以新代码重启——
        比自建 exec/reload 更省，且天然复用单实例 PID 锁。
        线程模式（watchdog 内启动）无 supervisor，退出等于永久停摆，故不查。

        WHY 要求"映入后稳定一轮"：源文件正被编辑（多人/多 agent 同时改 main）时，
        每次重拉都会立刻读到又变了的 mtime → 立即再退出 → systemd 连续重拉，
        RestartSteps=5 的退避触顶后服务永久停摆。实测 2026-10-16 21:03-21:04：
        21:03:41/21:04:03/21:04:28 三次「源码已更新→退出」，restart counter 累到 3，
        随后 ccs-intent-guardian 停摆。故 mtime 需连续两轮相同才算稳定，
        只有稳定后才退出，避免边写边拉的自激循环。
        """
        src_mtime = os.path.getmtime(self._SRC_FILE)
        while not self._stop.is_set():
            self._tick()
            if self._supervised:
                try:
                    cur = os.path.getmtime(self._SRC_FILE)
                    if cur != src_mtime:
                        if cur == self._pending_mtime:
                            # 连续两轮 mtime 一致 → 编辑已停止，可安全退出重拉
                            print("[ccls_daemon] 源码已更新（稳定）→ 退出，"
                                  "等 supervisor 以新代码拉起", file=sys.stderr, flush=True)
                            return
                        self._pending_mtime = cur  # 首次见到变化：待下一轮确认
                    else:
                        self._pending_mtime = 0.0
                except OSError:
                    pass  # 源文件暂不可读（原子替换窗口）：下一轮再判
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
        d = IntentReminderDaemon()
        d._supervised = True  # 由 systemd 托管：源码更新时退出重拉（见 run）
        d.run()
    else:
        print("Run with --daemon or --selfcheck")
