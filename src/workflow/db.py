"""workflow/db.py — 数据库连接管理。

Schema 定义从 workflow.schema 导入（单一来源）。

用法:
    from workflow.db import create_connection
    from workflow.schema import SCHEMA_SQL, init_db
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
