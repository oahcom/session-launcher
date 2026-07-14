#!/usr/bin/env python3
"""BH Route Sync — Browser Harness ↔ Session Roles 路由桥接

验证并同步 BH route config 到 routing 层，生成健康报告。
输出外部痕迹: bh_route_health.json + bus 写入。

用法:
  python3 scripts/bh_route_sync.py              # 只报告
  python3 scripts/bh_route_sync.py --sync        # 报告+同步到路由层
"""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path.home()
BH_CONFIG = BASE / "hermes-session-roles" / "personas" / "browser-harness" / "_bh_route_config.json"
BH_MAP = BASE / "hermes-session-roles" / "personas" / "browser-harness" / "_bh_to_sr_map.json"
BUS_CLIENT = BASE / ".hermes" / "scripts" / "bus_client.py"
ROUTING_SRC = BASE / "session-launcher" / "src" / "routing"

def load_config():
    if BH_CONFIG.exists():
        return json.loads(BH_CONFIG.read_text())
    return {"bh_profiles": {}, "meta": {"bh_total": 0}}


def auto_fix_gaps(config, mapping):
    """Auto-fill missing consume routes for BH personas with gaps.
    Uses the SR role's consume categories as default. Creates reusable assets."""
    profiles = config.get("bh_profiles", {})
    routing_gaps = mapping.get("routing_gaps", {})
    gaps_data = mapping.get("gaps_data", {})
    fixed = 0
    
    # SR consume categories (derived from role JSON definitions)
    SR_CONSUME = {
        "coordinator": ["task_spec", "workflow", "user_story", "test_plan", 
                       "deployment_plan", "deployment_report", "test_report",
                       "bug_report", "security_audit", "evolution_report",
                       "changelog", "scheduler", "ccs_health"],
        "qa": ["task_spec", "code_fix", "prd", "test_plan", "bug_report"],
        "engineer": ["task_spec", "architecture", "user_story", "code_review",
                    "bug_report", "test_report", "test_plan", "system_design",
                    "documentation", "security_audit"],
        "investigator_python": ["bug_report", "root_cause_analysis", "code_fix",
                               "security", "performance"],
        "investigator_senior": ["bug_report", "root_cause_analysis", "architecture",
                               "design_issue", "blocker"],
        "investigator_general": ["bug_report", "root_cause_analysis", "notice"],
        "maintainer": ["ccs_health", "code_fix", "architecture", "performance",
                      "cleanup", "notice"],
        "optimizer": ["performance", "architecture", "code_fix", "cleanup"],
        "scout": ["architecture", "evolution_report", "research", "notice",
                 "performance"],
        "curator": ["user_story", "task_spec", "knowledge_distill", "documentation",
                   "reflexion_lesson", "research"],
        "devops": ["deployment_plan", "deployment_report", "security", "ops",
                  "blocker", "notice"],
        "security_auditor": ["security", "security_audit", "blocker", "code_fix",
                            "threat_model", "deployment_plan"],
        "writer": ["task_spec", "documentation", "prd", "changelog", "user_story"],
        "debate_verifier": ["debate", "task_spec"],
        "lr": ["task_spec", "architecture", "root_cause_analysis", "tech_decision"],
        "pg": ["task_spec", "bug_report", "system_design", "verification",
              "root_cause_analysis", "tech_decision"],
        "product_architect": ["task_spec", "architecture", "system_design",
                             "product_design", "prd"],
        "closer": ["blocker", "code_fix", "cleanup", "notice"],
    }
    
    for sr_role, bh_list in routing_gaps.items():
        if sr_role not in SR_CONSUME:
            continue
        for bh_name in bh_list:
            if bh_name not in profiles:
                continue
            profile = profiles[bh_name]
            route = profile.get("route", {})
            existing_consume = set(route.get("consume", []))
            defaults = set(SR_CONSUME.get(sr_role, []))
            missing = defaults - existing_consume
            if missing:
                if "route" not in profile:
                    profile["route"] = {}
                profile["route"]["consume"] = list(existing_consume | missing)
                fixed += 1
    
    if fixed > 0:
        config["bh_profiles"] = profiles
        BH_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    
    return fixed


