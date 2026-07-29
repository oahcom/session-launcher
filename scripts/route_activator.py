#!/usr/bin/env python3
"""route_activator.py — Activate the message delivery pipeline"""
import json, re, subprocess, sys, time
from pathlib import Path

PIPELINE_SRC = Path.home() / "session-pipeline" / "src"
BUS_CLIENT = Path.home() / ".hermes" / "scripts" / "bus_client.py"

def run_route_all(dry_run):
    dry_str = "True" if dry_run else "False"
    code = ('import sys, json\n'
            f'sys.path.insert(0, "{PIPELINE_SRC}")\n'
            'from routing.routes import route_all\n'
            'from paths import ensure_paths\n'
            'ensure_paths()\n'
            f'r = route_all(dry_run={dry_str}, parallel=True)\n'
            'print("__R__" + json.dumps(r))\n')
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    m = re.search(r'__R__(\{.*\})', r.stdout, re.DOTALL)
    if m: return json.loads(m.group(1))
    return {"error": r.stderr[:500] if r.stderr else "no result"}

def main():
    dry_run = "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv
    if not quiet: print(f"[{time.strftime('%H:%M:%S')}] Route Activator")
    result = run_route_all(dry_run)
    routed = result.get("routed", result.get("total", 0))
    total = result.get("total", 0)
    if not quiet: print(f"  Routed: {routed}/{total}")
    if not dry_run and "error" not in result and routed > 0:
        try:
            subprocess.run([sys.executable, str(BUS_CLIENT), "write", "notice",
                          f"[route-activator] routed {routed}/{total} msgs",
                          "--src", "route-activator",
                          "--evidence", json.dumps({"routed": routed, "total": total})],
                         capture_output=True, timeout=10)
        except Exception: pass

if __name__ == "__main__":
    main()
