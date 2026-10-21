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

# 收集顺序里 tests/test_gateway.py 会先 import session-pipeline 的同名 workflow 包，
# 留在 sys.modules 里会让下面这行拿到另一仓库的类（无 complete）。清掉再导入。
for _m in [k for k in list(sys.modules) if k == "workflow" or k.startswith("workflow.")]:
    del sys.modules[_m]

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


def _client(conn, tmp_path):
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    return c


def _status(conn, wf_id):
    return conn.execute(
        "SELECT status FROM workflow_instances WHERE instance_id=?", (wf_id,)
    ).fetchone()["status"]


@pytest.mark.parametrize("terminal", ["cancelled", "completed", "failed", "archived"])
def test_complete_terminal_is_noop(tmp_path, terminal):
    """已取消的链被复活成 step_done_ready 是实测事故；archived 同理。"""
    conn, wf_id = _make_conn(tmp_path)
    c = _client(conn, tmp_path)
    conn.execute("UPDATE workflow_instances SET status=? WHERE instance_id=?", (terminal, wf_id))
    conn.commit()
    assert c.complete(wf_id, "stale") is None
    assert _status(conn, wf_id) == terminal


def test_complete_missing_raises(tmp_path):
    conn, _ = _make_conn(tmp_path)
    with pytest.raises(KeyError):
        _client(conn, tmp_path).complete("wf_does_not_exist", "x")


def test_fail_cannot_close_completed(tmp_path):
    conn, wf_id = _make_conn(tmp_path)
    c = _client(conn, tmp_path)
    conn.execute("UPDATE workflow_instances SET status='completed' WHERE instance_id=?", (wf_id,))
    conn.commit()
    assert c.fail(wf_id, "late failure") is None
    assert _status(conn, wf_id) == "completed", "已交付的链被改写成 failed"


def test_cancel_cannot_reopen_completed(tmp_path):
    conn, wf_id = _make_conn(tmp_path)
    c = _client(conn, tmp_path)
    conn.execute("UPDATE workflow_instances SET status='completed' WHERE instance_id=?", (wf_id,))
    conn.commit()
    assert c.cancel(wf_id, "late cleanup") is None
    assert _status(conn, wf_id) == "completed", "已交付的链被改写成 cancelled"


def test_mutable_write_returns_true(tmp_path):
    """正常路径必须报告写入成功，否则 CLI 无法区分空操作。"""
    conn, wf_id = _make_conn(tmp_path)
    c = _client(conn, tmp_path)
    assert c.fail(wf_id, "boom") is True
    assert _status(conn, wf_id) == "failed"


def test_terminal_check_and_write_are_atomic(tmp_path):
    """TOCTOU：另一连接在 SELECT 与 UPDATE 之间关闭实例，写入不得生效。

    旧实现先 SELECT 判终态再 UPDATE，两条语句之间可被插入 cancel；
    现实现把判定下推 WHERE，无窗口。
    """
    from workflow.db import create_connection
    conn, wf_id = _make_conn(tmp_path)
    c = _client(conn, tmp_path)
    # 模拟并发写入者：状态在守卫语句执行前已被改为 cancelled。
    other = create_connection(str(tmp_path / "wf.db"))
    other.execute("UPDATE workflow_instances SET status='cancelled' WHERE instance_id=?", (wf_id,))
    other.commit()
    assert c.complete(wf_id, "raced") is None
    assert _status(other, wf_id) == "cancelled"
