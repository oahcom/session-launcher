#!/usr/bin/env python3
"""Bus消息智能分类器 — 根据内容自动推断分类, 修复积压消息。"""
import json, subprocess, sys, re
from pathlib import Path

BUS = Path.home() / ".hermes" / "scripts" / "bus_client.py"

# 关键词→分类映射 (优先级从高到低)
CLASSIFIERS = [
    (["cve-", "cve:", "security", "漏洞", "入侵", "exploit"], "security"),
    (["bug", "fix", "修复", "缺陷", "break"], "code_fix"),
    (["architecture", "架构", "design", "设计", "prd"], "architecture"),
    (["review", "审查", "cr:", "pr:", "merge"], "code_review"),
    (["test", "测试", "qa", "regression"], "bug_report"),
    (["性能", "performance", "latency", "慢", "优化"], "performance"),
    (["blocker", "blocked", "阻塞", "卡住"], "blocker"),
    (["notice", "通知", "告警", "alert"], "notice"),
    (["lesson", "reflexion", "经验", "总结"], "reflexion_lesson"),
    (["evolut", "进化", "轮次", "report"], "evolution_report"),
    (["plan", "计划", "sprint", "milestone"], "prd"),
]

def classify(text: str) -> str:
    text_lower = text.lower()
    for keywords, cat in CLASSIFIERS:
        for kw in keywords:
            if kw in text_lower:
                return cat
    return "notice"  # default

def fix_backlog(limit=200, dry_run=True):
    r = subprocess.run(["python3", str(BUS), "unread", "--all", "--json"], capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    facts = data if isinstance(data, list) else data.get("facts", [])
    
    fixed = 0
    for f in facts[:limit]:
        fid = f.get("id", 0)
        text = f.get("t", f.get("text", ""))
        cat = f.get("cat", "")
        if cat in ("", "?"):
            inferred = classify(text)
            if not dry_run:
                # re-mark as consumed, then re-write with category
                subprocess.run(["python3", str(BUS), "mark_consumed", str(fid)], capture_output=True, timeout=5)
                subprocess.run(["python3", str(BUS), "write", inferred, text[:200], "--src", "classifier"], capture_output=True, timeout=5)
            fixed += 1
            if fixed <= 3 or "--verbose" in sys.argv:
                print(f"  [{inferred}] {text[:60]}...")
    
    print(f"\n📊 分析: {len(facts)} 积压, {fixed} 可分类 (dry_run={'是' if dry_run else '否'})")

if __name__ == "__main__":
    dry = "--exec" not in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit")+1]) if "--limit" in sys.argv else 500
    fix_backlog(limit=limit, dry_run=dry)
