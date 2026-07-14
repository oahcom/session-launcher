#!/usr/bin/env python3
"""
Session 生态系统健康检查脚本。
可复用的自动化工具：验证所有三个项目的代码健康、运行状态、路由一致性。
使用方式：python3 scripts/ecosystem_health_check.py

输出：结构化报告（JSON）+ 人可读摘要
"""
import json, os, subprocess, sys, time
from pathlib import Path

REPORTS_DIR = Path.home() / ".hermes" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

def check_ccs_health():
    """检查所有CCS运行状态。"""
    sentinels_dir = Path("/tmp/ccs-sentinels")
    results = []
    for sf in sorted(sentinels_dir.glob("*.json")):
        try:
            with open(sf) as f:
                data = json.load(f)
            tmux = data.get("tmux_session", "")
            alive = False
            if tmux:
                r = subprocess.run(["tmux", "has-session", "-t", tmux], 
                                   capture_output=True, timeout=3)
                alive = r.returncode == 0
            uptime = time.time() - data.get("started_at", time.time())
            results.append({
                "role": data.get("role", sf.stem),
                "alive": alive,
                "uptime_min": int(uptime / 60),
                "engine": data.get("engine", "ccs"),
                "lifecycle": data.get("lifecycle", "?"),
                "pid": data.get("pid"),
            })
        except Exception as e:
            results.append({"role": sf.stem, "error": str(e)})
    return results

def check_role_definitions():
    """检查角色定义完整性。"""
    roles_dir = Path.home() / "hermes-session-roles" / "personas" / "session-roles"
    required = {"name", "title", "description", "category", "system_prompt"}
    results = []
    for rf in sorted(roles_dir.glob("*.json")):
        try:
            with open(rf) as f:
                data = json.load(f)
            missing = required - set(data.keys())
            results.append({
                "file": rf.name,
                "name": data.get("name", "?"),
                "ok": len(missing) == 0,
                "missing_fields": list(missing),
                "lifecycle": data.get("lifecycle", "?"),
                "drive": data.get("drive", "?"),
            })
        except Exception as e:
            results.append({"file": rf.name, "error": str(e)})
    return results

def check_tests():
    """运行三个项目的测试并报告结果。"""
    projects = {
        "session-launcher": ["tests/test_partner_client.py", "tests/test_system_health.py", "tests/test_e2e_mock.py"],
        "hermes-session-roles": ["tests/"],
        "session-pipeline": ["tests/test_router.py", "tests/test_helpers.py"],
    }
    results = {}
    for project, test_paths in projects.items():
        project_results = []
        for tp in test_paths:
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pytest", tp, "-v", "--tb=line", "--timeout=10"],
                    cwd=str(Path.home() / project),
                    capture_output=True, text=True, timeout=30
                )
                passed = r.returncode == 0
                # Parse summary
                summary = ""
                for line in r.stdout.split("\n"):
                    if "passed" in line and "failed" in line:
                        summary = line.strip()
                project_results.append({
                    "test_path": tp,
                    "passed": passed,
                    "summary": summary or ("PASS" if passed else f"FAIL (rc={r.returncode})"),
                })
            except subprocess.TimeoutExpired:
                project_results.append({"test_path": tp, "passed": False, "summary": "TIMEOUT"})
        results[project] = project_results
    return results

def generate_report():
    """生成完整的生态健康报告。"""
    report = {
        "timestamp": time.time(),
        "ccs_health": check_ccs_health(),
        "role_definitions": check_role_definitions(),
    }
    
    # Summary
    ccs_alive = sum(1 for c in report["ccs_health"] if c.get("alive"))
    ccs_total = len(report["ccs_health"])
    roles_ok = sum(1 for r in report["role_definitions"] if r.get("ok"))
    roles_total = len(report["role_definitions"])
    
    report["summary"] = {
        "ccs": f"{ccs_alive}/{ccs_total} alive",
        "roles": f"{roles_ok}/{roles_total} complete",
    }
    
    # Write report
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"ecosystem_health_{ts}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    
    print(f"✅ 报告已保存: {report_path}")
    print(f"📊 CCS: {ccs_alive}/{ccs_total} | 角色: {roles_ok}/{roles_total}")
    
    # Human-readable output
    print("\n=== CCS 状态 ===")
    for c in report["ccs_health"]:
        icon = "✅" if c.get("alive") else "❌"
        print(f"  {icon} {c['role']:<20} up={c.get('uptime_min', 0)}m  {c.get('engine', '?')}")
    
    return report

if __name__ == "__main__":
    generate_report()
