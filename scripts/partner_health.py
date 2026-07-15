#!/usr/bin/env python3
"""partner_health.py — Cross-role partner health check

Activates routing/partner.py PartnerClient and cross-role routing.

Usage: python3 scripts/partner_health.py [--quiet]
Cron: */30 * * * *
"""
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home() / "session-launcher" / "src"))

BUS_CLIENT = Path.home() / ".hermes" / "scripts" / "bus_client.py"

def main():
    quiet = "--quiet" in sys.argv
    if not quiet:
        print(f"[{time.strftime('%H:%M:%S')}] Partner Health Check")

    from routing.partner import is_ccs_running
    
    key_roles = ["coordinator", "maintainer", "curator", "ccs-monitor", "codex-dev"]
    results = []
    for role in key_roles:
        try:
            alive = is_ccs_running(role)
            results.append({"role": role, "alive": alive})
        except Exception as e:
            results.append({"role": role, "alive": False, "error": str(e)})

    alive_count = sum(1 for r in results if r.get("alive"))

    if not quiet:
        print(f"  Partners: {alive_count}/{len(results)} alive")
        for r in results:
            status = "OK" if r.get("alive") else "DOWN"
            print(f"    {r['role']}: {status}")

    # Bus trace
    try:
        subprocess.run([sys.executable, str(BUS_CLIENT), "write", "ccs_health",
                       f"[partner-health] {alive_count}/{len(results)} partners alive",
                       "--src", "partner-health"],
                      capture_output=True, timeout=10)
    except: pass

if __name__ == "__main__":
    main()
