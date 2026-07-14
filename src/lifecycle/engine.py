#!/usr/bin/env python3
"""
step_engine.py — 5 种步骤类型引擎

为每种步骤类型定义独立的 complete/confirm 策略。
handoff 需对方确认，review 需审批者确认，single 自完成，
gate 条件自检+timeout+escalation，notify 即完成。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH
from paths import BUS_CLIENT
CCS_CLI = Path(__file__).resolve().parent.parent / "ccs.py"

VALID_TYPES = {"handoff", "review", "single", "gate", "notify"}


class StepEngine:
    """步骤类型引擎：封装每种类型的 complete/confirm 策略。"""

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    def complete_step(self, wf_id: str, step_id: str) -> dict:
        """执行步骤 complete，按类型返回结果。"""
        wf = self._get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")
        if wf.get("current_step_id") != step_id:
            raise ValueError(
                f"步骤不匹配: 当前={wf['current_step_id']}, 传入={step_id}")

        step = self._get_step(wf, step_id)
        if not step:
            raise ValueError(f"步骤定义不存在: {step_id}")

        step_type = step.get("type", "single")
        handler = {
            "handoff": self._complete_handoff,
            "review": self._complete_review,
            "single": self._complete_single,
            "gate": self._complete_gate,
            "notify": self._complete_notify,
        }
        handler_fn = handler.get(step_type)
        if not handler_fn:
            raise ValueError(f"未知步骤类型: {step_type}")

        return handler_fn(wf, step)

    def confirm_step(self, wf_id: str, step_id: str) -> dict:
        """确认步骤完成（handoff/review 需此操作）。"""
        wf = self._get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")

        cur = self._get_step_result(wf, step_id)
        if cur.get("status") != "step_done_ready":
            raise ValueError(
                f"步骤 {step_id} 状态为 {cur.get('status','?')}，"
                f"非 step_done_ready")

        cur["status"] = "completed"
        cur["confirmed_at"] = time.time()
        cur["confirmed_by"] = self.role
        self._put_step_result(wf_id, step_id, cur)

        task_id = wf.get("task_id")
        self._advance(wf_id, task_id)

        self._log(wf_id, task_id, "step_confirmed",
                  detail=f"{step_id} confirmed by {self.role}")

        return {
            "status": "completed",
            "next_action": "advanced" if self._has_next_step(wf) else "wf_closed",
        }

    def fail_step(self, wf_id: str, step_id: str,
                  reason: str = "", allow_retry: bool = False) -> dict:
        """标记步骤失败。"""
        wf = self._get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")
        if wf.get("current_step_id") != step_id:
            raise ValueError(
                f"步骤不匹配: 当前={wf['current_step_id']}, 传入={step_id}")

        task_id = wf.get("task_id")

        if allow_retry:
            cur = self._get_step_result(wf, step_id)
            cur["status"] = "failed"
            cur["failed_at"] = time.time()
            cur["failed_by"] = self.role
            cur["reason"] = reason
            self._put_step_result(wf_id, step_id, cur)
            self._log(wf_id, task_id, "step_failed",
                      detail=f"{step_id}: {reason}")
            return {"status": "failed", "allow_retry": True}
        else:
            self._set_wf_status(wf_id, "failed")
            self._sync_task_status(task_id)
            self._log(wf_id, task_id, "wf_failed",
                      detail=f"step {step_id}: {reason}")
            return {"status": "wf_failed", "allow_retry": False}

    def check_gate_timeouts(self) -> list[dict]:
        """扫描所有 running 工作流中被 gate 阻塞的步骤。

        超时则通知 escalation_role，保持 workflow 状态不变。
        """
        triggered = []
        try:
            rows = self._conn.execute(
                "SELECT * FROM workflow_instances WHERE status='running'"
            ).fetchall()
        except Exception:
            return triggered  # 空 DB / 无表时静默返回
        for row in rows:
            inst = dict(row)
            tid = inst.get("template_id")
            sid = inst.get("current_step_id")
            if not tid or not sid:
                continue
            step = self._get_step_from_template(tid, sid)
            if not step or step.get("type") != "gate":
                continue
            timeout_hours = step.get("estimated_hours", 24)
            escalation = step.get("target_role", "coordinator")
            elapsed = (time.time() - inst["created_at"]) / 3600
            if elapsed > timeout_hours:
                self._notify_ccs(escalation,
                    f"【门禁超时】工作流 {inst['instance_id']} "
                    f"步骤 {sid} 已超时 {elapsed:.1f}h/{timeout_hours}h")
                self._notify_bus("blocker",
                    f"门禁超时: {inst['instance_id']}/{sid}",
                    evidence=f"elapsed={elapsed:.1f}h, timeout={timeout_hours}h")
                self._log(inst["instance_id"], inst.get("task_id"),
                         "gate_timeout",
                         detail=f"step {sid}: {elapsed:.1f}h")
                triggered.append({
                    "wf_id": inst["instance_id"],
                    "step_id": sid,
                    "elapsed_hours": round(elapsed, 1),
                    "timeout_hours": timeout_hours,
                })
        return triggered

    # ── 类型处理器 ──────────────────────────────

    def _complete_handoff(self, wf: dict, step: dict) -> dict:
        step_id = step["step_id"]
        target = step.get("target_role", "")
        task_id = wf.get("task_id")
        self._set_step_done_ready(wf["instance_id"], step_id, task_id)
        self._notify_ccs(target,
            f"【待办】工作流 {wf['instance_id']} 步骤 {step_id} "
            f"- {step.get('title','')} 已移交")
        self._log(wf["instance_id"], task_id, "step_done_ready",
                  detail=f"handoff → {target}: {step_id}")
        return {"status": "step_done_ready", "next_action": "wait_confirm",
                "notified_roles": [target]}

    def _complete_review(self, wf: dict, step: dict) -> dict:
        step_id = step["step_id"]
        task_id = wf.get("task_id")
        self._set_step_done_ready(wf["instance_id"], step_id, task_id)
        reviewer = step.get("target_role", "")
        if reviewer:
            self._notify_ccs(reviewer,
                f"【审查待办】工作流 {wf['instance_id']} 步骤 {step_id} "
                f"- {step.get('title','')} 待审批")
        self._log(wf["instance_id"], task_id, "step_done_ready",
                  detail=f"review → {reviewer}: {step_id}")
        return {"status": "step_done_ready", "next_action": "wait_confirm",
                "notified_roles": [reviewer] if reviewer else []}

    def _complete_single(self, wf: dict, step: dict) -> dict:
        step_id = step["step_id"]
        task_id = wf.get("task_id")
        self._set_step_completed(wf["instance_id"], step_id, task_id)
        has_next = self._advance(wf["instance_id"], task_id)
        self._log(wf["instance_id"], task_id, "step_completed",
                  detail=f"single auto-advance: {step_id}")
        return {
            "status": "completed",
            "next_action": "auto_advance" if has_next else "wf_closed",
        }

    def _complete_gate(self, wf: dict, step: dict) -> dict:
        step_id = step["step_id"]
        task_id = wf.get("task_id")
        check = step.get("completion_check", {})
        passed, msg = self._check_condition(check)

        if passed:
            self._set_step_completed(wf["instance_id"], step_id, task_id)
            has_next = self._advance(wf["instance_id"], task_id)
            self._log(wf["instance_id"], task_id, "gate_passed",
                      detail=f"{step_id}: {msg}")
            return {
                "status": "completed",
                "next_action": "auto_advance" if has_next else "wf_closed",
            }
        else:
            self._put_step_result(
                wf["instance_id"], step_id,
                {"status": "blocked", "blocked_at": time.time(),
                 "reason": msg})
            self._log(wf["instance_id"], task_id, "gate_blocked",
                      detail=f"{step_id}: {msg}")
            return {"status": "blocked", "next_action": "wait_gate",
                    "reason": msg}

    def _complete_notify(self, wf: dict, step: dict) -> dict:
        step_id = step["step_id"]
        task_id = wf.get("task_id")
        target = step.get("target_role", "")
        msg = (f"【通知】工作流 {wf['instance_id']} 步骤 {step_id} "
               f"- {step.get('title','')}")
        if target:
            self._notify_ccs(target, msg)
        self._notify_bus("workflow", f"步骤完成通知: {step_id}",
                         evidence=f"wf={wf['instance_id']}, step={step_id}")
        self._set_step_completed(wf["instance_id"], step_id, task_id)
        has_next = self._advance(wf["instance_id"], task_id)
        self._log(wf["instance_id"], task_id, "notify_sent",
                  detail=f"{step_id} → {target}")
        return {
            "status": "completed",
            "next_action": "auto_advance" if has_next else "wf_closed",
        }

    # ── 内部辅助 ──────────────────────────────

    def _get_wf(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?",
            (wf_id,)
        ).fetchone()
        return dict(row) if row else None

    def _get_step(self, wf: dict, step_id: str) -> Optional[dict]:
        template = self._get_template(wf.get("template_id"))
        if not template:
            return None
        for s in template.get("steps", []):
            if s.get("step_id") == step_id:
                return s
        return None

    def _get_template(self, template_id: Optional[str]) -> Optional[dict]:
        if not template_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM workflow_templates WHERE template_id=?",
            (template_id,)
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

    def _get_step_from_template(self, template_id: str,
                                  step_id: str) -> Optional[dict]:
        t = self._get_template(template_id)
        if not t:
            return None
        for s in t.get("steps", []):
            if s.get("step_id") == step_id:
                return s
        return None

    def _get_step_result(self, wf: dict, step_id: str) -> dict:
        sr = wf.get("step_results", "{}")
        try:
            results = json.loads(sr) if isinstance(sr, str) else sr
        except (json.JSONDecodeError, TypeError):
            results = {}
        return results.get(step_id, {})

    def _put_step_result(self, wf_id: str, step_id: str, data: dict):
        wf = self._get_wf(wf_id)
        if not wf:
            return
        sr = wf.get("step_results", "{}")
        try:
            results = json.loads(sr) if isinstance(sr, str) else sr
        except (json.JSONDecodeError, TypeError):
            results = {}
        results[step_id] = data
        self._conn.execute(
            "UPDATE workflow_instances SET step_results=? WHERE instance_id=?",
            (json.dumps(results, ensure_ascii=False), wf_id)
        )
        self._conn.commit()

    def _set_step_done_ready(self, wf_id: str, step_id: str,
                              task_id: Optional[str]):
        data = {
            "status": "step_done_ready",
            "completed_at": time.time(),
            "completed_by": self.role,
        }
        self._put_step_result(wf_id, step_id, data)

    def _set_step_completed(self, wf_id: str, step_id: str,
                             task_id: Optional[str]):
        data = {
            "status": "completed",
            "completed_at": time.time(),
            "completed_by": self.role,
        }
        self._put_step_result(wf_id, step_id, data)

    def _set_wf_status(self, wf_id: str, status: str):
        self._conn.execute(
            "UPDATE workflow_instances SET status=? WHERE instance_id=?",
            (status, wf_id)
        )
        self._conn.commit()

    def _advance(self, wf_id: str, task_id: Optional[str]) -> bool:
        """推进到下一步或关闭。返回 True 如果有下一步。"""
        wf = self._get_wf(wf_id)
        if not wf:
            return False
        steps = self._get_all_steps(wf.get("template_id"))
        current = wf.get("current_step_id", "")
        idx = -1
        for i, s in enumerate(steps):
            if s.get("step_id") == current:
                idx = i
                break
        if idx == -1 or idx + 1 >= len(steps):
            self._close_wf(wf_id, task_id)
            return False
        next_step = steps[idx + 1]
        self._conn.execute(
            "UPDATE workflow_instances SET current_step_id=? WHERE instance_id=?",
            (next_step["step_id"], wf_id)
        )
        self._conn.commit()
        self._log(wf_id, task_id, "advanced",
                  detail=f"{current} → {next_step['step_id']}")
        return True

    def _close_wf(self, wf_id: str, task_id: Optional[str]):
        self._set_wf_status(wf_id, "completed")
        self._conn.execute(
            "UPDATE workflow_instances SET completed_at=? WHERE instance_id=?",
            (time.time(), wf_id)
        )
        self._conn.commit()
        self._sync_task_status(task_id)
        self._log(wf_id, task_id, "wf_closed",
                  detail="all steps completed")

    def _has_next_step(self, wf: dict) -> bool:
        steps = self._get_all_steps(wf.get("template_id"))
        current = wf.get("current_step_id", "")
        for i, s in enumerate(steps):
            if s.get("step_id") == current:
                return i + 1 < len(steps)
        return False

    def _get_all_steps(self, template_id: Optional[str]) -> list:
        t = self._get_template(template_id)
        return t.get("steps", []) if t else []

    def _sync_task_status(self, task_id: Optional[str]):
        if not task_id:
            return
        rows = self._conn.execute(
            "SELECT status FROM workflow_instances WHERE task_id=?",
            (task_id,)
        ).fetchall()
        statuses = [dict(r)["status"] for r in rows]
        if not statuses:
            return
        if all(s == "completed" for s in statuses):
            ts = "completed"
        elif any(s == "failed" for s in statuses):
            ts = "failed"
        elif any(s in ("running", "pending", "step_done_ready")
                 for s in statuses):
            ts = "in_progress"
        else:
            ts = "completed"
        self._conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (ts, time.time(), task_id)
        )
        self._conn.commit()

    def _check_condition(self, check: dict) -> tuple:
        """检查门禁条件。返回 (passed, message)。"""
        if not check:
            return (True, "no conditions")
        output_exists = check.get("output_exists", [])
        if output_exists:
            missing = [p for p in output_exists if not Path(p).exists()]
            if missing:
                return (False, f"output not found: {', '.join(missing)}")
        return (True, "conditions satisfied")

    def _log(self, wf_id: str, task_id: str = None,
             action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
            "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (wf_id, task_id, action, self.role, detail, time.time())
        )
        self._conn.commit()

    def _notify_ccs(self, target: str, message: str):
        import subprocess
        subprocess.run(
            ["python3", str(CCS_CLI), "send", target, message],
            capture_output=True, timeout=15,
        )

    def _notify_bus(self, category: str, title: str, evidence: str = ""):
        import subprocess
        cmd = ["python3", str(BUS_CLIENT), "write", category,
               f"[{self.role}] {title}", "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        subprocess.run(cmd, capture_output=True, timeout=15)

    def close(self):
        self._conn.close()
