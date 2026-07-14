#!/usr/bin/env python3
"""
autonomous_loop.py — 自主闭环进化循环

职责（闭环者/运维者模式）：
  1. 定时运行生态系统健康检查
  2. 分析测试结果 → 识别退化或 gap
  3. 归档健康报告到 .hermes/state/ecosystem-archive/
  4. 生成摘要 → 写 bus cat=evolution_report
  5. 基于历史趋势自我调整检查频率

运行: python3 scripts/autonomous_loop.py [--once]
Cron: */30 * * * * cd ~/session-launcher && python3 scripts/autonomous_loop.py --once
"""

import json, subprocess, sys, time, os
from pathlib import Path
from datetime import datetime, timezone

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

ARCHIVE = Path.home() / ".hermes" / "state" / "ecosystem-archive"
ARCHIVE.mkdir(parents=True, exist_ok=True)

def run_health_check() -> dict:
    """运行生态系统健康检查"""
    r = subprocess.run(
        [sys.executable, str(SRC.parent / "scripts" / "ecosystem_health.py")],
        capture_output=True, text=True, timeout=180,
        cwd=str(SRC.parent)
    )
    # 读取最新报告
    report_path = SRC.parent / ".ecosystem_health.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
    else:
        report = {"error": "no report generated"}
    report["_health_stdout"] = r.stdout[-200:] if r.stdout else ""
    report["_health_stderr"] = r.stderr[-200:] if r.stderr else ""
    return report

def archive_report(report: dict) -> str:
    """归档健康报告到时间戳文件。"""
    ts = datetime.fromtimestamp(report.get("timestamp", time.time()), tz=timezone.utc)
    filename = f"health_{ts.strftime('%Y%m%d_%H%M%S')}.json"
    path = ARCHIVE / filename
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return str(path)

def analyze_trend() -> dict:
    """分析历史趋势。"""
    files = sorted(ARCHIVE.glob("health_*.json"))
    if len(files) < 2:
        return {"runs": len(files), "trend": "insufficient_data"}
    
    recent = files[-3:]
    failures = 0
    for f in recent:
        try:
            r = json.loads(f.read_text())
            if r.get("failed", 0) > 0:
                failures += 1
        except: pass
    
    trend = "stable" if failures == 0 else "degrading" if failures >= 2 else "mixed"
    return {"runs": len(files), "recent_checks": len(recent), "recent_failures": failures, "trend": trend}

def write_bus_summary(report: dict, trend: dict):
    """写 bus 摘要（仅当有可用 bus_client 时）。"""
    ts = datetime.fromtimestamp(report.get("timestamp", time.time()), tz=timezone.utc)
    passed = report.get("passed", 0)
    failed = report.get("failed", 0)
    total = passed + failed
    status = "✅" if failed == 0 else "⚠"
    summary = (
        f"[autonomous-loop] {status} 生态系统健康检查 #{trend['runs']} | "
        f"通过 {passed}/{total}, 失败 {failed} | "
        f"趋势: {trend['trend']} | {ts.strftime('%H:%M UTC')}"
    )
    print(f"\n  📋 {summary}")
    
    # 尝试写 bus
    try:
        bus_path = ARCHIVE.parent / ".." / "scripts" / "bus_client.py"
        # 只在 bus 可用时尝试写入
        if Path(bus_path).exists():
            bus = Path.home() / ".hermes" / "scripts" / "bus_client.py"
            if bus.exists():
                subprocess.run(
                    [sys.executable, str(bus), "write", "evolution_report", summary,
                     "--src", "autonomous-loop", "--evidence", json.dumps(report.get("checks", []), ensure_ascii=False)[:500]],
                    capture_output=True, timeout=15
                )
    except Exception as e:
        print(f"  (bus write 不可用: {e})")

def run_once() -> dict:
    print("=" * 60)
    print("🧬 自主闭环进化循环 — 单次运行")
    print(f"   时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   归档: {ARCHIVE}")
    print()

    # 1. 健康检查
    print("  Step 1/3: 运行健康检查...")
    report = run_health_check()
    passed = report.get("passed", 0)
    failed = report.get("failed", 0)
    total = passed + failed
    print(f"    通过 {passed}/{total}, 失败 {failed}")

    # 2. 归档
    print("  Step 2/3: 归档报告...")
    path = archive_report(report)
    print(f"    归档: {path}")

    # 3. 趋势分析 + 摘要
    print("  Step 3/3: 趋势分析 & 摘要...")
    trend = analyze_trend()
    write_bus_summary(report, trend)

    print(f"\n  结果: {'✅ 健康' if failed == 0 else '⚠ 异常'}")
    print("=" * 60)
    return report

if __name__ == "__main__":
    once = "--once" in sys.argv
    if once:
        run_once()
    else:
        # 守护模式：每 30 分钟运行一次
        print("🧬 自主闭环进化循环 — 守护模式 (30min 间隔)")
        print("  按 Ctrl+C 停止\n")
        while True:
            run_once()
            print(f"\n  等待 30 分钟...\n")
            time.sleep(1800)
