#!/usr/bin/env python3
"""
core.py — CCS 核心操作

start / stop / status / send / output / health
所有操作通过哨兵文件协调，不直接操作 tmux。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from sentinel import (
    CcsSentinel, CcsHealth, write_sentinel, read_sentinel,
    delete_sentinel, list_sentinels, SENTINEL_DIR,
)
from watchdog import start_watchdog
from tracker import start_tracker

TMUX_PREFIX = "ccs-"
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()
FEED_LISTENER = Path(__file__).parent.parent / "feed_listener.py"


# ── tmux 底层操作 ──────────────────────────────────────────

def _find_claude_pid(tmux_name: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = r.stdout.strip()
        if pane_pid.isdigit():
            r2 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", "claude"],
                capture_output=True, text=True, timeout=3
            )
            if r2.stdout.strip():
                return int(r2.stdout.strip().split("\n")[0])
            return int(pane_pid)
    except Exception:
        pass
    return None


def _find_claude_session_id(role: str = "") -> Optional[str]:
    """从 CCS 独立工作目录中查找最新的 claude session ID。"""
    try:
        if role:
            base = Path(f"/tmp/ccs-sessions/{role}/.claude/projects")
        else:
            base = Path.home() / ".claude" / "projects"
        if not base.exists():
            return None
        latest_file = None
        latest_time = 0
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                mtime = f.stat().st_mtime
                if mtime > latest_time:
                    latest_time = mtime
                    latest_file = f
        if not latest_file:
            return None
        return latest_file.stem
    except Exception:
        return None


def _is_alive(tmux_name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _tmux_send(tmux_name: str, message: str):
    for i in range(0, len(message), 500):
        chunk = message[i:i+500]
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", chunk],
            capture_output=True, timeout=3
        )
        time.sleep(0.05)
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
        capture_output=True, timeout=3
    )


def _tmux_output(tmux_name: str, tail: int = 20) -> str:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0"],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.strip().split("\n")
        return "\n".join(lines[-tail:])
    except Exception as e:
        return f"[错误] {e}"


def _tmux_kill(tmux_name: str):
    subprocess.run(
        ["tmux", "kill-session", "-t", tmux_name],
        capture_output=True, timeout=5
    )


# ── 核心操作 ────────────────────────────────────────────────

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300, workspace: str = "",
          drive: str = "loop", feed_cat: str = "") -> dict:
    """创建一个 CCS 并写入哨兵。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    partners = partners or []

    if _is_alive(tmux_name):
        return {"success": False, "error": "已存在", "tmux_session": tmux_name}

    # 1. 启动 tmux + claude
    # 每个 CCS 分配固定的 session ID，存储在哨兵中用于恢复
    import uuid
    old_sentinel = read_sentinel(role)
    if old_sentinel and old_sentinel.session_id:
        session_id = old_sentinel.session_id
        print(f"📋 恢复 session: {session_id}")
    else:
        session_id = str(uuid.uuid4())
        print(f"📋 新 session: {session_id}")

    cmd = (
        "claude --model 9router_hermes"
        f" --resume {session_id}"
        " --dangerously-skip-permissions"
        " --effort max"
        " --permission-mode bypassPermissions"
    )
    r = subprocess.run([
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-e", "FORCE_PERSONA=0",
        "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
    ], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {r.stderr.strip()}"}

    # 2. 等 claude 就绪
    for i in range(15):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0", "-S-3"],
                capture_output=True, text=True, timeout=3
            )
            if "❯" in out.stdout:
                break
        except Exception:
            pass
    time.sleep(1)

    # 3. 注入初始 prompt
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 4. 写哨兵（使用固定的 UUID 作为 session ID，用于重启时恢复对话上下文）
    pid = _find_claude_pid(tmux_name)
    # 为每个 CCS 生成固定的 UUID，存储在哨兵中用于恢复
    import uuid
    session_id = str(uuid.uuid4())
    print(f"📋 session ID: {session_id}")
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
        lifecycle="infinite",
        partner=partners[0] if partners else "",
        bus_track=bus_track,
        bus_timeout=bus_timeout,
        session_id=session_id or "",
    )
    write_sentinel(s)

    # 5. 启动守护线程（仅 detach 模式，non-detach 会被 os.execvp 杀死）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30)
            print(f"✅ 守护线程: 监控 {p} 存活")

        # 6. 启动轮次追踪（如有 bus_track）
        if bus_track:
            start_tracker(role, bus_track, timeout_sec=bus_timeout,
                          interval=10, partners=partners)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")

        # 7. 启动 feed listener（如有 feed_cat）
        if feed_cat:
            _start_feed_listener(role, feed_cat)
            print(f"✅ feed listener: 实时监控 {feed_cat} 分类")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True,
        "role": role,
        "tmux_session": tmux_name,
        "pid": pid,
        "partners": partners or [],
        "bus_track": bus_track or None,
    }

    if not detach:
        print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
        os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
    else:
        print(f"🎯 后台运行 (tmux attach -t {tmux_name} 进入)")

    return result


