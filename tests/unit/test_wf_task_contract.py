#!/usr/bin/env python3
"""`wf task` 必须暴露当前步骤的机器验收契约。

为什么：引擎按 exit_condition 判完成、按 exit_schema 校验产出正文。角色看不到这份
契约就只能猜——实测 qa 的 bug_report.md 正文缺 mustContain 里的 P0/严重度/复现，
完成消息标题合规也不被认，同一份产出被重投 4 次、白等约 30 分钟。
"""
import json
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 收集顺序里其它测试会先 import session-pipeline 的同名 workflow 包
for _m in [k for k in list(sys.modules) if k == "workflow" or k.startswith("workflow.")]:
    del sys.modules[_m]

from workflow.client import WorkflowClient  # noqa: E402
from workflow.db import create_connection  # noqa: E402


def _seed(tmp_path, steps_json, fname="wf.db"):
    db = str(tmp_path / fname)
    conn = create_connection(db)
    conn.execute(
        "INSERT INTO workflow_templates (template_id,name,steps_json,created_at) "
        "VALUES ('T1','T1',?,0)", (steps_json,))
    conn.execute(
        "INSERT INTO tasks (task_id,title,assigner,status,created_at,updated_at) "
        "VALUES ('t1','probe','pm','running',0,0)")
    conn.execute(
        "INSERT INTO workflow_instances (instance_id,template_id,task_id,assigner,assignee,"
        "status,current_step_id,created_at) VALUES ('wf_x','T1','t1','pm','qa','running','s1',0)")
    conn.commit()
    return db, conn


def test_task_exposes_exit_condition_and_schema(tmp_path):
    steps = [{"step_id": "s1", "target_role": "qa",
              "exit_condition": {"bus_category": "test_report", "text_contains": "s1-complete",
                                 "source_contains": "qa"},
              "exit_schema": {"required": ["bug_report.md"],
                              "properties": {"bug_report.md": {"mustContain": ["P0", "严重度"]}}}}]
    db, conn = _seed(tmp_path, json.dumps(steps))
    c = WorkflowClient("qa", db_path=db)
    c._conn = conn
    cur = c.get_task("t1")["progress"]["current"]
    assert cur["current_step"] == "s1"
    assert cur["contract"]["exit_condition"]["text_contains"] == "s1-complete"
    assert cur["contract"]["exit_schema"]["properties"]["bug_report.md"]["mustContain"] == ["P0", "严重度"]


def test_missing_template_or_step_yields_none_without_raising(tmp_path):
    db, conn = _seed(tmp_path, "[]")          # 模板存在但查不到该步
    c = WorkflowClient("qa", db_path=db)
    c._conn = conn
    assert c.get_task("t1")["progress"]["current"]["contract"] is None

    db2, conn2 = _seed(tmp_path, '[{"step_id": "s9"}]', "wf2.db")  # 步骤 id 不匹配
    c2 = WorkflowClient("qa", db_path=db2)
    c2._conn = conn2
    assert c2.get_task("t1")["progress"]["current"]["contract"] is None


def test_step_without_id_does_not_match_null_current_step(tmp_path):
    """current_step_id 为空时不得把"没有 step_id 的步骤"当成它的契约。"""
    db, conn = _seed(tmp_path, json.dumps([{"target_role": "qa",
                                            "exit_condition": {"text_contains": "ghost"}}]),
                     "wf4.db")
    conn.execute("UPDATE workflow_instances SET current_step_id=NULL WHERE instance_id='wf_x'")
    conn.commit()
    c = WorkflowClient("qa", db_path=db)
    c._conn = conn
    assert c.get_task("t1")["progress"]["current"]["contract"] is None


def test_bare_subflow_template_name_resolves_to_prefixed_row(tmp_path):
    """子链行写裸名、模板只登记 WF- 前缀名 —— 契约与首步仍要查得到。

    生产实测 7 天 254 个实例里 126 个（49.6%）是这种不匹配：pipeflow/wf_lifecycle.py
    建子链时用步骤里的 subflow_template（裸名 architect_full_design），模板表里只有
    WF-architect_full_design → contract=None，角色看不到 exit_condition 只能猜验收词；
    首步也退回硬编码 s1（首步是 s0 的模板会跳过 s0）。
    """
    steps = [{"step_id": "s0", "target_role": "product_architect",
              "exit_condition": {"text_contains": "s0-complete"}}]
    db = str(tmp_path / "wf3.db")
    conn = create_connection(db)
    conn.execute(
        "INSERT INTO workflow_templates (template_id,name,steps_json,created_at) "
        "VALUES ('WF-architect_full_design','PA',?,0)", (json.dumps(steps),))
    conn.execute(
        "INSERT INTO tasks (task_id,title,assigner,status,created_at,updated_at) "
        "VALUES ('t2','probe','pm','running',0,0)")
    conn.execute(
        "INSERT INTO workflow_instances (instance_id,template_id,task_id,assigner,assignee,"
        "status,current_step_id,created_at) "
        "VALUES ('wf_y','architect_full_design','t2','pm','product_architect','running','s0',0)")
    conn.commit()
    c = WorkflowClient("product_architect", db_path=db)
    c._conn = conn
    assert c._first_step_id("architect_full_design") == "s0", "首步不得退回 s1"
    cur = c.get_task("t2")["progress"]["current"]
    assert cur["contract"]["exit_condition"]["text_contains"] == "s0-complete"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
