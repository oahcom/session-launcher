"""test_tmux_ops.py — tmux_ops 核心函数测试覆盖。"""
import sys
from pathlib import Path

# conftest.py 已设置 sys.path
from tmux_ops import make_tmux_name, parse_tmux_name

# ── parse_tmux_name 正反向一致性 ──

class TestParseTmuxName:
    """parse_tmux_name(make_tmux_name(role, id)) == (role, id) round-trip."""

    def test_round_trip_id0(self):
        assert parse_tmux_name(make_tmux_name("pm", 0)) == ("pm", 0)

    def test_round_trip_id1(self):
        assert parse_tmux_name(make_tmux_name("engineer", 3)) == ("engineer", 3)

    def test_round_trip_id16(self):
        assert parse_tmux_name(make_tmux_name("writer", 16)) == ("writer", 16)

    def test_round_trip_hyphen_role(self):
        assert parse_tmux_name(make_tmux_name("code-reviewer", 2)) == ("code-reviewer", 2)

    def test_round_trip_underscore_role(self):
        assert parse_tmux_name(make_tmux_name("qa_engineer", 5)) == ("qa_engineer", 5)

    def test_round_trip_cdx_prefix(self):
        """cdx- prefix is also valid (codex sessions)."""
        name = "cdx-coderunner-7"
        assert parse_tmux_name(name) == ("coderunner", 7)

    def test_non_ccs_prefix_returns_empty(self):
        assert parse_tmux_name("other-something") == ("", 0)

    def test_ccs_plain_no_id(self):
        assert parse_tmux_name("ccs-pm") == ("pm", 0)

    def test_ccs_with_ambiguous_digits(self):
        """ccs-role-12abc is treated as role='role-12abc', id=0 (digits must be at end)."""
        assert parse_tmux_name("ccs-role-12abc") == ("role-12abc", 0)

    def test_ccs_digits_only(self):
        """ccs-42 parses as role='42', id=0 (no dash before digits)."""
        assert parse_tmux_name("ccs-42") == ("42", 0)

# ── make_tmux_name 特殊字符 ──

class TestMakeTmuxName:
    def test_default_prefix(self):
        assert make_tmux_name("pm") == "ccs-pm"

    def test_instance_id_prefix(self):
        assert make_tmux_name("pm", 3) == "ccs-pm-3"

    def test_instance_id_zero_no_suffix(self):
        """id=0 应该无后缀（向后兼容）。"""
        assert make_tmux_name("engineer", 0) == "ccs-engineer"

    def test_empty_role(self):
        """空 role 仍然生成合法 tmux 名。"""
        assert make_tmux_name("") == "ccs-"
        assert make_tmux_name("", 2) == "ccs--2"

    def test_role_with_hyphens(self):
        assert make_tmux_name("code-reviewer", 1) == "ccs-code-reviewer-1"

    def test_role_with_underscores(self):
        assert make_tmux_name("qa_engineer", 5) == "ccs-qa_engineer-5"

    def test_role_with_digits(self):
        assert make_tmux_name("role1", 2) == "ccs-role1-2"

    def test_instance_999(self):
        assert make_tmux_name("dev", 999) == "ccs-dev-999"

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
