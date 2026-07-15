#!/usr/bin/env python3
"""Workflow 僵死自动清理器 — 标记已完成任务的 stuck workflow 为 completed。"""
import sys, time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'src'))
from workflow.client import WorkflowClient
from workflow.db import create_connection

def main(dry_run: bool = True) -> int:
    conn = create_connection()
    rows = conn.execute("""
        SELECT w.instance_id, w.assignee, w.created_at, t.status as task_status
        FROM workflow_instances w
        JOIN tasks t ON t.task_id = w.task_id
        WHERE w.status='running' AND t.status='completed'
    """).fetchall()
    now = time.time()
    wc = WorkflowClient('cleanup')
    cleaned = 0
    for inst_id, assignee, created_at, task_status in rows:
        if not created_at or (now - created_at) < 3600:
            continue
        action = '[DRY-RUN] would complete' if dry_run else 'completed'
        print(f'  {action} {inst_id} ({assignee})')
        if not dry_run:
            wc.complete(inst_id, f'auto-cleaned task already {task_status}')
            wc._log(wf_id=inst_id, action='auto_cleanup', detail=f'stale {int((now-created_at)/3600)}h')
        cleaned += 1
    wc.close()
    conn.close()
    print(f'\n{"DRY-RUN: " if dry_run else ""}{cleaned} stale workflows can be cleaned')
    return cleaned

if __name__ == '__main__':
    dry_run = '--apply' not in sys.argv
    count = main(dry_run=dry_run)
    if not dry_run and count:
        print('✅ Applied — use --dry-run to preview next time')

# ── 自检 ──────────────────────────────────────────────────
def _self_check():
    """assert-based 自检：验证脚本核心逻辑正确。"""
    conn = create_connection()
    # 不应再有 task=completed 但 instance=running 的记录
    stale = conn.execute("""
        SELECT COUNT(*) FROM workflow_instances w
        JOIN tasks t ON t.task_id = w.task_id
        WHERE w.status='running' AND t.status='completed'
    """).fetchone()[0]
    conn.close()
    assert stale == 0, f"仍有 {stale} 个 stale workflow 未被清理"

if '--self-check' in sys.argv:
    _self_check()
    print('✅ self-check: 0 stale workflows remaining')

