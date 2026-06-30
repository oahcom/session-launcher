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


def _safe_run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """安全执行外部命令，统一超时/异常处理。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        # ponytail: 暂返回空结果，后续可加重试逻辑
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))


def check_signal(signal: dict) -> bool:
    """执行 input_signals 判断是否有任务。

    shlex.split 替代脆弱的 str.split()，避免命令参数带空格时出错。
    """
    source = signal.get("source", "")
    filter_str = signal.get("filter", "")

    try:
        if "bus_client.py" in source:
            parts = shlex.split(source)
            result = _safe_run(["python3", str(BUS_CLIENT)] + parts, timeout=10)
            output = result.stdout
            if filter_str and filter_str not in output:
                return False
            return "unread" not in output.lower() or "0 unread" not in output.lower()

        elif "systemctl" in source:
            result = _safe_run(shlex.split(source), timeout=5)
            if filter_str:
                for f in filter_str.split("|"):
                    if f in result.stdout:
                        return True
                return False
            # returncode=0 表示 active（正常），非 0 才是有异常/有工作
            return result.returncode != 0

        elif "curl" in source:
            for url in shlex.split(source):
                if "http" in url:
                    result = _safe_run(
                        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                         "--connect-timeout", "3", url],
                        timeout=3
                    )
                    if result.stdout != "200":
                        return True
            return False

        elif "journalctl" in source:
            result = _safe_run(shlex.split(source), timeout=10)
            if filter_str:
                for f in filter_str.split("|"):
                    if f.lower() in result.stdout.lower():
                        return True
                return False
            return result.returncode == 0

        elif "git diff" in source:
            result = _safe_run(shlex.split(source), timeout=5)
            return bool(result.stdout.strip())

        elif "ls -lt" in source or "ps aux" in source:
            result = _safe_run(source.split() if "|" not in source else source,
                               timeout=5)
            return len(result.stdout.strip().split("\n")) > 2

    except Exception:
        return False

    return True


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
    sentinel_dir = Path("/tmp/session-launcher")
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    sentinel = sentinel_dir / f"{role['name']}.active"
    sentinel.write_text(json.dumps({
        "role": role["name"],
        "title": role["title"],
        "lifecycle": lifecycle,
        "pid": os.getpid(),
        "started_at": subprocess.run(
            ["date", "-Iseconds"], capture_output=True, text=True
        ).stdout.strip(),
    }))
    # ponytail: 若要跨 session 持久化，改用 ~/.hermes/state/ 目录


def cleanup_stale_sentinels() -> list[str]:
    """清理孤儿哨兵文件（进程已退出的遗留标记）。返回清理数量。"""
    sentinel_dir = Path("/tmp/session-launcher")
    if not sentinel_dir.exists():
        return []
    cleaned = []
    for f in sentinel_dir.glob("*.active"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            if pid and not Path(f"/proc/{pid}").exists():
                f.unlink()
                cleaned.append(data["role"])
        except (json.JSONDecodeError, OSError):
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
    else:
        # 无工作 → 清除旧注入，避免 stale prompt
        clear_injected_prompt()
        print("No work for any role. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()