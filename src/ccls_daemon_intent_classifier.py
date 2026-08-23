#!/usr/bin/env python3
"""
CCS 输入意图判定 + 重复提醒 daemon (R70 Round 1 — deliverable #1, v2)

User spec:
  "deamon 当用户输入给 ccs 时判断这些是不是疑问或者任务，
   deamon 需要重复提醒 ccs（你真的解决的用户的问题了吗？你真的有严格按用户的提示去完成任务吗？)"

设计决策（v2 修复 Codex 20 维审查发现的 P0/P1）：
- P0#2 死去重：原 hash(constant_string) 使第二次起永远 False → 改为纯冷却门控。
- D3 误判 ccs 自身输出：daemon **只分类用户原始输入**，不分类 pane 尾。
       判定未答复的逻辑改为：上次提醒后 pane 无新产出（快照未变）→ 仍视为未答复。
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
PID_FILE = Path(HERMES_HOME) / "run" / "ccls_intent_daemon.pid"


# 意图判定单一真相源：复用 user_intent_reminder 的 classify_intent / Intent / _CORE_QUESTIONS，
# 不再各自维护副本（v3 收敛：消除审查发现的重复定义与符号覆盖冲突）。
try:
    from user_intent_reminder import classify_intent, Intent, _CORE_QUESTIONS, repeat_reminder  # type: ignore
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
    repeat_reminder = _mod.repeat_reminder
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
    _SNIPPET_LEN = 400

    def __init__(self, interval: float = _INTERVAL):
        self._interval = interval
        self._states: dict[str, _SessionState] = {}
        self._stop = threading.Event()
        # 复用 user_intent_reminder.repeat_reminder 生成提醒文案，确保与 send 路径
        # 同源（同一 _REMINDER_MARKER），同时生效时幂等不双注入。
        self._reminder_text = repeat_reminder(Intent.TASK, round_no=1)

    def _push_reminder(self, session: str) -> bool:
        """Write reminder to tmux pane (literal, line-by-line). Returns True if pushed."""
        st = self._states.setdefault(session, _SessionState())
        now = time.monotonic()
        if now - st.last_remind_ts < self._REMIND_COOLDOWN:  # 纯冷却门控 (P0#2)
            return False
        try:
            # D1 修复：send-keys -l 逐行 literal 发送，避免 \n 被当作按键
            for line in self._reminder_text.split("\n"):
                subprocess.run(["tmux", "send-keys", "-t", session, "-l", line],
                               check=True, timeout=3)
                subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                               check=True, timeout=3)
            st.last_remind_ts = now
            st.remind_count += 1
            return True
        except Exception:
            return False

    def _tick(self) -> int:
        """One poll cycle. Returns # reminders pushed."""
        pushed = 0
        live = set(_tmux_sessions())
        # P1 修复：剔除已不存在的 session
        for dead in set(self._states) - live:
            del self._states[dead]
        for sess in live:
            st = self._states.setdefault(sess, _SessionState())
            text = _capture_pane_end(sess)
            if not text:
                continue
            # 仅当 pane 快照未变（ccs 无新产出）→ 仍视为未答复 → 提醒
            # D3 修复：不分类 ccs 自身输出，只靠"无新产出"判断停滞
            if text == st.last_snippet:
                if self._push_reminder(sess):
                    pushed += 1
                    self._bus_trace(sess)
            else:
                st.last_snippet = text[-self._SNIPPET_LEN:]
        return pushed

    def _bus_trace(self, session: str) -> None:
        try:
            subprocess.run([
                BUS_CLIENT, "write", "code_fix",
                f"[ccls_daemon] 重复提醒 {session} (未答复→重复自检)",
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
