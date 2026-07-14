#!/usr/bin/env python3
"""
notification_engine.py — 通知引擎

步骤完成后自动通知下游角色（ccs send + bus write），
含标准化通知模板和自定义模板支持。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH
from paths import BUS_CLIENT
CCS_CLI = Path(__file__).resolve().parent / "ccs.py"


class NotificationEngine:
    """通知引擎：步骤完成通知 + 消息模板。"""

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row

    # ── 核心通知 ─────────────────────────────────

    def notify_handoff_complete(self, wf_id: str, step_id: str,
                                 target_role: str):
        """handoff 完成：通知下游角色 + 写 bus。"""
        step = self._get_step(wf_id, step_id)
        template = self._resolve_template(wf_id, step, "handoff_complete")
        msg = self._format_msg(template, wf_id, step_id, step, target_role)
        self._ccs_send(target_role, msg)
        self._bus_write("workflow", f"handoff_complete: {step_id}",
                        evidence=f"wf={wf_id}, step={step_id}, target={target_role}")

    def notify_review_complete(self, wf_id: str, step_id: str,
                                reviewer_role: str):
        """review 完成：通知审批者 + 写 bus。"""
        step = self._get_step(wf_id, step_id)
        template = self._resolve_template(wf_id, step, "review_complete")
        msg = self._format_msg(template, wf_id, step_id, step, reviewer_role)
        self._ccs_send(reviewer_role, msg)
        self._bus_write("workflow", f"review_ready: {step_id}",
                        evidence=f"wf={wf_id}, step={step_id}, reviewer={reviewer_role}")

    def notify_gate_timeout(self, wf_id: str, step_id: str,
                             escalation_role: str, elapsed_hours: float,
                             timeout_hours: float):
        """gate 超时：通知升级角色。"""
        msg = (f"【门禁超时】工作流 {wf_id} 步骤 {step_id} "
               f"已超时 {elapsed_hours:.1f}h/{timeout_hours}h")
        self._ccs_send(escalation_role, msg)
        self._bus_write("blocker", f"gate_timeout: {wf_id}/{step_id}",
                        evidence=f"elapsed={elapsed_hours:.1f}h, timeout={timeout_hours}h")

    def notify_wf_closed(self, wf_id: str, task_id: str,
                          initiator_role: Optional[str]):
        """工作流完成：通知发起者。"""
        if not initiator_role:
            return
        msg = (f"【工作流完成】{wf_id} — task={task_id}\n"
               f"所有步骤已完成，task 已自动完成。")
        self._ccs_send(initiator_role, msg)
        self._bus_write("workflow", f"wf_closed: {wf_id}",
                        evidence=f"task={task_id}, initiator={initiator_role}")

    # ── 步骤通知（完整信息版） ──────────────────

    def notify_step_complete(self, wf_id: str, step_id: str,
                              step_type: str, target_role: str,
                              task_title: str = "",
                              output_paths: list = None):
        """发送完整的步骤完成通知（含操作提示）。"""
        step = self._get_step(wf_id, step_id)
        if not step:
            step = {"title": step_id, "step_id": step_id}

        step_title = step.get("title", step_id)

        msg = (
            f"【步骤完成通知】工作流: {wf_id}\n"
            f"步骤: {step_id} - {step_title}\n"
            f"类型: {step_type}\n"
            f"执行角色: {self.role}\n"
            f"任务: {task_title or '—'}\n"
            f"产出物: {', '.join(output_paths) if output_paths else '—'}\n\n"
            f"请验收:\n"
            f"→ confirm_step('{wf_id}', '{step_id}') 确认\n"
            f"→ fail_step('{wf_id}', '{step_id}', '原因') 退回"
        )

        if target_role:
            self._ccs_send(target_role, msg)

        # 尝试使用模板自定义模板
        template = self._get_workflow_template(wf_id)
        if template:
            notify_tpl = template.get("notify_template", {})
            if notify_tpl:
                custom_msg = self._apply_custom_template(
                    notify_tpl, wf_id, step_id, step,
                    target_role, task_title, output_paths or [])
                if target_role:
                    self._ccs_send(target_role, custom_msg)

    # ── 批量通知 ─────────────────────────────────

    def notify_step_events(self, wf_id: str, step_id: str,
                            events: list[dict]):
        """批量发送步骤事件通知。"""
        for ev in events:
            etype = ev.get("type", "info")
            target = ev.get("target", "")
            title = ev.get("title", "")
            detail = ev.get("detail", "")
            msg = f"【{etype}】{wf_id}/{step_id}: {title} — {detail}"
            if target:
                self._ccs_send(target, msg)

    # ── 内部方法 ────────────────────────────────

    def _get_step(self, wf_id: str, step_id: str) -> Optional[dict]:
        template = self._get_workflow_template(wf_id)
        if not template:
            return None
        steps = template.get("steps", [])
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except (json.JSONDecodeError, TypeError):
                steps = []
        for s in steps:
            if s.get("step_id") == step_id:
                return s
        return None

    def _get_workflow_template(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT t.* FROM workflow_instances wi "
            "JOIN workflow_templates t ON wi.template_id = t.template_id "
            "WHERE wi.instance_id=?", (wf_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        steps = d.get("steps_json")
        if isinstance(steps, str):
            try:
                d["steps"] = json.loads(steps)
            except (json.JSONDecodeError, TypeError):
                d["steps"] = []
        return d

    def _resolve_template(self, wf_id: str, step: Optional[dict],
                           event: str) -> str:
        """解析通知模板：优先自定义，其次默认。"""
        if step and step.get("notify_template"):
            tpl = step["notify_template"]
            if isinstance(tpl, dict):
                return tpl.get(event, self._default_template(event))
        template = self._get_workflow_template(wf_id)
        if template:
            nt = template.get("notify_template", {})
            if isinstance(nt, dict) and nt.get(event):
                return nt[event]
        return self._default_template(event)

    @staticmethod
    def _default_template(event: str) -> str:
        templates = {
            "handoff_complete": (
                "【步骤完成通知】工作流: {wf_id} | 步骤: {step_id} - {step_title}\n"
                "执行角色: {role} | 任务: {task_title}\n"
                "请验收\n"
                "→ confirm_step({wf_id}, {step_id}, YOUR_ROLE) 确认\n"
                "→ fail_step({wf_id}, {step_id}, 原因) 退回"
            ),
            "review_complete": (
                "【审查待办】工作流: {wf_id} | 步骤: {step_id} - {step_title}\n"
                "执行角色: {role}\n"
                "请评审\n"
                "→ confirm_step({wf_id}, {step_id}, YOUR_ROLE) 通过\n"
                "→ fail_step({wf_id}, {step_id}, 原因) 退回"
            ),
        }
        return templates.get(event, f"【通知】{event}")

    def _format_msg(self, template: str, wf_id: str, step_id: str,
                    step: Optional[dict], target_role: str) -> str:
        step_title = step.get("title", step_id) if step else step_id
        task_title = self._get_task_title(wf_id)
        return template.format(
            wf_id=wf_id, step_id=step_id,
            step_title=step_title, role=self.role,
            target_role=target_role, task_title=task_title,
        )

    def _get_task_title(self, wf_id: str) -> str:
        row = self._conn.execute(
            "SELECT t.title FROM workflow_instances wi "
            "JOIN tasks t ON wi.task_id = t.task_id "
            "WHERE wi.instance_id=?", (wf_id,)
        ).fetchone()
        return row["title"] if row else ""

    def _ccs_send(self, target: str, message: str):
        import subprocess
        subprocess.run(
            ["python3", str(CCS_CLI), "send", target, message],
            capture_output=True, timeout=15,
        )

    def _bus_write(self, category: str, title: str, evidence: str = ""):
        import subprocess
        cmd = ["python3", str(BUS_CLIENT), "write", category,
               f"[{self.role}] {title}", "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        subprocess.run(cmd, capture_output=True, timeout=15)

    @staticmethod
    def _apply_custom_template(template: dict, wf_id: str, step_id: str,
                                step: dict, target_role: str,
                                task_title: str, output_paths: list) -> str:
        """应用自定义模板。"""
        # 简单实现：拼接 key:value
        parts = []
        for key, val in template.items():
            resolved = str(val).format(
                wf_id=wf_id, step_id=step_id,
                step_title=step.get("title", step_id),
                target_role=target_role, task_title=task_title,
                outputs=", ".join(output_paths),
            )
            parts.append(f"{key}: {resolved}")
        return "\n".join(parts)

    def close(self):
        self._conn.close()
