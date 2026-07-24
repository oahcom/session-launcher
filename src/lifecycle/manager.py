#!/usr/bin/env python3
"""LifecycleManager — 工作流生命周期管理器，集成 Gate 门禁 + 分配者链。"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH


class LifecycleManager:
    """工作流生命周期管理器。

    职责:
      - start_wf / complete_step / confirm_step / fail_step / close_wf
      - get_wf / rollback_step / _check_gate_condition
      - 分配者链强制 (assigner chain enforcement)
      - 集成 LifecycleGate step 级权限校验
    """

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    # ── 工作流控制 ──────────────────────────────

    def start_wf(self, wf_id: str) -> bool:
        wf = self.get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")
        if wf.get("status") not in ("pending", "created"):
            return False
        self._conn.execute(
            "UPDATE workflow_instances SET status='running' WHERE instance_id=?",
            (wf_id,)
        )
        self._conn.commit()
        self._log(wf_id, wf.get("task_id"), "started")
        return True

    def complete_step(self, wf_id: str, step_id: str) -> str:
        wf = self.get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")
        if wf.get("status") != "running":
            raise ValueError(f"workflow 未运行: {wf.get('status')}")
        if wf.get("current_step_id") != step_id:
            raise ValueError(
                f"步骤不匹配: 当前={wf.get('current_step_id')}, 传入={step_id}")

        cur = self._get_step_result(wf, step_id)
        if cur.get("status") == "completed":
            return "already_completed"

        # 设为 step_done_ready (handoff/review 需确认)
        cur["status"] = "step_done_ready"
        cur["completed_at"] = time.time()
        cur["completed_by"] = self.role
        self._put_step_result(wf_id, step_id, cur)
        self._log(wf_id, wf.get("task_id"), "step_completed",
                  detail=f"{step_id} → step_done_ready")
        return "step_done_ready"

    def confirm_step(self, wf_id: str, step_id: str, token: str = "") -> dict:
        wf = self.get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")

        current = self._get_step_result(wf, step_id)
        if current.get("status") != "step_done_ready":
            raise ValueError(
                f"步骤 {step_id} 状态为 {current.get('status','?')}，需要 step_done_ready")

        # WL-P2-01: LifecycleGate step 级权限校验
        from lifecycle.gate import LifecycleGate
        gate = LifecycleGate(str(self.db_path))
        if not gate.check_can_execute(self.role, wf.get("template_id", ""), step_id):
            raise PermissionError(
                f"LifecycleGate 拒绝: role={self.role} "
                f"无权限执行 template={wf.get('template_id')} step={step_id}")
        gate.reload()

        # 分配者链校验: 谁有权确认
        assigner = self._get_assigner_for_step(wf, step_id)
        # review 类型步骤: target_role(执行者) 也可以自确认
        step_def = self._get_step_def(wf.get("template_id"), step_id)
        step_type = (step_def or {}).get("type", "")
        if step_type == "review" and self.role == (step_def or {}).get("target_role", ""):
            pass  # reviewer 可自确认
        elif self.role != assigner:
            raise PermissionError(
                f"approval token required: {self.role} ≠ assigner {assigner}")

        current["status"] = "completed"
        current["confirmed_at"] = time.time()
        current["confirmed_by"] = self.role
        self._put_step_result(wf_id, step_id, current)

        # 推进到下一步
        self._advance(wf_id, wf.get("task_id"))
        self._log(wf_id, wf.get("task_id"), "step_confirmed",
                  detail=f"{step_id} confirmed by {self.role}")
        return {"status": "completed", "next_action": "advanced"}

    def fail_step(self, wf_id: str, step_id: str,
                  reason: str = "", allow_retry: bool = False) -> dict:
        wf = self.get_wf(wf_id)
        if not wf:
            raise ValueError(f"workflow 不存在: {wf_id}")
        if wf.get("current_step_id") != step_id:
            raise ValueError(
                f"步骤不匹配: 当前={wf.get('current_step_id')}, 传入={step_id}")

        if allow_retry:
            cur = self._get_step_result(wf, step_id)
            cur["status"] = "failed"
            cur["failed_at"] = time.time()
            cur["failed_by"] = self.role
            cur["reason"] = reason
            self._put_step_result(wf_id, step_id, cur)
            self._log(wf_id, wf.get("task_id"), "step_failed",
                      detail=f"{step_id}: {reason}")
            return {"status": "failed", "allow_retry": True}
        else:
            self._set_wf_status(wf_id, "failed")
            self._sync_task_status(wf.get("task_id"))
            self._log(wf_id, wf.get("task_id"), "wf_failed",
                      detail=f"step {step_id}: {reason}")
            return {"status": "wf_failed", "allow_retry": False}

    def close_wf(self, wf_id: str):
        wf = self.get_wf(wf_id)
        if not wf:
            return
        self._set_wf_status(wf_id, "completed")
        self._conn.execute(
            "UPDATE workflow_instances SET completed_at=? WHERE instance_id=?",
            (time.time(), wf_id)
        )
        self._conn.commit()
        self._sync_task_status(wf.get("task_id"))
        self._log(wf_id, wf.get("task_id"), "wf_closed")

    def rollback_step(self, wf_id: str, step_id: str) -> bool:
        wf = self.get_wf(wf_id)
        if not wf:
            return False
        steps = self._get_all_steps(wf.get("template_id"))
        for i, s in enumerate(steps):
            if s.get("step_id") == step_id:
                self._conn.execute(
                    "UPDATE workflow_instances SET current_step_id=? WHERE instance_id=?",
                    (step_id, wf_id)
                )
                self._conn.commit()
                # 重置步骤结果
                self._put_step_result(wf_id, step_id, {"status": "rollback"})
                self._log(wf_id, wf.get("task_id"), "rollback",
                          detail=f"→ {step_id}")
                return True
        return False

    # ── 查询 ────────────────────────────────────

    def get_wf(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_approval_token(self, wf_id: str, step_id: str) -> str:
        """生成确认令牌（用于分配者链验证）。"""
        wf = self.get_wf(wf_id)
        if not wf:
            return ""
        assigner = self._get_assigner_for_step(wf, step_id)
        return f"token_{assigner}_{step_id}" if assigner else ""

    # ── 内部: 分配者链 ──────────────────────────

    @staticmethod
    def _step_id(s: object) -> str:
        """提取 step_id，兼容 dict 格式和纯字符串格式。"""
        if isinstance(s, dict):
            return s.get("step_id", "")
        if isinstance(s, str):
            return s
        return ""

    @staticmethod
    def _step_target_role(s: object) -> str:
        """提取 target_role，兼容 dict 和字符串格式。"""
        if isinstance(s, dict):
            return s.get("target_role", "")
        return ""

    def _get_assigner_for_step(self, wf: dict, step_id: str) -> str:
        """分配者链: s1=wf.assigner, sN=prev_step.target_role。"""
        steps = self._get_all_steps(wf.get("template_id"))
        for i, s in enumerate(steps):
            if self._step_id(s) == step_id:
                if i == 0:
                    return wf.get("assigner", "")
                return self._step_target_role(steps[i - 1])
        return ""

    def _get_step_def(self, template_id: Optional[str], step_id: str) -> Optional[dict]:
        """获取步骤定义。"""
        steps = self._get_all_steps(template_id)
        for s in steps:
            if self._step_id(s) == step_id:
                return s if isinstance(s, dict) else {"step_id": s}
        return None

    # ── 内部: 条件检查 ──────────────────────────

    def _check_gate_condition(self, check: Optional[dict]) -> tuple:
        """检查门禁条件。返回 (passed, message)。"""
        if not check:
            return (True, "no conditions")
        output_exists = check.get("output_exists", [])
        if output_exists:
            missing = [p for p in output_exists if not Path(p).exists()]
            if missing:
                return (False, f"output not found: {', '.join(missing)}")
        return (True, "conditions satisfied")

    # ── 内部: DB 操作 ────────────────────────────

    def _get_step_result(self, wf: dict, step_id: str) -> dict:
        sr = wf.get("step_results", "{}")
        try:
            results = json.loads(sr) if isinstance(sr, str) else sr
        except (json.JSONDecodeError, TypeError):
            results = {}
        return results.get(step_id, {})

    def _put_step_result(self, wf_id: str, step_id: str, data: dict):
        wf = self.get_wf(wf_id)
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

    def _set_wf_status(self, wf_id: str, status: str):
        self._conn.execute(
            "UPDATE workflow_instances SET status=? WHERE instance_id=?",
            (status, wf_id)
        )
        self._conn.commit()

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

    def _advance(self, wf_id: str, task_id: Optional[str]) -> bool:
        wf = self.get_wf(wf_id)
        if not wf:
            return False
        steps = self._get_all_steps(wf.get("template_id"))
        current = wf.get("current_step_id", "")
        for i, s in enumerate(steps):
            if self._step_id(s) == current:
                if i + 1 >= len(steps):
                    self.close_wf(wf_id)
                    return False
                next_step = steps[i + 1]
                next_id = self._step_id(next_step)
                self._conn.execute(
                    "UPDATE workflow_instances SET current_step_id=? WHERE instance_id=?",
                    (next_id, wf_id)
                )
                self._conn.commit()
                self._log(wf_id, task_id, "advanced",
                          detail=f"{current} → {next_step['step_id']}")
                return True
        return False

    def _get_all_steps(self, template_id: Optional[str]) -> list:
        if not template_id:
            return []
        row = self._conn.execute(
            "SELECT steps_json FROM workflow_templates WHERE template_id=?",
            (template_id,)
        ).fetchone()
        if not row or not row["steps_json"]:
            return []
        try:
            return json.loads(row["steps_json"])
        except (json.JSONDecodeError, TypeError):
            return []

    def _log(self, wf_id: str = None, task_id: str = None,
             action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
            "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (wf_id, task_id, action, self.role, detail, time.time())
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