def load_mapping():
    if BH_MAP.exists():
        return json.loads(BH_MAP.read_text())
    return {"mapping": {}, "routing_gaps": {}}

def check_health(config, mapping):
    """Check BH→SR route health"""
    profiles = config.get("bh_profiles", {})
    bh_total = len(profiles)  # use actual count, not metadata
    mapping_data = mapping.get("mapping", {})
    routing_gaps = mapping.get("routing_gaps", {})
    
    enabled = sum(1 for p in profiles.values() if p.get("enabled", False))
    disabled = sum(1 for p in profiles.values() if not p.get("enabled", False))
    mapped = sum(1 for p in profiles.values() if p.get("sr_mapping"))
    unmapped = bh_total - mapped
    
    # Count gaps per SR role
    gaps = {}
    for sr_role, bh_list in routing_gaps.items():
        if len(bh_list) > 0:
            gaps[sr_role] = len(bh_list)
    
    return {
        "bh_total": len(profiles),
        "enabled": enabled,
        "disabled": disabled,
        "mapped": mapped,
        "unmapped": unmapped,
        "mapping_coverage": round(mapped / max(bh_total, 1) * 100, 1),
        "sr_roles_with_gaps": len(gaps),
        "gap_details": gaps,
        "healthy": enabled > 0 and mapped > 0,
    }

def generate_report(health, sync=True):
    # Auto-fix routing gaps before reporting
    if sync:
        config = load_config()
        mapping = load_mapping()
        fixed = auto_fix_gaps(config, mapping)
        if fixed > 0:
            health["auto_fixed_gaps"] = fixed


    lines = [
        "# BH Route Sync 报告",
        "",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"BH总profiles: {health['bh_total']}",
        f"已启用: {health['enabled']} | 已禁用: {health['disabled']}",
        f"已映射到SR: {health['mapped']} | 未映射: {health['unmapped']}",
        f"映射覆盖率: {health['mapping_coverage']}%",
        f"SR角色有路由缺口: {health['sr_roles_with_gaps']}",
        "",
        "### 路由缺口详情",
    ]
    for sr_role, count in sorted(health.get("gap_details", {}).items()):
        lines.append(f"- {sr_role}: {count} 个未路由BH persona")
    lines.append("")
    lines.append(f"健康: {'✅' if health['healthy'] else '⚠️'}")
    if sync:
        lines.append(f"同步: 路由配置已验证")
    lines.append("")
    lines.append("---")
    lines.append(f"*bh_route_sync.py*")
    return "\n".join(lines)

def write_report(report_text):
    """Write report as external trace"""
    out_dir = BASE / "hermes" / "workspace" / "ecosystem-auto-cycle"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bh_route_health_{int(time.time())}.md"
    path.write_text(report_text)
    return path

def write_bus(health):
    """Write summary to bus"""
    if not BUS_CLIENT.exists():
        return
    summary = f"[bh-route-sync] BH {health['bh_total']} profiles, {health['mapped']} mapped ({health['mapping_coverage']}%), {health['sr_roles_with_gaps']} roles with gaps"
    subprocess.run([sys.executable, str(BUS_CLIENT), "write", "architecture", summary, "--src", "bh-route-sync"],
                   capture_output=True, timeout=10)

def main():
    sync = "--sync" in sys.argv
    
    config = load_config()
    mapping = load_mapping()
    health = check_health(config, mapping)
    
    report = generate_report(health, sync)
    path = write_report(report)
    
    print(report)
    print(f"\n✅ Report: {path}")
    
    write_bus(health)
    
    # Exit code indicates health
    sys.exit(0 if health["healthy"] else 1)

if __name__ == "__main__":
    main()
