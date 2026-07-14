#!/usr/bin/env python3
"""
lifecycle_manager.py — 生命周期管理器

管理工作流状态流转：pending→running→step_done_ready→completed。
5 种步骤类型（handoff/review/single/gate/notify）各有不同完成策略。
并发安全：使用 threading.RLock + SQLite WAL 模式 + BEGIN IMMEDIATE。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH

class LifecycleManager:
    """生命周期管理器：状态机 + 步骤执行 + 推进逻辑。"""

    WF_STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
    STEP_STATUSES = {"pending", "running", "step_done_ready", "completed", "failed"}
    STEP_TYPES = {"handoff", "review", "single", "gate", "notify"}

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    # ── 状态查询 ─────────────────────────────────

    def get_wf(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_step(self, wf_id: str, step_id: str) -> Optional[dict]:
        wf = self.get_wf(wf_id)
        if not wf:
            return None
        template = self._get_template(wf.get("template_id"))
        if not template:
            return None
        for step in template.get("steps", []):
            if step.get("step_id") == step_id:
                return step
        return None

    def _get_template(self, template_id: Optional[str]) -> Optional[dict]:
        if not template_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM workflow_templates WHERE template_id=?", (template_id,)
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

    def _log(self, wf_id: str, task_id: str = None,
             action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
            "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (wf_id, task_id, action, self.role, detail, time.time())
        )
        self._conn.commit()

    # ── 核心操作（并发安全） ─────────────────────

    def start_wf(self, wf_id: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE workflow_instances SET status='running' "
                    "WHERE instance_id=? AND status='pending'", (wf_id,)
                )
                self._conn.commit()
                if cur.rowcount == 0:
                    return False
                wf = self.get_wf(wf_id)
                task_id = wf.get("task_id") if wf else None
                self._log(wf_id, task_id, "wf_started",
                          detail=f"workflow started by {self.role}")
                return True
            except Exception:
                self._conn.rollback()
                raise

    def complete_step(self, wf_id: str, step_id: str) -> str:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                wf = self._get_wf_unsafe(wf_id)
                if not wf:
                    raise ValueError(f"workflow 不存在: {wf_id}")
                if wf.get("current_step_id") != step_id:
                    raise ValueError(
                        f"步骤不匹配: 当前步骤={wf['current_step_id']}, 传入={step_id}")

                results = self._parse_results(wf)
                cur_status = results.get(step_id, {}).get("status")
                if cur_status in ("step_done_ready", "completed"):
                    self._conn.commit()
                    return "already_completed"

                step = self.get_step(wf_id, step_id)
                if not step:
                    raise ValueError(f"步骤定义不存在: {step_id}")
                step_type = step.get("type", "single")
                task_id = wf.get("task_id")

                handler = {
                    "single": self._complete_single_unsafe,
                    "handoff": self._complete_handoff_unsafe,
                    "review": self._complete_review_unsafe,
                    "gate": self._complete_gate_unsafe,
                    "notify": self._complete_notify_unsafe,
                }
                fn = handler.get(step_type)
                if not fn:
                    raise ValueError(f"未知步骤类型: {step_type}")
                result = fn(wf_id, step_id, step, task_id, results)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def confirm_step(self, wf_id: str, step_id: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                wf = self._get_wf_unsafe(wf_id)
                if not wf:
                    raise ValueError(f"workflow 不存在: {wf_id}")

                step = self.get_step(wf_id, step_id)
                if step:
                    target_role = step.get("target_role", "")
                    if target_role and self.role != target_role:
                        raise PermissionError(
                            f"not the assignee: {self.role} != {target_role}")

                results = self._parse_results(wf)
                current = results.get(step_id, {})
                if current.get("status") != "step_done_ready":
                    raise ValueError(
                        f"步骤 {step_id} 状态为 {current.get('status','?')}，非 step_done_ready")

                current["status"] = "completed"
                current["confirmed_at"] = time.time()
                current["confirmed_by"] = self.role
                results[step_id] = current
                self._conn.execute(
                    "UPDATE workflow_instances SET step_results=? WHERE instance_id=?",
                    (json.dumps(results, ensure_ascii=False), wf_id)
                )
                task_id = wf.get("task_id")
                self._advance_unsafe(wf_id, task_id)
                self._log_unsafe(wf_id, task_id, "step_confirmed",
                                 detail=f"{step_id} confirmed by {self.role}")
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def fail_step(self, wf_id: str, step_id: str,
                  reason: str = "", allow_retry: bool = False) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                wf = self._get_wf_unsafe(wf_id)
                if not wf:
                    raise ValueError(f"workflow 不存在: {wf_id}")
                if wf.get("current_step_id") != step_id:
                    raise ValueError(
                        f"步骤不匹配: 当前={wf['current_step_id']}, 传入={step_id}")

                results = self._parse_results(wf)
                task_id = wf.get("task_id")

                if allow_retry:
                    results[step_id] = {"status": "failed", "failed_at": time.time(),
                                        "failed_by": self.role, "reason": reason}
                    self._conn.execute(
                        "UPDATE workflow_instances SET step_results=? WHERE instance_id=?",
                        (json.dumps(results, ensure_ascii=False), wf_id)
                    )
                    self._log_unsafe(wf_id, task_id, "step_failed", detail=f"{step_id}: {reason}")
                    self._conn.commit()
                    return True
                else:
                    self._conn.execute(
                        "UPDATE workflow_instances SET status='failed', completed_at=? "
                        "WHERE instance_id=?", (time.time(), wf_id)
                    )
                    self._sync_task_unsafe(task_id)
                    self._log_unsafe(wf_id, task_id, "wf_failed", detail=f"step {step_id}: {reason}")
                    self._conn.commit()
                    return False
            except Exception:
                self._conn.rollback()
                raise

    def close_wf(self, wf_id: str):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE workflow_instances SET status='completed', completed_at=? "
                    "WHERE instance_id=?", (time.time(), wf_id)
                )
                wf = self.get_wf(wf_id)
                task_id = wf.get("task_id") if wf else None
                self._sync_task_unsafe(task_id)
                self._log_unsafe(wf_id, task_id, "wf_closed", detail="workflow completed")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── 内部 unsafe 方法（在已开启的事务中调用） ──

    def _get_wf_unsafe(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _parse_results(wf: dict) -> dict:
        if wf.get("step_results"):
            try:
                return json.loads(wf["step_results"])
            except (json.JSONDecodeError, TypeError):
                pass
        return {}

    def _log_unsafe(self, wf_id: str, task_id: str = None,
                    action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
            "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (wf_id, task_id, action, self.role, detail, time.time())
        )

    def _set_step_result_unsafe(self, wf_id: str, step_id: str,
                                 status: str, results: dict):
        results[step_id] = {"status": status, "completed_at": time.time(),
                            "completed_by": self.role}
        self._conn.execute(
            "UPDATE workflow_instances SET step_results=? WHERE instance_id=?",
            (json.dumps(results, ensure_ascii=False), wf_id)
        )

    # ── 步骤类型处理 ────────────────────────────

    def _complete_single_unsafe(self, wf_id, step_id, step, task_id, results):
        self._set_step_result_unsafe(wf_id, step_id, "completed", results)
        self._advance_unsafe(wf_id, task_id)
        self._log_unsafe(wf_id, task_id, "step_completed",
                         detail=f"single auto-advance: {step_id}")
        return "completed_and_advanced"

    def _complete_handoff_unsafe(self, wf_id, step_id, step, task_id, results):
        self._set_step_result_unsafe(wf_id, step_id, "step_done_ready", results)
        self._log_unsafe(wf_id, task_id, "step_done_ready",
                         detail=f"handoff waiting confirm: {step_id}")
        return "step_done_ready"

    def _complete_review_unsafe(self, wf_id, step_id, step, task_id, results):
        self._set_step_result_unsafe(wf_id, step_id, "step_done_ready", results)
        self._log_unsafe(wf_id, task_id, "step_done_ready",
                         detail=f"review waiting approval: {step_id}")
        return "step_done_ready"

    def _complete_gate_unsafe(self, wf_id, step_id, step, task_id, results):
        check = step.get("completion_check", {})
        passed, msg = self._check_gate_condition(check)
        if passed:
            self._set_step_result_unsafe(wf_id, step_id, "completed", results)
            self._advance_unsafe(wf_id, task_id)
            self._log_unsafe(wf_id, task_id, "gate_passed",
                             detail=f"gate passed: {step_id}: {msg}")
            return "completed_and_advanced"
        else:
            self._set_step_result_unsafe(wf_id, step_id, "blocked", results)
            self._log_unsafe(wf_id, task_id, "gate_blocked",
                             detail=f"gate blocked: {step_id}: {msg}")
            return "gate_blocked"

    def _complete_notify_unsafe(self, wf_id, step_id, step, task_id, results):
        target = step.get("target_role", "")
        self._set_step_result_unsafe(wf_id, step_id, "completed", results)
        self._advance_unsafe(wf_id, task_id)
        self._log_unsafe(wf_id, task_id, "notify_sent",
                         detail=f"notify sent to {target}: {step_id}")
        return "completed_and_advanced"

    def _check_gate_condition(self, check: dict) -> tuple:
        if not check:
            return (True, "no conditions")
        output_exists = check.get("output_exists", [])
        if output_exists:
            missing = [p for p in output_exists if not Path(p).exists()]
            if missing:
                return (False, f"output not found: {', '.join(missing)}")
        return (True, "conditions satisfied")

    def _advance_unsafe(self, wf_id: str, task_id: Optional[str]) -> bool:
        wf = self._get_wf_unsafe(wf_id)
        if not wf:
            return False
        steps = self._get_all_steps_unsafe(wf.get("template_id"))
        current = wf.get("current_step_id", "")
        idx = -1
        for i, s in enumerate(steps):
            if s.get("step_id") == current:
                idx = i
                break
        if idx == -1 or idx + 1 >= len(steps):
            self._conn.execute(
                "UPDATE workflow_instances SET status='completed', completed_at=? "
                "WHERE instance_id=?", (time.time(), wf_id)
            )
            self._sync_task_unsafe(task_id)
            return False
        next_step = steps[idx + 1]
        self._conn.execute(
            "UPDATE workflow_instances SET current_step_id=? WHERE instance_id=?",
            (next_step["step_id"], wf_id)
        )
        return True

    def _sync_task_unsafe(self, task_id: Optional[str]):
        if not task_id:
            return
        rows = self._conn.execute(
            "SELECT status FROM workflow_instances WHERE task_id=?", (task_id,)
        ).fetchall()
        statuses = [dict(r)["status"] for r in rows]
        if not statuses:
            return
        if all(s == "completed" for s in statuses):
            ts = "completed"
        elif any(s == "failed" for s in statuses):
            ts = "failed"
        elif any(s in ("running", "pending", "step_done_ready") for s in statuses):
            ts = "in_progress"
        else:
            ts = "completed"
        self._conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (ts, time.time(), task_id)
        )

    def _get_all_steps_unsafe(self, template_id: Optional[str]) -> list:
        t = self._get_template(template_id)
        return t.get("steps", []) if t else []

    def check_gate_timeouts(self) -> list[dict]:
        timeouts = []
        templates = self._conn.execute("SELECT * FROM workflow_templates").fetchall()
        for tpl_row in templates:
            tpl = dict(tpl_row)
            if not tpl.get("steps_json"):
                continue
            try:
                steps = json.loads(tpl["steps_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            for step in steps:
                if step.get("type") != "gate":
                    continue
                timeout_hours = step.get("estimated_hours", 24)
                escalation_role = step.get("target_role", "coordinator")
                rows = self._conn.execute(
                    "SELECT * FROM workflow_instances "
                    "WHERE template_id=? AND status='running' AND current_step_id=?",
                    (tpl["template_id"], step["step_id"])
                ).fetchall()
                for row in rows:
                    inst = dict(row)
                    elapsed = (time.time() - inst["created_at"]) / 3600
                    if elapsed > timeout_hours:
                        timeouts.append({
                            "wf_id": inst["instance_id"],
                            "step_id": step["step_id"],
                            "elapsed_hours": round(elapsed, 1),
                            "timeout_hours": timeout_hours,
                        })
        return timeouts

    def close(self):
        self._conn.close()
