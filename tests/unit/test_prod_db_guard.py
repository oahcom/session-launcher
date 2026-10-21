"""守护：测试进程不得指向生产 workflows.db。

2026-10-21：test_wf_create_entry 的缺省建流用例写进生产库（tasks/workflow_instances
各 +1，且 fixture 被当成真实任务派进 pm 的 pane）。根因是路径入口分属
launcher/pipeline 两侧，缺口子时子进程回落生产。
"""
import sqlite3
from pathlib import Path

from conftest import PROD_WF_DB, assert_workflows_db_isolated


def test_prod_db_guard_all_entrypoints_point_to_tmp():
    """四条入口（2 常量 + 2 环境变量）都不能等于生产路径。"""
    assert Path(PROD_WF_DB).exists(), "生产库不存在，本守护失去意义"
    assert_workflows_db_isolated()


def test_prod_db_guard_test_write_lands_outside_production():
    """真实写一次，生产库行数必须不变 —— 缺会话夹具时本断言会失败。"""
    import paths

    before = _counts(PROD_WF_DB)
    conn = sqlite3.connect(str(paths.WORKFLOWS_DB))
    conn.execute(
        "INSERT INTO tasks (task_id, title, description, assigner, assignee, "
        "status, created_at, updated_at) "
        "VALUES ('task_guard_probe', '守卫探针', '', 'qa', 'qa', 'created', 0, 0)"
    )
    conn.commit()
    conn.close()
    assert _counts(PROD_WF_DB) == before, f"写入落到了生产库: {before} → {_counts(PROD_WF_DB)}"


def _counts(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return (
            conn.execute("SELECT count(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT count(*) FROM workflow_instances").fetchone()[0],
        )
    finally:
        conn.close()
