#!/usr/bin/env python3
"""
sentinel.py — 哨兵系统（基于 tmux 运行时，无持久化 JSON 文件）

所有数据从 tmux sessions + 角色 JSON 实时派生。
watchdog 状态（restart_count, watchdog_ok）写入 /tmp/ccs-health/{role}.json。
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
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── 跨 Session 内存（omux 模式，进程内）──
_CROSS_SESSION_MEMORY: dict[str, dict] = {}

def record_cross_session_action(role: str, action: str, summary: str = "") -> None:
    _CROSS_SESSION_MEMORY[role] = {
        "last_action": action,
        "last_ts": time.time(),
        "summary": summary,
    }

def get_cross_session_memory(role: str) -> dict:
    return _CROSS_SESSION_MEMORY.get(role, {})

def get_all_cross_session_memories() -> dict:
    return dict(_CROSS_SESSION_MEMORY)


# ── watchdog 状态存储（唯一需要持久化的运行时状态）──
_HEALTH_DIR = Path("/tmp/ccs-health")
_HEALTH_DIR.mkdir(parents=True, exist_ok=True)
_HEALTH_LOCK = threading.RLock()
# 哨兵只读接口兼容（以前从 /tmp/ccs-sentinels 读，现在从 /tmp/ccs-health 读 watch 数据）
# 但 SENTINEL_DIR 不再用于读写，仅用于兼容旧引用
SENTINEL_DIR = _HEALTH_DIR


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
    """哨兵数据——运行时派生，仅 memory 中转，不持久化。
    保留此 dataclass 仅用于 API 兼容，所有字段由 tmux + 角色 JSON 实时填充。"""
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
            "role": self.role, "title": self.title,
            "tmux_session": self.tmux_session, "pid": self.pid,
            "started_at": self.started_at, "lifecycle": self.lifecycle,
            "partners": self.partners, "bus_track": self.bus_track,
            "bus_timeout": self.bus_timeout, "session_id": self.session_id,
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


# ── TMUX_SESSIONS_ROOT ──
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
        # 只保留 ccs- 和 cdx- 前缀的 session
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
    """从 hermes-session-roles 读角色定义。"""
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
    """通过 tmux display-message 获取 pane PID（不精确匹配子进程）。"""
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
    """估算 session 启动时间——用文件修改时间近似。"""
    pid = _get_pid(tmux_session)
    if pid:
        try:
            import os
            # stat /proc/{pid} 的 create time
            stat = os.stat(f"/proc/{pid}")
            return stat.st_ctime
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return time.time()


# ── API 函数（旧版兼容，无 JSON 哨兵文件）──


def list_sentinels() -> list[CcsSentinel]:
    """列出所有运行中的 CCS/Codex sessions（从 tmux 实时派生）。"""
    sessions = _list_tmux_sessions()
    result = []
    for tmux_name in sorted(sessions):
        role = _role_name_from_tmux(tmux_name)
        engine = _engine_from_tmux(tmux_name)
        role_json = _get_role_json(role)

        # 基础字段
        sentinel = CcsSentinel(
            role=role,
            title=role_json.get("title", role) if role_json else role,
            tmux_session=tmux_name,
            pid=_get_pid(tmux_name),
            started_at=_get_started_at(tmux_name),
            lifecycle="infinite",
            engine=engine,
        )

        # 从角色 JSON 补 partners/bus_track
        if role_json:
            sentinel.partners = role_json.get("partners", [])
            sentinel.bus_track = role_json.get("bus_track", "")

        # 读取 watchdog 健康状态
        health_path = _HEALTH_DIR / f"{role}.json"
        if health_path.exists():
            try:
                hd = json.loads(health_path.read_text())
                sentinel.health = CcsHealth(**{k: hd.get(k, v) for k, v in {
                    "last_watchdog_check": 0.0, "watchdog_ok": True,
                    "last_turn_check": 0.0, "last_bus_msg_age": -1.0,
                    "restart_count": 0,
                }.items()})
            except (json.JSONDecodeError, OSError):
                pass

        result.append(sentinel)
    return result


def read_sentinel(role: str) -> Optional[CcsSentinel]:
    """读特定角色哨兵（从 tmux 实时派生）。"""
    # 查 ccs-{role} 或 cdx-{role}
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
                    sentinel.partners = role_json.get("partners", [])
                    sentinel.bus_track = role_json.get("bus_track", "")
                health_path = _HEALTH_DIR / f"{role}.json"
                if health_path.exists():
                    try:
                        hd = json.loads(health_path.read_text())
                        sentinel.health = CcsHealth(**hd)
                    except (json.JSONDecodeError, OSError):
                        pass
                return sentinel
        except Exception:
            continue
    return None


def write_sentinel(s: CcsSentinel) -> Path:
    """空操作——哨兵数据不从文件读写。保留仅用于 API 兼容。"""
    return _HEALTH_DIR / f"{s.role}.json"


def delete_sentinel(role: str) -> bool:
    """删除健康状态文件（非哨兵文件，哨兵不存在）。"""
    path = _HEALTH_DIR / f"{role}.json"
    if path.exists():
        path.unlink()
        return True
    return False


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
