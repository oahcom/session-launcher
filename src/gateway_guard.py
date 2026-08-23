#!/usr/bin/env python3
"""
gateway_guard.py — 红线1 代码化拦截器

红线1："绝对禁止重启 hermes-gateway（SIGKILL → 长连接中断 5-30min）"

本模块检测任意命令/指令文本中是否包含 hermes-gateway 重启意图，
在指令执行前进行拦截。防止因误操作或脚本缺陷导致
hermes-gateway 被 SIGKILL/强制重启，从而中断 WeChat 等长连接，
造成所有 agent session 断开 5~30 分钟。

仅使用 stdlib re。
"""
import re

# 红线1 模式：匹配任何意图重启/杀死 hermes-gateway 的指令。
# 大小写不敏感，容忍多位空格，覆盖常见 shell 表单。
_FORBIDDEN_PATTERNS = [
    # hermes-gateway 重启/停止/强杀 系列
    re.compile(r"hermes\s*[-]?gateway\s*(restart|stop|kill|killall|terminate)", re.IGNORECASE),
    # 泛 gateway 重启 + force
    re.compile(r"gateway\s+restart\s+--force", re.IGNORECASE),
    re.compile(r"gateway\s+restart", re.IGNORECASE),
    # systemctl 操作 gateway / hermes-gateway
    re.compile(r"systemctl\s+\S+\s+\S*gateway", re.IGNORECASE),
    re.compile(r"systemctl\s+\S+\s+hermes\s*[-]?gateway", re.IGNORECASE),
    # kill / killall / SIGKILL 针对 gateway
    re.compile(r"kill\s+.*gateway", re.IGNORECASE),
    re.compile(r"killall\s+.*gateway", re.IGNORECASE),
    re.compile(r"SIGKILL.*gateway", re.IGNORECASE),
    re.compile(r"gateway.*SIGKILL", re.IGNORECASE),
    # 显式 hermes-gateway 被杀
    re.compile(r"hermes\s*[-]?gateway.*kill", re.IGNORECASE),
    re.compile(r"kill.*hermes\s*[-]?gateway", re.IGNORECASE),
]


def is_forbidden_command(text: str) -> bool:
    """
    检测指令文本是否含 hermes-gateway 重启意图。

    参数:
        text: 待检测的指令/命令文本。

    返回:
        True  — 违规，包含红线1 重启意图。
        False — 安全，无红线1 匹配。
    """
    if not text:
        return False
    return any(p.search(text) for p in _FORBIDDEN_PATTERNS)


def guard_command(text: str) -> tuple[bool, str]:
    """
    红线1 校验：判断指令是否允许执行。

    参数:
        text: 待校验的指令/命令文本。

    返回:
        (True, "")       — 允许执行，无违规。
        (False, reason)  — 拒绝执行，reason 为红线1 拒绝原因。
    """
    if is_forbidden_command(text):
        reason = (
            "红线1：禁止重启 hermes-gateway。"
            "SIGKILL 会中断 WeChat 等长连接，"
            "导致所有 agent session 断开 5~30 分钟。"
            f"检测指令: {text!r}"
        )
        return (False, reason)
    return (True, "")
