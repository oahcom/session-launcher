#!/usr/bin/env python3
"""file_size_audit.py — 文件行数红线的棘轮检查（AGENTS.md §9）。

规则：>600 行 = 红区，阻止合并；400-599 = 黄区，需要拆分 TODO。
现实：2026-10-21 实测三仓库已有 21 个红文件、20 个连 TODO 都没有，且全仓零门禁，
于是红线形同虚设（同一抽象在两仓各留一份、engine 用 mixin 把复杂度搬了家）。

这里不要求一天拆完，只做一件事：**不许变多**。基线是数字，不是文件清单——
减少后请把基线一起调低，否则棘轮就失效了。
"""
from __future__ import annotations

from pathlib import Path

HOME = Path.home()
REPOS = ("session-launcher", "hermes-session-roles", "session-pipeline")
RED_LINES = 600
TODO_HEAD = 25  # 红区文件必须在前 25 行内声明欠账

# 2026-10-21 20:20 实测 21（launcher 6 / roles 1 / pipeline 14）。只许调低：
# 减少后把基线一起降下来，否则棘轮失效。
UNDECLARED_RED_BASELINE = 21


def undeclared_red_files() -> list[tuple[str, int]]:
    """返回「超红线且没有 TODO 声明」的文件（仓内相对路径, 行数）。"""
    out: list[tuple[str, int]] = []
    for repo in REPOS:
        root = HOME / repo
        if not root.is_dir():
            continue
        for f in root.rglob("*.py"):
            if ".git" in f.parts or "__pycache__" in f.parts or "venv" in f.parts:
                continue
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            if len(lines) <= RED_LINES:
                continue
            if "TODO" in "\n".join(lines[:TODO_HEAD]):
                continue
            out.append((f"{repo}/{f.relative_to(root)}", len(lines)))
    return sorted(out, key=lambda x: -x[1])

if __name__ == "__main__":
    files = undeclared_red_files()
    print(f"undeclared_red={len(files)} baseline={UNDECLARED_RED_BASELINE}")
    for path, lines in files:
        print(f"  {lines:5d}  {path}")
    raise SystemExit(0 if len(files) <= UNDECLARED_RED_BASELINE else 1)
