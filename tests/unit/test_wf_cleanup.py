"""wf cleanup — find_zombies 测试。"""
import json, os, sqlite3, tempfile, time
from pathlib import Path

os.environ.setdefault("CCS_ROLE", "engineer")

# patch DB before import
_tmp = Path(tempfile.mktemp(suffix=".db"))
_tmp_conn = sqlite3.connect(_tmp)
_tmp_conn.executescript("""
CREATE TABLE workflow_instances (
    instance_id TEXT PRIMARY KEY,
    template_id TEXT, task_id TEXT, assigner TEXT, assignee TEXT,
    status TEXT, current_step_id TEXT, step_results TEXT DEFAULT '{}',
    created_at REAL, completed_at REAL, parent_wf_id TEXT,
    subflow_source_step_id TEXT, context TEXT DEFAULT '{}'
);
""")
_tmp_conn.commit()
_tmp_conn.close()

import workflow.client as wc
_orig = wc.create_connection

def _fake_conn(db_path=None):
    c = sqlite3.connect(_tmp)
    c.row_factory = sqlite3.Row
    return c

wc.create_connection = _fake_conn

from workflow.client import WorkflowClient
now = time.time()

with WorkflowClient("engineer") as wf:
    # running < 30min 且有 timeout_count → 不是僵尸
    wf._conn.execute("INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wf_old1", "tpl", "t1", "engineer", "engineer", "running", "s1",
         json.dumps({"s1": {"timeout_count": 5}}), now - 7200, None, None, None, "{}"))
    # running > 60min 无 timeout_count → 僵尸
    wf._conn.execute("INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wf_zombie1", "tpl", "t2", "engineer", "engineer", "running", "s1",
         json.dumps({"s1": {"status": "notified"}}), now - 5400, None, None, None, "{}"))
    # running > 60min 且 timeout_count=0 → 也是僵尸（追踪启动但无回收）
    wf._conn.execute("INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wf_zombie0", "tpl", "t2b", "engineer", "engineer", "running", "s1",
         json.dumps({"s1": {"status": "notified", "timeout_count": 0}}), now - 5400, None, None, None, "{}"))
    # completed → 不算
    wf._conn.execute("INSERT INTO workflow_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wf_done", "tpl", "t3", "engineer", "engineer", "completed", "s1",
         "{}", now - 10800, now, None, None, "{}"))
    wf._conn.commit()

    zombies = wf.find_zombies(minutes=30)
    ids = {z["instance_id"] for z in zombies}
    assert "wf_zombie1" in ids, f"wf_zombie1 未检出: {ids}"
    assert "wf_zombie0" in ids, f"wf_zombie0(timeout_count=0) 未检出: {ids}"
    assert "wf_old1" not in ids, "wf_old1 应被过滤(有 timeout_count)"
    assert "wf_done" not in ids, "completed 不应出现"

    zombies60 = wf.find_zombies(minutes=60)
    ids60 = {z["instance_id"] for z in zombies60}
    assert "wf_zombie1" in ids60
    assert "wf_zombie0" in ids60
    assert "wf_old1" not in ids60

print("all passed")
