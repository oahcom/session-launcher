"""ecosystem_cli.py — 三项目生态统一 CLI（全自动产出循环入口）

集成 session-launcher、session-pipeline、hermes-session-roles。
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

_LAUNCHER_SRC = Path(__file__).resolve().parent
_PIPELINE_SRC = Path.home() / "session-pipeline" / "src"
_ROLES_SRC = Path.home() / "hermes-session-roles" / "src"
_HERMES_SCRIPTS = Path.home() / ".hermes" / "scripts"

# launcher's routing must come BEFORE pipeline's routing
# Insert in REVERSE order so launcher is FIRST
for p in [_HERMES_SCRIPTS, _ROLES_SRC, _PIPELINE_SRC, _LAUNCHER_SRC]:
    p_str = str(p)
    if p_str in sys.path:
        sys.path.remove(p_str)
    sys.path.insert(0, p_str)

# Ensure cwd '' is at the end
if '' in sys.path:
    sys.path.remove('')
    sys.path.append('')


def cmd_status() -> dict:
    from core import status as ccs_status
    from role_relations import get_all_roles
    from workflow.client import WorkflowClient

    sessions = ccs_status()
    roles = get_all_roles()
    wc = WorkflowClient("ecosystem")
    try:
        stats = wc.workflow_stats()
        board = wc.kanban_board()
    except Exception:
        stats = {"error": "workflow DB not available"}
        board = []
    wc.close()

    launcher_size = sum(f.stat().st_size for f in _LAUNCHER_SRC.glob("*.py"))
    pipeline_size = sum(f.stat().st_size for f in _PIPELINE_SRC.glob("*.py"))
    roles_size = sum(f.stat().st_size for f in _ROLES_SRC.glob("*.py"))

    report = {
        "timestamp": datetime.now().isoformat(),
        "sessions": {"count": len(sessions) if isinstance(sessions, list) else 0},
        "role_ecosystem": {"defined_roles": len(roles), "roles": sorted(roles)},
        "workflows": stats,
        "kanban": board,
        "codebase_kb": {
            "launcher": round(launcher_size / 1024, 1),
            "pipeline": round(pipeline_size / 1024, 1),
            "roles": round(roles_size / 1024, 1),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


def cmd_relations() -> dict:
    from role_relations import get_data_flow, get_all_roles, get_data_categories
    roles = get_all_roles()
    cats = get_data_categories()
    print(f"🌐 {len(roles)} roles, {len(cats)} categories\n")
    for role in sorted(roles):
        flow = get_data_flow(role)
        p = ", ".join(flow["produces_to"]) or "—"
        c = ", ".join(flow["consumes_from"]) or "—"
        print(f"  {role:<25} -> [{p}]")
        print(f"  {'':<25} <- [{c}]\n")


def cmd_board() -> dict:
    from workflow.client import WorkflowClient
    wc = WorkflowClient("ecosystem")
    try:
        board = wc.kanban_board()
        stats = wc.workflow_stats()
    except Exception as e:
        print(f"❌ {e}")
        return
    wc.close()
    print(f"📋 Kanban ({stats.get('total',0)} total, {stats.get('completion_rate',0)}% complete)\n")
    for lane in board:
        icon = {"backlog": "📥", "in_progress": "🔄", "blocked": "🚫", "completed": "✅", "failed": "❌"}.get(lane["lane"], "⏳")
        print(f"  {icon} {lane['lane']:<15} {lane['count']}")
    print()


def cmd_fleet() -> dict:
    """Fleet-deck 风格 session 控制面板。"""
    from ops.sentinel import list_sentinels, get_all_cross_session_memories
    from core import get_routing_policy, _is_alive
    sentinels = list_sentinels()
    memories = get_all_cross_session_memories()
    print(f"🚀 CCS Fleet ({len(sentinels)} sessions)\n")
    print(f"  {'ROLE':<20} {'STATUS':<10} {'UPTIME':<12} {'POLICY':<10} {'LAST'}")
    for s in sorted(sentinels, key=lambda x: x.role):
        role = s.role
        alive = _is_alive(f"ccs-{role}")
        st = "🟢" if alive else "🔴"
        up = int(time.time() - s.started_at) if s.started_at else 0
        up_s = f"{up//3600}h{(up%3600)//60}m" if up else "—"
        pol = get_routing_policy(role)
        mem = memories.get(role, {})
        act = mem.get("summary", "—")[:25]
        print(f"  {role:<20} {st:<10} {up_s:<12} {pol:<10} {act}")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="三项目生态 CLI")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="生态全景状态")
    sub.add_parser("relations", help="角色关系图谱")
    sub.add_parser("board", help="Kanban 看板")
    sub.add_parser("fleet", help="Fleet session 状态")
    args = parser.parse_args()
    if args.command == "status":
        cmd_status()
    elif args.command == "relations":
        cmd_relations()
    elif args.command == "board":
        cmd_board()
    elif args.command == "fleet":
        cmd_fleet()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
