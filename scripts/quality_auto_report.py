#!/usr/bin/env python3
"""
Quality Auto-Report — 自动质量基线测量 + bus 通报。

每轮产出：
1. 密度基线（density-check.sh）
2. 纯度检测（purity-check.sh）
3. 综合评分推送 bus

运行方式: python3 scripts/quality_auto_report.py [--quiet]
注册为cron: */60 * * * * python3 ~/session-launcher/scripts/quality_auto_report.py --quiet
"""
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path.home()
SCRIPTS = BASE / "session-launcher" / "scripts"
REPORTS_DIR = BASE / ".hermes" / "reports" / "quality"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
BUS_CLIENT = BASE / ".hermes" / "scripts" / "bus_client.py"

ROLES = [
    "coordinator", "pm", "pg", "qa", "engineer",
    "maintainer", "scout", "reviewer", "lr",
    "knowledge_curator", "curator", "consumer",
    "test", "debate_verifier",
]


def run_check(script: str, role: str) -> dict:
    """Run a check script for a role and return parsed JSON."""
    r = subprocess.run(
        ["bash", str(SCRIPTS / script), role],
        capture_output=True, text=True, timeout=30,
        cwd=str(SCRIPTS)
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"role": role, "error": r.stdout[:100], "raw": r.stdout}


def cmd_report():
    quiet = "--quiet" in sys.argv
    results = []

    for role in ROLES:
        density = run_check("density-check.sh", role)
        purity = run_check("purity-check.sh", role)
        grade = density.get("grade", "?")
        purity_grade = purity.get("grade", "?")
        l1 = density.get("l1", 0)
        l3 = density.get("l3", 0)
        total = density.get("total", 0)
        fails = purity.get("fails", 0)

        entry = {
            "role": role,
            "density": {"total": total, "l1": l1, "l3": l3, "grade": grade},
            "purity": {"grade": purity_grade, "fails": fails},
        }
        results.append(entry)

        if not quiet:
            icon = "✅" if grade == "良好" and purity_grade == "良好" else "⚠️" if grade == "及格" or purity_grade == "及格" else "❌"
            print(f"  {icon} {role:<20} density={grade:<5} purity={purity_grade:<5}")

    # Calculate summary
    good = sum(1 for r in results if r["density"]["grade"] == "良好" and r["purity"]["grade"] == "良好")
    warn = sum(1 for r in results if r["density"]["grade"] == "及格" or r["purity"]["grade"] == "及格")
    bad = sum(1 for r in results if r["density"]["grade"] == "不合格" or r["purity"]["grade"] == "不合格")

    summary = {
        "ts": time.time(),
        "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"),
        "roles_checked": len(results),
        "good": good,
        "warning": warn,
        "bad": bad,
        "results": results,
    }

    # Save report
    report_path = REPORTS_DIR / f"quality_{time.strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    if not quiet:
        print(f"\n  综合: ✅{good} ⚠️{warn} ❌{bad} / {len(results)} 角色")
        print(f"  💾 报告: {report_path}")

    # Push to bus
    if BUS_CLIENT.exists():
        subprocess.run(
            ["python3", str(BUS_CLIENT), "write", "quality",
             f"[auto-report] 质量基线: ✅{good} ⚠️{warn} ❌{bad} / {len(results)}角色",
             "--evidence", f"density+purity checked at {summary['ts_h']}",
             "--src", "quality-monitor"],
            capture_output=True, timeout=10,
        )

    return summary


if __name__ == "__main__":
    print(f"[{time.strftime('%H:%M:%S')}] Quality Auto-Report")
    cmd_report()
