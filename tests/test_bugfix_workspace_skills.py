"""pytest: 验证 workspace_create 增量更新 + pg skill 文件补全"""
import os, sys, tempfile, shutil
from pathlib import Path

# 必须在 import 目标模块前设好 env var
os.environ.setdefault('HERMES_SKILLS_ROOT', '/home/administrator/shared-skills/hermes-origin')
os.environ.setdefault('SESSION_ROLES_ROOT', '/home/administrator/hermes-session-roles')

LAUNCHER_SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
ROLES_SRC = '/home/administrator/hermes-session-roles/src'
sys.path.insert(0, LAUNCHER_SRC)
sys.path.insert(0, ROLES_SRC)

from core import workspace_create

import pytest

errors = 0
exit_on_fail = False

if __name__ == "__main__":
    exit_on_fail = True

# ── Bug #1: workspace_create 增量更新 ──
def test_workspace_create_incremental():
    """验证 workspace_create 正确支持 created/updated 双态."""
    ws_base = Path(os.environ['HOME']) / 'ccs-workspaces' / 'test'
    _ = workspace_create('test')

    # 0) 清理再创 → created
    _ = workspace_create('test')  # create once to 'updated'
    shutil.rmtree(ws_base, ignore_errors=True)
    r0 = workspace_create('test')
    assert r0['action'] == 'created', f"首次应为 created: {r0}"
    assert r0['success']
    # 再调一次 → updated
    _ = workspace_create('test')

    # 1a) action=created (已验证)
    # 1b) action=updated
    r2 = workspace_create('test')
    assert r2['action'] == 'updated', f"再次应为 updated: {r2}"

    # 1c) marker 存在
    md = ws_base / 'CLAUDE.md'
    content = md.read_text()
    assert '<!-- WORKSPACE_SYS:START -->' in content, "缺少 START marker"
    assert '<!-- WORKSPACE_SYS:END -->' in content, "缺少 END marker"

    # 1d) 保留用户内容
    md.write_text('>> 用户自定义 <<\n' + content)
    r3 = workspace_create('test')
    c3 = md.read_text()
    assert '>> 用户自定义 <<' in c3, "用户内容被覆盖"

    # 1e) 系统 marker 保留
    assert '<!-- WORKSPACE_SYS:END -->' in c3, "系统区域丢失"


def test_skill_content():
    """验证 pg skill 文件有足够内容."""
    from role_assembler import read_skill

    SKILLS = ['decision_ladder', 'impl_plan', 'risk_assess', 'redline_enforce']
    for sk in SKILLS:
        doc = read_skill(f'skills/pg/{sk}.md')
        assert doc, f"{sk} 内容为空"
        assert len(doc.splitlines()) >= 8, \
            f"{sk}: 内容不足 ({len(doc.splitlines())} lines)"
        # 确认包含关键内容
        assert 'YAGNI' in doc or '红线' in doc or '风险' in doc or 'ponytail' in doc or 'ADMIN_START' in doc, \
            f"{sk}: 缺少关键内容"


def test_pg_prompt_assembly():
    """验证 pg prompt 完整组装."""
    from role_assembler import assemble_role_prompt
    prompt = assemble_role_prompt('pg')
    assert '## 技能库' in prompt, "技能库未注入"
    assert '决策阶梯' in prompt, "decision_ladder 未注入"
    assert '实施计划' in prompt, "impl_plan 未注入"
    assert '风险评估' in prompt, "risk_assess 未注入"
    assert '红线' in prompt, "redline_enforce 未注入"
    assert len(prompt.splitlines()) > 20, f"prompt 过短 ({len(prompt.splitlines())} lines)"
