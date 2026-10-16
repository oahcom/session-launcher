"""wf cleanup — find_zombies 测试。

原实现把全部逻辑写在模块级（无 def test_*），导致两个后果：
1. 断言在 **collection 期**执行、pytest 收集到 0 个用例——即使断言失败也只是
   报告成"收集错误"，正常时则显示 "collected 0 items / no tests ran"，是假通过。
2. 在 CI 布局（三仓同级 clone）下，`import workflow.client` 会解析到
   **session-pipeline 的 workflow 包**（其 WorkflowClient 无 find_zombies）→
   AttributeError 中断整个套件的收集。本地跑时恰好解析到 launcher 自己的
   workflow 包，所以问题不显形（DR-002 同名包靠 sys.path 顺序决定谁生效）。

改为真正的测试函数：用例在运行时执行，且显式绑定到 launcher 自己的 workflow
包（用文件路径直载），不再依赖 sys.path 顺序。
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

_LAUNCHER_SRC = Path(__file__).resolve().parent.parent.parent / "src"


def _load_launcher_workflow_client():
    """按文件路径直载 launcher 的 workflow/client.py，绕开同名包竞争。

    不可用 `import workflow.client`：CI 布局下它可能解析到 session-pipeline 的
    workflow 包（无 find_zombies）。client.py 内部 `from workflow import db`
    等自引用依赖包可导入，故先把 launcher/src 置于 sys.path 首位。
    """
    if str(_LAUNCHER_SRC) not in sys.path:
        sys.path.insert(0, str(_LAUNCHER_SRC))
    spec = importlib.util.spec_from_file_location(
        "launcher_wf_client", str(_LAUNCHER_SRC / "workflow" / "client.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wf_client_with_rows():
    """造一个临时 DB（含 workflow_instances），注入 WorkflowClient 后返回。

    WHY 临时 DB：本用例的语义是"给定若干 running/completed 行，find_zombies 的
    过滤是否正确"，与生产库无关，绝不能碰生产 workflows.db。
    """
    mod = _load_launcher_workflow_client()
    tmp_db = Path(tempfile.mkdtemp()) / "wf_test.db"
    conn = sqlite3.connect(tmp_db)
    conn.executescript("""
    CREATE TABLE workflow_instances (
        instance_id TEXT PRIMARY KEY,
        template_id TEXT, task_id TEXT, assigner TEXT, assignee TEXT,
        status TEXT, current_step_id TEXT, step_results TEXT DEFAULT '{}',
        created_at REAL, completed_at REAL, parent_wf_id TEXT,
        subflow_source_step_id TEXT, context TEXT DEFAULT '{}'
    );
    """)
    conn.commit()
    conn.close()

    orig = mod.create_connection

    def _fake_conn(db_path=None):
        # row_factory 必须设：find_zombies 用 `dict(r)` 取列名，默认的元组行会抛
        # `ValueError: dictionary update sequence element #0 has length 10`
        c = sqlite3.connect(tmp_db)
        c.row_factory = sqlite3.Row
        return c

    mod.create_connection = _fake_conn
    try:
        now = time.time()
        with mod.WorkflowClient("engineer") as wf:
            wf._conn.execute(
                "INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wf_old1", "tpl", "t1", "engineer", "engineer", "running", "s1",
                 json.dumps({"s1": {"timeout_count": 5}}), now - 7200, None, None, None, "{}"))
            wf._conn.execute(
                "INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wf_zombie1", "tpl", "t2", "engineer", "engineer", "running", "s1",
                 json.dumps({"s1": {"status": "notified"}}), now - 5400, None, None, None, "{}"))
            wf._conn.execute(
                "INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wf_zombie0", "tpl", "t2b", "engineer", "engineer", "running", "s1",
                 json.dumps({"s1": {"status": "notified", "timeout_count": 0}}),
                 now - 5400, None, None, None, "{}"))
            wf._conn.execute(
                "INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wf_done", "tpl", "t3", "engineer", "engineer", "completed", "s1",
                 "{}", now - 10800, now, None, None, "{}"))
            wf._conn.commit()
            yield wf
    finally:
        mod.create_connection = orig


def test_find_zombies_detects_stale_running(wf_client_with_rows):
    """running 超阈值且无有效 timeout_count → 判为僵尸。"""
    ids = {z["instance_id"] for z in wf_client_with_rows.find_zombies(minutes=30)}
    assert "wf_zombie1" in ids, f"wf_zombie1 未检出: {ids}"
    assert "wf_zombie0" in ids, f"wf_zombie0(timeout_count=0) 未检出: {ids}"


def test_find_zombies_excludes_tracked_and_completed(wf_client_with_rows):
    """有 timeout_count 的 running 与 completed 都不算僵尸。"""
    ids = {z["instance_id"] for z in wf_client_with_rows.find_zombies(minutes=30)}
    assert "wf_old1" not in ids, "wf_old1 应被过滤(有 timeout_count)"
    assert "wf_done" not in ids, "completed 不应出现"


def test_find_zombies_wider_window_still_excludes_tracked(wf_client_with_rows):
    """窗口放宽到 60 分钟：僵尸仍在，有 timeout_count 的仍被排除。"""
    ids60 = {z["instance_id"] for z in wf_client_with_rows.find_zombies(minutes=60)}
    assert "wf_zombie1" in ids60
    assert "wf_zombie0" in ids60
    assert "wf_old1" not in ids60