def stop(role: str) -> dict:
    """终止 CCS 并清理哨兵。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    was_alive = _is_alive(tmux_name)
    _tmux_kill(tmux_name)
    delete_sentinel(role)
    if not was_alive:
        return {"success": False, "error": "CCS 不存在", "role": role}
    return {"success": True, "role": role}


def status() -> list[dict]:
    """列出所有 CCS 的运行状态（含死亡的）。"""
    sentinels = list_sentinels()
    statuses = []
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        statuses.append({
            "role": s.role,
            "title": s.title,
            "alive": alive,
            "pid": s.pid,
            "uptime_sec": int(time.time() - s.started_at),
            "lifecycle": s.lifecycle,
            "partner": s.partner or None,
            "bus_track": s.bus_track or None,
            "workspace": f"~/ccs-workspaces/{s.role}" if Path(f"~/ccs-workspaces/{s.role}").expanduser().exists() else None,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                "bus_msg_age": round(s.health.last_bus_msg_age, 0),
                "restart_count": s.health.restart_count,
            },
        })
    return statuses


def send(role: str, message: str) -> dict:
    """向 CCS 发送消息。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"CCS {role} 未运行"}
    _tmux_send(tmux_name, message)
    return {"success": True, "sent_chars": len(message)}


def output(role: str, tail: int = 20) -> str:
    """截取 CCS tmux pane 的最新输出。"""
    return _tmux_output(f"{TMUX_PREFIX}{role}", tail=tail)


def health_check(role: str = "") -> dict:
    """健康检查：返回所有（或指定）CCS 的存活状态。"""
    sentinels = list_sentinels() if not role else (
        [s] if (s := read_sentinel(role)) else []
    )
    result = {}
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        result[s.role] = {
            "alive": alive,
            "partner": s.partner or None,
            "bus_track": s.bus_track or None,
            "last_bus_msg_age": round(s.health.last_bus_msg_age, 0),
            "watchdog_ok": s.health.watchdog_ok,
            "restart_count": s.health.restart_count,
            "uptime_sec": int(time.time() - s.started_at) if alive else 0,
        }
    return result


def _start_feed_listener(role: str, feed_cat: str) -> None:
    """启动 feed listener 线程，监听指定 bus 分类的新消息。"""
    import socket as _socket
    import json as _json
    import threading

    def _run():
        """后台线程：连接 feed socket，检测新消息写 bus notice。"""
        tag = f"feed:{role}"
        while True:
            try:
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(30)
                s.connect("/tmp/sister_bus_feed.sock")
                s.sendall(b'{"cmd":"SUBSCRIBE","agent":"feed"}\n')
                print(f"[{tag}] ✅ 已连接 feed socket，监听 {feed_cat}", flush=True)
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        print(f"[{tag}] 连接断开，5秒后重试...", flush=True)
                        time.sleep(5)
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line:
                            event = _json.loads(line).get("msg", {})
                            cat = event.get("cat", "")
                            if cat == feed_cat:
                                title = event.get("title", "")[:100]
                                src = event.get("src", "")
                                print(f"[{tag}] 收到 {cat}: {title} (src={src})", flush=True)
                                subprocess.run(
                                    ["python3", str(BUS_CLIENT), "write", "notice",
                                     f"[{role}] 收到 {cat} 消息: {title}", "--src", role],
                                    capture_output=True, timeout=15
                                )
            except Exception as e:
                print(f"[{tag}] 异常: {e}，5秒后重试...", flush=True)
                time.sleep(5)
            finally:
                try: s.close()
                except: pass

    t = threading.Thread(target=_run, daemon=False, name=f"feed:{role}")
    t.start()


def register(role: str, tmux_name: str, title: str = "") -> dict:
    """将手动创建的 tmux session 注册为 CCS。"""
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"tmux session '{tmux_name}' 不存在"}
    pid = _find_claude_pid(tmux_name)
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
    )
    write_sentinel(s)
    return {"success": True, "role": role, "tmux_session": tmux_name, "pid": pid}


def workspace_create(name: str) -> dict:
    """创建系统级 CCS 工作空间并写入默认 CLAUDE.md。"""
    path = Path(f"~/ccs-workspaces/{name}").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    claude_md = path / "CLAUDE.md"
    if claude_md.exists():
        return {"success": False, "error": f"工作空间已存在: {claude_md}"}
    claude_md.write_text(f"""# {name}

## 身份

你是 {name}，系统级 CCS。你通过两种驱动方式接收指令：

| 驱动方式 | 触发源 | 说明 |
|---------|--------|------|
| ① /loop | 自循环 | 定时自动巡检 |
| ② ccs-send | 其他 CCS 发消息 | 按需分析 |
| ③ feed push | bus 新消息实时推送 | 即时检测 |

## 驱动方式

### ① /loop 自循环
每一轮执行 CLAUDE.md 中定义的工作内容，完成后自动进入下一轮。不可退出。

### ② ccs-send 外驱
接收到其他 CCS / 本 session 发来的消息后，按需分析并回复。

### ③ feed push 实时
接收到 bus cat=watch_cat 的新消息后，即时处理并回复。

## 禁令
- 不调 9Router / 不执行业务逻辑（除非明确职责包含）
- 不退出 / 不休眠超过 60s
- 不直接操作 tmux（通过 bus action 指令）
- 所有决策写入 bus cat=audit 审计

## 工作空间
~/ccs-workspaces/{name}/
""")
    return {"success": True, "workspace": str(path)}


def workspace_list() -> list[dict]:
    """列出所有系统级 CCS 工作空间。"""
    root = Path("~/ccs-workspaces").expanduser()
    if not root.exists():
        return []
    result = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "CLAUDE.md").exists():
            result.append({
                "name": d.name,
                "path": str(d),
                "claude_md": str(d / "CLAUDE.md"),
            })
    return result
