#!/usr/bin/env python3
"""bus_monitor.py — Bus 消息堆积监控 + 自动告警。"""
import json, os, subprocess, sys, time
from pathlib import Path

BUS_CLIENT = Path.home() / ".hermes" / "scripts" / "bus_client.py"
MAX_FACTS = 5000
CAT_THRESHOLDS = {"blocker": 100, "task_spec": 3000}

def check() -> dict:
    r = subprocess.run(["python3", str(BUS_CLIENT), "stats"], capture_output=True, text=True, timeout=15)
    lines = r.stdout.strip().split("\n")
    facts = 0
    cats = {}
    for l in lines:
        if "facts" in l:
            try: facts = int(l.split()[2])
            except: pass
        elif ": " in l and not l.startswith("📊"):
            parts = l.split(": ")
            name = parts[0].strip()
            try: count = int(parts[1].split()[0])
            except: continue
            cats[name] = count
    alerts = []
    if facts > MAX_FACTS:
        alerts.append(f"bus 堆积 {facts} facts > {MAX_FACTS}")
    for cat, limit in CAT_THRESHOLDS.items():
        if cats.get(cat, 0) > limit:
            alerts.append(f"{cat} {cats[cat]} > {limit}")
    if alerts:
        title = "; ".join(alerts)
        subprocess.run(["python3", str(BUS_CLIENT), "write", "notice", f"[bus-monitor] {title}"], timeout=10)
    return {"facts": facts, "categories": cats, "alerts": alerts, "healthy": len(alerts) == 0}

if __name__ == "__main__":
    result = check()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["healthy"] else 1)
