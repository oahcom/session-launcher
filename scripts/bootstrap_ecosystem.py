#!/usr/bin/env python3
"""bootstrap_ecosystem.py — 一次性恢复整个Session Ecosystem。

在系统完全崩溃后执行: 重建所有workspace + 启动所有infinite CCS + 注册路由。
这是生态的最后一道防线 (resilience layer)。

用法: python3 scripts/bootstrap_ecosystem.py [--dry-run]
"""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path.home()
LAUNCHER = BASE / "session-launcher" / "src" / "ccs.py"
ROLES_DIR = BASE / "hermes-session-roles" / "personas" / "session-roles"
PIPELINE_SRC = BASE / "session-pipeline" / "src"
PYTHON = sys.executable

def bootstrap(dry_run=True):
    print(f"{'='*60}")
    print(f"🚀 Session Ecosystem 启动恢复程序")
    print(f"{'='*60}")
    
    # Phase 1: 检查角色定义
    roles = sorted(ROLES_DIR.glob("*.json"))
    infinite_roles = []
    for rf in roles:
        with open(rf) as f:
            d = json.load(f)
        if d.get("lifecycle") == "infinite":
            infinite_roles.append((d["name"], d.get("drive", "loop")))
    print(f"\n📋 Phase 1: 发现 {len(infinite_roles)} 个infinite角色")
    
    # Phase 2: 创建workspace
    print(f"\n📁 Phase 2: 创建工作空间...")
    for role_name, _ in infinite_roles:
        ws = BASE / "ccs-workspaces" / role_name
        if not ws.exists() or not (ws / "CLAUDE.md").exists():
            if dry_run:
                print(f"  [dry-run] 创建 {ws.name}")
    
    # Phase 3: 启动所有infinite CCS
    print(f"\n🚀 Phase 3: 启动所有infinite CCS...")
    started = 0
    for role_name, drive in infinite_roles:
        if dry_run:
            print(f"  [dry-run] ccs start {role_name} --drive {drive}")
            started += 1
        else:
            r = subprocess.run([PYTHON, str(LAUNCHER), "start", role_name, "--no-attach", f"--drive={drive}"],
                             capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                started += 1
                print(f"  ✅ {role_name}")
            else:
                print(f"  ❌ {role_name}: {r.stderr[:60]}")
    
    # Phase 4: 注册路由
    print(f"\n🔀 Phase 4: 注册路由...")
    sys.path.insert(0, str(PIPELINE_SRC))
    from routing_db import save_routing
    
    for rf in roles:
        with open(rf) as f:
            d = json.load(f)
        name = d["name"]
        produce = [t.split("cat=")[1].split()[0] for t in d.get("output_targets", []) if "cat=" in t]
        consume = []
        for sig in d.get("input_signals", []):
            if isinstance(sig, dict):
                src = sig.get("source", "")
                if "cat=" in src:
                    cat = src.split("cat=")[1].split()[0]
                    if cat not in consume:
                        consume.append(cat)
                elif "unread --all" in src:
                    if "*" not in consume:
                        consume.append("*")
                # New format
                spec = sig.get("spec", {})
                if isinstance(spec, dict) and spec.get("category"):
                    cat = spec["category"]
                    if cat not in consume:
                        consume.append(cat)
        if not dry_run:
            save_routing(name, produce, consume, changed_by="bootstrap")
        print(f"  {name}: produce={produce} consume={consume}")
    
    print(f"\n{'='*60}")
    if dry_run:
        print(f"✅ Dry-run完成: 可启动 {started} CCS, {len(roles)} 路由注册")
    else:
        print(f"✅ 生态系统恢复完成: {started} CCS已启动, {len(roles)} 路由已注册")
    print(f"{'='*60}")

if __name__ == "__main__":
    dry = "--exec" not in sys.argv
    bootstrap(dry_run=dry)
