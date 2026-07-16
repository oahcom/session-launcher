#!/usr/bin/env python3
"""bh_route_sync.py — Browser Harness → Session Role routing bridge

Reads _bh_route_config.json (68 BH personas with routing configs)
and registers each BH persona's produce/consume categories in the
session-pipeline routing system via bus_client.
Also injects BH sub-persona knowledge into CCS workspaces (qa_personas).

Previously this file was deleted — data existed but routing was inactive.

Usage: python3 scripts/bh_route_sync.py [--dry-run] [--quiet]
Cron: 0 * * * * (every hour)
"""
import json, os, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
ROLES_DIR = HOME / "hermes-session-roles" / "personas" / "browser-harness"
LAUNCHER_SRC = HOME / "session-launcher" / "src"
BUS_CLIENT = HOME / ".hermes" / "scripts" / "bus_client.py"
STATE_FILE = HOME / ".hermes" / "state" / "bh_route_cursor.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
BH_PERSONA_DIR = ROLES_DIR

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


def inject_bh_knowledge_into_workspace(
    sr_role: str, bh_persona_name: str,
    person_def: dict | None = None,
    dry_run: bool = False
) -> str:
    """Inject BH sub-persona knowledge into a CCS workspace CLAUDE.md."""
    ws_path = HOME / "ccs-workspaces" / sr_role / "CLAUDE.md"
    if not ws_path.exists():
        return "no_workspace"

    if person_def is None:
        pf = BH_PERSONA_DIR / f"persona_qa_{bh_persona_name}.json"
        if pf.exists():
            person_def = json.loads(pf.read_text())
    if not person_def:
        return "no_persona"

    # Build a BH knowledge block
    title = person_def.get("title", bh_persona_name)
    desc = person_def.get("description", "")
    sp = person_def.get("system_prompt", "")
    skills = person_def.get("skills", [])
    constraints = person_def.get("constraints", [])

    block = (
        f"\n\n<!-- BH_KNOWLEDGE:{bh_persona_name} -->\n"
        f"## BH子人格: {title}\n\n"
        f"{desc}\n\n"
        f"{sp}\n"
    )
    if skills:
        block += f"\n技能: {', '.join(skills)}\n"
    if constraints:
        block += f"\n约束:\n" + "\n".join(f"- {c}" for c in constraints) + "\n"
    block += f"<!-- /BH_KNOWLEDGE:{bh_persona_name} -->\n"

    if dry_run:
        return f"would inject {bh_persona_name} → {sr_role}"

    content = ws_path.read_text(encoding="utf-8")
    start_marker = f"<!-- BH_KNOWLEDGE:{bh_persona_name} -->"
    end_marker = f"<!-- /BH_KNOWLEDGE:{bh_persona_name} -->"

    if start_marker in content:
        # Replace existing block
        start_idx = content.index(start_marker)
        end_idx = content.index(end_marker, start_idx) + len(end_marker)
        new_content = content[:start_idx] + block + content[end_idx:]
    else:
        # Append after WORKSPACE_SYS:END or at end
        sys_end = "<!-- WORKSPACE_SYS:END -->"
        if sys_end in content:
            insert_at = content.rindex(sys_end) + len(sys_end)
            new_content = content[:insert_at] + block + content[insert_at:]
        else:
            new_content = content + block

    ws_path.write_text(new_content, encoding="utf-8")
    return "injected"


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

    # Inject BH knowledge into CCS workspaces for QA personas
    sr_to_bh = {}
    for bh_name in route_config.get("qa_personas", []):
        # Determine target SR role
        sr_role = bh_profiles.get(bh_name, {}).get("sr_mapping", "")
        if not sr_role:
            sr_role = "qa" if "test" in bh_name else "security_auditor"
        sr_to_bh.setdefault(sr_role, []).append(bh_name)

    for sr_role, bh_list in sr_to_bh.items():
        for bh_name in bh_list:
            r = inject_bh_knowledge_into_workspace(
                sr_role, bh_name,
                person_def=bh_profiles.get(bh_name),
                dry_run=dry_run
            )
            if not quiet:
                print(f"  BH注入: {bh_name} → {sr_role}: {r}")
            all_actions.append(f"BH注入 {bh_name} → {sr_role}: {r}")

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
        summary = f"[bh-route-sync] {registered} BH personas registered + BH知识注入"
        try:
            subprocess.run([sys.executable, str(BUS_CLIENT), "write", "architecture", summary,
                          "--src", "bh-route-sync", "--evidence", json.dumps({"new": registered, "total_mapped": len(mapping), "bh_injections": len(sr_to_bh)})],
                         capture_output=True, timeout=10)
        except: pass

    return registered

if __name__ == "__main__":
    main()
