#!/usr/bin/env python3
"""
migration_scripts.py — 存量数据迁移脚本 + 回滚

包含 pre-flight 统计、dry-run 评估、导出备份、truncate 清空、回滚。
所有脚本在非生产环境 dry-run 验证通过后再正式执行。
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from paths import WORKFLOWS_DB as DB_PATH


# ── Pre-flight 统计 ─────────────────────────────

def pre_flight(db_path: str = None, dry_run: bool = False) -> dict:
    """统计当前 DB 状态：活跃 workflow 数、template_id 绑定的 task 比例等。"""
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        return {"error": f"DB 不存在: {path}"}

    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("SELECT COUNT(*) as c FROM workflow_instances").fetchone()
        total_instances = rows["c"] if rows else 0

        rows = conn.execute(
            "SELECT COUNT(*) as c FROM workflow_instances WHERE status IN ('running','pending')"
        ).fetchone()
        active_instances = rows["c"] if rows else 0

        rows = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()
        total_tasks = rows["c"] if rows else 0

        try:
            rows = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE template_id IS NOT NULL").fetchone()
            tasks_with_template = rows["c"] if rows else 0
        except sqlite3.OperationalError:
            tasks_with_template = 0

        rows = conn.execute("SELECT COUNT(*) as c FROM workflow_templates").fetchone()
        total_templates = rows["c"] if rows else 0

        rows = conn.execute("SELECT COUNT(*) as c FROM workflow_logs").fetchone()
        total_logs = rows["c"] if rows else 0

        template_ratio = (tasks_with_template / total_tasks * 100) if total_tasks > 0 else 0

    return {
        "total_instances": total_instances,
        "active_instances": active_instances,
        "total_tasks": total_tasks,
        "tasks_with_template_id": tasks_with_template,
        "template_binding_ratio_pct": round(template_ratio, 1),
        "total_templates": total_templates,
        "total_logs": total_logs,
        "db_path": str(path),
        "db_size_bytes": path.stat().st_size,
    }


# ── Dry-run 评估 ────────────────────────────────

def dry_run_assessment(db_path: str = None, dry_run: bool = False) -> list[dict]:
    """模拟迁移评估，不修改数据。返回逐行建议。"""
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        return [{"error": f"DB 不存在: {path}"}]

    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        suggestions = []

        # 1. 检查 workflow_templates 是否有 is_active 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_templates)").fetchall()}
        if "is_active" not in cols:
            suggestions.append({
                "table": "workflow_templates",
                "action": "ALTER TABLE ADD COLUMN is_active INTEGER DEFAULT 1",
                "status": "pending",
                "note": "V2 schema 新增字段",
            })
        else:
            suggestions.append({
                "table": "workflow_templates",
                "action": "is_active 已存在",
                "status": "skipped",
            })

        # 2. 检查 tasks 是否有 template_id 列
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "template_id" not in cols2:
            suggestions.append({
                "table": "tasks",
                "action": "ALTER TABLE ADD COLUMN template_id TEXT",
                "status": "pending",
                "note": "V2 schema 新增字段",
            })
        else:
            suggestions.append({
                "table": "tasks",
                "action": "template_id 已存在",
                "status": "skipped",
            })

        # 3. 检查索引
        indexes = {r[2] for r in conn.execute("PRAGMA index_list(workflow_logs)").fetchall()}
        if "idx_logs_action" not in indexes:
            suggestions.append({
                "table": "workflow_logs",
                "action": "CREATE INDEX idx_logs_action ON workflow_logs(action)",
                "status": "pending",
            })
        else:
            suggestions.append({
                "table": "workflow_logs", "action": "idx_logs_action 已存在", "status": "skipped"
            })
        if "idx_logs_ts" not in indexes:
            suggestions.append({
                "table": "workflow_logs",
                "action": "CREATE INDEX idx_logs_ts ON workflow_logs(ts)",
                "status": "pending",
            })
        else:
            suggestions.append({
                "table": "workflow_logs", "action": "idx_logs_ts 已存在", "status": "skipped"
            })

        # 4. 旧模板标记 deprecated
        rows = conn.execute(
            "SELECT template_id FROM workflow_templates WHERE template_id LIKE 'tmpl_%'"
        ).fetchall()
        for r in rows:
            suggestions.append({
                "table": "workflow_templates",
                "action": f"SET is_active=0 WHERE template_id='{r['template_id']}'",
                "status": "pending",
                "note": "旧格式模板需停用",
            })

    return suggestions


# ── 导出备份 ────────────────────────────────────

from paths import HERMES_BACKUPS as BACKUP_DIR


def export_backup(db_path: str = None, dry_run: bool = False) -> Path:
    """将 workflows.db 导出为 JSONL 备份文件。"""
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        raise FileNotFoundError(f"DB 不存在: {path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = BACKUP_DIR / f"workflows_backup_{timestamp}.jsonl"

    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        entries = []
        tables = ["workflow_templates", "workflow_instances", "tasks", "workflow_logs"]
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                entries.append(json.dumps({
                    "table": table,
                    "data": dict(row),
                    "exported_at": time.time(),
                }, ensure_ascii=False, default=str))

        out.write_text("\n".join(entries) + "\n", encoding="utf-8")

    return out


# ── Truncate 清空 ───────────────────────────────

def truncate_tables(db_path: str = None,
                    dry_run: bool = True) -> dict:
    """安全清空 workflow_instances 和 tasks 的 current_workflow_id。"""
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        return {"error": f"DB 不存在: {path}"}

    with sqlite3.connect(str(path), timeout=10) as conn:
        before = pre_flight(str(path))

        if dry_run:
            return {
                "dry_run": True,
                "message": "仅模拟，未修改任何数据",
                "before": before,
            }

        # 先备份再清空
        backup_path = export_backup(str(path))
        conn.execute("DELETE FROM workflow_instances")
        conn.execute("UPDATE tasks SET current_workflow_id=NULL")
        conn.commit()

        after = pre_flight(str(path))

    return {
        "dry_run": False,
        "truncated_instances": before["total_instances"],
        "backup_path": str(backup_path),
        "before": before,
        "after": after,
    }


# ── 回滚 ────────────────────────────────────────

def restore_from_backup(backup_path: str,
                         db_path: str = None) -> dict:
    """从 JSONL 备份恢复数据。"""
    path = Path(db_path) if db_path else DB_PATH
    backup = Path(backup_path)
    if not backup.exists():
        return {"error": f"备份文件不存在: {backup_path}"}

    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        # 清空现有数据
        conn.executescript("""
            DELETE FROM workflow_logs;
            DELETE FROM workflow_instances;
            DELETE FROM tasks;
            DELETE FROM workflow_templates;
        """)
        conn.commit()

        restored = {"workflow_templates": 0, "workflow_instances": 0, "tasks": 0, "workflow_logs": 0}

        # 白名单：只允许已知表名
        _ALLOWED_TABLES = {"workflow_templates", "workflow_instances", "tasks", "workflow_logs"}
        for line in backup.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            table = entry["table"]
            if table not in _ALLOWED_TABLES:
                continue
            data = entry["data"]
            # 白名单校验列名：只允许小写字母+下划线
            safe_cols = [k for k in data.keys() if k.replace("_", "").isalpha()]
            if not safe_cols:
                continue
            cols = ", ".join(safe_cols)
            placeholders = ", ".join(["?"] * len(safe_cols))
            vals = [data[k] for k in safe_cols]
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
                vals
            )
            restored[table] = restored.get(table, 0) + 1

        conn.commit()

    return {
        "restored_from": str(backup),
        "restored_counts": restored,
        "checksum": {"tables_sum": sum(restored.values())},
    }


# ── 迁移执行（按依赖顺序：先列/索引，再数据标记） ──

def run_migration(db_path: str = None,
                  dry_run: bool = True) -> dict:
    """执行 ALTER TABLE 迁移（添加 is_active 列和索引）。"""
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        return {"error": f"DB 不存在: {path}"}

    with sqlite3.connect(str(path), timeout=10) as conn:

        results = []

        # 1. workflow_templates 新增列（幂等）
        existing = {r[1] for r in conn.execute(
            "PRAGMA table_info(workflow_templates)").fetchall()}
        for col_name, col_def in [("is_active", "INTEGER NOT NULL DEFAULT 1"),
                                   ("trigger_scene", "TEXT"),
                                   ("allowed_initiators", "TEXT"),
                                   ("allowed_executors", "TEXT"),
                                   ("max_duration_hours", "REAL"),
                                   ("quality_standards", "TEXT")]:
            if col_name not in existing:
                if dry_run:
                    results.append(f"需要 ADD: {col_name}")
                else:
                    conn.execute(
                        f"ALTER TABLE workflow_templates ADD COLUMN {col_name} {col_def}")
                    results.append(f"已 ADD: {col_name}")
            else:
                results.append(f"已存在: {col_name}")

        # 2. tasks 新增 template_id + P0 列
        existing2 = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "template_id" not in existing2:
            if not dry_run:
                conn.execute("ALTER TABLE tasks ADD COLUMN template_id TEXT")
                results.append("已 ADD: tasks.template_id")
        else:
            results.append("已存在: tasks.template_id")
        for col_name, col_def in [("p0_state", "TEXT DEFAULT NULL"),
                                   ("p0_reason", "TEXT DEFAULT ''"),
                                   ("p0_marked_at", "REAL DEFAULT NULL"),
                                   ("p0_marked_by", "TEXT DEFAULT ''")]:
            if col_name not in existing2:
                if not dry_run:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
                    results.append(f"已 ADD: tasks.{col_name}")
            else:
                results.append(f"已存在: tasks.{col_name}")

        # 3. 索引
        existing_idx = {r[1] for r in conn.execute(
            "PRAGMA index_list(workflow_logs)").fetchall()}
        for idx_name, idx_def in [("idx_logs_action", "action"),
                                   ("idx_logs_ts", "ts")]:
            if idx_name not in existing_idx:
                if not dry_run:
                    conn.execute(
                        f"CREATE INDEX {idx_name} ON workflow_logs({idx_def})")
                    results.append(f"已创建索引: {idx_name}")
            else:
                results.append(f"索引已存在: {idx_name}")

        # 4. 旧模板标记为 inactive
        if not dry_run:
            rows = conn.execute(
                "SELECT template_id FROM workflow_templates WHERE template_id LIKE 'tmpl_%'"
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE workflow_templates SET is_active=0 WHERE template_id=?", (r[0],))
                results.append(f"已停用旧模板: {r[0]}")

        conn.commit()

    return {
        "dry_run": dry_run,
        "results": results,
        "success": True,
    }


# ── 存量数据扫描 (P1-03-c) ────────────────────────

def stale_task_scan(db_path: str = None, dry_run: bool = False) -> dict:
    """扫描 current_workflow_id IS NULL 的存量 task。

    干运行 → 输出评估报告 (count + 详情)。
    写模式 → 逐条标记 (未来回滚用，当前仅扫描)。
    """
    path = Path(db_path) if db_path else DB_PATH
    if dry_run:
        import warnings
        warnings.warn("Migration is in DRY RUN mode - NO changes will be made")
    if not path.exists():
        return {"error": f"DB 不存在: {path}"}
    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        count = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE current_workflow_id IS NULL"
        ).fetchone()["c"]
        try:
            rows = conn.execute(
                "SELECT task_id, title, assignee, status FROM tasks WHERE current_workflow_id IS NULL LIMIT 100"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT task_id, current_workflow_id FROM tasks WHERE current_workflow_id IS NULL LIMIT 100"
            ).fetchall()
    return {
        "dry_run": dry_run,
        "stale_count": count,
        "sample": [dict(r) for r in rows],
        "recommendation": "NO_ACTION" if count == 0 else "REVIEW_EACH",
    }


# ── CLI ─────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "pre-flight"

    if cmd == "pre-flight":
        r = pre_flight()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "dry-run":
        r = dry_run_assessment()
        for s in r:
            status = "pending" if s.get("status") == "pending" else "skipped"
            print(f"[{status}] [{s['table']}] {s['action']}")

    elif cmd == "export":
        path = export_backup()
        print(f"exported: {path} ({path.stat().st_size} bytes)")

    elif cmd == "truncate":
        dry = "--exec" not in sys.argv
        r = truncate_tables(dry_run=dry)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("usage: python3 -m migration.scripts restore <backup.jsonl>")
            sys.exit(1)
        r = restore_from_backup(sys.argv[2])
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "migrate":
        dry = "--exec" not in sys.argv
        r = run_migration(dry_run=dry)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "stale-scan":
        r = stale_task_scan(dry_run="--exec" not in sys.argv)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    else:
        print(f"unknown command: {cmd}")
        print("available: pre-flight | dry-run | export | truncate | restore | migrate | stale-scan")
        sys.exit(1)
