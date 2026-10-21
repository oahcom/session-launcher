#!/usr/bin/env python3
"""wf state —— 角色查链状态的浅接口。

为什么加这个命令：角色 48h 内手写 14,909 次 sqlite3（占 Bash 调用 32%），其中
1,207 次只为读 workflow_instances.step_results 一列（每步状态/排队/完成者）。
列名散落在角色提示词里，引擎改一次列名，角色就集体空跑。

契约：
- 实例不存在 → rc≠0 + stderr（stdout+rc=0 会被读成"这条链没有状态"）
- step_results 里的非 dict 值（历史 'summary'/'files' 简写）跳过，不猜语义
- 只读，不改库
"""
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = str(_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

for _m in [k for k in list(sys.modules) if k == "workflow" or k.startswith("workflow.")]:
    del sys.modules[_m]

from workflow.client import WorkflowClient  # noqa: E402


def _conn(tmp_path):
    from workflow.db import create_connection
    conn = create_connection(str(tmp_path / "wf.db"))
    conn.execute(
        "INSERT INTO tasks (task_id,title,assigner,status,created_at,updated_at) "
        "VALUES ('t1','probe','qa','running',0,0)"
    )
    conn.execute(
        "INSERT INTO workflow_instances (instance_id,task_id,assigner,assignee,status,created_at,step_results) "
        "VALUES ('wf_x','t1','qa','engineer','running',0,?)",
        ('{"s1":{"status":"completed","completed_by":"workflow_engine"},'
         '"s2":{"status":"","queued":12,"queued_since":1000.0},'
         '"s3":"summary-only"}',),
    )
    conn.commit()
    return conn


def test_state_reports_step_level_truth(tmp_path):
    conn = _conn(tmp_path)
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    st = c.state("wf_x")
    assert st and st["status"] == "running"
    steps = {s["step"]: s for s in st["steps"]}
    assert steps["s1"]["status"] == "completed"
    assert steps["s1"]["completed_by"] == "workflow_engine"
    assert steps["s2"]["queued"] == 12 and steps["s2"]["queued_h"] > 0
    assert "s3" not in steps, "非 dict 的 step_results 值应跳过而不是当状态读"
    c.close()


def test_state_missing_instance_returns_none(tmp_path):
    conn = _conn(tmp_path)
    c = WorkflowClient("engineer", db_path=str(tmp_path / "wf.db"))
    c._conn = conn
    assert c.state("wf_nope") is None
    c.close()


def test_cli_state_missing_instance_exits_nonzero(tmp_path):
    """查不到必须 rc≠0：rc=0 + 空 stdout 会被角色读成"链没有状态"。"""
    from workflow.db import create_connection
    db = tmp_path / "wf.db"
    create_connection(str(db)).close()
    import os
    env = {**os.environ, "HERMES_WORKFLOWS_DB": str(db)}
    r = subprocess.run(
        [sys.executable, str(_ROOT / "src" / "wf.py"), "-r", "engineer", "state", "wf_nope"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert "未找到" in r.stderr
