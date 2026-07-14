#!/usr/bin/env python3
"""
cross_role_router.py — 跨角色路由层（三源验证）

在 ccs send 时拦截跨角色消息，验证消息来源真实性。
三源验证逻辑（ccs-send-source-verification memory）：
  1. bus --src 查询
  2. DB assigner 查询
  3. sentinel 角色状态
  至少 2 个源一致才放行。
"""

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from paths import WORKFLOWS_DB as DB_PATH
from paths import BUS_CLIENT


class CrossRoleRouter:
    """跨角色路由拦截器 — 三源消息溯源。"""

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
        """拦截跨角色消息，三源验证后决定放行/拒绝。

        返回 True 放行，False 拒绝。
        """
        self._log_cross_role_send(source, target, message)

        # 同角色消息直接放行
        if source == target or source in ("cli", "loop"):
            return True

        # 三源验证
        evidence = self._source_triple_check(source, message)
        source_ok = evidence["sources_ok"]
        detail = evidence["detail"]

        if not source_ok:
            self._log_violation(source, target, message, detail)
            return False

        return True

    def _source_triple_check(self, claimed_source: str, message: str) -> dict:
        """三源验证：bus --src / DB assigner / sentinel。

        返回 dict: {sources_ok: bool, detail: str, match_count: int, total_checked: int}
        """
        matches = 0
        total = 0
        details = []

        # 源1: bus --src 查询
        total += 1
        bus_src = self._check_bus_source(claimed_source, message)
        if bus_src:
            matches += 1
            details.append(f"bus_src={bus_src}")
        else:
            details.append("bus_src=not_found")

        # 源2: DB assigner 查询
        total += 1
        db_assigner = self._check_db_assigner(claimed_source)
        if db_assigner:
            matches += 1
            details.append(f"db_assigner={db_assigner}")
        else:
            details.append("db_assigner=not_found")

        # 源3: sentinel 角色状态
        total += 1
        sentinel_ok = self._check_sentinel(claimed_source)
        if sentinel_ok:
            matches += 1
            details.append("sentinel=exists")
        else:
            details.append("sentinel=not_found")

        sources_ok = matches >= 2
        return {
            "sources_ok": sources_ok,
            "match_count": matches,
            "total_checked": total,
            "detail": "; ".join(details),
        }

    def _check_bus_source(self, claimed_source: str, message: str) -> str:
        """源1: 从 message 提取关键词搜索 bus，检查 --src 是否匹配 claimed_source。"""
        # 提取消息中的任务关键词（取前20个非空格字符）
        keywords = message.strip().split()[:3]
        if not keywords:
            return ""
        query = " ".join(keywords)

        try:
            r = subprocess.run(
                ["python3", str(BUS_CLIENT), "search", query],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return ""
            # 搜索结果格式: [id] (cat) text
            for line in r.stdout.split("\n"):
                if "--src" in line or "src=" in line:
                    found_src = line.strip()
                    if claimed_source in found_src:
                        return "matched"
            # 如果搜索到任何结果，返回 "found" 但不一定匹配
            if r.stdout.strip() and not r.stdout.strip().startswith("No results"):
                return "partial"
            return ""
        except Exception:
            return ""

    def _check_db_assigner(self, claimed_source: str) -> str:
        """源2: 查询 workflows.db 最近 10 条任务，找 assigner 匹配。"""
        try:
            conn = self._get_conn()
            try:
                # 检查表是否存在
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_instances'"
                ).fetchone()
                if not tbl:
                    return ""
                rows = conn.execute(
                    """SELECT wi.assigner FROM workflow_instances wi
                       ORDER BY wi.created_at DESC LIMIT 10"""
                ).fetchall()
                for r in rows:
                    if r["assigner"] == claimed_source:
                        return "matched"
                if rows:
                    return ""  # 有记录但无匹配 → 不计数
                return ""
            finally:
                conn.close()
        except Exception:
            return ""

    def _check_sentinel(self, claimed_source: str) -> bool:
        """源3: 检查 tmux session 是否存在。"""
        for prefix in ("ccs", "cdx"):
            try:
                r = subprocess.run(
                    ["tmux", "has-session", "-t", f"{prefix}-{claimed_source}"],
                    capture_output=True, timeout=3)
                if r.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def check_send_permission(self, source: str, target: str,
                               category: str = "") -> bool:
        """检查发送权限（兼容旧接口）。"""
        return self.intercept(source, target, category)

    def _log_cross_role_send(self, source: str, target: str, message: str):
        """记录跨角色消息到 workflow_logs。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "cross_role_send", source,
                 json.dumps({
                     "source": source, "target": target,
                     "message_preview": message[:200],
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()

    def _log_violation(self, source: str, target: str, message: str, detail: str):
        """记录验证失败违规日志。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "source_verification_denied", source,
                 json.dumps({
                     "source": source, "target": target,
                     "message_preview": message[:200],
                     "detail": detail,
                     "violation": True,
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()
