#!/usr/bin/env python3
"""task_utils.py — 独立任务操作函数（从 workflow_client.py 提取）"""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

# WorkflowClient imported lazily inside functions

from paths import BUS_CLIENT

CCS_CLI = Path(__file__).resolve().parent.parent / "ccs.py"
def check(role: str) -> str:
    from workflow.client import WorkflowClient
    client = WorkflowClient(role)
    task = client.check_task()
    client.close()
    if not task:
        return "无待完成任务"
    return (f"任务: {task['task_id']}\n"
            f"workflow_id: {task['instance_id']}\n"
            f"创建时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(task['created_at']))}")


def complete_task(role: str, wf_id: str, summary: str, files: list = None) -> str:
    from workflow.client import WorkflowClient
    import subprocess
    client = WorkflowClient(role)
    client.complete(wf_id, summary, files)
    client.notify("workflow", f"{role} 完成任务: {summary}",
                  evidence=f"文件: {', '.join(files) if files else '无'}")

    # 通知安排者（task 完成语义的一部分，非工作流编排）
    wf = client.get(wf_id)
    if wf and wf.get("task_id"):
        task = client.get_task(wf["task_id"])
        if task and task.get("assigner") and task["assigner"] != role:
            subprocess.run(
                ["python3", str(CCS_CLI), "send", task["assigner"],
                 f"[{role}] 任务完成: {summary} — task_id={wf['task_id']}"],
                capture_output=True, timeout=15,
            )
    client.close()
    return "任务已标记完成"


def fail_task(role: str, wf_id: str, reason: str) -> str:
    from workflow.client import WorkflowClient
    client = WorkflowClient(role)
    client.fail(wf_id, reason)
    client.notify("blocker", f"{role} 任务失败: {reason}", evidence=reason)
    client.close()
    return "任务已标记失败"


def logs(role: str, wf_id: str = None, task_id: str = None) -> str:
    from workflow.client import WorkflowClient
    client = WorkflowClient(role)
    entries = client.get_logs(wf_id, task_id)
    client.close()
    if not entries:
        return "无日志"
    lines = []
    for e in entries:
        ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts']))
        lines.append(f"[{ts}] {e['action']} by {e['actor']}: {e['detail']}")
    return "\n".join(lines)


STEP_TYPES = {"single", "handoff", "review"}


def render_step(step: dict) -> str:
    """渲染步骤描述。handoff 步骤只输出摘要，不输出 CLI 命令。"""
    t = step.get("type", "single")
    if t == "handoff":
        target = step.get("target_role", "?")
        timeout = step.get("confirm_timeout", 300)
        return (f"## {step.get('title', '')}\n"
                f"已将任务交由 {target} 处理\n"
                f"等待接单确认中...（超时 {timeout}s）\n"
                f"确认完成后会通知结果。")
    return step.get("prompt_template", step.get("title", ""))


def _next_step_id(step: dict) -> str:
    """获取下个步骤 ID（基于当前步骤 ID 递增语义，简单实现）。"""
    sid = step.get("id", "s1")
    import re
    m = re.search(r'(\d+)$', sid)
    if m:
        return f"s{int(m.group(1)) + 1}"
    return sid


def execute_handoff(wf, wf_id: str, step: dict) -> dict:
    from workflow.client import WorkflowClient
    """执行 handoff 步骤（分配 + 等待确认 + 上下文迁移 + 降级）。

    流程：
      1. 获取/创建 task
      2. confirm_delivery（双信号等待）
      3. 超时 → wake → 重试
      4. 二次超时 → 升级给人
      5. 确认成功 → 更新 assignee

    参数：
      wf: WorkflowClient 实例（提供 _conn 和 role）
      wf_id: workflow 实例 ID
      step: 步骤定义字典
    """
    target = step["target_role"]
    timeout = step.get("confirm_timeout", 300)

    # 从 workflow 实例获取 task_id
    wf_inst = wf.get(wf_id)
    task_id = wf_inst.get("task_id") if wf_inst else None

    # 优先已有 task_id，没有则创建
    if not task_id:
        task_id = wf.create_task(step.get("title", ""), assignee=target)
        # 关联到当前 workflow 实例
        wf._conn.execute(
            "UPDATE workflow_instances SET task_id=? WHERE instance_id=?",
            (task_id, wf_id)
        )
        wf._conn.commit()

    # 第 1 次等确认
    from routing.partner import PartnerClient
    result = PartnerClient(wf.role).confirm_delivery(task_id, target, timeout)

    # 超时 → wake → 第 2 次等确认
    if not result.get("confirmed") and step.get("on_timeout") == "wake_and_continue":
        PartnerClient(wf.role).wake(target, context=step.get("title", ""))
        result = PartnerClient(wf.role).confirm_delivery(task_id, target, timeout)

        # 二次超时 → 升级给人
        if not result.get("confirmed"):
            wf.notify("architecture",
                f"升级: handoff {step.get('title', '')} → {target} "
                f"二次超时 {timeout}s, task={task_id}",
                evidence="需要人工介入")

    # 上下文迁移：更新 workflow_instances.assignee
    if result.get("confirmed"):
        next_id = _next_step_id(step)
        wf._conn.execute(
            "UPDATE workflow_instances SET assignee=?, current_step_id=? "
            "WHERE instance_id=?",
            (target, next_id, wf_id)
        )
        wf._conn.commit()

    return result


# ── CLI 便捷函数 ──────────────────────────────────────────────────


# ── 独立操作函数（从 task_utils 导入）──

