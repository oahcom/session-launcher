"""workflow/schema.py — DB schema 单一来源。

所有 workflow 相关模块从此文件导入 schema，避免定义漂移。

用法:
    from workflow.schema import init_db, SCHEMA_SQL
"""
import sqlite3

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS workflow_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        steps_json TEXT NOT NULL,
        steps_mermaid TEXT,
        created_at REAL NOT NULL,
        is_active INTEGER DEFAULT 1,
        trigger_scene TEXT DEFAULT '[]',
        allowed_initiators TEXT DEFAULT '[]',
        allowed_executors TEXT DEFAULT '[]',
        max_duration_hours REAL,
        quality_standards TEXT
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
        parent_wf_id TEXT,
        subflow_source_step_id TEXT,
        context TEXT,
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
        context TEXT DEFAULT '{}',
        template_id TEXT,
        p0_state TEXT,
        p0_reason TEXT,
        p0_marked_at REAL,
        p0_marked_by TEXT,
        parent_task_id TEXT
    );
    CREATE TABLE IF NOT EXISTS workflow_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id TEXT,
        task_id TEXT,
        action TEXT NOT NULL,
        actor NOT NULL,
        detail TEXT,
        ts REAL NOT NULL
    );
"""

def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
