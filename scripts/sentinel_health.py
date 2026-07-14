#!/usr/bin/env python3
"""sentinel_health.py — 哨兵健康守卫
Minimal: validate PID in each sentinel, clean if dead.
Runs on cron, no new deps.
"""
import json, os, sys, subprocess
from pathlib import Path

SENTINEL_DIRS = ["/tmp/ccs-sentinels", "/tmp/cdx-sentinels", "/tmp/ccs-lifecycle-sentinels"]
ARCHIVE_BASE = Path("/tmp/ccs-sentinels-archived")

for d in SENTINEL_DIRS:
    dpath = Path(d)
    if not dpath.exists():
        continue
    for f in sorted(dpath.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            if pid is None or pid == 0 or pid == 99999:
                # invalid pid — archive
                ARCHIVE_BASE.mkdir(parents=True, exist_ok=True)
                f.replace(ARCHIVE_BASE / f.name)
                print(f"  archived {f.name} (pid={pid})")
                continue
            # verify pid is alive
            r = subprocess.run(["kill", "-0", str(pid)], capture_output=True, timeout=3)
            if r.returncode != 0:
                ARCHIVE_BASE.mkdir(parents=True, exist_ok=True)
                f.replace(ARCHIVE_BASE / f.name)
                print(f"  archived {f.name} (dead pid={pid})")
        except Exception as e:
            ARCHIVE_BASE.mkdir(parents=True, exist_ok=True)
            f.replace(ARCHIVE_BASE / f.name)
            print(f"  archived {f.name} (error: {e})")

print("sentinel_health: done")
