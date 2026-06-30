#!/usr/bin/env python3
"""
Session Launcher 完整入口：启动时自动运行角色匹配，输出启动配置供 shell eval。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import load_roles, has_work
from signals import check_signal_by_name


def check_role_signals(role: dict) -> bool:
    """检查角色的所有 input_signals。"""
    signals = role.get("input_signals", [])
    if not signals:
        return False

    for signal in signals:
        source = signal.get("source", "")
        filter_str = signal.get("filter", "")

        # Map source to checker name
        if "bus_client.py unread" in source:
            if check_signal_by_name("bus_unread", filter_str):
                return True
        elif "systemctl" in source and "is-active" in source:
            if check_signal_by_name("systemctl_active", filter_str):
                return True
        elif "curl" in source:
            if check_signal_by_name("http_health", filter_str):
                return True
        elif "journalctl" in source:
            if check_signal_by_name("journalctl_errors", filter_str):
                return True
        elif "git diff" in source:
            if check_signal_by_name("git_staged", filter_str):
                return True
        elif "ls -lt" in source:
            if check_signal_by_name("session_size", filter_str):
                return True
        elif "ps aux" in source:
            if check_signal_by_name("running_sessions", filter_str):
                return True

    return False


def main():
    # 读取角色
    from launcher import load_roles as lr
    roles = lr()

    # 检查每个角色是否有工作
    active_roles = []
    for role in roles:
        if check_role_signals(role):
            active_roles.append(role)

    if not active_roles:
        print("# No work for any session role. Exiting.")
        print("exit 0")
        return

    # 优先级：先处理 infinite cron 角色，再 ondemand
    priority_order = ["cron", "loop", "ondemand"]
    active_roles.sort(key=lambda r: priority_order.index(r.get("drive", "ondemand")))

    # 选择第一个有工作的角色（优先级最高的）
    selected = active_roles[0]

    # 生成启动配置
    prompt = selected.get("system_prompt", "").format(
        persona_name=selected["name"],
        persona_title=selected["title"]
    )

    print(f"export SESSION_ROLE={selected['name']}")
    print(f"export SESSION_ROLE_TITLE={selected['title']}")
    print(f"export SESSION_LIFECYCLE={selected['lifecycle']}")
    print(f"export SESSION_DRIVE={selected['drive']}")

    if selected.get("cron_schedule"):
        print(f"export SESSION_CRON='{selected['cron_schedule']}'")

    # 通过 launcher.inject_prompt_into_claudemd 写入 CLAUDE.md
    from launcher import inject_prompt_into_claudemd, write_lifecycle_sentinel
    inj_status = inject_prompt_into_claudemd(selected)
    print(f"# Prompt injection: {inj_status}")

    # ondemand 角色写哨兵
    if selected.get("lifecycle") == "ondemand":
        write_lifecycle_sentinel(selected)
        print(f"# Lifecycle: ondemand — sentinel created")

    # 给用户看的信息
    print(f"# Session Role: {selected['title']} ({selected['name']})")
    print(f"# Lifecycle: {selected['lifecycle']}, Drive: {selected['drive']}")

    if selected["drive"] == "cron":
        print(f"# CronCreate will be: CronCreate(cron=\"{selected['cron_schedule']}\", prompt=..., recurring=true)")
    elif selected["drive"] == "loop":
        print(f"# /loop will be used with this role's prompt")


if __name__ == "__main__":
    main()