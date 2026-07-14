#!/usr/bin/env python3
"""
Lesson Injection Cycle — 周期性的经验教训注入。

每个周期：
1. 从 bus 读取所有 reflexion_lesson
2. 按角色归类 + 去重
3. 注入到对应角色的 CLAUDE.md
4. 记录日志

用法: python3 scripts/lesson_injection_cycle.py [--dry-run] [--role coordinator]
"""
import sys
import time
from pathlib import Path

SCRIPTS = Path.home() / "session-launcher" / "scripts"
SRC = Path.home() / "session-launcher" / "src"
sys.path.insert(0, str(SRC))

REPORT_DIR = Path.home() / ".hermes" / "reports" / "lessons"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = REPORT_DIR / "injection_log.jsonl"


def log_event(event: str, detail: str):
    import json
    entry = {"ts": time.time(), "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"),
             "event": event, "detail": detail}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['ts_h']}] {event}: {detail}")


def main():
    from lesson_injector import LessonInjector

    dry_run = "--dry-run" in sys.argv
    target_role = None
    for i, arg in enumerate(sys.argv):
        if arg == "--role" and i + 1 < len(sys.argv):
            target_role = sys.argv[i + 1]

    li = LessonInjector()

    # Load all lessons
    lessons = li.load_lessons(limit=500)
    if not lessons:
        log_event("no_lessons", "No reflexion_lesson found on bus")
        return

    # Group and deduplicate
    grouped = li.group_by_role(lessons)
    deduped = li.deduplicate(grouped)

    if target_role:
        # Single role
        role_lessons = list(deduped.get(target_role, []))
        if not role_lessons:
            log_event("skip", f"No lessons for {target_role}")
            return

        formatted = li.format_for_injection(target_role, role_lessons)
        if dry_run:
            log_event("dry_run", f"Would inject {len(role_lessons)} lessons to {target_role}")
            print(formatted[:300])
        else:
            ok = li.inject_to_role(target_role, formatted)
            if ok:
                log_event("injected", f"{len(role_lessons)} lessons → {target_role}")
            else:
                log_event("failed", f"Could not inject to {target_role} (CLAUDE.md not found)")
    else:
        # All roles with lessons
        total = 0
        for role, role_lessons in sorted(deduped.items()):
            formatted = li.format_for_injection(role, role_lessons)
            if dry_run:
                log_event("dry_run", f"Would inject {len(role_lessons)} lessons to {role}")
            else:
                ok = li.inject_to_role(role, formatted)
                if ok:
                    total += len(role_lessons)
                    log_event("injected", f"{len(role_lessons)} lessons → {role}")
                else:
                    log_event("skip", f"No CLAUDE.md for {role}")
        log_event("total", f"Injected {total} lessons across all roles")


if __name__ == "__main__":
    main()
