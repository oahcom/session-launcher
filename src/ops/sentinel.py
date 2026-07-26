#!/usr/bin/env python3
"""
sentinel.py — 哨兵系统（统一路径：/tmp/ccs-sentinels/）

哨兵文件写入 /tmp/ccs-sentinels/{role}.json，供外部脚本/ccs-status 读取。
watchdog 状态额外写入 /tmp/ccs-health/{role}.json（进程健康数据）。
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
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── 跨 Session 内存（omux 模式，持久化到哨兵文件）──
_MEMORY_DIR = Path("/tmp/ccs-cross-session-memory")
_MEMORY_LOCK = threading.Lock()

def _ensure_memory_dir():
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)

def record_cross_session_action(role: str, action: str, summary: str = "") -> None:
    _ensure_memory_dir()
    data = {"last_action": action, "last_ts": time.time(), "summary": summary}
    path = _MEMORY_DIR / f"{role}.json"
    with _MEMORY_LOCK:
        path.write_text(json.dumps(data))

def get_cross_session_memory(role: str) -> dict:
    path = _MEMORY_DIR / f"{role}.json"
    if not path.exists():
        return {}
    with _MEMORY_LOCK:
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

def get_all_cross_session_memories() -> dict:
    _ensure_memory_dir()
    result = {}
    with _MEMORY_LOCK:
        for path in _MEMORY_DIR.glob("*.json"):
            try:
                result[path.stem] = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return result

# ── 哨兵目录（对外暴露）──
SENTINEL_DIR = Path("/tmp/ccs-sentinels")
SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

# ── watchdog 健康状态目录（仅内部，进程健康信息）──
_HEALTH_DIR = Path("/tmp/ccs-health")
_HEALTH_DIR.mkdir(parents=True, exist_ok=True)
_HEALTH_LOCK = threading.RLock()

@dataclass
class CcsHealth:
    """健康状态字段（持久化到 /tmp/ccs-health/{role}.json）。"""
    last_watchdog_check: float = 0.0
    watchdog_ok: bool = True
    last_turn_check: float = 0.0
    last_bus_msg_age: float = -1.0
    restart_count: int = 0

@dataclass
class CcsSentinel:
    """哨兵数据——持久化到 /tmp/ccs-sentinels/{role}.json。"""
    role: str = ""
    title: str = ""
    tmux_session: str = ""
    pid: Optional[int] = None
    started_at: float = 0.0
    lifecycle: str = "infinite"
    drive: str = ""
    partners: list[str] = field(default_factory=list)
    bus_track: str = ""
    bus_timeout: int = 300
    session_id: str = ""
    engine: str = "ccs"
    health: CcsHealth = field(default_factory=CcsHealth)

    def to_dict(self) -> dict:
        return {
            "role": self.role, "title": self.title,
            "tmux_session": self.tmux_session, "pid": self.pid,
            "started_at": self.started_at, "lifecycle": self.lifecycle,
            "partners": self.partners, "bus_track": self.bus_track,
            "drive": self.drive, "bus_timeout": self.bus_timeout, "session_id": self.session_id,
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


# ── TMUX 工具（内部）──
_SESSION_ROLES_JSON = Path(os.environ.get(
    "SESSION_ROLES_ROOT", str(Path.home() / "hermes-session-roles")
)) / "personas" / "session-roles"


def _list_tmux_sessions() -> set[str]:
    """返回当前所有 tmux session 名（ccs-*/cdx-*）。"""
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return set()
        names = set()
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("ccs-") or line.startswith("cdx-"):
                names.add(line)
        return names
    except Exception:
        return set()


def _role_name_from_tmux(tmux_name: str) -> str:
    return tmux_name.removeprefix("ccs-").removeprefix("cdx-")


def _engine_from_tmux(tmux_name: str) -> str:
    return "codex" if tmux_name.startswith("cdx-") else "ccs"


def _get_role_json(role: str) -> Optional[dict]:
    if not _SESSION_ROLES_JSON.exists():
        return None
    for f in sorted(_SESSION_ROLES_JSON.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text())
            if data.get("name") == role:
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _get_pid(tmux_session: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_session}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        if r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    except Exception:
        pass
    return None


def _get_started_at(tmux_session: str) -> float:
    pid = _get_pid(tmux_session)
    if pid:
        try:
            stat = os.stat(f"/proc/{pid}")
            return stat.st_ctime
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return time.time()


# ── 哨兵文件级锁（防止多线程 read-modify-write 竞态）──
_SENTINEL_LOCK = threading.Lock()

def write_sentinel(s: CcsSentinel) -> Path:
    """写入哨兵 /tmp/ccs-sentinels/{role}.json（线程安全）。"""
    path = SENTINEL_DIR / f"{s.role}.json"
    with _SENTINEL_LOCK:
        path.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
    return path


def delete_sentinel(role: str) -> bool:
    """删除哨兵 + 健康文件（线程安全）。"""
    deleted = False
    with _SENTINEL_LOCK:
        for d in (SENTINEL_DIR, _HEALTH_DIR):
            path = d / f"{role}.json"
            if path.exists():
                path.unlink()
                deleted = True
    return deleted


def read_sentinel(role: str) -> Optional[CcsSentinel]:
    """读哨兵：文件优先 → tmux 回退（线程安全）。"""
    path = SENTINEL_DIR / f"{role}.json"
    with _SENTINEL_LOCK:
        if path.exists():
            try:
                return CcsSentinel.from_dict(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    # 回退：tmux 实时派生（文件不存在时）
    for prefix in ("ccs", "cdx"):
        tmux_name = f"{prefix}-{role}"
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", tmux_name],
                capture_output=True, timeout=3
            )
            if r.returncode == 0:
                engine = "codex" if prefix == "cdx" else "ccs"
                role_json = _get_role_json(role)
                sentinel = CcsSentinel(
                    role=role,
                    title=role_json.get("title", role) if role_json else role,
                    tmux_session=tmux_name,
                    pid=_get_pid(tmux_name),
                    started_at=_get_started_at(tmux_name),
                    lifecycle="infinite",
                    engine=engine,
                )
                if role_json:
                    # partners/bus_track 是运行时参数(不在JSON schema中)，不从此处读取
                    sentinel.lifecycle = role_json.get("lifecycle", "infinite")
                    sentinel.drive = role_json.get("drive", "")
                health_path = _HEALTH_DIR / f"{role}.json"
                if health_path.exists():
                    try:
                        hd = json.loads(health_path.read_text())
                        sentinel.health = CcsHealth(**hd)
                    except (json.JSONDecodeError, OSError, TypeError):
                        pass
                return sentinel
        except Exception:
            continue
    return None


def list_sentinels() -> list[CcsSentinel]:
    """列出哨兵：合并文件 + tmux 实时数据，活 PID 优先（线程安全）。"""
    result: dict[str, CcsSentinel] = {}

    # 1. 读文件（持锁避免读到并发写入的 torn 数据）
    for path in sorted(SENTINEL_DIR.glob("*.json")):
        role = path.stem
        with _SENTINEL_LOCK:
            try:
                s = CcsSentinel.from_dict(json.loads(path.read_text()))
                result[role] = s
            except (json.JSONDecodeError, OSError):
                pass

    # 2. 读 tmux（补充未被文件覆盖的活 session，或更新 pid）
    for tmux_name in sorted(_list_tmux_sessions()):
        role = _role_name_from_tmux(tmux_name)
        pid = _get_pid(tmux_name)
        if pid is None:
            continue  # 死 session 不覆盖
        engine = _engine_from_tmux(tmux_name)
        role_json = _get_role_json(role)
        sentinel = CcsSentinel(
            role=role,
            title=role_json.get("title", role) if role_json else role,
            tmux_session=tmux_name,
            pid=pid,
            started_at=_get_started_at(tmux_name),
            lifecycle="infinite",
            engine=engine,
        )
        if role_json:
            sentinel.lifecycle = role_json.get("lifecycle", "infinite")
            sentinel.drive = role_json.get("drive", "")
        health_path = _HEALTH_DIR / f"{role}.json"
        if health_path.exists():
            try:
                hd = json.loads(health_path.read_text())
                sentinel.health = CcsHealth(**hd)
            except (json.JSONDecodeError, OSError):
                pass
        result[role] = sentinel

    # filter out zombie sentinels (pid=0/None + empty tmux)
    return [s for s in result.values() if s.tmux_session or (s.pid or 0) > 0]


def update_health(role: str, **kwargs) -> bool:
    """更新 /tmp/ccs-health/{role}.json 中的 watchdog 状态。"""
    with _HEALTH_LOCK:
        path = _HEALTH_DIR / f"{role}.json"
        health = {}
        if path.exists():
            try:
                health.update(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        health.update(kwargs)
        path.write_text(json.dumps(health))
        return True
