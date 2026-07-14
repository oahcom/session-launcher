#!/usr/bin/env python3
"""
sentinel.py — 哨兵文件管理

读/写/清理 /tmp/ccs-sentinels/<role>.json 哨兵文件。
哨兵记录 CCS 的完整运行状态，供其他模块查询。
"""
__all__ = [
    'CcsSentinel',
    'CcsHealth',
    'write_sentinel',
    'read_sentinel',
    'delete_sentinel',
    'list_sentinels',
    'update_health',
    'SENTINEL_DIR',
    'record_cross_session_action',
    'get_cross_session_memory',
    'get_all_cross_session_memories',
]

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── 跨 Session 内存（omux 模式）──
# 记录每个角色上次操作摘要，供其他 session 引用
_CROSS_SESSION_MEMORY: dict[str, dict] = {}  # role -> {last_action, last_ts, summary}

def record_cross_session_action(role: str, action: str, summary: str = "") -> None:
    _CROSS_SESSION_MEMORY[role] = {
        "last_action": action,
        "last_ts": __import__("time").time(),
        "summary": summary,
    }

def get_cross_session_memory(role: str) -> dict:
    return _CROSS_SESSION_MEMORY.get(role, {})

def get_all_cross_session_memories() -> dict:
    return dict(_CROSS_SESSION_MEMORY)

SENTINEL_DIR = Path("/tmp/ccs-sentinels")


@dataclass
class CcsHealth:
    """哨兵内的健康状态字段。"""
    last_watchdog_check: float = 0.0
    watchdog_ok: bool = True
    last_turn_check: float = 0.0
    last_bus_msg_age: float = -1.0
    restart_count: int = 0


@dataclass
class CcsSentinel:
    """哨兵文件的完整结构。"""
    role: str = ""
    title: str = ""
    tmux_session: str = ""
    pid: Optional[int] = None
    started_at: float = 0.0
    lifecycle: str = "infinite"
    partners: list[str] = field(default_factory=list)
    bus_track: str = ""
    bus_timeout: int = 300
    session_id: str = ""
    engine: str = "ccs"
    health: CcsHealth = field(default_factory=CcsHealth)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "title": self.title,
            "tmux_session": self.tmux_session,
            "pid": self.pid,
            "started_at": self.started_at,
            "lifecycle": self.lifecycle,
            "partners": self.partners,
            "bus_track": self.bus_track,
            "bus_timeout": self.bus_timeout,
            "session_id": self.session_id,
            "engine": self.engine,
            "health": {
                "last_watchdog_check": self.health.last_watchdog_check,
                "watchdog_ok": self.health.watchdog_ok,
                "last_turn_check": self.health.last_turn_check,
                "last_bus_msg_age": self.health.last_bus_msg_age,
                "restart_count": self.health.restart_count,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CcsSentinel":
        h = data.get("health", {})
        health = CcsHealth(
            last_watchdog_check=h.get("last_watchdog_check", 0.0),
            watchdog_ok=h.get("watchdog_ok", True),
            last_turn_check=h.get("last_turn_check", 0.0),
            last_bus_msg_age=h.get("last_bus_msg_age", -1.0),
            restart_count=h.get("restart_count", 0),
        )
        # 兼容旧版哨兵：旧文件可能用了单字符串 partner
        partners_raw = data.get("partners", data.get("partner", ""))
        if isinstance(partners_raw, str):
            partners = [partners_raw] if partners_raw else []
        else:
            partners = list(partners_raw) if partners_raw else []
        return cls(
            role=data.get("role", ""),
            title=data.get("title", ""),
            tmux_session=data.get("tmux_session", ""),
            pid=data.get("pid"),
            started_at=data.get("started_at", 0.0),
            lifecycle=data.get("lifecycle", "infinite"),
            partners=partners,
            bus_track=data.get("bus_track", ""),
            bus_timeout=data.get("bus_timeout", 300),
            session_id=data.get("session_id", ""),
            engine=data.get("engine", "ccs"),
            health=health,
        )


# ── 操作函数 ──────────────────────────────────────────────────

_WRITE_LOCK = threading.RLock()

def write_sentinel(s: CcsSentinel) -> Path:
    """线程安全写入哨兵文件（加锁 + 原子替换）。"""
    with _WRITE_LOCK:
        SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
        path = SENTINEL_DIR / f"{s.role}.json"
        # 合并旧文件的 session_id（避免被并发线程覆盖）
        if path.exists():
            try:
                old = json.loads(path.read_text())
                if not s.session_id and old.get("session_id"):
                    s.session_id = old["session_id"]
            except (json.JSONDecodeError, OSError):
                pass
        content = json.dumps(s.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(SENTINEL_DIR), suffix='.json', prefix=f"{s.role}_")
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        # os.replace 在 POSIX 上是原子的，无需先 unlink
        os.replace(tmp, path)
        return path


def read_sentinel(role: str) -> Optional[CcsSentinel]:
    """读哨兵文件。文件不存在返回 None。"""
    path = SENTINEL_DIR / f"{role}.json"
    if not path.exists():
        return None
    try:
        return CcsSentinel.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def delete_sentinel(role: str) -> bool:
    """删除哨兵文件。返回是否成功。"""
    with _WRITE_LOCK:
        path = SENTINEL_DIR / f"{role}.json"
        if path.exists():
            path.unlink()
            return True
        return False


def list_sentinels() -> list[CcsSentinel]:
    """读取所有哨兵文件。"""
    SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for f in sorted(SENTINEL_DIR.glob("*.json")):
        try:
            result.append(CcsSentinel.from_dict(json.loads(f.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return result


def update_health(role: str, **kwargs) -> bool:
    """更新哨兵的 health 字段（线程安全，不覆盖其他字段）。"""
    with _WRITE_LOCK:
        s = read_sentinel(role)
        if not s:
            return False
        for key, val in kwargs.items():
            if hasattr(s.health, key):
                setattr(s.health, key, val)
        write_sentinel(s)
        return True
