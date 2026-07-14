#!/usr/bin/env python3
"""ecosystem_diagnostics.py — 系统诊断，扫描待办工作项。

产出: prioritized list of actionable work items with evidence.

用法:
  python3 scripts/ecosystem_diagnostics.py         # 打印报告
  python3 scripts/ecosystem_diagnostics.py --json  # JSON 输出供 auto_cycle 消费
"""
import json, os, subprocess, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
os.chdir(str(SRC))

def scan_test_failures() -> list[dict]:
    """运行测试并解析失败项"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header",
         "--ignore=tests/test_6dimension_deep_qa.py"],
        capture_output=True, text=True, timeout=60
    )
    items = []
    for line in r.stderr.split("\n") + r.stdout.split("\n"):
        if "FAILED" in line:
            items.append({
                "type": "test_failure",
                "target": line.split("FAILED ")[-1].split(" ")[0],
                "detail": line.strip(),
                "priority": 1,
            })
    return items


def scan_system_health() -> list[dict]:
    """检查系统健康"""
    items = []
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from ops.sentinel import list_sentinels
    total = 0; alive = 0
    for s in list_sentinels():
        total += 1
        r = subprocess.run(["tmux", "has-session", "-t", s.tmux_session],
                           capture_output=True, timeout=3)
        if r.returncode == 0:
            alive += 1
    if alive < total:
        items.append({
            "type": "dead_ccs",
            "target": f"{total-alive} dead of {total}",
            "detail": "tmux sessions not alive",
            "priority": 1,
        })
    return items

def scan_todo_markers() -> list[dict]:
    """扫描代码中的 TODO/FIXME/HACK"""
    items = []
    for pattern, pri in [("FIXME", 1), ("HACK", 2), ("TODO", 3), ("ponytail", 4)]:
        r = subprocess.run(
            ["rg", "-n", pattern, "src/", "--type", "py", "--no-heading"],
            capture_output=True, text=True, timeout=30
        )
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            items.append({
                "type": f"marker_{pattern.lower()}",
                "target": line.split(":")[0] + ":" + line.split(":")[1],
                "detail": line.strip()[:120],
                "priority": pri,
            })
    return items

def scan_git_status() -> list[dict]:
    """检查 git 工作区状态"""
    r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10)
    modified = [l.strip() for l in r.stdout.split("\n") if l.strip()]
    if modified:
        return [{
            "type": "git_uncommitted",
            "target": f"{len(modified)} modified files",
            "detail": "; ".join(modified[:10]),
            "priority": 3,
        }]
    return []

def scan_workspace_gaps() -> list[dict]:
    """检查 workspace 是否有未闭环的条目"""
    ws = Path.home() / "hermes" / "workspace"
    items = []
    for d in sorted(ws.iterdir()):
        if not d.is_dir():
            continue
        has_summary = (d / "SUMMARY.md").exists() or (d / "ROUND5_SUMMARY.md").exists()
        if not has_summary:
            items.append({
                "type": "workspace_no_closure",
                "target": d.name,
                "detail": f"{d.name} 缺少闭环文档",
                "priority": 3,
            })
    return items

def scan_cron_health() -> list[dict]:
    """检查 cron 任务完整性"""
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    tasks = [l for l in r.stdout.split("\n") if l.strip() and not l.startswith("#")]
    if len(tasks) < 5:
        return [{
            "type": "cron_too_few",
            "target": f"{len(tasks)} tasks",
            "detail": "cron 任务数 < 5, 生态系统可能不完整",
            "priority": 2,
        }]
    return []

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON mode")
    args = parser.parse_args()

    all_items = []
    scanners = [
        ("test_failures", scan_test_failures),
        ("todos", scan_todo_markers),
        ("health", scan_system_health),
        ("git", scan_git_status),
        ("workspace", scan_workspace_gaps),
        ("cron", scan_cron_health),
    ]
    for name, scanner in scanners:
        try:
            items = scanner()
            all_items.extend(items)
        except Exception as e:
            all_items.append({
                "type": f"scan_error_{name}",
                "target": str(e)[:80],
                "detail": f"扫描 {name} 失败: {e}",
                "priority": 5,
            })

    all_items.sort(key=lambda x: (x["priority"], x["type"]))
    urgent = [i for i in all_items if i["priority"] <= 1]
    normal = [i for i in all_items if 1 < i["priority"] <= 3]
    low = [i for i in all_items if i["priority"] > 3]

    if args.json:
        print(json.dumps({
            "urgent": urgent,
            "normal": normal,
            "low": low,
            "total": len(all_items),
            "has_urgent": len(urgent) > 0,
        }, ensure_ascii=False, indent=2))
        return

    print(f"=== 生态系统诊断报告 ===")
    print(f"扫描项: {len(all_items)}")
    print()
    if urgent:
        print(f"🔴 紧急 ({len(urgent)}):")
        for i in urgent:
            print(f"  [{i['type']}] {i['target']}: {i['detail'][:100]}")
    if normal:
        print(f"\n🟡 一般 ({len(normal)}):")
        for i in normal:
            print(f"  [{i['type']}] {i['target']}")
    if low:
        print(f"\n🟢 低优先级 ({len(low)}):")
        for i in low:
            print(f"  [{i['type']}] {i['target']}")
    if not all_items:
        print("✅ 未发现问题")

if __name__ == "__main__":
    main()
