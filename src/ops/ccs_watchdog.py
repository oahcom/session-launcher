#!/usr/bin/env python3
"""
ccs_watchdog.py — CCS 用户意图守护 (R70 Deliverable #1，systemd 部署入口)

systemd 服务 `ccs-intent-watchdog.service` 每 30s 调用 --once --roles <list>，
執行 單次輪詢→如發現 session 停滞(面板無新產出)→重發自檢提醒。

單一真相源: classify_intent / Intent / repeat_reminder / _REMINDER_MARKER
  來自 user_intent_reminder.py。
IntentReminderDaemon 輪詢引擎來自 ccls_daemon_intent_classifier.py (symlink in src/)。

P0: PID 鎖單實例 (Redline 3) — systemd 30s 重疊不得產生雙進程。
"""
from __future__ import annotations
import sys, os, argparse, time, subprocess, fcntl
from pathlib import Path

# 確保 session-launcher/src 在 sys.path（本文件位於 src/ops/）
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
PID_FILE = Path(HERMES_HOME) / "run" / "ccs_intent_watchdog_once.pid"

from user_intent_reminder import classify_intent, Intent, _REMINDER_MARKER  # noqa: E402
from ccls_daemon_intent_classifier import (  # noqa: E402
    _tmux_sessions, _capture_pane_end, _SessionState,
    IntentReminderDaemon, _acquire_pid_lock,
)


def _run_once(roles: list[str] | None = None) -> int:
    """單次輪詢: 掃描 ccs-* session, 面板無變化(session 停滞)→寫入提醒。

    --once 模式: 執行一步纔退出 (systemd timer 驅動 30s 重調)。
    """
    sessions = roles if roles else _tmux_sessions()
    # 一次性 --once，無需持續 _states (每次重啟均為全新輪詢)。
    # 判定停滞: 此次 capture 長度 < 上次記錄 (session 不活躁) 視為未答復。
    # 簡化: 若 capture 非空且不含 "────────────────" (上次提醒痕跡) → 發提醒。
    pushed = 0
    for sess in sessions:
        text = _capture_pane_end(sess)
        if not text:
            continue
        # 幂等: 若 pane 已含提醒標記 → 當輪跳過 (避免連續提醒刷屏)
        if _REMINDER_MARKER in text:
            continue
        # 發送一次提醒 (cooldown 由 systemd 30s 間隔自然擔保)
        if _push_once(sess):
            pushed += 1
    return pushed


def _push_once(session: str) -> bool:
    """單次發送提醒 (literal, 行序)。"""
    from ccls_daemon_intent_classifier import IntentReminderDaemon as _D
    d = _D()
    return d._push_reminder(session)


def main():
    ap = argparse.ArgumentParser(prog="ccs_watchdog")
    ap.add_argument("--once", action="store_true", help="Run single poll cycle and exit")
    ap.add_argument("--roles", type=str, default="",
                    help="Comma-separated ccs-* session names (e.g. ccs-scout,ccs-maintainer)")
    ap.add_argument("--daemon", action="store_true", help="Run continuous daemon (10s loop)")
    args = ap.parse_args()

    if not args.once and not args.daemon:
        ap.print_help(); return 1

    lock = _acquire_pid_lock()
    if lock is None:
        print("ccs_watchdog: another instance holds the lock. Skipping.", file=sys.stderr)
        return 0  # not an error — systemd will retry

    if args.once:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
        n = _run_once(roles)
        if n:
            print(f"ccs_watchdog --once: pushed {n} reminder(s)")
        return 0

    if args.daemon:
        IntentReminderDaemon().run()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
