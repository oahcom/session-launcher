"""workflow/state.py — 一条工作流链的状态快照（只读）。

为什么独立成模块：client.py 已 615 行（规范红线 600），而"把 step_results 这种
半结构化列翻成人能读的状态"本身是一块完整逻辑，塞在客户端里只会让两边都变胖。

判据（2026-10-22 实测）：角色 48h 手写 14,909 次 sqlite3（占 Bash 32%），其中 1,207 次
只为读 workflow_instances.step_results 一列；列名还得角色自己记（6 个角色 1 小时 11 次
猜错列名空跑）。把这次读取收敛成一个函数，列名只在这里出现一次。
"""
import json
import time
from typing import Optional


def chain_state(conn, wf_id: str) -> Optional[dict]:
    """实例 + 每步状态。实例不存在返回 None（调用方据此 rc≠0）。

    失败即正常输入：step_results 可能是坏 JSON、顶层有非 dict 旧值
    （'summary'/'files' 简写，生产 599 个）——一律降级为"该步没有结构化状态"，
    不让一条脏数据把整条链的查询打崩。
    """
    row = conn.execute(
        "SELECT instance_id, task_id, template_id, status, current_step_id, "
        "step_results, created_at FROM workflow_instances WHERE instance_id=?",
        (wf_id,)).fetchone()
    if not row:
        return None
    inst = dict(row)
    raw = inst.pop("step_results", "") or "{}"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    now = time.time()
    steps = []
    for sid, val in parsed.items():
        # 下划线开头是元数据键（_close_reason 等），不是步骤；渲染成步骤会伪造一条
        # status=cancelled 的假步。
        if not isinstance(val, dict) or sid.startswith("_"):
            continue
        # queued 不是"队列深度"，是"派单被拒的 tick 次数"（471 次 × 14.5s ≈ 1.90h，
        # 与 queued_since 吻合）。计数与时长必须是两个字段，否则会被读成一个。
        since = val.get("queued_since") or 0
        steps.append({
            "step": sid,
            "status": val.get("status") or "",
            "completed_by": val.get("completed_by") or "",
            "requeued": val.get("queued") or 0,
            "since_h": round((now - since) / 3600, 2) if since else 0,
        })
    steps.sort(key=lambda s: s["step"])
    created = inst.get("created_at") or 0
    inst["age_h"] = round((now - created) / 3600, 2) if created else 0
    inst["steps"] = steps
    return inst