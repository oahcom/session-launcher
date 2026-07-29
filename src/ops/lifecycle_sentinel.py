"""生命周期哨兵管理 — ondemand 超时检测 + 孤儿清理。

从 core.py 提取的独立模块，减少 core.py 体积。
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_LIFECYCLE_SENTINEL_DIR = Path.home() / ".hermes" / "run" / "lifecycle-sentinels"

def write_lifecycle_sentinel(role: dict) -> None:
    """为 ondemand 角色写生命周期哨兵文件。"""
    _LIFECYCLE_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = _LIFECYCLE_SENTINEL_DIR / f"{role['name']}.active"
    sentinel.write_text(json.dumps({
        "role": role["name"],
        "title": role["title"],
        "lifecycle": role.get("lifecycle", "infinite"),
        "pid": os.getpid(),
        "max_minutes": role.get("max_minutes", 30) if role.get("lifecycle") == "ondemand" else None,
        "started_at": time.time(),
    }))

def check_ondemand_timeout(max_minutes: int = 30) -> list[str]:
    """检查 ondemand 角色是否超时。"""
    if not _LIFECYCLE_SENTINEL_DIR.exists():
        return []
    timed_out = []
    for f in sorted(_LIFECYCLE_SENTINEL_DIR.glob("*.active")):
        try:
            data = json.loads(f.read_text())
            if data.get("lifecycle") != "ondemand":
                continue
            started_at = data.get("started_at")
            if not started_at:
                continue
            if isinstance(started_at, (int, float)):
                started = datetime.fromtimestamp(started_at, tz=timezone.utc)
            else:
                started = datetime.fromisoformat(started_at)
            elapsed = datetime.now(timezone.utc) - started
            max_m = data.get("max_minutes", max_minutes)
            if elapsed > timedelta(minutes=max_m):
                timed_out.append(data["role"])
                f.unlink()
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return timed_out

def cleanup_stale_sentinels() -> list[str]:
    """清理孤儿哨兵文件。"""
    if not _LIFECYCLE_SENTINEL_DIR.exists():
        return []
    cleaned = []
    for f in sorted(_LIFECYCLE_SENTINEL_DIR.glob("*.active")):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            if pid and not Path(f"/proc/{pid}").exists():
                f.unlink()
                cleaned.append(data["role"])
                continue
            lifecycle = data.get("lifecycle")
            if lifecycle == "ondemand":
                started_at = data.get("started_at")
                if started_at:
                    if isinstance(started_at, (int, float)):
                        started = datetime.fromtimestamp(started_at, tz=timezone.utc)
                    else:
                        started = datetime.fromisoformat(started_at)
                    elapsed = datetime.now(timezone.utc) - started
                    max_m = data.get("max_minutes", 30)
                    if elapsed > timedelta(minutes=max_m):
                        f.unlink()
                        cleaned.append(f"{data['role']}(timeout)")
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return cleaned
