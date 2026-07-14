#!/usr/bin/env python3
"""
migrate_claude_md.py — T14：CLAUDE.md V1→V2 工作流 API 迁移

将 ccs-workspaces/*/CLAUDE.md 中的 V1 workflow API 替换为 V2。
- create_task → create_task_v2（绑定 template_id）
- complete_task → complete_step
- fail_task → fail_step
执行前备份原始文件为 *.bak。
"""

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

SESSION_LAUNCHER = Path.home() / "session-launcher"
CCS_WORKSPACES = Path.home() / "ccs-workspaces"
PROD_DB = Path.home() / ".hermes" / "state" / "workflows.db"

DEFAULT_TEMPLATE = {
    "pm": "WL-02", "coordinator": "WL-02", "ccs-coordinator": "WL-02",
    "lr": "WL-02", "pg": "WL-01", "product_architect": "WL-04",
    "reviewer": "WL-01", "qa": "WL-03", "maintainer": "WL-05",
    "optimizer": "WL-05", "devops": "WL-01", "engineer": "WL-01",
    "archivist": "WL-04", "curator": "WL-04", "consumer": "WL-02",
    "regression_test_return": "WL-03", "test-pg": "WL-01",
}


def log_migration(role, filepath, changes):
    if not PROD_DB.exists():
        return
    conn = sqlite3.connect(str(PROD_DB))
    conn.execute(
        "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
        "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (None, None, "claude_md_migration", "system",
         json.dumps({"role": role, "file": str(filepath), "changes": changes},
                     ensure_ascii=False),
         time.time()))
    conn.commit()
    conn.close()


def replace_block(content, old_marker, new_block):
    """查找标记行并替换到下一个 ### 或文件末尾。"""
    if old_marker not in content:
        return content, False
    idx = content.find(old_marker)
    # 找到行尾
    end = idx + len(old_marker)
    # 找到下一个 ### 或文件末尾
    next_section = content.find("\n### ", end)
    if next_section == -1:
        next_section = len(content)
    content = content[:idx] + new_block + content[next_section:]
    return content, True


def migrate_file(filepath, role):
    template = DEFAULT_TEMPLATE.get(role, "WL-01")
    changes = []
    content = filepath.read_text(encoding="utf-8")

    # 备份
    backup = filepath.with_suffix(".md.bak")
    if not backup.exists():
        shutil.copy2(filepath, backup)
        changes.append(f"备份: {backup.name}")

    # 1. create_task block → create_task_v2
    v2_create = (
        f"### 创建任务（绑定工作流模板）\n"
        f"\n"
        f"```python\n"
        f"import sys; sys.path.insert(0, '{SESSION_LAUNCHER}')\n"
        f"from workflow.client import WorkflowClient\n"
        f"\n"
        f"with WorkflowClient(\"{role}\") as wf:\n"
        f"    task_id, wf_id = wf.create_task_v2(\n"
        f"        \"任务标题\",\n"
        f"        assignee=\"{role}\",\n"
        f"        template_id=\"{template}\",\n"
        f"        initiator_role=\"{role}\",\n"
        f"    )\n"
        f"    print(f\"任务: {{task_id}}, 工作流: {{wf_id}}\")\n"
        f"```"
    )
    found = False
    for marker in [
        "### 创建任务（带工作流）",
        "### 创建任务（绑定工作流模板）",
    ]:
        if marker in content and "create_task_v2" not in content:
            content, f = replace_block(content, marker, v2_create)
            if f:
                changes.append(f"create_task → create_task_v2 (template={template})")
                found = True
                break
    if not found:
        # 试试更灵活的匹配
        if "wf.create_task(" in content and "create_task_v2" not in content:
            # 替换 create_task 调用行为 create_task_v2
            lines = content.split("\n")
            new_lines = []
            in_create_block = False
            for line in lines:
                if "wf.create_task(" in line and "create_task_v2" not in line:
                    # 替换这行
                    indent = line[:len(line) - len(line.lstrip())]
                    new_lines.append(f"{indent}    task_id, wf_id = wf.create_task_v2(")
                    new_lines.append(f"{indent}        \"任务标题\",")
                    new_lines.append(f"{indent}        assignee=\"{role}\",")
                    new_lines.append(f"{indent}        template_id=\"{template}\",")
                    new_lines.append(f"{indent}        initiator_role=\"{role}\",")
                    new_lines.append(f"{indent}    )")
                    in_create_block = True
                    changes.append(f"内联 create_task → create_task_v2")
                elif in_create_block and ("wf.create(" in line or "task_id=task_id" in line):
                    continue  # 跳过旧 wf.create 行
                elif in_create_block and not line.strip():
                    continue  # 跳过空行
                else:
                    if in_create_block and "print" in line and "task_id" in line:
                        new_lines.append(f"    print(f\"任务: {{task_id}}, 工作流: {{wf_id}}\")")
                        in_create_block = False
                        continue
                    in_create_block = False
                    new_lines.append(line)
            content = "\n".join(new_lines)

    # 2. complete_task → complete_step
    v2_complete = (
        f"### 完成当前步骤\n"
        f"\n"
        f"```python\n"
        f"import sys; sys.path.insert(0, '{SESSION_LAUNCHER}')\n"
        f"from workflow.client import WorkflowClient\n"
        f"wf = WorkflowClient(\"{role}\")\n"
        f"result = wf.complete_step(\"{{wf_id}}\", \"s1\")\n"
        f"print(result)\n"
        f"```"
    )
    if "complete_task(" in content and "complete_step(" not in content:
        if "### 标记任务完成" in content:
            content, f = replace_block(content, "### 标记任务完成", v2_complete)
            if f:
                changes.append("complete_task → complete_step")
        else:
            content = content.replace("complete_task(", "complete_step(")
            changes.append("complete_task → complete_step (直接替换)")

    # 3. fail_task → fail_step
    v2_fail = (
        f"### 标记步骤失败\n"
        f"\n"
        f"```python\n"
        f"import sys; sys.path.insert(0, '{SESSION_LAUNCHER}')\n"
        f"from workflow.client import WorkflowClient\n"
        f"wf = WorkflowClient(\"{role}\")\n"
        f"wf.fail_step(\"{{wf_id}}\", \"s1\", \"失败原因\")\n"
        f"```"
    )
    if "fail_task(" in content and "fail_step(" not in content:
        if "### 标记任务失败" in content:
            content, f = replace_block(content, "### 标记任务失败", v2_fail)
            if f:
                changes.append("fail_task → fail_step")
        else:
            content = content.replace("fail_task(", "fail_step(")
            changes.append("fail_task → fail_step (直接替换)")

    # 4. 更新工作流程说明
    v2_workflow = (
        f"## 工作流程\n"
        f"\n"
        f"1. 启动时调用 `check(\"{role}\")` 检查任务\n"
        f"2. 有任务 → 执行任务内容（参考工作流模板）\n"
        f"3. 步骤完成调用 `complete_step()` 标记\n"
        f"4. 需要下游确认时等待调用方 `confirm_step()`\n"
        f"5. 失败时调用 `fail_step()` 标记失败原因\n"
        f"6. 需要时调用 `get_logs()` 查看操作历史\n"
        f"\n"
        f"## 状态流转\n"
        f"\n"
        f"### Task 状态\n"
        f"```\n"
        f"created → assigned → in_progress → completed/failed/cancelled\n"
        f"```\n"
        f"\n"
        f"### Workflow Instance 状态\n"
        f"```\n"
        f"pending → running → step_done_ready → completed/failed\n"
        f"```\n"
        f"\n"
        f"### Step 状态\n"
        f"```\n"
        f"pending → running → step_done_ready → completed/failed\n"
        f"```"
    )
    old_wf_marker = "## 工作流程"
    if old_wf_marker in content:
        content, f = replace_block(content, old_wf_marker, v2_workflow)
        if f:
            changes.append("工作流程 → V2")

    # 5. 禁令
    content = content.replace(
        "完成任务必须调用 `complete_task()`，不调用 = 未完成",
        "完成任务必须调用工作流 API，不调用 = 未完成"
    )

    # 6. check() → 保持兼容（check 函数还在）
    # 7. 清理 import
    content = content.replace(
        "from workflow.client import complete_task",
        "# from workflow.client import complete_step, confirm_step"
    )
    content = content.replace(
        "from workflow.client import fail_task",
        "# from workflow.client import fail_step"
    )

    filepath.write_text(content, encoding="utf-8")
    changes.append("写入完成")
    return changes


