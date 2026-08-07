"""test_gateway.py — workflow.gateway.Gate 单元测试。

覆盖所有公开方法和主要分支路径。临时 DB 隔离，无外部状态依赖。
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# 从 worktree 源码导入 Gateway（当前 main 分支尚未合入）
import sys

_GW_SRC = str(Path(__file__).resolve().parent.parent / ".claude" / "worktrees" / "fix-hardening" / "src")
if _GW_SRC not in sys.path:
    sys.path.insert(0, _GW_SRC)

from workflow.gateway import Gate  # noqa: E402

# Gate 已导入到本模块作用域。移除 worktree src 并清空 workflow 包缓存，
# 否则 sys.modules['workflow'].__path__ 仍指向 worktree，后续测试的
# import workflow.client 会解析到 worktree 版（无 find_zombies）。
if _GW_SRC in sys.path:
    sys.path.remove(_GW_SRC)
for _m in [k for k in list(sys.modules) if k == "workflow" or k.startswith("workflow.")]:
    del sys.modules[_m]


# ── fixtures ──────────────────────────────────────────

def _base_schema() -> str:
    """最小 schema，覆盖 is_active 列。"""
    return """
    CREATE TABLE IF NOT EXISTS workflow_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        steps_json TEXT NOT NULL,
        steps_mermaid TEXT,
        created_at REAL NOT NULL,
        is_active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS workflow_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id TEXT,
        task_id TEXT,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        ts REAL NOT NULL
    );
    """


def _old_schema() -> str:
    """无 is_active 列的旧 schema。"""
    return """
    CREATE TABLE IF NOT EXISTS workflow_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        steps_json TEXT NOT NULL,
        steps_mermaid TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workflow_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id TEXT,
        task_id TEXT,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        ts REAL NOT NULL
    );
    """


@pytest.fixture()
def gate():
    """返回一个连接到临时 DB 的 Gate 实例。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(_base_schema())
    # 插入测试模板
    conn.execute(
        "INSERT INTO workflow_templates "
        "(template_id, name, description, steps_json, is_active, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("TPL-A", "模板A", "描述足够长的测试模板描述内容",
         json.dumps([{"step_id": "s1", "title": "步骤1", "type": "handoff",
                       "target_role": "reviewer",
                       "prompt_template": "做什么：测试\n怎么做：运行\n验收标准：通过",
                       "failure_patterns": ["错误模式1", "错误模式2"],
                       "estimated_hours": 1.0}]),
         1, time.time()),
    )
    conn.execute(
        "INSERT INTO workflow_templates "
        "(template_id, name, description, steps_json, is_active, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("TPL-INACTIVE", "模板已停用", "用于测试 is_active=0 的模板描述内容",
         json.dumps([{"step_id": "s1", "title": "步骤1", "type": "single",
                       "prompt_template": "做什么：测试\n怎么做：运行\n验收标准：通过",
                       "failure_patterns": ["错误1", "错误2"],
                       "estimated_hours": 1.0}]),
         0, time.time()),
    )
    conn.commit()
    conn.close()
    g = Gate(db_path=f.name)
    yield g
    g.close()
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture()
def old_gate():
    """旧 schema（无 is_active 列）的 Gate。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(_old_schema())
    conn.execute(
        "INSERT INTO workflow_templates "
        "(template_id, name, description, steps_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("OLD-TPL", "旧模板", "无 is_active 列的旧 schema 模板描述内容",
         json.dumps([{"step_id": "s1", "title": "步骤1", "type": "single",
                       "prompt_template": "做什么：测试\n怎么做：运行\n验收标准：通过",
                       "failure_patterns": ["错误1", "错误2"],
                       "estimated_hours": 1.0}]),
         time.time()),
    )
    conn.commit()
    conn.close()
    g = Gate(db_path=f.name)
    yield g
    g.close()
    Path(f.name).unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════
# is_template_exists
# ═══════════════════════════════════════════════════════

class TestIsTemplateExists:
    def test_returns_true_for_existing(self, gate):
        assert gate.is_template_exists("TPL-A") is True

    def test_returns_false_for_missing(self, gate):
        assert gate.is_template_exists("TPL-NONEXISTENT") is False

    def test_empty_string_returns_false(self, gate):
        assert gate.is_template_exists("") is False


# ═══════════════════════════════════════════════════════
# is_template_active
# ═══════════════════════════════════════════════════════

class TestIsTemplateActive:
    def test_active_template(self, gate):
        assert gate.is_template_active("TPL-A") is True

    def test_inactive_template(self, gate):
        assert gate.is_template_active("TPL-INACTIVE") is False

    def test_nonexistent_template(self, gate):
        assert gate.is_template_active("TPL-MISSING") is False

    def test_fallback_to_exists_when_no_is_active_column(self, old_gate):
        # 旧 schema 无 is_active 列 → OperationalError → 降级为 exists
        assert old_gate.is_template_active("OLD-TPL") is True

    def test_fallback_nonexistent_when_no_column(self, old_gate):
        assert old_gate.is_template_active("NOPE") is False


# ═══════════════════════════════════════════════════════
# check_can_initiate
# ═══════════════════════════════════════════════════════

class TestCheckCanInitiate:
    def test_allowed_role(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_initiators=? WHERE template_id=?",
            (json.dumps(["pm", "coordinator"]), "TPL-A"),
        )
        gate._conn.commit()
        assert gate.check_can_initiate("pm", "TPL-A") is True

    def test_disallowed_role(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_initiators=? WHERE template_id=?",
            (json.dumps(["pm"]), "TPL-A"),
        )
        gate._conn.commit()
        assert gate.check_can_initiate("engineer", "TPL-A") is False

    def test_missing_template(self, gate):
        assert gate.check_can_initiate("pm", "NOPE") is False

    def test_fallback_when_no_column(self, old_gate):
        # 旧 schema 无 allowed_initiators 列 → 跳过校验返回 True
        assert old_gate.check_can_initiate("anyone", "OLD-TPL") is True

    def test_corrupt_json_graceful(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_initiators=? WHERE template_id=?",
            ("{invalid json", "TPL-A"),
        )
        gate._conn.commit()
        # JSON 解析失败 → initiators=[] → role 不在空列表 → False
        assert gate.check_can_initiate("pm", "TPL-A") is False

    def test_empty_allowed_list(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_initiators=? WHERE template_id=?",
            ("[]", "TPL-A"),
        )
        gate._conn.commit()
        assert gate.check_can_initiate("pm", "TPL-A") is False


# ═══════════════════════════════════════════════════════
# check_can_execute
# ═══════════════════════════════════════════════════════

class TestCheckCanExecute:
    def test_allowed_role(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_executors=? WHERE template_id=?",
            (json.dumps(["pg", "engineer"]), "TPL-A"),
        )
        gate._conn.commit()
        assert gate.check_can_execute("pg", "TPL-A") is True

    def test_disallowed_role(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_executors=? WHERE template_id=?",
            (json.dumps(["pg"]), "TPL-A"),
        )
        gate._conn.commit()
        assert gate.check_can_execute("devops", "TPL-A") is False

    def test_missing_template(self, gate):
        assert gate.check_can_execute("pg", "NOPE") is False

    def test_fallback_when_no_column(self, old_gate):
        assert old_gate.check_can_execute("anyone", "OLD-TPL") is True

    def test_corrupt_json_graceful(self, gate):
        gate._conn.execute(
            "UPDATE workflow_templates SET allowed_executors=? WHERE template_id=?",
            ("[bad]", "TPL-A"),
        )
        gate._conn.commit()
        # json.loads("[bad]") 成功但结果是 ["bad"]，"pg" not in ["bad"]
        assert gate.check_can_execute("pg", "TPL-A") is False


# ═══════════════════════════════════════════════════════
# validate_create_task（完整校验链）
# ═══════════════════════════════════════════════════════

class TestValidateCreateTask:
    def test_empty_template_id_raises(self, gate):
        with pytest.raises(ValueError, match="template_id is required"):
            gate.validate_create_task("", "pm", "pg")

    def test_nonexistent_template_raises(self, gate):
        with pytest.raises(ValueError, match="template_id not found"):
            gate.validate_create_task("TPL-MISSING", "pm", "pg")

    def test_inactive_template_raises(self, gate):
        with pytest.raises(ValueError, match="template is inactive"):
            gate.validate_create_task("TPL-INACTIVE", "pm", "pg")

    def test_valid_template_passes(self, gate):
        # 不抛异常即通过
        gate.validate_create_task("TPL-A", "pm", "pg")


# ═══════════════════════════════════════════════════════
# record_p0_audit
# ═══════════════════════════════════════════════════════

class TestRecordP0Audit:
    def test_inserts_log_row(self, gate):
        gate.record_p0_audit("pm", "task-001", "紧急生产事故需要立即处理")
        rows = gate._conn.execute(
            "SELECT * FROM workflow_logs WHERE task_id=? AND action='p0_exemption'",
            ("task-001",),
        ).fetchall()
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail"])
        assert detail["reason"] == "紧急生产事故需要立即处理"

    def test_uses_external_conn(self, gate):
        external = sqlite3.connect(gate.db_path)
        external.row_factory = sqlite3.Row
        gate.record_p0_audit("coordinator", "task-002", "外部连接测试场景描述内容",
                              conn=external)
        row = external.execute(
            "SELECT * FROM workflow_logs WHERE task_id=?", ("task-002",)
        ).fetchone()
        assert row is not None
        assert row["actor"] == "coordinator"
        external.close()


# ═══════════════════════════════════════════════════════
# route_task
# ═══════════════════════════════════════════════════════

class TestRouteTask:
    def test_creates_routed_log(self, gate):
        gate.route_task("task-100", "pm", "engineer")
        rows = gate._conn.execute(
            "SELECT * FROM workflow_logs WHERE task_id=? AND action='routed'",
            ("task-100",),
        ).fetchall()
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail"])
        assert "pm" in detail["chain"] and "engineer" in detail["chain"]

    def test_actor_is_from_role(self, gate):
        gate.route_task("task-200", "coordinator", "reviewer")
        rows = gate._conn.execute(
            "SELECT actor FROM workflow_logs WHERE task_id=?", ("task-200",)
        ).fetchall()
        assert rows[0]["actor"] == "coordinator"


# ═══════════════════════════════════════════════════════
# close
# ═══════════════════════════════════════════════════════

class TestClose:
    def test_close_prevents_further_queries(self, gate):
        gate.close()
        with pytest.raises(sqlite3.ProgrammingError):
            gate._conn.execute("SELECT 1")


# ═══════════════════════════════════════════════════════
# get_template（委托给 TemplateRegistry）
# ═══════════════════════════════════════════════════════

class TestGetTemplate:
    def test_returns_dict_for_existing(self, gate):
        t = gate.get_template("TPL-A")
        assert t is not None
        assert t["name"] == "模板A"

    def test_returns_none_for_missing(self, gate):
        t = gate.get_template("NOPE")
        assert t is None
