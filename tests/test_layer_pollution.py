#!/usr/bin/env python3
"""Test layer pollution — convergence of workspace knowledge injection.

运行: cd ~/session-launcher && python3 -m pytest tests/test_layer_pollution.py -v -x
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest


# ── Force import routing.roles from launcher src (not pipeline) ──

def _load_routing_roles():
    """Import routing.roles from session-launcher/src, bypassing session-pipeline/src."""
    launcher_src = Path(__file__).resolve().parent.parent / "src"
    for p in list(sys.path):
        if "session-pipeline" in p:
            sys.path.remove(p)
    if str(launcher_src) not in sys.path:
        sys.path.insert(0, str(launcher_src))

    spec = importlib.util.spec_from_file_location(
        "routing.roles",
        launcher_src / "routing" / "roles.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_rmod = _load_routing_roles()


# ── helpers ──

def _minimal_role(name="test_role", title="Test Role", **kw):
    role = {
        "name": name, "title": title,
        "output_targets": ["bus cat=test_produce"],
        "input_signals": [{"type": "bus", "spec": {"category": "test_consume"}}],
        "workgroup": ["dev"],
        "drive": "ondemand",
    }
    role.update(kw)
    return role


def _lines_outside_code_blocks(lines):
    result = []
    in_block = False
    for line in lines:
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            result.append(line)
    return result


_HEADING_RE = re.compile(r"^#{1,6}\s+")


# ════════════════════════════════════════════════════════════

class TestInjectConvergence:
    """inject_role_knowledge_into_workspace 收敛：KNOWLEDGE 块只含契约，不含 prompt dump。"""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.ws_dir = tmp_path / "ccs-workspaces" / "test_role"
        self.ws_dir.mkdir(parents=True)
        self.claude_md_path = self.ws_dir / "CLAUDE.md"
        self.claude_md_path.write_text(
            "# Test Workspace\n\n"
            "<!-- WORKSPACE_SYS:START -->\n"
            "Global rules content\n"
            "<!-- WORKSPACE_SYS:END -->\n"
        )
        monkeypatch.setattr(
            _rmod, "_resolve_ws_paths",
            lambda _name: [self.claude_md_path],
        )

    def test_inject_only_contract_block(self):
        """KNOWLEDGE 块内容只含'契约'（不含 prompt dump 关键词）"""
        _rmod.inject_role_knowledge_into_workspace(_minimal_role())
        content = self.claude_md_path.read_text(encoding="utf-8")
        assert "<!-- KNOWLEDGE:START -->" in content

        start = content.index("<!-- KNOWLEDGE:START -->")
        end = content.index("<!-- KNOWLEDGE:END -->") + len("<!-- KNOWLEDGE:END -->")
        block = content[start:end]

        dump_keywords = ["行为约束", "输入信号", "输出目标", "评估标准", "动作模板"]
        for kw in dump_keywords:
            assert kw not in block, f"KNOWLEDGE 块不应包含 '{kw}'"
        assert "契约" in block

    def test_inject_idempotent(self):
        """连续两次注入同一个角色，CLAUDE.md 中只出现一次 KNOWLEDGE marker"""
        role = _minimal_role()
        _rmod.inject_role_knowledge_into_workspace(role)
        _rmod.inject_role_knowledge_into_workspace(role)
        content = self.claude_md_path.read_text(encoding="utf-8")
        assert content.count("<!-- KNOWLEDGE:START -->") == 1
        assert content.count("<!-- KNOWLEDGE:END -->") == 1

    def test_contract_block_format(self):
        """_contract_block() 输出包含 '产出分类'、'消费分类'、'协作组'、'驱动方式'"""
        block = _rmod._contract_block(_minimal_role())
        for kw in ["产出分类", "消费分类", "协作组", "驱动方式"]:
            assert kw in block, f"_contract_block 应包含 '{kw}'"

    def test_contract_block_eval_criteria(self):
        """当 eval_criteria 存在时，输出 '验证标准'"""
        role = _minimal_role(eval_criteria=["标准1 | 验证: 检查XX", "标准2 | 验证: 检查YY"])
        assert "验证标准" in _rmod._contract_block(role)

        role2 = _minimal_role()
        assert "验证标准" not in _rmod._contract_block(role2)

    def test_inject_with_broken_marker_only_start(self):
        """CLAUDE.md 只有 KNOWLEDGE:START 无 END marker 时不崩溃，注入后补齐双 marker 且唯一"""
        self.claude_md_path.write_text(
            "# Test Workspace\n\n"
            "<!-- WORKSPACE_SYS:START -->\n"
            "Global rules content\n"
            "<!-- WORKSPACE_SYS:END -->\n"
            "<!-- KNOWLEDGE:START -->\n"
            "Old broken content\n"
        )
        _rmod.inject_role_knowledge_into_workspace(_minimal_role())
        content = self.claude_md_path.read_text(encoding="utf-8")
        assert "<!-- KNOWLEDGE:START -->" in content
        assert "<!-- KNOWLEDGE:END -->" in content
        assert content.count("<!-- KNOWLEDGE:START -->") == 1
        assert content.count("<!-- KNOWLEDGE:END -->") == 1

    def test_inject_no_markers_append(self):
        """CLAUDE.md 无任何 marker 时，KNOWLEDGE 块追加到文件末尾"""
        self.claude_md_path.write_text("# Bare Workspace\n")
        _rmod.inject_role_knowledge_into_workspace(_minimal_role())
        content = self.claude_md_path.read_text(encoding="utf-8")
        assert content.rstrip().endswith("<!-- KNOWLEDGE:END -->")
        assert "契约" in content
        assert "安全红线" in content


# ════════════════════════════════════════════════════════════

class TestLayerPollution:
    """各层不应交叉污染"""

    def test_global_claudemd_no_project_details(self):
        """~/.claude/CLAUDE.md 的 '项目引用'段不含 '关键端口'/'Sister Bus'/'CCS 跨 Session' 作为子段落标题"""
        path = Path.home() / ".claude" / "CLAUDE.md"
        assert path.exists(), "~/.claude/CLAUDE.md 不存在"
        lines = path.read_text(encoding="utf-8").splitlines()
        outside = _lines_outside_code_blocks(lines)

        # 只检查"项目引用"段内的子标题（## 七之后的 ### 标题）
        ref_section_start = None
        for i, l in enumerate(outside):
            if "项目引用" in l and _HEADING_RE.match(l):
                ref_section_start = i
                break
        if ref_section_start is None:
            return  # 没有"项目引用"段 → 通过
        ref_headings = [l for l in outside[ref_section_start:] if _HEADING_RE.match(l)]
        # 排除 ## 七本身（排除行首为 ## 但非 ### 的标题）
        ref_headings = [l for l in ref_headings if l.strip().startswith("###")]

        errors = []
        for kw in ["关键端口", "Sister Bus", "CCS 跨 Session"]:
            for hl in ref_headings:
                if kw in hl:
                    errors.append(f"项目引用段内标题 '{hl.strip()}' 不应包含 '{kw}'")
        assert not errors, "\n".join(errors)

    def test_basemd_no_global_rules(self):
        """base.md 不含全球规则段标题"""
        path = Path.home() / "hermes-session-roles" / "prompts" / "base.md"
        assert path.exists(), "base.md 不存在"
        headings = [l for l in _lines_outside_code_blocks(path.read_text(encoding="utf-8").splitlines()) if _HEADING_RE.match(l)]

        errors = []
        for kw in ["验证协议", "诚信准则", "代码质量", "安全准则", "输出风格", "AI 行为约束", "中文思考"]:
            for hl in headings:
                if kw in hl:
                    errors.append(f"段落标题 '{hl.strip()}' 不应包含全球规则关键词 '{kw}'")
        assert not errors, "\n".join(errors)

    def test_all_projects_have_claudemd(self):
        """三个项目的 CLAUDE.md 都存在且非空"""
        for name in ["hermes-session-roles", "session-launcher", "session-pipeline"]:
            p = Path.home() / name / "CLAUDE.md"
            assert p.exists(), f"{name} 的 CLAUDE.md 不存在"
            assert len(p.read_text(encoding="utf-8").strip()) > 0, f"{name} 的 CLAUDE.md 为空"


# ════════════════════════════════════════════════════════════

class TestSkillAutoLoad:
    """Skill 自动加载机制验证"""

    def test_core_has_skill_autoload(self):
        """core.py 的 start() 函数中包含 '/skill' 字符串"""
        path = Path.home() / "session-launcher" / "src" / "core.py"
        assert path.exists(), "core.py 不存在"
        content = path.read_text(encoding="utf-8")
        start_idx = content.find("def start(")
        assert start_idx >= 0, "start() 函数未找到"
        rest = content[start_idx:]
        end_idx = rest.find("\ndef ", 1)
        assert "/skill" in (rest[:end_idx] if end_idx > 0 else rest), "start() 中未找到 '/skill'"

    def test_skill_path_fallback(self):
        """read_skill() 代码中包含 dir/SKILL.md fallback 逻辑"""
        path = Path.home() / "hermes-session-roles" / "src" / "role_assembler.py"
        assert path.exists(), "role_assembler.py 不存在"
        content = path.read_text(encoding="utf-8")
        # 必须包含 SKILL.md fallback（dir.md → dir/SKILL.md）
        assert "/SKILL.md" in content, (
            "read_skill 没有 SKILL.md fallback 逻辑"
        )
        # 必须包含 'SKILL NOT FOUND' 容错返回
        assert "SKILL NOT FOUND" in content, (
            "read_skill 没有 SKILL NOT FOUND 容错返回"
        )
