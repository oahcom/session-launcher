# =============================================================================
# [DEPRECATED] migrate_v11_collab.py — 废弃
#
# 跨角色协作工作流已统一至 session-pipeline：
#   - pipeflow/engine.py 管理所有工作流模板和实例
#   - lifecycle/manager.py 处理步骤状态机
# =============================================================================

#!/usr/bin/env python3
"""
migrate_v11_collab.py — 跨角色协作 V1.1 数据库迁移。

添加新的跨角色工作流模板（bug 修复 / 功能实现）。

运行: python3 src/migrate_v11_collab.py
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

from paths import WORKFLOWS_DB as DB_PATH

NEW_TEMPLATES = [
    {
        "template_id": "tmpl_bug_fix",
        "name": "bug 修复",
        "description": "QA → PG → QA 闭环",
        "steps_json": json.dumps({
            "steps": [
                {"id": "s1", "type": "handoff", "title": "提交 bug 给 PG",
                 "target_role": "pg", "confirm_timeout": 300,
                 "on_timeout": "wake_and_continue"},
                {"id": "s2", "type": "single", "title": "PG 修复代码"},
                {"id": "s3", "type": "handoff", "title": "提交给 QA 验证",
                 "target_role": "qa", "confirm_timeout": 300,
                 "on_timeout": "wake_and_continue"},
                {"id": "s4", "type": "single", "title": "QA 回归验证"},
            ]
        }),
        "steps_mermaid": "QA(s1)→PG(s2)→QA(s3)→QA(s4)",
    },
    {
        "template_id": "tmpl_feature_impl",
        "name": "功能实现",
        "description": "PM → PG → QA 交付",
        "steps_json": json.dumps({
            "steps": [
                {"id": "s1", "type": "handoff", "title": "提交需求给 PG",
                 "target_role": "pg", "confirm_timeout": 300,
                 "on_timeout": "wake_and_continue"},
                {"id": "s2", "type": "single", "title": "PG 实现"},
                {"id": "s3", "type": "handoff", "title": "提交给 QA 测试",
                 "target_role": "qa", "confirm_timeout": 300,
                 "on_timeout": "wake_and_continue"},
                {"id": "s4", "type": "single", "title": "QA 测试"},
            ]
        }),
        "steps_mermaid": "PM(s1)→PG(s2)→QA(s3)→QA(s4)",
    },
]


def migrate():
    """运行迁移：插入新工作流模板（幂等）。"""
    db_path = DB_PATH
    if not db_path.exists():
        print(f"数据库不存在: {db_path}，跳过迁移")
        return

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    now = time.time()

    # 确保表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_templates (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            steps_json TEXT NOT NULL,
            steps_mermaid TEXT,
            created_at REAL NOT NULL
        )
    """)

    inserted = 0
    for tmpl in NEW_TEMPLATES:
        existing = conn.execute(
            "SELECT template_id FROM workflow_templates WHERE template_id=?",
            (tmpl["template_id"],)
        ).fetchone()
        if existing:
            print(f"  已存在: {tmpl['name']} ({tmpl['template_id']})")
            continue

        conn.execute("""
            INSERT OR IGNORE INTO workflow_templates (template_id, name, description,
                                            steps_json, steps_mermaid, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (tmpl["template_id"], tmpl["name"], tmpl["description"],
              tmpl["steps_json"], tmpl["steps_mermaid"], now))
        inserted += 1
        print(f"  + 插入: {tmpl['name']} ({tmpl['template_id']})")

    conn.commit()
    conn.close()
    print(f"\n迁移完成: 新插入 {inserted} 个模板")
    return inserted


if __name__ == "__main__":
    migrate()
