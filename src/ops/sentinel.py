#!/usr/bin/env python3
"""
sentinel.py — 哨兵系统

哨兵文件写入 {SENTINEL_DIR}/{role}.json，供外部脚本/ccs-status 读取。
watchdog 状态额外写入 {_HEALTH_DIR}/{role}.json（进程健康数据）。
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
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("sentinel")


def _get_role_json(role: str) -> Optional[dict]:
    """按名称获取角色定义（统一走 routing.roles.get_role）。

    与 roles.py 共用 shared_loader 加载管线，避免哨兵独立 glob 导致
    解析逻辑分叉（曾在此处直接读 persona JSON）。
    延迟导入防循环依赖：partner → sentinel → roles，模块级导入会打结。
    """
    from routing.roles import get_role as _gr
    return _gr(role)


# 延迟导入 parse_tmux_name（在 _role_name_from_tmux 和 list_sentinels 中用到）
# 在模块级别不导入，避免循环依赖

# ── 跨 Session 内存（omux 模式，持久化到哨兵文件）──
_MEMORY_DIR = Path.home() / ".hermes" / "run" / "ccs-cross-session-memory"
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
SENTINEL_DIR = Path.home() / ".hermes" / "run" / "ccs-sentinels"
SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

# ── watchdog 健康状态目录（仅内部，进程健康信息）──
_HEALTH_DIR = Path.home() / ".hermes" / "run" / "ccs-health"
_HEALTH_DIR.mkdir(parents=True, exist_ok=True)
_HEALTH_LOCK = threading.RLock()

@dataclass
class CcsHealth:
    """健康状态字段（持久化到 ~/.hermes/run/ccs-health/{role}.json）。"""
    last_watchdog_check: float = 0.0
    watchdog_ok: bool = True
    last_turn_check: float = 0.0
    last_bus_msg_age: float = -1.0
    restart_count: int = 0

@dataclass
class CcsSentinel:
    """哨兵数据——持久化到 {SENTINEL_DIR}/{role}.json (instance_id=0) 或 {role}-{id}.json (>0)。"""
    role: str = ""
    title: str = ""
    instance_id: int = 0  # 0=单实例/主实例, >0=扩展实例
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

    @property
    def sentinel_key(self) -> str:
        """哨兵文件 key：instance_id=0 时用 role，>0 时用 role-{id}。"""
        if self.instance_id:
            return f"{self.role}-{self.instance_id}"
        return self.role

    def to_dict(self) -> dict:
        return {
            "role": self.role, "title": self.title,
            "instance_id": self.instance_id,
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
            instance_id=data.get("instance_id", 0),
            tmux_session=data.get("tmux_session", ""),
            pid=data.get("pid"),
            started_at=data.get("started_at", 0.0),
            lifecycle=data.get("lifecycle", "infinite"),
            drive=data.get("drive", ""),
            partners=partners,
            bus_track=data.get("bus_track", ""),
            bus_timeout=data.get("bus_timeout", 300),
            session_id=data.get("session_id", ""),
            engine=data.get("engine", "ccs"),
            health=health,
        )



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
    except Exception as _e:
        LOG.debug("_list_tmux_sessions failed: %s", _e)
        return set()


def _role_name_from_tmux(tmux_name: str) -> str:
    """从 tmux name 提取角色名（去掉 instance 后缀）。"""
    _role, _ = _parse_tmux_name(tmux_name)
    if _role:
        return _role
    return tmux_name.removeprefix("ccs-").removeprefix("cdx-")


def _parse_tmux_name(tmux_name: str) -> tuple[str, int]:
    """从 tmux name 解析 (role, instance_id)，延迟导入防循环。"""
    from tmux_ops import parse_tmux_name as _ptn  # fmt: skip — 延迟导入防循环
    return _ptn(tmux_name)


def _engine_from_tmux(tmux_name: str) -> str:
    return "codex" if tmux_name.startswith("cdx-") else "ccs"


def _get_pid(tmux_session: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_session}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        if r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    except Exception as _e:
        LOG.debug("_get_pid tmux display-message failed for %s: %s", tmux_session, _e)
    return None


def _get_started_at(tmux_session: str) -> float:
    pid = _get_pid(tmux_session)
    if pid:
        try:
            stat = os.stat(f"/proc/{pid}")
            return stat.st_ctime
        except (FileNotFoundError, PermissionError, OSError):
            LOG.debug("_get_started_at stat /proc/%s failed", pid)
    return time.time()


# ── 哨兵文件级锁（防止多线程 read-modify-write 竞态）──
_SENTINEL_LOCK = threading.Lock()

def _sentinel_file_path(key: str) -> Path:
    """哨兵文件路径：SENTINEL_DIR / {key}.json。key 为 role 或 role-{id}。"""
    return SENTINEL_DIR / f"{key}.json"


def _health_file_path(key: str) -> Path:
    """健康文件路径：_HEALTH_DIR / {key}.json。"""
    return _HEALTH_DIR / f"{key}.json"


def write_sentinel(s: CcsSentinel) -> Path:
    """写入哨兵文件（线程安全）。
    instance_id=0 → {role}.json，>0 → {role}-{id}.json。
    """
    path = _sentinel_file_path(s.sentinel_key)
    with _SENTINEL_LOCK:
        path.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
    return path


def delete_sentinel(key_or_role: str, instance_id: int = 0) -> bool:
    """删除哨兵 + 健康文件（线程安全）。
    支持两种调用方式：
      delete_sentinel("engineer")          → 删 engineer.json
      delete_sentinel("engineer", 2)       → 删 engineer-2.json
      delete_sentinel("engineer-2")        → 删 engineer-2.json
    """
    if instance_id:
        key = f"{key_or_role}-{instance_id}"
    else:
        key = key_or_role
    key = key.removesuffix(".json")  # 防御：如果调用方传了 .json
    deleted = False
    with _SENTINEL_LOCK:
        for d in (SENTINEL_DIR, _HEALTH_DIR):
            path = d / f"{key}.json"
            if path.exists():
                path.unlink()
                deleted = True
    return deleted


def read_sentinel(key_or_role: str, instance_id: int = 0) -> Optional[CcsSentinel]:
    """读哨兵：文件优先 → tmux 回退（线程安全）。
    支持两种调用方式：
      read_sentinel("engineer")           → 读 engineer.json（兼容旧格式）
      read_sentinel("engineer", 2)        → 读 engineer-2.json
      read_sentinel("engineer-2")         → 读 engineer-2.json
    """
    if instance_id:
        key = f"{key_or_role}-{instance_id}"
    else:
        key = key_or_role
    key = key.removesuffix(".json")
    # 解析 role: engineer-2 → (engineer, 2); ccs-coordinator → (ccs-coordinator, 0)
    _dp = key.rsplit("-", 1)
    if len(_dp) == 2 and _dp[1].isdigit() and not _dp[1].startswith("0"):
        role = _dp[0]
        parsed_inst = int(_dp[1])
    else:
        role = key
        parsed_inst = 0
    if not instance_id:
        instance_id = parsed_inst

    path = _sentinel_file_path(key)
    with _SENTINEL_LOCK:
        if path.exists():
            try:
                return CcsSentinel.from_dict(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                LOG.warning("read_sentinel 文件损坏或不可读: %s", path)

    # 回退：tmux 实时派生（文件不存在时）
    for prefix in ("ccs", "cdx"):
        if instance_id:
            tmux_name = f"{prefix}-{role}-{instance_id}"
        else:
            tmux_name = f"{prefix}-{key}"  # 兼容 old ccs-role 格式
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
                    instance_id=instance_id,
                    tmux_session=tmux_name,
                    pid=_get_pid(tmux_name),
                    started_at=_get_started_at(tmux_name),
                    lifecycle="infinite",
                    engine=engine,
                )
                if role_json:
                    sentinel.lifecycle = role_json.get("lifecycle", "infinite")
                    sentinel.drive = role_json.get("drive", "")
                health_path = _health_file_path(key)
                if health_path.exists():
                    try:
                        hd = json.loads(health_path.read_text())
                        sentinel.health = CcsHealth(**{k: v for k, v in hd.items()
                                              if k in CcsHealth.__dataclass_fields__})
                    except (json.JSONDecodeError, OSError, TypeError):
                        LOG.warning("read_sentinel health 文件损坏或不可读: %s", health_path)
                return sentinel
        except Exception as _e:
            LOG.debug("read_sentinel tmux fallback failed for %s: %s", tmux_name, _e)
            continue
    return None


def list_sentinels() -> list[CcsSentinel]:
    """列出哨兵：合并文件 + tmux 实时数据，活 PID 优先（线程安全）。

    文件名格式：{role}.json（instance_id=0）或 {role}-{instance_id}.json（>0）。
    使用 sentinel_key 作为 result dict 的 key 而非 role，避免多 instance 互相覆盖。
    """
    result: dict[str, CcsSentinel] = {}

    # 1. 读文件（持锁避免读到并发写入的 torn 数据）
    for path in sorted(SENTINEL_DIR.glob("*.json")):
        key = path.stem  # "engineer" 或 "engineer-2"
        with _SENTINEL_LOCK:
            try:
                s = CcsSentinel.from_dict(json.loads(path.read_text()))
                result[s.sentinel_key] = s
            except (json.JSONDecodeError, OSError) as _e:
                LOG.warning("list_sentinels read sentinel file %s failed: %s", path, _e)

    # 2. 读 tmux（补充未被文件覆盖的活 session，或更新 pid）
    for tmux_name in sorted(_list_tmux_sessions()):
        role, instance_id = _parse_tmux_name(tmux_name)
        if not role:
            continue
        pid = _get_pid(tmux_name)
        if pid is None:
            continue  # 死 session 不覆盖
        key = role if instance_id == 0 else f"{role}-{instance_id}"
        engine = _engine_from_tmux(tmux_name)
        role_json = _get_role_json(role)
        sentinel = CcsSentinel(
            role=role,
            instance_id=instance_id,
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
        health_path = _health_file_path(key)
        if health_path.exists():
            try:
                hd = json.loads(health_path.read_text())
                sentinel.health = CcsHealth(**{k: v for k, v in hd.items()
                                              if k in CcsHealth.__dataclass_fields__})
            except (json.JSONDecodeError, OSError) as _e:
                LOG.warning("list_sentinels read health file %s failed: %s", health_path, _e)
        result[key] = sentinel

    # filter out zombie sentinels (pid=0/None + empty tmux)
    return [s for s in result.values() if s.tmux_session or (s.pid or 0) > 0]


def update_health(role: str, instance_id: int = 0, **kwargs) -> bool:
    """更新 ccs-health 中的 watchdog 状态。
    instance_id=0 → {role}.json，>0 → {role}-{id}.json。
    """
    key = f"{role}-{instance_id}" if instance_id else role
    with _HEALTH_LOCK:
        path = _health_file_path(key)
        health = {}
        if path.exists():
            try:
                health.update(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError) as _e:
                LOG.warning("update_health read existing health for %s failed: %s", key, _e)
        health.update(kwargs)
        path.write_text(json.dumps(health))
        return True
