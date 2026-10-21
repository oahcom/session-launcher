"""revert-guard 的行为检查：暂存旧版本必须被拦，正常编辑必须放行。

保护对象是 .githooks/pre-commit 里的 REVERT-GUARD 段。它会拦下“暂存内容等于历史旧版本”
的裸提交 —— 实测 2026-10-22 三仓同时命中（wt-launcher 倒退生产总线守卫、roles 倒退 base.md、
pipeline 倒退 engine.py），一次裸提交就会静默回滚已合入的修复。
"""
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / ".githooks" / "pre-commit"


def _git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("guard = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "add guard")
    (repo / "a.py").write_text("")
    _git(repo, "commit", "-qam", "drop guard")
    return repo


def test_blocks_staged_historical_rollback(tmp_path):
    repo = _repo(tmp_path)
    # 工作树与暂存区一起退回上一个版本（= 真实事故形态）
    (repo / "a.py").write_text("guard = 1\n")
    _git(repo, "add", "a.py")
    r = subprocess.run(["bash", str(HOOK)], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REVERT-GUARD" in r.stderr and "a.py" in r.stderr


def test_allows_normal_edit(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("guard = 2\n")
    _git(repo, "add", "a.py")
    r = subprocess.run(["bash", str(HOOK)], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_escape_hatch_allows_deliberate_rollback(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("guard = 1\n")
    _git(repo, "add", "a.py")
    import os
    env = {**os.environ, "ALLOW_HISTORY_ROLLBACK": "1"}
    r = subprocess.run(["bash", str(HOOK)], cwd=repo, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
