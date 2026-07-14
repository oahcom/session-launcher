#!/usr/bin/env python3
"""
新角色设置工具：自动创建workspace + 注册路由 + 生成CLAUDE.md。
一次性完成角色上线全流程。

用法:
  python3 scripts/setup_new_role.py <role_name> --title "描述" --lifecycle infinite --drive loop
  python3 scripts/setup_new_role.py <role_name> --template  # 从已有角色克隆
"""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path.home()

def setup_role(role, title="", lifecycle="infinite", drive="loop", produce=None, consume=None, clone_from=""):
    steps = []
    
    # 1. Create role JSON definition
    if clone_from:
        src_path = BASE / "hermes-session-roles" / "personas" / "session-roles" / f"persona_{clone_from}.json"
        if src_path.exists():
            data = json.loads(src_path.read_text())
            data["name"] = role
            data["title"] = title or data["title"]
            del data["system_prompt"]
            role_path = BASE / "hermes-session-roles" / "personas" / "session-roles" / f"persona_{role}.json"
            role_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            steps.append(f"✅ {role_path.name} (从 {clone_from} 克隆)")
        else:
            return {"success": False, "error": f"源角色 {clone_from} 不存在"}
    else:
        pass  # role CLI import not available from script
        data = {
            "name": role, "title": title or role, "description": f"{role} 角色", "category": "自定义",
            "lifecycle": lifecycle, "drive": drive,
            "input_signals": [{"source": f"bus cat={c}"} for c in (consume or [])],
            "output_targets": [f"bus cat={p}" for p in (produce or [])],
            "system_prompt": f"# {title or role}\\n\\n你是 {title or role}，Session Ecosystem 中的角色。",
        }
        role_path = BASE / "hermes-session-roles" / "personas" / "session-roles" / f"persona_{role}.json"
        role_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        steps.append(f"✅ {role_path.name} 创建")
    
    # 2. Create workspace
    ws_path = BASE / "ccs-workspaces" / role
    ws_path.mkdir(parents=True, exist_ok=True)
    claude_md = ws_path / "CLAUDE.md"
    if not claude_md.exists():
        marker_start = "<!-- WORKSPACE_SYS:START -->"
        marker_end = "<!-- WORKSPACE_SYS:END -->"
        content = f"""{marker_start}
# {role}

## 身份

你是 {role}，系统级 CCS。你通过以下方式接收指令：
| 驱动方式 | 触发源 | 说明 |
|---------|--------|------|
| ① /loop | 自循环 | 定时自动巡检 |
| ② ccs-send | 其他 CCS 发消息 | 按需分析 |
| ③ feed push | bus 新消息实时推送 | 即时检测 |
{marker_end}
"""
        claude_md.write_text(content)
    steps.append(f"✅ workspace: ~/ccs-workspaces/{role}/")
    
    # 3. Register pipeline route
    sys.path.insert(0, str(BASE / "session-pipeline" / "src"))
    from routing_db import save_routing
    save_routing(role, produce or [], consume or [], changed_by="setup_new_role")
    steps.append(f"✅ 路由: produce={produce}, consume={consume}")
    
    return {"success": True, "steps": steps, "role": role}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Setup new session role")
    p.add_argument("role", help="Role name")
    p.add_argument("--title", default="", help="Human-readable title")
    p.add_argument("--lifecycle", default="infinite", choices=["infinite", "ondemand"])
    p.add_argument("--drive", default="loop", choices=["loop", "cron", "ondemand", "goal"])
    p.add_argument("--produce", default="", help="Comma-separated produce categories")
    p.add_argument("--consume", default="", help="Comma-separated consume categories")
    p.add_argument("--clone", default="", help="Clone from existing role")
    args = p.parse_args()
    
    produce = [c.strip() for c in args.produce.split(",") if c.strip()] if args.produce else []
    consume = [c.strip() for c in args.consume.split(",") if c.strip()] if args.consume else []
    
    result = setup_role(args.role, args.title, args.lifecycle, args.drive, produce, consume, args.clone)
    if result["success"]:
        print(f"✅ 角色 {args.role} 上线完成:")
        for s in result["steps"]:
            print(f"  {s}")
    else:
        print(f"❌ {result['error']}")
        sys.exit(1)
