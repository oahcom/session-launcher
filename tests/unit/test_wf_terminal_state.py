#!/usr/bin/env python3
"""wf complete/fail/cancel 的终止态守卫。

实测事故（2026-10-21）：对已 cancelled 的实例执行 `wf complete` 返回「完成: <id>」
rc=0，并把 status 从 cancelled 改写为 step_done_ready、生成新的 approval_token。
对不存在的 id 同样打印「完成: <id>」。
"""
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflow.client import WorkflowClient  # noqa: E402


def _make_conn(tmp_path):
    """临时库 + 一条最小任务/实例，返回 (conn, wf_id)。"""
    from workflow.db import create_connection
    conn = create_connection(str(tmp_path / "wf.db"))
    tpl = Path(__file__).resolve().parent.parent.parent / "src" / "workflow" / "schema.py"
    assert tpl.exists(), "schema 缺失时本测试无意义"
    conn.execute(
        "INSERT INTO tasks (task_id,title,assigner,status,created_at,updated_at) "
        "VALUES ('t1','probe','qa','running',0,0)"
    )
    conn.execute(
        "INSERT INTO workflow_instances (instance_id,task_id,assigner,assignee,status,created_at) "
        "VALUES ('wf_x','t1','qa','engineer','running',0)"
    )
    conn.commit()
    return conn, "wf_x"


def test_complete_cancelled_is_noop(tmp_path):
    from workflow import client as client_mod
    conn, wf_id = _make_conn(tmp_path)
    client_mod.create_connection = lambda *a, **k: conn
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    conn.execute("UPDATE workflow_instances SET status='cancelled' WHERE instance_id=?", (wf_id,))
    conn.commit()
    assert c.complete(wf_id, "stale") is None
    st = conn.execute("SELECT status FROM workflow_instances WHERE instance_id=?", (wf_id,)).fetchone()["status"]
    assert st == "cancelled", f"已取消的链被复活成 {st}"


def test_complete_missing_raises(tmp_path):
    conn, _ = _make_conn(tmp_path)
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    with pytest.raises(KeyError):
        c.complete("wf_does_not_exist", "x")


def test_cancel_cannot_reopen_completed(tmp_path):
    conn, wf_id = _make_conn(tmp_path)
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    conn.execute("UPDATE workflow_instances SET status='completed' WHERE instance_id=?", (wf_id,))
    conn.commit()
    assert c.cancel(wf_id, "late cleanup") is None
    st = conn.execute("SELECT status FROM workflow_instances WHERE instance_id=?", (wf_id,)).fetchone()["status"]
    assert st == "completed", f"已交付的链被改写成 {st}"
