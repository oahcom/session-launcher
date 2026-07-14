#!/usr/bin/env python3
"""
test_system_health.py — 跨三项目系统有效性测试
所有 FAILED 都是真实问题。
"""

import json
import sys
import sqlite3
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest

SESSION_LAUNCHER = Path("/home/administrator/session-launcher")
CCS_WORKSPACES = Path("/home/administrator/ccs-workspaces")
HERMES_ROLES = Path("/home/administrator/hermes-session-roles")
PROD_DB = Path("/home/administrator/.hermes/state/workflows.db")


class TestOldTemplateMigration:
    """T11: 旧模板迁移验证。"""

    def test_t11_01_old_templates_inactive(self):
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        conn = sqlite3.connect(str(PROD_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT template_id, is_active FROM workflow_templates "
            "WHERE template_id LIKE 'tmpl_%'"
        ).fetchall()
        conn.close()
        inactive = all(r["is_active"] in (0, "0", False) for r in rows)
        assert inactive, f"旧模板未标记 inactive: {[r['template_id'] for r in rows if r['is_active'] not in (0, '0', False)]}"

    def test_t11_02_table_has_new_columns(self):
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        conn = sqlite3.connect(str(PROD_DB))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_templates)").fetchall()}
        conn.close()
        for col in ["is_active", "trigger_scene", "allowed_initiators", "allowed_executors"]:
            assert col in cols, f"缺少 {col}"

    def test_t11_03_tasks_has_template_id(self):
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        conn = sqlite3.connect(str(PROD_DB))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        conn.close()
        assert "template_id" in cols

    def test_t11_04_migration_idempotent(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = sqlite3.connect(f.name)
        conn.executescript("""
            CREATE TABLE workflow_templates (template_id TEXT PRIMARY KEY, name TEXT, description TEXT, steps_json TEXT, created_at REAL NOT NULL);
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, assigner TEXT NOT NULL, assignee TEXT, status TEXT DEFAULT 'created', created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE workflow_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_instance_id TEXT, task_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT, ts REAL NOT NULL);
        """)
        conn.commit(); conn.close()
        from migration_scripts import run_migration
        r1 = run_migration(db_path=f.name, dry_run=False)
        assert r1["success"]
        r2 = run_migration(db_path=f.name, dry_run=False)
        Path(f.name).unlink(missing_ok=True)
        assert r2["success"], "迁移脚本非幂等"


class TestClaudeMdMigration:
    """T14: CLAUDE.md 迁移验证。"""

    def _get_code_block_content(self, content):
        """提取所有代码块内容。"""
        lines = content.split("\n")
        in_code = False
        code_lines = []
        for line in lines:
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                code_lines.append(line)
        return "\n".join(code_lines)

    def test_t14_01_no_v1_in_code_blocks(self):
        """T14-01 代码块中不应含 V1 API。"""
        all_v1 = []
        for f in sorted(CCS_WORKSPACES.glob("*/CLAUDE.md")):
            role = f.parent.name
            code = self._get_code_block_content(f.read_text(encoding="utf-8"))
            for pat in ["create_task("]:
                for i, line in enumerate(code.split("\n"), 1):
                    # Only flag create_task( without _v2 (V1 deprecated API)
                    # complete_task() and fail_task() are legitimate V2 functions
                    if pat in line and "_v2" not in line:
                        all_v1.append(f"{role} 代码块第{i}行: {line.strip()[:80]}")
        assert not all_v1, (
            f"代码块中仍有 {len(all_v1)} 处 V1 API\n" + "\n".join(all_v1[:10])
        )

    def test_t14_02_v2_api_adopted(self):
        """T14-02 角色 CLAUDE.md 已使用 V2 API。"""
        roles_with_v2 = []
        for f in sorted(CCS_WORKSPACES.glob("*/CLAUDE.md")):
            code = self._get_code_block_content(f.read_text(encoding="utf-8"))
            if any(p in code for p in ["create_task_v2(", "complete_step(", "confirm_step("]):
                roles_with_v2.append(f.parent.name)
        assert roles_with_v2, "0 个角色 CLAUDE.md 引用了 V2 API"

    def test_t14_03_core_roles_consistent(self):
        """T14-03 核心角色在两个系统中都存在。"""
        claude_roles = {f.parent.name for f in CCS_WORKSPACES.glob("*/CLAUDE.md")}
        persona_dir = HERMES_ROLES / "personas" / "session-roles"
        persona_roles = set()
        if persona_dir.exists():
            for pf in persona_dir.glob("persona_*.json"):
                try:
                    with open(pf) as fh:
                        d = json.load(fh)
                    name = d.get("assignee", d.get("name", ""))
                    if name:
                        persona_roles.add(name)
                except (json.JSONDecodeError, OSError):
                    continue
        core = {"coordinator", "pm", "pg", "reviewer", "qa", "lr", "product_architect", "maintainer", "devops"}
        missing_claude = core - claude_roles
        missing_persona = core - persona_roles
        errors = []
        if missing_claude:
            errors.append(f"有 persona 无 CLAUDE.md: {missing_claude}")
        if missing_persona:
            errors.append(f"有 CLAUDE.md 无 persona: {missing_persona}")
        assert not errors, "; ".join(errors)


class TestCrossProjectIntegration:
    """跨三项目集成验证。"""

    def test_cp_01_gate_loads_persona_roles(self):
        from workflow_gate import _load_role_registry
        roles = _load_role_registry()
        core = {"coordinator", "pm", "pg", "reviewer", "qa", "lr", "product_architect"}
        missing = core - set(roles)
        assert not missing, f"角色注册表缺少: {missing}"

    def test_cp_02_template_registry_loads_roles(self):
        from template_registry import get_role_registry
        assert len(get_role_registry()) >= 20

    def test_cp_03_templates_only_reference_valid_roles(self):
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        from template_registry import TemplateRegistry, get_role_registry
        valid = set(get_role_registry())
        reg = TemplateRegistry(str(PROD_DB))
        templates = reg.list()
        reg.close()
        bad = set()
        for t in templates:
            for key in ("allowed_initiators", "allowed_executors"):
                val = t.get(key, [])
                if isinstance(val, list):
                    bad.update(set(val) - valid)
        assert not bad, f"模板引用不存在角色: {bad}"

    def test_cp_04_production_db_healthy(self):
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        conn = sqlite3.connect(str(PROD_DB))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        required = {"workflow_templates", "workflow_instances", "tasks", "workflow_logs"}
        missing = required - tables
        assert not missing, f"生产 DB 缺少表: {missing}"

    def test_cp_05_persona_code_consistency(self):
        from template_registry import get_role_registry
        code_roles = set(get_role_registry())
        persona_dir = HERMES_ROLES / "personas" / "session-roles"
        if not persona_dir.exists():
            pytest.skip("persona 目录不存在")
        persona_names = set()
        for pf in persona_dir.glob("persona_*.json"):
            try:
                with open(pf) as fh:
                    d = json.load(fh)
                name = d.get("assignee", d.get("name", ""))
                if name:
                    persona_names.add(name)
            except (json.JSONDecodeError, OSError):
                continue
        extra = code_roles - persona_names
        assert not extra, f"代码多出 persona 未定义角色: {extra}"

    def test_cp_06_workflow_adoption_trend(self):
        """CP-06 模板绑定率健康基线。迁移后新 task 应使用模板。"""
        if not PROD_DB.exists():
            pytest.skip("生产 DB 不存在")
        conn = sqlite3.connect(str(PROD_DB))
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        bound = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE template_id IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        ratio = bound / total * 100 if total > 0 else 0
        # 仅报告不阻断——历史 task 无法回溯，但 <10% 表明流程未被使用
        assert ratio >= 10, f"采用率仅 {ratio:.0f}% ({bound}/{total})，工作流几乎未被使用"

    def test_cp_07_db_accessible(self):
        assert PROD_DB.exists(), f"生产 DB 不存在: {PROD_DB}"
        conn = sqlite3.connect(str(PROD_DB))
        conn.execute("SELECT 1")
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "--color=yes"])
