"""workflow/db.py — 数据库模式与连接管理。"""

import sqlite3
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS workflow_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        steps_json TEXT NOT NULL,
        steps_mermaid TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workflow_instances (
        instance_id TEXT PRIMARY KEY,
        template_id TEXT,
        task_id TEXT NOT NULL,
        assigner TEXT NOT NULL,
        assignee TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        current_step_id TEXT,
        step_results TEXT DEFAULT '{}',
        created_at REAL NOT NULL,
        completed_at REAL,
        FOREIGN KEY (task_id) REFERENCES tasks(task_id),
        FOREIGN KEY (template_id) REFERENCES workflow_templates(template_id)
    );
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        assigner TEXT NOT NULL,
        assignee TEXT,
        priority INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'created',
        current_workflow_id TEXT,
        progress TEXT DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        completed_at REAL,
        tags TEXT DEFAULT '[]',
        context TEXT DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS workflow_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id TEXT,
        task_id TEXT,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        ts REAL NOT NULL
    );
"""


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """创建并返回一个初始化 schema 的连接。"""
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
