"""
ccs_config.py — CCS 全局配置管理

读取/缓存 ccs_config.json, 提供 auto_send 消息查询,
支持运行时修改（config set, auto-send add/rm）。
"""
import json
import os
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "ccs_config.json"
_CONFIG_CACHE: Optional[dict] = None


def _path() -> Path:
    return Path(os.environ.get("CCS_CONFIG_PATH", str(_CONFIG_PATH)))


def load(refresh: bool = False) -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not refresh:
        return _CONFIG_CACHE
    p = _path()
    if not p.exists():
        _CONFIG_CACHE = _defaults()
        return _CONFIG_CACHE
    with _lock:
        try:
            with open(p) as f:
                _CONFIG_CACHE = json.load(f)
        except (json.JSONDecodeError, OSError):
            _CONFIG_CACHE = _defaults()
    return _CONFIG_CACHE


def _defaults() -> dict:
    return {
        "version": 1,
        "auto_send": {"enabled": True, "interval_sec": 2.0, "messages": {"default": [], "roles": {}}},
        "routing": {"default_policy": "sticky", "overrides": {}},
        "defaults": {"drive": "ondemand", "bus_timeout": 300, "bus_track": "", "feed_cat": ""},
    }


def save() -> None:
    with _lock:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(_CONFIG_CACHE or _defaults(), f, indent=2, ensure_ascii=False)
            f.write("\n")


def invalidate_cache() -> None:
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


# ── auto_send 查询 ──

def get_auto_send_messages(role: str) -> list[str]:
    """获取某角色的 auto_send 消息列表。

    加法策略: default + 角色专用, 去重保序。
    外部调用方（core.py）再叠 persona JSON 的 auto_send_messages。
    """
    cfg = load()
    messages = cfg.get("auto_send", {}).get("messages", {})
    defaults = messages.get("default", [])
    roles_map = messages.get("roles", {})
    role_msgs = roles_map.get(role, [])
    seen = set()
    result = []
    for m in defaults + role_msgs:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def is_auto_send_enabled() -> bool:
    return load().get("auto_send", {}).get("enabled", True)


def get_interval_sec() -> float:
    return float(load().get("auto_send", {}).get("interval_sec", 2.0))


def get_default(key: str, fallback=""):
    return load().get("defaults", {}).get(key, fallback)


# ── 修改操作 ──

def set_value(key_path: str, value) -> dict:
    """通过点号路径设置值, 如 'auto_send.interval_sec' → 3.0"""
    cfg = load()
    keys = key_path.split(".")
    obj = cfg
    for k in keys[:-1]:
        if k not in obj or not isinstance(obj[k], dict):
            obj[k] = {}
        obj = obj[k]
    obj[keys[-1]] = value
    save()
    return {"success": True, "key": key_path, "value": value}


def get_value(key_path: str):
    """通过点号路径取值"""
    cfg = load()
    keys = key_path.split(".")
    obj = cfg
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def _ensure_messages_list(cfg: dict, key: str) -> list:
    """获取 auto_send.messages.{key} 列表，不存在则创建。"""
    msgs = cfg.setdefault("auto_send", {}).setdefault("messages", {})
    if key not in msgs:
        msgs[key] = []
    return msgs[key]


def add_auto_send_message(role: str, message: str) -> dict:
    if role == "default":
        cfg = load()
        lst = _ensure_messages_list(cfg, "default")
        lst.append(message)
        save()
        return {"success": True, "target": "default", "index": len(lst) - 1, "message": message}
    from routing.roles import get_role  # 延迟导入避免循环
    if not get_role(role):
        return {"success": False, "error": f"角色 '{role}' 不存在, 可用 'ccs roles' 查看"}
    cfg = load()
    roles_map = cfg.setdefault("auto_send", {}).setdefault("messages", {}).setdefault("roles", {})
    if role not in roles_map:
        roles_map[role] = []
    roles_map[role].append(message)
    save()
    return {"success": True, "role": role, "index": len(roles_map[role]) - 1, "message": message}


def remove_auto_send_message(role: str, index: int | list[int] | None = None) -> dict:
    """删除 auto_send 消息。

    - index=None:     删除整个角色的配置（或清空 default）
    - index=int:      删除该角色单条消息
    - index=list[int]: 批量删除（从大到小排序防偏移）
    """
    indices: list[int] | None = None
    single_index: int | None = None
    if isinstance(index, list):
        indices = sorted(set(index), reverse=True)
    elif index is not None:
        single_index = index

    if role == "default":
        cfg = load()
        lst = _ensure_messages_list(cfg, "default")
        if indices is not None:
            for i in indices:
                if 0 <= i < len(lst):
                    lst.pop(i)
                else:
                    return {"success": False, "error": f"索引 {i} 超出范围 (0-{len(lst)})"}
            save()
            return {"success": True, "target": "default", "removed_count": len(indices)}
        if single_index is not None:
            if single_index < 0 or single_index >= len(lst):
                return {"success": False, "error": f"索引 {single_index} 超出范围 (0-{len(lst)-1})"}
            removed = lst.pop(single_index)
            save()
            return {"success": True, "target": "default", "index": single_index, "removed": removed}
        old = list(lst)
        lst.clear()
        save()
        return {"success": True, "target": "default", "removed_count": len(old)}

    cfg = load()
    roles_map = cfg.get("auto_send", {}).get("messages", {}).get("roles", {})

    # 删除整个角色
    if indices is None and single_index is None:
        if role not in roles_map:
            return {"success": False, "error": f"角色 {role} 没有 auto_send 配置"}
        cnt = len(roles_map.pop(role))
        save()
        return {"success": True, "role": role, "removed_count": cnt}

    if role not in roles_map:
        return {"success": False, "error": f"角色 {role} 没有 auto_send 配置"}
    msgs = roles_map[role]

    # 索引偏移: rm 的用户索引按合并视图（default+role）传，减掉 default 长度得到 role 内索引
    cfg2 = load()
    offset = len(cfg2.get("auto_send", {}).get("messages", {}).get("default", []))

    def _to_role_idx(merged_idx: int) -> int:
        return merged_idx - offset

    # 批量删除
    if indices is not None:
        for i in indices:
            ri = _to_role_idx(i)
            if 0 <= ri < len(msgs):
                msgs.pop(ri)
            else:
                return {"success": False, "error": f"索引 {i} 超出范围 (合并视图 0-{len(msgs)+offset-1})"}
        save()
        return {"success": True, "role": role, "removed_count": len(indices)}

    # 单条删除
    si = _to_role_idx(single_index)
    if si < 0 or si >= len(msgs):
        return {"success": False, "error": f"索引 {single_index} 超出范围 (合并视图 0-{len(msgs)+offset-1})"}
    removed = msgs.pop(si)
    save()
    return {"success": True, "role": role, "index": single_index, "removed": removed}
