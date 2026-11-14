#!/usr/bin/env python3
"""validators.py — 角色名校验装饰器，消除 start/stop/send 等函数的重复校验。

行为变更（2026-11-14，按用户「不记名角色…不给予其他限制」需求）：
persona 注册表白名单拒绝已移除——未注册名 = 不记名角色，start/stop/send/wake
全链放行（链路一致性：起得出就要发/停得了）。只保留格式校验（防路径遍历/
shell 注入），这是安全线，不动。typo 防呆改由 core.start 对未注册名显式打印
「不记名角色（未注册，无 persona）：<名>」承担（原白名单的快速报错职责）。
"""
from functools import wraps
import re

# 合法角色名：字母数字+连字符+下划线，不含 path traversal 或 shell 注入字符
_ROLE_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-]+\Z')  # \Z 而非 $，防 role 名尾随换行注入


def validate_role(func):
    """装饰器：函數第一個位置参数為 role 或 StartOptions，自動校驗格式合法性。

    支持兩種調用方式:
      start("engineer", ...)        → role 為位置字符串
      start(StartOptions(role="engineer", ...)) → role 在 StartOptions 內

    只校验格式（_ROLE_NAME_RE）：未注册名 = 不记名角色，放行；persona
    注入与否由 get_role 有无定义决定，不在校验层拦截。
    校驗失敗返回 {"success": False, "error": "非法角色名: {role}"}。
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # 提取 role 名稱來校驗 (支持 str 或 StartOptions)
        first_arg = args[0] if args else kwargs.get('role')
        if first_arg is None:
            return {"success": False, "error": "缺少 role 参数"}
        if hasattr(first_arg, 'role'):
            role = first_arg.role
        else:
            role = first_arg
        role = str(role)
        if not _ROLE_NAME_RE.match(role):
            return {"success": False, "error": f"非法角色名: {role}"}
        return func(*args, **kwargs)
    return wrapper
