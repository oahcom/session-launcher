#!/usr/bin/env python3
"""
cross_role_router.py — 跨角色路由层（存根）

在 ccs send 时拦截跨角色消息，当前实现：
- 默认放行（intercept 返回 True）
- 记录跨角色消息审计日志到 workflow_logs
- 预留可插拔拦截点（后续实现 workgroup 矩阵时只需替换 check 逻辑）
"""

import json
import sqlite3
import time
from pathlib import Path

from paths import WORKFLOWS_DB as DB_PATH


class CrossRoleRouter:
    """跨角色路由拦截器。

    当前为存根实现：默认放行所有跨角色消息，仅记录日志。
    """

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # workflow_logs schema managed by workflow_db.py
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_instance_id TEXT,
                task_id TEXT,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT,
                ts REAL NOT NULL
            );
        """)
        return conn

    def intercept(self, source: str, target: str, message: str) -> bool:
        """拦截跨角色消息。

        当前存根实现：记录审计日志后始终放行。

        参数：
            source: 发送角色
            target: 目标角色
            message: 消息内容

        返回：
            True 放行，False 拒绝（当前始终为 True）
        """
        # 记录审计日志
        self._log_cross_role_send(source, target, message)

        # 当前存根始终放行。后续实现 workgroup 矩阵时替换 check 逻辑。
        return True

    def _log_cross_role_send(self, source: str, target: str, message: str):
        """将跨角色消息记录到 workflow_logs。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "cross_role_send", source,
                 json.dumps({
                     "source": source,
                     "target": target,
                     "message_preview": message[:200],
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()

    def check_send_permission(self, source: str, target: str,
                               category: str = "") -> bool:
        """检查发送权限（占位方法）。

        当前始终返回 True。后续实现时：

        1. 从 workgroup 矩阵表查询 source 是否允许给 target 发消息
        2. category 参数用于按消息分类控制权限
        """
        return True

    def log_violation(self, source: str, target: str, message: str):
        """记录权限违规（占位方法）。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "cross_role_violation", source,
                 json.dumps({
                     "source": source, "target": target,
                     "message_preview": message[:200],
                     "violation": True,
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()
