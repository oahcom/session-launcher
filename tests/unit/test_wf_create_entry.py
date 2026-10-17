#!/usr/bin/env python3
"""回归：`wf create` 不传 -t 时必须有可用入口（自动推荐）。

锁定 2026-10-17 的完整性断裂：session-launcher 与 session-pipeline 各有一份
`workflow/client.py`，且是**分叉**的两份。pipeline 侧修好「不传 template_id 时
自动推荐」后，角色走的是 launcher 侧——其 `_validate_create_task` 里的
`raise "template_id is required"` 仍在，建流入口物理不可用。而角色 prompt
（hermes-session-roles/prompts/roles/pm.md:33）教的正是不带 -t 的形式：

    wf -r pm create "做一个高质量网页" -d "需求: ..." -i pm

故 `wf create <title>` 必须能建成，且首步落在发起角色自己身上（用户场景：
「pm 建工作流且自己是第一步的工作者」）。

跑法：python3 -m pytest tests/unit/test_wf_create_entry.py -q
"""
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))


def _real_db() -> Path:
    sys.path.insert(0, str(SRC / "workflow"))
    from paths import WORKFLOWS_DB
    return Path(WORKFLOWS_DB)


def test_delegate_method_exists():
    """缺省路径必须有委托实现——它是入口能通的前提。"""
    from workflow.client import WorkflowClient
    assert hasattr(WorkflowClient, "_delegate_create_task_v2"), \
        "template_id 缺省时无委托实现，wf create 会再次抛 template_id is required"
    src = Path(WorkflowClient.__module__ and
               sys.modules[WorkflowClient.__module__].__file__).read_text(encoding="utf-8")
    # 硬闸门不得再挡住缺省路径：create_task_v2 必须先判空再走原逻辑
    idx = src.index("def create_task_v2")
    body = src[idx:idx + 1400]
    assert "if not template_id:" in body, \
        "create_task_v2 缺 template_id 空判分支，缺省调用会被 _validate_create_task 拦死"


def test_recommend_branch_reachable_from_launcher():
    """委托目标（pipeline 的 create_task_v2）必须真的带自动推荐分支。"""
    from paths import SESSION_PIPELINE_SRC
    p = Path(SESSION_PIPELINE_SRC) / "workflow" / "client.py"
    assert p.exists(), f"pipeline client 不存在: {p}"
    src = p.read_text(encoding="utf-8")
    assert "TemplateRegistry" in src and "recommend(" in src, \
        "pipeline 侧缺自动推荐，委托无意义"


def test_pipeline_client_loadable_in_child_process():
    """子进程能 import pipeline 版（两项目都有 workflow 包，in-process 会遮蔽）。"""
    from paths import SESSION_PIPELINE_SRC
    code = (
        f"import sys; sys.path.insert(0, {str(SESSION_PIPELINE_SRC)!r})\n"
        "from workflow.client import WorkflowClient\n"
        "print('OK', WorkflowClient.__module__)\n"
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"子进程 import 失败: {r.stderr[-300:]}"
    assert "OK" in r.stdout


@pytest.mark.skipif(not _real_db().exists(), reason="生产 DB 不存在")
def test_default_path_creates_workflow_and_cleans_up():
    """端到端：缺省建流成功，首步落在发起角色，且测试数据被清理。

    只读断言 + 单一实例清理，不触碰其他行。
    """
    import json
    import sqlite3

    db = _real_db()
    code = (
        f"import sys, json; sys.path.insert(0, {str(SRC)!r}); "
        f"sys.path.insert(0, {str(SRC / 'workflow')!r})\n"
        "from workflow.client import WorkflowClient\n"
        "wc = WorkflowClient('pm')\n"
        "try:\n"
        "    tid, wid = wc.create_task_v2('做一个高质量网页', 'pm', '', 'pm',\n"
        "                                 description='unittest probe')\n"
        "    print(json.dumps({'ok': True, 'tid': tid, 'wid': wid}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'err': str(e)}))\n"
        "finally:\n"
        "    wc.close()\n"
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=120)
    last = (r.stdout or "").strip().splitlines()[-1:] or [""]
    assert last[0].startswith("{"), f"无结构化输出: {r.stderr[-300:]}"
    res = json.loads(last[0])
    try:
        assert res.get("ok"), f"缺省建流失败: {res.get('err')}"
        wid, tid = res["wid"], res["tid"]
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        inst = conn.execute(
            "SELECT template_id, current_step_id, status, assignee "
            "FROM workflow_instances WHERE instance_id=?", (wid,)).fetchone()
        assert inst is not None, "实例未写入"
        tpl_id = inst["template_id"]
        row = conn.execute("SELECT steps_json FROM workflow_templates WHERE template_id=?",
                           (tpl_id,)).fetchone()
        first_target = json.loads(row["steps_json"])[0].get("target_role") if row else None
        conn.close()
        # 用户场景：pm 建流 → 首步就是 pm 自己
        assert first_target == "pm", \
            f"首步 target_role={first_target}，pm 建流应落在 pm 自己（模板 {tpl_id}）"
    finally:
        if res.get("ok"):
            conn = sqlite3.connect(str(db))
            conn.execute("DELETE FROM workflow_instances WHERE instance_id=?", (res["wid"],))
            conn.execute("DELETE FROM tasks WHERE task_id=?", (res["tid"],))
            conn.execute("DELETE FROM workflow_logs WHERE task_id=?", (res["tid"],))
            conn.commit()
            conn.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all wf-create entry checks green")