def scan_and_migrate(dry_run=True):
    results = []
    for f in sorted(CCS_WORKSPACES.glob("*/CLAUDE.md")):
        role = f.parent.name
        content = f.read_text(encoding="utf-8")
        has_v1 = any(p in content for p in
                      ["wf.create_task(", "complete_task(", "fail_task("])
        has_v2 = any(p in content for p in
                      ["create_task_v2(", "complete_step(", "confirm_step("])

        entry = {
            "role": role, "file": str(f),
            "has_v1": has_v1, "has_v2": has_v2,
            "status": "skipped", "changes": [],
        }
        if has_v1 and not has_v2:
            if not dry_run:
                changes = migrate_file(f, role)
                entry["changes"] = changes
                entry["status"] = "migrated"
                log_migration(role, f, changes)
            else:
                entry["status"] = "would_migrate"
        elif has_v2:
            entry["status"] = "already_v2"
        results.append(entry)
    return results


if __name__ == "__main__":
    dry_run = "--exec" not in sys.argv
    print("=" * 60)
    print(f"T14 CLAUDE.md 迁移 — {'Dry Run' if dry_run else '执行'}")
    print("=" * 60)

    results = scan_and_migrate(dry_run=dry_run)
    stats = {"total": 0, "v1": 0, "v2": 0, "migrated": 0, "skipped": 0}
    for r in results:
        stats["total"] += 1
        if r["has_v1"]: stats["v1"] += 1
        if r["has_v2"]: stats["v2"] += 1
        if r["status"] in ("would_migrate", "migrated"):
            stats["migrated"] += 1
        else:
            stats["skipped"] += 1
        tag = {"would_migrate": "→", "migrated": "✓", "already_v2": "=", "skipped": "="}[r["status"]]
        detail = f" ({', '.join(r['changes'])})" if r["changes"] else ""
        print(f"  [{tag}] {r['role']:20s}{detail}")

    print("-" * 60)
    print(f"总计: {stats['total']} | 将迁移: {stats['migrated']} | 跳过: {stats['skipped']}")
    if dry_run and stats["migrated"] > 0:
        print(f"\n执行: python3 scripts/migrate_claude_md.py --exec")
