#!/usr/bin/env python3
"""redline_probe.py v2 — 源文件对比验证

从行为约束 skill 的源 .md 文件中提取含禁令关键词的完整行，
与 KNOWLEDGE 块内容做存在性对比。
跳过 code fence 和表格行。
"""
import re, os, sys
from pathlib import Path

SKILLS_ROOT = Path.home() / "shared-skills" / "hermes-origin" / "skills"

# ---- 行为约束 skill 清单 ----
_ROLE_BEHAVIORAL = {
    "pg": ["pg/redline_enforce", "pg/decision_ladder"],
    "coordinator": ["coordination/cluster_monitor"],
    "lr": ["lr/redline_check"],
}

def extract_redlines_from_source(skill_dir: str) -> list[str]:
    """从行为约束 skill 文件中提取含禁令关键词的完整行。
    跳过 code fence 和表格行。"""
    md_file = SKILLS_ROOT / skill_dir / "SKILL.md"
    if not md_file.exists():
        md_file = SKILLS_ROOT / f"{skill_dir}.md"  # 过渡期兼容
    if not md_file.exists():
        return []
    text = md_file.read_text().split("\n")
    redlines, in_code = [], False
    for line in text:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or stripped.startswith("|"):
            continue
        if any(kw in stripped for kw in ["禁止", "必须", "绝对", "严禁", "不可", "不得"]):
            redlines.append(stripped)
    return redlines

def verify_workspace(role: str) -> list[str]:
    """验证 workspace CLAUDE.md KNOWLEDGE 块包含所有源文件红线。"""
    claude = Path.home() / "ccs-workspaces" / role / "CLAUDE.md"
    if not claude.exists():
        return ["NO_CLAUDE_MD"]
    content = claude.read_text()
    start = content.find("<!-- KNOWLEDGE:START -->")
    end = content.find("<!-- KNOWLEDGE:END -->")
    if start < 0 or end < 0:
        return ["NO_KNOWLEDGE_BLOCK"]
    knowledge = content[start:end]
    missing = []
    for skill_dir in _ROLE_BEHAVIORAL.get(role, []):
        for redline in extract_redlines_from_source(skill_dir):
            if redline not in knowledge:
                missing.append(f"[{skill_dir}] {redline}")
    return missing

def verify_all():
    failures = 0
    for role in _ROLE_BEHAVIORAL:
        missing = verify_workspace(role)
        if missing:
            if "NO_CLAUDE_MD" in missing:
                print(f"FAIL {role}: CLAUDE.md 不存在")
            elif "NO_KNOWLEDGE_BLOCK" in missing:
                print(f"FAIL {role}: KNOWLEDGE 块不存在")
            else:
                print(f"FAIL {role}: {len(missing)} 条红线缺失")
                for m in missing:
                    print(f"  MISSING: {m}")
            failures += 1
    if failures:
        print(f"\n{failures} role(s) 缺少红线 — 禁止部署")
        sys.exit(1)
    print("✅ 所有 workspace 红线完整。")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Redline probe")
    parser.add_argument("--role", "-r", action="append", help="仅验证指定角色（可重复）")
    args = parser.parse_args()
    if args.role:
        for r in args.role:
            missing = verify_workspace(r)
            if missing:
                print(f"{r}: {len(missing)} missing")
    else:
        verify_all()
