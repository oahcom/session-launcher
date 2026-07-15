#!/usr/bin/env python3
"""bh_route_sync.py — Browser Harness → Session Role routing bridge

Reads _bh_route_config.json (68 BH personas with routing configs)
and registers each BH persona's produce/consume categories in the
session-pipeline routing system via bus_client.

Previously this file was deleted — data existed but routing was inactive.

Usage: python3 scripts/bh_route_sync.py [--dry-run] [--quiet]
Cron: 0 * * * * (every hour)
"""
import json, os, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
ROLES_DIR = HOME / "hermes-session-roles" / "personas" / "browser-harness"
BUS_CLIENT = HOME / ".hermes" / "scripts" / "bus_client.py"
STATE_FILE = HOME / ".hermes" / "state" / "bh_route_cursor.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_bh_config() -> dict:
    """Load the BH route config with 68 persona routing definitions."""
    path = ROLES_DIR / "_bh_route_config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())

def load_bh_to_sr_map() -> dict:
    """Load the BH→SR mapping (68 BH → 12 SR)."""
    path = ROLES_DIR / "_bh_to_sr_map.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())

def register_bh_persona(bh_name: str, sr_role: str, config: dict,
                       dry_run: bool = False) -> list:
    """Register a BH persona's routing in the session-pipeline."""
    actions = []
    route = config.get("route", {})
    produce = route.get("produce", [])
    consume = route.get("consume", [])

    # Register each produce category
    for cat in produce:
        if dry_run:
            actions.append(f"[DRY-RUN] register {sr_role} produce: {cat}")
        else:
            try:
                subprocess.run([sys.executable, str(BUS_CLIENT), "register", sr_role, cat],
                              capture_output=True, timeout=15)
                actions.append(f"registered {sr_role} produce: {cat}")
            except Exception as e:
                actions.append(f"FAIL {sr_role} produce {cat}: {e}")

    # Register each consume category
    for cat in consume:
        if dry_run:
            actions.append(f"[DRY-RUN] register {sr_role} consume: {cat}")
        else:
            try:
                subprocess.run([sys.executable, str(BUS_CLIENT), "register", sr_role, cat],
                              capture_output=True, timeout=15)
                actions.append(f"registered {sr_role} consume: {cat}")
            except Exception as e:
                actions.append(f"FAIL {sr_role} consume {cat}: {e}")

    return actions

def main():
    dry_run = "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv
    if not quiet:
        print(f"[{time.strftime('%H:%M:%S')}] BH Route Sync")

    # Load configs
    route_config = load_bh_config()
    sr_map = load_bh_to_sr_map()
    bh_profiles = route_config.get("bh_profiles", {})
    mapping = sr_map.get("mapping", {})

    if not bh_profiles and not mapping:
        if not quiet:
            print("  No BH configs found")
        return

    # Use cursor to track progress
    cursor = {}
    if STATE_FILE.exists():
        try: cursor = json.loads(STATE_FILE.read_text())
        except: pass

    registered = 0
    all_actions = []

    # Process each BH persona
    for bh_name, sr_role in mapping.items():
        # Skip if already processed
        if bh_name in cursor.get("processed", []):
            continue

        config = bh_profiles.get(bh_name, {})
        if not config:
            continue

        actions = register_bh_persona(bh_name, sr_role, config, dry_run)
        all_actions.extend(actions)
        if not config.get("enabled", True):
            continue
        registered += 1

        # Add BH-specific personas from route config
        if bh_name not in cursor.get("processed", []):
            cursor.setdefault("processed", []).append(bh_name)

    if not quiet:
        print(f"  BH personas: {len(bh_profiles)} configured, {len(mapping)} mapped")
        print(f"  New registrations: {registered}")
        if all_actions:
            for a in all_actions[:5]:
                print(f"    {a}")
            if len(all_actions) > 5:
                print(f"    ... and {len(all_actions) - 5} more")

    # Save cursor
    if not dry_run:
        cursor["updated"] = time.time()
        STATE_FILE.write_text(json.dumps(cursor, indent=2))

        # Write bus trace
        summary = f"[bh-route-sync] {registered} BH personas registered to {len(set(mapping.values()))} SR roles"
        try:
            subprocess.run([sys.executable, str(BUS_CLIENT), "write", "architecture", summary,
                          "--src", "bh-route-sync", "--evidence", json.dumps({"new": registered, "total_mapped": len(mapping)})],
                         capture_output=True, timeout=10)
        except: pass

    return registered

if __name__ == "__main__":
    main()
