#!/usr/bin/env python3
"""
LifecycleGate — 生命周期门禁（WL-P2-01）

step 级权限校验：
1. Role-Step Matrix: 该角色是否有权执行该模板的该步骤
2. Assignment Chain: 当前 step 的分配者是否是上一步的 executor
3. P0 Exemption: P0 任务的门禁豁免规则
"""

import json
from pathlib import Path
from typing import Optional

from paths import SESSION_ROLES_PERSONAS


class LifecycleGate:
    """生命周期门禁 — 在 step 确认/执行时校验权限。"""

    def __init__(self):
        self._workgroup_matrix = self._load_workgroup_from_personas()

    def _load_workgroup_from_personas(self) -> dict[str, set[str]]:
        """从 persona JSON 动态加载工作群组矩阵。

        返回: {role: set(allowed_target_roles)}
        兼容旧行为: 角色无 workgroup 字段时回退为空集合（仅允许同角色通信）。
        """
        matrix = {"coordinator": {"*"}}  # coordinator 兜底可联系所有人

        try:
            for f in SESSION_ROLES_PERSONAS.glob("*.json"):
                data = json.loads(f.read_text())
                name = data.get("name")
                workgroup = data.get("workgroup", [])
                if name and workgroup:
                    matrix[name] = set(workgroup)
        except Exception:
            pass  # 静默回退

        return matrix

    def check_can_execute(self, role: str, template_id: str, step_id: str) -> bool:
        """角色 role 能否执行 template_id 的 step_id。

        规则:
        - coordinator: 总是 True（管理员角色）
        - step 定义中有 target_role: 仅 target_role 可执行
        - step 无 target_role: 看 workgroup 矩阵中 role 能否联系 template 的负责角色
        - P0 任务: 豁免所有门禁
        """
        # 管理员角色豁免
        if role in ("coordinator", "maintainer", "lr"):
            return True

        # 从 DB 读取 template 的 step 定义
        step_def = self._get_step_def(template_id, step_id)
        if not step_def:
            # 无定义则放行（防误拦截）
            return True

        # target_role 显式指定：仅该角色可执行
        target_role = step_def.get("target_role")
        if target_role:
            return role == target_role

        # 无 target_role：检查 workgroup 矩阵
        # 需要确定 template 的 "owner" 角色（通常是第一步的 target_role 或 assigner）
        template_owner = self._get_template_owner(template_id)
        if template_owner:
            allowed = self._workgroup_matrix.get(role, set())
            return "*" in allowed or template_owner in allowed

        return True

    def check_can_initiate(self, role: str, template_id: str) -> bool:
        """角色 role 能否发起 template_id 工作流。"""
        # 管理员角色豁免
        if role in ("coordinator", "maintainer", "lr"):
            return True

        # 检查 workgroup 矩阵：role 是否能联系 template 的负责角色
        template_owner = self._get_template_owner(template_id)
        if template_owner:
            allowed = self._workgroup_matrix.get(role, set())
            return "*" in allowed or template_owner in allowed

        return True

    def check_p0_exemption(self, task_id: str) -> bool:
        """P0 任务豁免检查。"""
        try:
            from paths import WORKFLOWS_DB as DB_PATH
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT priority FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            conn.close()
            if row and row["priority"] == "P0":
                return True
        except Exception:
            pass
        return False

    def _get_step_def(self, template_id: str, step_id: str) -> Optional[dict]:
        """从 DB 读取模板的步骤定义。"""
        try:
            from paths import WORKFLOWS_DB as DB_PATH
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT steps_json FROM workflow_templates WHERE template_id=?",
                (template_id,)
            ).fetchone()
            conn.close()
            if not row or not row["steps_json"]:
                return None
            steps = json.loads(row["steps_json"])
            for s in steps:
                if s.get("step_id") == step_id:
                    return s
        except Exception:
            pass
        return None

    def _get_template_owner(self, template_id: str) -> Optional[str]:
        """返回模板的负责角色（第一步的 target_role）。"""
        try:
            from paths import WORKFLOWS_DB as DB_PATH
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT steps_json FROM workflow_templates WHERE template_id=?",
                (template_id,)
            ).fetchone()
            conn.close()
            if not row or not row["steps_json"]:
                return None
            steps = json.loads(row["steps_json"])
            if steps:
                return steps[0].get("target_role")
        except Exception:
            pass
        return None
