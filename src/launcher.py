#!/usr/bin/env python3
"""
Session Launcher — session 启动时自动匹配角色、注入 prompt、控制生命周期。

依赖：hermes-session-roles（项目 A 的 CLI + 数据文件）
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 确保本项目模块可以从任意 CWD 导入
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


SESSION_ROLES_ROOT = Path("/home/administrator/hermes-session-roles")
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()
CLAUDE_MD = Path(os.environ.get("CLAUDE_MD_PATH",
                  "~/.claude/projects/-home-administrator/CLAUDE.md")).expanduser()
SESSION_MARKER_START = "<!-- SESSION_ROLE:START -->"
SESSION_MARKER_END = "<!-- SESSION_ROLE:END -->"


def load_roles() -> list[dict]:
    """读取角色定义。"""
    roles = []
    for f in sorted(SESSION_ROLES_ROOT.glob("personas/session-roles/persona_*.json")):
        with open(f) as fp:
            roles.append(json.load(fp))
    return roles


def check_signal(signal: dict) -> bool:
    """执行 input_signals 判断是否有任务。

    统一使用 signals.check_signal_by_name，避免重复逻辑。
    """
    from signals import check_signal_by_name

    source = signal.get("source", "")
    filter_str = signal.get("filter", "")

    # Map source to signals.py checker name
    if "bus_client.py" in source:
        return check_signal_by_name("bus_unread", filter_str)
    elif "systemctl" in source and "is-active" in source:
        return check_signal_by_name("systemctl_active", filter_str)
    elif "curl" in source:
        return check_signal_by_name("http_health", filter_str)
    elif "journalctl" in source:
        return check_signal_by_name("journalctl_errors", filter_str)
    elif "git diff" in source:
        return check_signal_by_name("git_staged", filter_str)
    elif "meminfo" in source or "df /" in source:
        return check_signal_by_name("mem_disk", filter_str)
    elif "ls -lt" in source:
        return check_signal_by_name("session_size", filter_str)
    elif "ps aux" in source:
        return check_signal_by_name("running_sessions", filter_str)

    return False


def has_work(roles: list[dict]) -> Optional[dict]:
    """检查是否有角色有工作。返回第一个有工作的角色。"""
    for role in roles:
        signals = role.get("input_signals", [])
        if not signals:
            continue

        for signal in signals:
            if check_signal(signal):
                return role
    return None


def inject_prompt_into_claudemd(role: dict) -> str:
    """将角色的 system_prompt 通过 marker 注入到 CLAUDE.md。

    在 <!-- SESSION_ROLE:START --> 和 <!-- SESSION_ROLE:END -->
    之间插入角色 prompt。若 marker 不存在则在文件末尾追加。
    """
    prompt = role.get("system_prompt", "").format(
        persona_name=role["name"],
        persona_title=role["title"]
    )
    lifecycle = role.get("lifecycle", "infinite")
    drive = role.get("drive", "cron")

    # 构建注入块
    inject_block = (
        f"{SESSION_MARKER_START}\n"
        f"# Session Role: {role['name']} ({role['title']})\n"
        f"# Lifecycle: {lifecycle} | Drive: {drive}\n"
        f"# 自动注入 — 由 session-launcher 管理\n\n"
        f"{prompt}\n\n"
        f"{SESSION_MARKER_END}\n"
    )

    if not CLAUDE_MD.exists():
        # 文件不存在则新建
        CLAUDE_MD.write_text(inject_block)
        return "created"

    content = CLAUDE_MD.read_text(encoding="utf-8")

    if SESSION_MARKER_START in content and SESSION_MARKER_END in content:
        # 替换现有 marker 块
        start = content.index(SESSION_MARKER_START)
        end = content.index(SESSION_MARKER_END) + len(SESSION_MARKER_END)
        new_content = content[:start] + inject_block + content[end:]
    elif SESSION_MARKER_START in content:
        # 只有起始标记，补充结束标记
        start = content.index(SESSION_MARKER_START)
        new_content = content[:start] + inject_block
    else:
        # 追加到文件末尾
        new_content = content.rstrip() + "\n\n" + inject_block

    CLAUDE_MD.write_text(new_content, encoding="utf-8")

    # 额外写入 /tmp/session_role_prompt.txt
    prompt_file = Path("/tmp/session_role_prompt.txt")
    prompt_file.write_text(prompt)

    return "injected"


def clear_injected_prompt() -> None:
    """清除 CLAUDE.md 中的 session role 注入块。"""
    if not CLAUDE_MD.exists():
        return
    content = CLAUDE_MD.read_text(encoding="utf-8")
    if SESSION_MARKER_START not in content:
        return

    start = content.index(SESSION_MARKER_START)
    if SESSION_MARKER_END in content:
        end = content.index(SESSION_MARKER_END) + len(SESSION_MARKER_END)
    else:
        end = len(content)

    new_content = content[:start] + content[end:]
    CLAUDE_MD.write_text(new_content, encoding="utf-8")


def write_lifecycle_sentinel(role: dict) -> None:
    """为 ondemand 角色写生命周期哨兵文件，session 退出时自动清理。"""
    lifecycle = role.get("lifecycle", "infinite")
    max_minutes = role.get("max_minutes", 30)
    sentinel_dir = Path("/tmp/session-launcher")
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    sentinel = sentinel_dir / f"{role['name']}.active"
    sentinel.write_text(json.dumps({
        "role": role["name"],
        "title": role["title"],
        "lifecycle": lifecycle,
        "pid": os.getpid(),
        "max_minutes": max_minutes if lifecycle == "ondemand" else None,
        "started_at": subprocess.run(
            ["date", "-Iseconds"], capture_output=True, text=True
        ).stdout.strip(),
    }))
    # ponytail: 若要跨 session 持久化，改用 ~/.hermes/state/ 目录


def check_ondemand_timeout(max_minutes: int = 30) -> list[str]:
    """检查 ondemand 角色是否超时，超时返回角色名列表。"""
    sentinel_dir = Path("/tmp/session-launcher")
    if not sentinel_dir.exists():
        return []
    from datetime import datetime, timezone, timedelta
    timed_out = []
    for f in sentinel_dir.glob("*.active"):
        try:
            data = json.loads(f.read_text())
            if data.get("lifecycle") != "ondemand":
                continue
            started_at = data.get("started_at")
            if not started_at:
                continue
            started = datetime.fromisoformat(started_at)
            elapsed = datetime.now(timezone.utc) - started
            max_m = data.get("max_minutes", max_minutes)
            if elapsed > timedelta(minutes=max_m):
                timed_out.append(data["role"])
                f.unlink()
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return timed_out


def cleanup_stale_sentinels() -> list[str]:
    """清理孤儿哨兵文件（进程已退出或 ondemand 超时的遗留标记）。返回清理角色名列表。"""
    sentinel_dir = Path("/tmp/session-launcher")
    if not sentinel_dir.exists():
        return []
    cleaned = []
    for f in sentinel_dir.glob("*.active"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            # 进程已退出 → 孤儿
            if pid and not Path(f"/proc/{pid}").exists():
                f.unlink()
                cleaned.append(data["role"])
                continue
            # ondemand 超时 → 强制清理
            lifecycle = data.get("lifecycle")
            if lifecycle == "ondemand":
                from datetime import datetime, timezone, timedelta
                started_at = data.get("started_at")
                if started_at:
                    started = datetime.fromisoformat(started_at)
                    elapsed = datetime.now(timezone.utc) - started
                    max_m = data.get("max_minutes", 30)
                    if elapsed > timedelta(minutes=max_m):
                        f.unlink()
                        cleaned.append(f"{data['role']}(timeout)")
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return cleaned


def main():
    # 启动前清理残留哨兵
    stale = cleanup_stale_sentinels()
    if stale:
        print(f"# Cleaned stale sentinels: {', '.join(stale)}", file=sys.stderr)

    roles = load_roles()
    if not roles:
        print("No roles loaded", file=sys.stderr)
        sys.exit(1)

    # 检查有没有工作
    active_role = has_work(roles)

    if active_role:
        lifecycle = active_role.get("lifecycle", "infinite")
        drive = active_role.get("drive", "cron")
        cron_schedule = active_role.get("cron_schedule", "")

        # 1. 注入 prompt 到 CLAUDE.md
        inj_status = inject_prompt_into_claudemd(active_role)
        print(f"# Prompt injection: {inj_status}")

        # 2. 写生命周期哨兵
        if lifecycle == "ondemand":
            write_lifecycle_sentinel(active_role)
            print(f"# Lifecycle: ondemand — sentinel created, will auto-exit after task")

        # 3. 生成启动指令
        prompt = active_role.get("system_prompt", "").format(
            persona_name=active_role["name"],
            persona_title=active_role["title"]
        )

        if drive == "cron" and cron_schedule:
            startup_cmd = f"CronCreate(cron=\"{cron_schedule}\", prompt=\"\"\"{prompt}\"\"\", recurring=true)"
        elif drive == "loop":
            startup_cmd = f"/loop \"{prompt[:200]}...\""
        else:
            startup_cmd = f"# {prompt[:200]}..."

        print(f"Active role: {active_role['name']} ({active_role['title']})")
        print(f"Startup command: {startup_cmd}")
        print(f"export SESSION_ROLE={active_role['name']}")
        print(f"export SESSION_STARTUP='{startup_cmd}'")
        print(f"export SESSION_LIFECYCLE={lifecycle}")
        print(f"export SESSION_DRIVE={drive}")

        # 写入 /tmp/session_role_env.sh
        env_file = Path("/tmp/session_role_env.sh")
        env_file.write_text(
            f"export SESSION_ROLE={active_role['name']}\n"
            f"export SESSION_STARTUP='{startup_cmd}'\n"
            f"export SESSION_LIFECYCLE={lifecycle}\n"
            f"export SESSION_DRIVE={drive}\n"
        )
    else:
        # 无工作 → 清除旧注入，避免 stale prompt
        clear_injected_prompt()
        print("# No work for any session role. Exiting.")
        print("exit 0")
        sys.exit(0)


if __name__ == "__main__":
    main()