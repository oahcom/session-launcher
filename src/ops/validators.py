#!/usr/bin/env python3
"""validators.py — 角色名校验装饰器，消除 start/stop/send 等函数的重复校验。"""

from functools import wraps
import json
import re
from pathlib import Path

# 合法角色名：字母数字+连字符+下划线，不含 path traversal 或 shell 注入字符
_ROLE_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')

# 从注册表加载合法角色名
_SESSION_ROLES_ROOT = Path.home() / "hermes-session-roles" / "personas" / "session-roles"
_KNOWN_ROLES: set[str] = set()
if _SESSION_ROLES_ROOT.exists():
    for _f in _SESSION_ROLES_ROOT.glob("persona_*.json"):
        try:
            _KNOWN_ROLES.add(json.loads(_f.read_text())["name"])
        except Exception:
            pass


def validate_role(func):
    """装饰器：函數第一個位置参数為 role 或 StartOptions，自動校驗合法性。

    支持兩種調用方式:
      start("engineer", ...)        → role 為位置字符串
      start(StartOptions(role="engineer", ...)) → role 在 StartOptions 內

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
        if _KNOWN_ROLES and role not in _KNOWN_ROLES:
            return {"success": False, "error": f"角色 {role} 不在注册表中 (known: {sorted(_KNOWN_ROLES)})"}
        return func(*args, **kwargs)
    return wrapper


def clear_role_cache():
    """运行时清除角色缓存，供 coordinator 调用"""
    global _KNOWN_ROLES
    _KNOWN_ROLES = set()
