#!/usr/bin/env python3
"""system_monitor.py — Activate 3 previously unused events modules

Activates:
  1. events/signals.py — 10 signal checkers (bus_unread, systemctl, http, journalctl, git, sessions, mem/disk)
  2. events/parser.py — Unified signal parsing + old format compat
  3. events/notify.py — NotificationEngine for step/process notifications

Usage: python3 scripts/system_monitor.py [--quiet]
Cron: */15 * * * *
"""
import json, os, subprocess, sys, time
from pathlib import Path

BASE = Path.home()
sys.path.insert(0, str(BASE / "session-launcher" / "src"))
BUS_CLIENT = BASE / ".hermes" / "scripts" / "bus_client.py"

SIGNALS_TO_CHECK = [
    "running_sessions", "mem_disk", "bus_unread", "git_staged",
    "http_health", "session_size",
]

def main():
    quiet = "--quiet" in sys.argv
    if not quiet:
        print(f"[{time.strftime('%H:%M:%S')}] System Monitor (events activator)")

    # 1. Activate events/signals.py — run all signal checkers
    from events.signals import check_signal_by_name
    signal_results = []
    for name in SIGNALS_TO_CHECK:
        try:
            r = check_signal_by_name(name)
            signal_results.append({"signal": name, "triggered": r})
        except Exception as e:
            signal_results.append({"signal": name, "error": str(e)})

    # 2. Activate events/parser.py — test old/new format parsing
    from events.parser import parse_signal
    parser_results = []
    for case in [
        {"type": "bus", "spec": {"category": "blocker"}},
        {"type": "memory", "spec": {"threshold_mb": 200}},
        {"type": "disk", "spec": {"threshold_pct": 95}},
    ]:
        try:
            r = parse_signal(case)
            parser_results.append({"type": case["type"], "result": r})
        except Exception as e:
            parser_results.append({"type": case["type"], "error": str(e)})

    # 3. Activate events/notify.py — instantiate NotificationEngine
    from events.notify import NotificationEngine
    try:
        engine = NotificationEngine("monitor")
        engine.close()
        engine_ok = True
    except Exception as e:
        engine_ok = False

    # Aggregate
    triggered = [s for s in signal_results if s.get("triggered")]
    errors = [s for s in signal_results + parser_results if s.get("error")]

    if not quiet:
        print(f"  Signal checks: {len(signal_results)} total, {len(triggered)} triggered")
        for t in triggered:
            print(f"    ! {t['signal']}")
        print(f"  Parser tests: {len(parser_results)}")
        print(f"  Engine: {'OK' if engine_ok else 'FAIL'}")

    # Bus trace
    summary = json.dumps({
        "signals": len(signal_results), "triggered": len(triggered),
        "parser": len(parser_results), "engine": engine_ok,
        "signals_list": [s["signal"] for s in triggered],
    })
    try:
        subprocess.run([sys.executable, str(BUS_CLIENT), "write", "architecture",
                       f"[system-monitor] {len(signal_results)} checks, {len(triggered)} triggered",
                       "--src", "system-monitor", "--evidence", summary],
                      capture_output=True, timeout=10)
    except: pass

    # Write workspace output
    out_dir = BASE / "hermes" / "workspace" / "auto-cycle-v5"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "ts": time.time(), "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_checks": signal_results, "parser_tests": parser_results,
        "engine_ok": engine_ok,
    }
    (out_dir / f"monitor-{int(time.time())}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
