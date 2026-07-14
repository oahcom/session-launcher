"""test_cross_role_router.py — 三源验证单元测试"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from routing.router import CrossRoleRouter


class TestCrossRoleRouter:
    """三源验证路由拦截器测试。"""

    @pytest.fixture
    def router(self):
        """使用临时数据库创建路由器。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        router = CrossRoleRouter(db_path=db_path)
        # 创建 workflow_instances 表供测试
        conn = router._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_instances (
                    instance_id TEXT PRIMARY KEY,
                    template_id TEXT,
                    task_id TEXT NOT NULL,
                    assigner TEXT NOT NULL,
                    assignee TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step_id TEXT,
                    step_results TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    completed_at REAL
                )
            """)
            conn.commit()
        finally:
            conn.close()
        yield router
        if Path(db_path).exists():
            Path(db_path).unlink()

    def test_same_role_passes(self, router):
        """同角色消息直接放行。"""
        assert router.intercept("maintainer", "maintainer", "你好") is True

    def test_cli_source_passes(self, router):
        """CLI 来源直接放行。"""
        assert router.intercept("cli", "maintainer", "你好") is True

    def test_intercept_without_raising(self, router):
        """跨角色消息调用不抛出异常（降级放行逻辑）。"""
        # 没有 sentinel/DB/bus 时应该降级放行而非报错
        result = router.intercept("unknown_role", "maintainer", "测试消息")
        # 不应抛出异常
        assert isinstance(result, bool)

    def test_log_cross_role_send(self, router):
        """审计日志应记录跨角色发送。"""
        router.intercept("role_a", "role_b", "测试消息")
        conn = router._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_logs WHERE action='cross_role_send'"
            ).fetchall()
            assert len(rows) >= 1
            detail = json.loads(rows[0]["detail"])
            assert detail["source"] == "role_a"
            assert detail["target"] == "role_b"
        finally:
            conn.close()

    def test_source_triple_check_no_evidence(self, router):
        """无任何证据时 match_count < 2 → 不应通过。"""
        result = router._source_triple_check("non_existent", "测试")
        assert result["sources_ok"] is False
        assert result["match_count"] < 2

    def test_check_db_assigner_empty(self, router):
        """空数据库返回空字符串（不计数）。"""
        result = router._check_db_assigner("any_role")
        assert result == "", f"应返回空字符串，实际={result!r}"

    def test_check_db_assigner_with_data(self, router):
        """有数据时返回 assigners 列表。"""
        conn = router._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_instances (instance_id, template_id, task_id, "
                "assigner, assignee, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'completed', ?)",
                ("test_wf_1", "WL-01", "test_task_1", "test_role", "test_role", time.time())
            )
            conn.commit()
        finally:
            conn.close()

        result = router._check_db_assigner("test_role")
        assert "matched" in result

    def test_check_sentinel_nonexistent(self, router):
        """不存在的哨兵返回 False。"""
        assert router._check_sentinel("no_such_role_xyz") is False
