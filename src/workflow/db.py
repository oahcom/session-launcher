"""workflow/db.py — 数据库连接管理。

Schema 定义从 workflow.schema 导入（单一来源）。

用法:
    from workflow.db import create_connection, template_row
    from workflow.schema import SCHEMA_SQL
"""
import sqlite3
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH
from workflow.schema import SCHEMA_SQL


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """创建并返回一个初始化 schema 的连接。"""
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def template_row(conn: sqlite3.Connection, template_id: str):
    """按 id 取模板行；裸名再试一次 WF- 前缀。

    同一模板有两种登记名：表里既有 `WF-x` 也有 `x`，而子链建行写的是步骤里的
    裸名（pipeflow/wf_lifecycle.py 的 subflow INSERT 用 `subflow_template`）。7 天
    254 个实例里 126 个（49.6%）因此查不到自己 → 契约 None、首步退回 s1、建链校验
    误报 template_id not found。别名解析放读侧，历史坏行一并救回。
    """
    for tid in (template_id, f"WF-{template_id}"):
        if not tid or tid == "WF-":
            continue
        row = conn.execute(
            "SELECT * FROM workflow_templates WHERE template_id=?", (tid,)).fetchone()
        if row:
            return row
    return None
