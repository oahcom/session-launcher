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
    ("僵尸回收: running 运行24h > 24h全局阈值", None, "zombie_global"),
    ("僵尸回收: running 运行4h > 4h全局阈值", None, "zombie_global"),
    ("僵尸回收: 当前步骤 s4 已终态(cancelled)但行级仍 running", None,
     "zombie_row_mismatch"),
    # 责任线索：queued 是角色忙时等待的空转 tick 数（系统侧）；
    # =0 只说明"没有等待信号"，实测 10/10 的角色当时都在干活——别读成沉默
    ("僵尸回收: running 状态 s1 无推进记录 步骤停滞 1.3h / 实例 1.3h (timeout_count=0, queued=96)",
     None, "zombie_stall_queue"),
    ("僵尸回收: running 状态 s1 无推进记录 步骤停滞 0.4h / 实例 1.4h (timeout_count=2, queued=0)",
     None, "zombie_stall_no_wait"),
    ("僵尸回收: running 状态 s1 无推进记录 1.0h (timeout_count=0)", None, "zombie_stall"),
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
    # 左边界与类型：requeued / 小数 / 非字符串 detail 都不能误判
    ("running 状态 s1 requeued=5 无推进记录", None, "other"),
    ("僵尸回收: running 状态 s1 无推进记录 (timeout_count=0, queued=0.5)", None, "zombie_stall_no_wait"),
    ("僵尸回收: running 状态 s1 无推进记录 (timeout_count=0, total_queued=96)", None, "zombie_stall"),
    (123, None, "other"),   # 非字符串 detail 不该打崩 stats，归 other
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
        assert wf._cancelled_by_cause() == {"starvation": 1, "zombie_global": 1, "zombie_stall": 1}
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


def test_close_does_not_clobber_concurrent_step_updates():
    """合并必须在一条 UPDATE 里完成：SELECT 之后再 UPDATE 会覆盖并发写入的步骤。"""
    uri = "file:race_demo_test?mode=memory&cache=shared"
    a = sqlite3.connect(uri, uri=True, timeout=5)
    b = sqlite3.connect(uri, uri=True, timeout=5)
    a.row_factory = sqlite3.Row
    a.executescript("""
        CREATE TABLE workflow_instances (instance_id TEXT PRIMARY KEY, status TEXT,
            completed_at REAL, step_results TEXT, task_id TEXT);
        CREATE TABLE workflow_logs (workflow_instance_id TEXT, task_id TEXT, action TEXT,
            actor TEXT, detail TEXT, ts REAL);
    """)
    a.execute("INSERT INTO workflow_instances VALUES ('wf1', 'running', NULL, ?, NULL)",
              (json.dumps({"s1": {"status": "notified", "notified_at": 1.0}}),))
    a.commit()

    class RaceProxy:
        """在 _close 的 UPDATE 真正执行前，模拟引擎提交一次步骤更新。"""
        def __init__(self, conn):
            self._conn, self.fired = conn, False

        def execute(self, sql, params=()):
            if not self.fired and sql.startswith("UPDATE workflow_instances SET status=?") \
                    and "json_set" in sql:
                self.fired = True
                b.execute("UPDATE workflow_instances SET step_results=? WHERE instance_id='wf1'",
                          (json.dumps({"s1": {"status": "completed"},
                                      "s2": {"subflow_id": "wf_sub"}}),))
                b.commit()
            return self._conn.execute(sql, params)

        def commit(self):
            return self._conn.commit()

        def rollback(self):
            return self._conn.rollback()

    wf = object.__new__(WorkflowClient)
    wf._conn, wf.role, wf.db_path = RaceProxy(a), 'tester', None
    try:
        assert wf.cancel('wf1', 'zombie cleanup') is True
        out = json.loads(a.execute("SELECT step_results FROM workflow_instances").fetchone()[0])
        assert out["s2"] == {"subflow_id": "wf_sub"}, f"并发写入被覆盖: {out}"
        assert out["s1"]["status"] == "completed"
        assert out["_close_reason"]["reason"] == "zombie cleanup"
    finally:
        a.close()
        b.close()




def test_chain_state_skips_metadata_keys():
    """_close_reason 是元数据，不能被渲染成一条假步骤。"""
    from workflow.state import chain_state

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE workflow_instances (instance_id TEXT, task_id TEXT, template_id TEXT,
            status TEXT, current_step_id TEXT, step_results TEXT, created_at REAL);
    """)
    payload = json.dumps({"s1": {"status": "cancelled"},
                          "_close_reason": {"status": "cancelled", "reason": "x"}})
    conn.execute(
        "INSERT INTO workflow_instances VALUES ('wf_m', NULL, 'WL-01', 'cancelled',"
        " 's1', ?, 0)", (payload,))
    steps = chain_state(conn, "wf_m")["steps"]
    assert [s["step"] for s in steps] == ["s1"], steps
    conn.close()


def test_task_sync_does_not_mark_cancelled_task_completed():
    """取消实例必须把 task 标 cancelled；未知状态 fail-safe 为 in_progress。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE workflow_instances (instance_id TEXT, task_id TEXT, status TEXT);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, updated_at REAL);
    """)
    conn.execute("INSERT INTO tasks VALUES ('t1', 'in_progress', 0)")
    conn.execute("INSERT INTO tasks VALUES ('t2', 'in_progress', 0)")
    conn.execute("INSERT INTO workflow_instances VALUES ('wf_a', 't1', 'cancelled')")
    conn.execute("INSERT INTO workflow_instances VALUES ('wf_b', 't2', 'delivery_blocked')")
    wf = object.__new__(WorkflowClient)
    wf._conn, wf.role, wf.db_path = conn, 'tester', None
    try:
        wf._sync_task_from_workflows('t1')
        wf._sync_task_from_workflows('t2')
        got = {r[0]: r[1] for r in conn.execute('SELECT task_id, status FROM tasks')}
        assert got == {'t1': 'cancelled', 't2': 'in_progress'}, got
    finally:
        conn.close()


if __name__ == "__main__":
    test_cancel_cause_buckets()
    test_cancelled_by_cause_uses_last_cancel_family_log()
    test_cancel_keeps_step_results_and_records_reason()
    test_close_does_not_clobber_concurrent_step_updates()
    test_chain_state_skips_metadata_keys()
    test_task_sync_does_not_mark_cancelled_task_completed()
    print("PASS", len(CASES), "cases + 5 sql cases")
