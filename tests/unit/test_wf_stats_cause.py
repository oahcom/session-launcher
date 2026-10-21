#!/usr/bin/env python3
"""单元测试: workflow/client.py:_cancel_cause / _cancelled_by_cause — 取消归因。

归因口径与 workflow_stats() 的 cancelled_by_cause 一致，
需求记录见 ~/hermes/workspace/R147-僵尸步取证与取消归因/需求.md。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from workflow.client import WorkflowClient, _cancel_cause  # noqa: E402

CASES = [
    # (detail, reason, 期望桶)
    ("僵尸回收: running 状态 s1 无推进记录 4.1h (timeout_count=0)", None, "zombie_stall"),
    ("僵尸回收: running 运行24h > 24h全局阈值", None, "zombie_24h"),
    ("僵尸回收: running 运行1271h > 72h全局阈值", None, "zombie_72h"),
    ("僵尸回收: 当前步骤 s4 已终态(cancelled)但行级仍 running", None,
     "zombie_row_mismatch"),
    ("无活跃工作流", None, "starvation"),
    ("角色不响应自动取消", None, "role_unresponsive"),
    ("workflow delivery_blocked", None, "delivery_blocked"),
    ("reason=重复派发: 同源 DESIGN.md 已产出(bus #376300)", None, "dedup"),
    ("reason=误路由: 知识采集类任务经 WL-full_dev_pipeline 错发 product_architect", None,
     "misroute"),
    ("reason=孤子步已终止", None, "orphan_subflow"),
    ("reason=PM自评#377395: WL-02 fanout无需求映射", None, "manual"),
    # 包装过一层的原因：不剥壳就会落进 other
    ("workflow cancelled (cli cancel: 重复派发: 同源 DESIGN.md 已产出)", None, "dedup"),
    ("workflow cancelled (cli cancel: 误路由: 错发 product_architect)", None, "misroute"),
    ("workflow cancelled (queue 45min 饥饿)", None, "manual"),
    ("workflow cancelled", None, "unspecified"),   # 默认文案，无真实原因
    ("", None, "no_log"),                          # 无取消日志，无 reason
    (None, None, "no_log"),                        # 同上：detail 缺失
    ("workflow cancelled", "连续6分钟无活跃工作流", "starvation"),  # 默认文案 → 回退 reason
    ("s1: 角色不响应自动取消", "任何理由都不改桶", "role_unresponsive"),  # detail 优先
    ("cascade from parent wf_sub_abc", None, "cascade"),  # 父链级联，不是角色问题
    ("reason=zombie cleanup", None, "zombie_stall"),      # launcher 自己的清理传英文
]


def test_cancel_cause_buckets():
    for detail, reason, expected in CASES:
        got = _cancel_cause(detail, reason)
        assert got == expected, (
            f"_cancel_cause({detail!r}, {reason!r}) = {got!r}, 期望 {expected!r}")


def test_cancelled_by_cause_uses_last_cancel_family_log():
    """末条 log 不一定是取消日志：取消之后再写 wf_started 也不能改归因。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE workflow_instances (instance_id TEXT, context TEXT, status TEXT);
        CREATE TABLE workflow_logs (id INTEGER PRIMARY KEY, workflow_instance_id TEXT,
                                    action TEXT, detail TEXT);
        INSERT INTO workflow_instances VALUES ('wf_a', '{"reason": "连续6分钟无活跃工作流"}',
                                              'cancelled');
        INSERT INTO workflow_logs VALUES (1, 'wf_a', 'wf_closed', 'workflow cancelled');
        INSERT INTO workflow_logs VALUES (2, 'wf_a', 'wf_started', 'workflow started');
        INSERT INTO workflow_instances VALUES ('wf_b', NULL, 'cancelled');
        INSERT INTO workflow_logs VALUES (3, 'wf_b', 'cancelled',
                                            '僵尸回收: running 运行24h > 24h全局阈值');
        -- 生产主路径：先 stale rescue 写 wf_closed，再写 cancelled；MAX(id) 必须取后者
        INSERT INTO workflow_instances VALUES ('wf_c', NULL, 'cancelled');
        INSERT INTO workflow_logs VALUES (4, 'wf_c', 'wf_closed',
                                          'workflow cancelled (stale rescue: 6h)');
        INSERT INTO workflow_logs VALUES (5, 'wf_c', 'cancelled',
                                          '僵尸回收: running 状态 s1 无推进记录 4.1h');
    """)
    wf = object.__new__(WorkflowClient)   # 只借方法，不走 __init__ 的真库
    wf._conn = conn
    try:
        assert wf._cancelled_by_cause() == {"starvation": 1, "zombie_24h": 1, "zombie_stall": 1}
    finally:
        conn.close()


def test_cancel_keeps_step_results_and_records_reason():
    """cancel 只加 _close_reason，不得抹掉步骤状态（旧实现写 {"reason": ...} 覆写）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE workflow_instances (instance_id TEXT PRIMARY KEY, status TEXT,
            completed_at REAL, step_results TEXT, task_id TEXT);
        CREATE TABLE workflow_logs (workflow_instance_id TEXT, task_id TEXT, action TEXT,
            actor TEXT, detail TEXT, ts REAL);
    """)
    steps = {"s1": {"status": "notified", "notified_at": 123.0},
             "s2": {"status": "completed", "subflow_id": "wf_sub_1"}}
    conn.execute("INSERT INTO workflow_instances VALUES ('wf_x', 'running', NULL, ?, NULL)",
                 (json.dumps(steps),))
    wf = object.__new__(WorkflowClient)
    wf._conn, wf.role, wf.db_path = conn, 'tester', None
    try:
        assert wf.cancel('wf_x', 'zombie cleanup') is True
        row = conn.execute('SELECT status, step_results FROM workflow_instances').fetchone()
        kept = json.loads(row['step_results'])
        assert row['status'] == 'cancelled'
        assert kept['s1']['notified_at'] == 123.0 and kept['s2']['subflow_id'] == 'wf_sub_1'
        assert kept['_close_reason'] == {'status': 'cancelled', 'reason': 'zombie cleanup'}
        # 终态不覆写：第二次 cancel 返回 None 且不改 _close_reason
        assert wf.cancel('wf_x', 'second') is None
        again = json.loads(conn.execute('SELECT step_results FROM workflow_instances')
                          .fetchone()[0])
        assert again['_close_reason']['reason'] == 'zombie cleanup'
    finally:
        conn.close()


if __name__ == "__main__":
    test_cancel_cause_buckets()
    test_cancelled_by_cause_uses_last_cancel_family_log()
    test_cancel_keeps_step_results_and_records_reason()
    print("PASS", len(CASES), "cases + 2 sql cases")
