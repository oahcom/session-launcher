#!/usr/bin/env python3
"""
Session Launcher — session 启动时自动匹配角色、注入 prompt、控制生命周期。

依赖：hermes-session-roles（项目 A 的 CLI + 数据文件）
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


SESSION_ROLES_ROOT = Path("/home/administrator/hermes-session-roles")
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()


def load_roles() -> list[dict]:
    """读取角色定义。"""
    roles = []
    for f in sorted(SESSION_ROLES_ROOT.glob("personas/session-roles/persona_*.json")):
        with open(f) as fp:
            roles.append(json.load(fp))
    return roles


def check_signal(signal: dict) -> bool:
    """执行 input_signals 判断是否有任务。"""
    source = signal.get("source", "")
    filter_str = signal.get("filter", "")

    try:
        if "bus_client.py" in source:
            result = subprocess.run(
                ["python3", str(BUS_CLIENT)] + source.split(),
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout
            if filter_str and filter_str not in output:
                return False
            return "unread" not in output.lower() or "0 unread" not in output.lower()

        elif "systemctl" in source:
            result = subprocess.run(
                source.split(), capture_output=True, text=True, timeout=5
            )
            if filter_str:
                for f in filter_str.split("|"):
                    if f in result.stdout:
                        return True
                return False
            return result.returncode == 0

        elif "curl" in source:
            # Check multiple URLs
            for url in source.split():
                if "http" in url:
                    result = subprocess.run(
                        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "3", url],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.stdout != "200":
                        return True  # 非 200 就是有问题
            return False

        elif "journalctl" in source:
            result = subprocess.run(
                source.split(), capture_output=True, text=True, timeout=10
            )
            if filter_str:
                for f in filter_str.split("|"):
                    if f.lower() in result.stdout.lower():
                        return True
                return False
            return result.returncode == 0

        elif "git diff" in source:
            result = subprocess.run(
                source.split(), capture_output=True, text=True, timeout=5
            )
            return bool(result.stdout.strip())

        elif "ls -lt" in source or "ps aux" in source:
            result = subprocess.run(
                source, shell=True, capture_output=True, text=True, timeout=5
            )
            return len(result.stdout.strip().split("\n")) > 2

    except subprocess.TimeoutExpired:
        return False
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


def inject_prompt(role: dict, output_path: str = "~/.claude/projects/-home-administrator/CLAUDE.md"):
    """将角色的 system_prompt 注入到 CLAUDE.md 启动块。"""
    # 这里只返回 prompt，实际注入由 shell 完成
    prompt = role.get("system_prompt", "").format(
        persona_name=role["name"],
        persona_title=role["title"]
    )

    # 生成启动指令
    lifecycle = role.get("lifecycle", "infinite")
    drive = role.get("drive", "cron")
    cron_schedule = role.get("cron_schedule", "")

    if drive == "cron" and cron_schedule:
        startup_cmd = f'CronCreate(cron="{cron_schedule}", prompt="""{prompt}""", recurring=true)'
    elif drive == "loop":
        startup_cmd = f"/loop \"{prompt[:200]}...\""
    else:
        startup_cmd = f"# {prompt[:200]}..."

    return startup_cmd


def main():
    roles = load_roles()
    if not roles:
        print("No roles loaded", file=sys.stderr)
        sys.exit(1)

    # 检查有没有工作
    active_role = has_work(roles)

    if active_role:
        print(f"Active role: {active_role['name']} ({active_role['title']})")
        startup_cmd = inject_prompt(active_role)
        print(f"Startup command: {startup_cmd}")
        # 输出到环境变量或文件供 shell 读取
        print(f"export SESSION_ROLE={active_role['name']}")
        print(f"export SESSION_STARTUP='{startup_cmd}'")
    else:
        print("No work for any role. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()