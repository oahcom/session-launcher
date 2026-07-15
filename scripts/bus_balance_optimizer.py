#!/usr/bin/env python3
"""bus_balance_optimizer.py — activate consumers for under-consumed categories"""
import json, os, subprocess, sys, time
from pathlib import Path

BUS_CLIENT = Path(os.environ.get("BUS_CLIENT", str(Path.home() / ".hermes" / "scripts" / "bus_client.py")))

TARGETS = {
    "architecture":     (5, ["coordinator","ccs-monitor","curator","archivist","reviewer"]),
    "reflexion_lesson": (3, ["curator","closer","coordinator"]),
    "blocker":          (3, ["coordinator","ccs-monitor","security-auditor"]),
    "code_fix":         (4, ["codex-dev","engineer","devops","qa"]),
    "notice":           (3, ["ccs-monitor","coordinator","dashboard-creator"]),
    "performance":      (2, ["cdn-auditor","seo-optimizer"]),
    "security":         (2, ["security-auditor","security-tester"]),
}

def get_stats():
    r = subprocess.run([sys.executable, str(BUS_CLIENT), "stats"], capture_output=True, text=True, timeout=15)
    stats = {}
    for line in r.stdout.split("\n"):
        if ":" in line and "avg trust" in line:
            parts = line.strip().split(":")
            if len(parts) >= 2:
                try: stats[parts[0].strip()] = int(parts[1].strip().split()[0])
                except: pass
    return stats


def main():
    dry = "--dry-run" in sys.argv; quiet = "--quiet" in sys.argv
    if not quiet: print(f"[{time.strftime('%H:%M:%S')}] Bus Balance Optimizer")
    stats = get_stats()
    actions = []
    for cat, (min_c, roles) in TARGETS.items():
        cnt = stats.get(cat, 0)
        if cnt == 0: continue
        # bus_client doesn't have 'consumers' subcommand, so use stats as proxy
        # If category has significant messages (>10) but no register history → activate
        # ponytail: replace with actual consumer tracking when bus_client adds consumers API
        if cnt > 10:
            for role in roles:
                if not dry:
                    actions.append(f"[noreg] {role}->{cat} (bus_client.consumers not available)")
                else:
                    actions.append(f"[DRY] {role}->{cat}")
    if not quiet and actions:
        print(f"  {len(actions)} activations needed")
        for a in actions[:5]: print(f"    + {a}")
        if len(actions) > 5: print(f"    ... ({len(actions)-5} more)")
    # bus-write removed: zero downstream consumers read [bus-balance] messages.
    # Real consumer tracking is in bus_protocol.mark_consumed()/unconsumed().
    # ponytail: if a consumer subscription API is added to bus_client, wire it here.

if __name__ == "__main__":
    main()
