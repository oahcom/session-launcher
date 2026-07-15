#!/usr/bin/env python3
"""step_engine_watchdog.py — Gate timeout watch (activates unused StepEngine)"""
import json, os, subprocess, sys, time
from pathlib import Path

LAUNCHER_SRC = Path.home() / "session-launcher" / "src"
BUS_CLIENT = Path(os.environ.get("BUS_CLIENT", str(Path.home() / ".hermes" / "scripts" / "bus_client.py")))

def check_gate_timeouts() -> dict:
    r = subprocess.run([sys.executable, "-c", f"""
import sys, json; sys.path.insert(0, '{LAUNCHER_SRC}')
from lifecycle.engine import StepEngine
engine = StepEngine('coordinator')
try:
    timeouts = engine.check_gate_timeouts()
    if timeouts:
        for t in timeouts:
            print(json.dumps(t))
    else:
        print('OK: no gate timeouts')
except Exception as e:
    print(f'ERR: {{e}}')
finally:
    engine.close()
"""], capture_output=True, text=True, timeout=30)
    result = {"checked": time.time(), "timeouts": [], "ok": True}
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("OK:"): continue
        if line.startswith("ERR:"): result["ok"] = False; result["error"] = line[4:].strip(); continue
        try:
            t = json.loads(line); result["timeouts"].append(t)
        except json.JSONDecodeError: pass
    return result

def main():
    quiet = "--quiet" in sys.argv
    if not quiet: print(f"[{time.strftime('%H:%M:%S')}] StepEngine Watchdog")
    result = check_gate_timeouts()
    if not quiet:
        print(f"  Gate timeouts: {len(result['timeouts'])}")
        for t in result["timeouts"]:
            print(f"    wf={t['wf_id']} step={t['step_id']} elapsed={t['elapsed_hours']}h")
    if result["timeouts"]:
        summary = f"[step-engine] {len(result['timeouts'])} gate timeouts"
        for t in result["timeouts"][:3]:
            summary += f" | {t['wf_id']}:{t['step_id']}({t['elapsed_hours']}h)"
        try:
            subprocess.run([sys.executable, str(BUS_CLIENT), "write", "blocker", summary,
                          "--src", "step-engine-watchdog", "--evidence", json.dumps(result["timeouts"])],
                         capture_output=True, timeout=10)
        except Exception as e:
            if not quiet: print(f"  Bus write failed: {e}")
    if not result["timeouts"] and not quiet: print("  All gates clean")

if __name__ == "__main__":
    main()
