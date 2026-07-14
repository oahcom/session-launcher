#!/usr/bin/env python3
"""
core.py — 生命周期编排（从 tmux_ops/role_manager/codex_ops 导入）
"""

__all__ = [
    'start',
    'set_routing_policy',
    'get_routing_policy',
    'route_target',
    'stop',
    'status',
    'send',
    'output',
    'health_check',
    'register',
    'workspace_create',
    'workspace_list',
    'force_start_ccs',
    'wake_ccs',
    'start_codex_session',
    'run_codex_task',
    'cdx_status',
    'get_role',
    'inject_role_knowledge_into_workspace',
    '_invalidate_role_cache',
    'load_roles',
    '_forbidden_list',
    'check_wake_permission',
    '_build_role_prompt',
    'inject_prompt_into_claudemd',
    'clear_injected_prompt',
    'check_signal',
    'has_work',
    'auto_schedule',
    'write_lifecycle_sentinel',
    'check_ondemand_timeout',
    'cleanup_stale_sentinels',
    '_is_alive',
    '_find_claude_pid',
    '_tmux_send',
    '_find_codex_pid',
    'TMUX_PREFIX',
    'BUS_CLIENT',
    'FEED_LISTENER',
    '_FORBIDDEN_MAP',
    '_WAKE_PERMISSION_MAP',
    'SESSION_ROLES_ROOT',
    '_CLAUDE_MD',
    '_LIFECYCLE_SENTINEL_DIR',
    '_check_memory_before_launch',
    '_validate_role_name',
    '_resolve_ws_paths',
    '_action_templates',
]



# ── 路由策略状态（MCP Gateway 模式）──
# sticky: 同角色消息路由到同一 CCS session
# round-robin: 轮询分发
# priority: 按消息优先级路由
_ROUTING_POLICIES: dict[str, str] = {}  # role -> policy

def set_routing_policy(role: str, policy: str = "sticky") -> None:
    if policy not in ("sticky", "round-robin", "priority"):
        policy = "sticky"
    _ROUTING_POLICIES[role] = policy

def get_routing_policy(role: str) -> str:
    return _ROUTING_POLICIES.get(role, "sticky")

def route_target(role: str, candidates: list[str]) -> str:
    policy = get_routing_policy(role)
    if policy == "sticky":
        return candidates[0] if candidates else role
    elif policy == "round-robin":
        idx = hash(role + str(int(__import__("time").time() / 60))) % max(len(candidates), 1)
        return candidates[idx] if candidates else role
    return candidates[0] if candidates else role
from tmux_ops import _check_memory_before_launch, _find_claude_pid, _find_claude_session_id, _is_alive, _tmux_send, _tmux_output, _tmux_kill, _find_codex_pid, _active_codex_session_count, _write_codex_sentinel, _wait_codex_ready, TMUX_PREFIX, CODEX_TMUX_PREFIX, CODEX_SENTINEL_DIR, CODEX_LOOP_DELAY, CODEX_OUTPUT_MAX, CODEX_ERROR_MAX, CODEX_SESSION_MAX, CODEX_READY_RETRIES, CODEX_READY_INTERVAL, _MEM_FREE_MIN_MB, _CCS_LAUNCH_INTERVAL

from role_manager import load_roles, get_role, _invalidate_role_cache, _forbidden_list, check_wake_permission, _action_templates, _build_role_prompt, _resolve_ws_paths, inject_role_knowledge_into_workspace, _validate_role_name, _ensure_bus_aliases_in_bashrc, inject_prompt_into_claudemd, clear_injected_prompt, _ROLE_NAME_RE, SESSION_ROLES_ROOT, _WS_MARKER_START, _WS_MARKER_END, SESSION_MARKER_START, SESSION_MARKER_END, _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY, _WAKE_PERMISSION_MAP, _CLAUDE_MD

from codex_ops import start_codex_session, _build_codex_runner_script, run_codex_task, cdx_status

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from sentinel import CcsSentinel, CcsHealth, write_sentinel, read_sentinel, delete_sentinel, list_sentinels, SENTINEL_DIR

from watchdog import start_watchdog
from tracker import start_tracker
from signals import check_signal

from paths import BUS_CLIENT
FEED_LISTENER = Path(__file__).resolve().parent.parent / "feed_listener.py"
from paths import LIFECYCLE_SENTINEL_DIR as _LIFECYCLE_SENTINEL_DIR

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "loop", feed_cat: str = "") -> dict:
    """创建一个 CCS 并写入哨兵。

    自动从 hermes-session-roles 加载角色定义（如存在），
    构建 system prompt 并注入专业知识到 workspace CLAUDE.md。
    """
    if not _validate_role_name(role):
        return {"success": False, "error": f"非法角色名: {role}"}
    tmux_name = f"{TMUX_PREFIX}{role}"
    partners = partners or []

    # 0. ondemand 模式：只写 workspace + 哨兵，不启动 tmux
    if drive == "ondemand":
        workspace_create(role)
        role_def = get_role(role)
        if role_def:
            inject_role_knowledge_into_workspace(role_def)
        s = CcsSentinel(
            role=role, title=title or role, tmux_session="",
            pid=0, started_at=time.time(), lifecycle="ondemand",
        )
        write_sentinel(s)
        print(f"📋 {role} (ondemand) — 已写 workspace + 哨兵")
        return {"success": True, "role": role, "mode": "ondemand",
                "workspace": str(Path(f"~/ccs-workspaces/{role}").expanduser())}

    # 1. 先检查是否已运行（不消耗内存守卫额度）
    if _is_alive(tmux_name):
        if not detach:
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        print(f"📋 {tmux_name} 已在运行")
        return {"success": True, "role": role, "tmux_session": tmux_name}

    # 2. 内存守卫：确认有足够资源启动新 CCS 进程
    err = _check_memory_before_launch(role)
    if err:
        return {"success": False, "error": err}

    # 3. 确保工作空间存在并更新系统 CLAUDE.md
    ws_path = Path(f"~/ccs-workspaces/{role}").expanduser()
    result = workspace_create(role)
    if result.get("success"):
        action = result.get("action", "")
        print(f"📁 {'已创建' if action == 'created' else '已更新'} 工作空间: {ws_path}")

    # 4. 注入角色知识到 workspace
    role_def = get_role(role)
    if role_def:
        inject_role_knowledge_into_workspace(role_def)
        if not init_prompt:
            init_prompt = _build_role_prompt(role_def)
            print(f"📋 已构建角色 prompt ({len(init_prompt)} 字符)")
    elif not init_prompt:
        print(f"⚠ 未找到 {role} 角色定义（{SESSION_ROLES_ROOT}），使用空 prompt 启动")

    # 5. 启动 tmux + claude
    cmd = (
        "claude --model 9router_hermes"
        " --dangerously-skip-permissions"
        " --effort max"
        " --permission-mode bypassPermissions"
    )
    r = subprocess.run([
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-c", str(ws_path),
        "-e", "FORCE_PERSONA=0",
        "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
    ], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {r.stderr.strip()}"}

    # 6. 等 claude 就绪
    for _ in range(15):
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

    # 7. 注入 prompt（自动构建的 role prompt 或用户提供的 init_prompt）
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 8. 写哨兵
    pid = _find_claude_pid(tmux_name)
    print(f"📋 pid: {pid}")
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
        lifecycle="infinite",
        partners=partners,
        bus_track=bus_track,
        bus_timeout=bus_timeout,
        session_id="",
    )
    write_sentinel(s)

    # 9. 启动守护线程（仅 detach 模式）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30)
            print(f"✅ 守护线程: 监控 {p} 存活")

        if bus_track:
            start_tracker(role, bus_track, timeout_sec=bus_timeout,
                          interval=10, partners=partners)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")

        if feed_cat:
            _start_feed_listener(role, feed_cat)
            print(f"✅ feed listener: 实时监控 {feed_cat} 分类")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True, "role": role, "tmux_session": tmux_name,
        "pid": pid, "partners": partners or [], "bus_track": bus_track or None,
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
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "workspace": f"~/ccs-workspaces/{s.role}" if Path(
                f"~/ccs-workspaces/{s.role}").expanduser().exists() else None,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                "bus_msg_age": round(s.health.last_bus_msg_age, 0),
                "restart_count": s.health.restart_count,
            },
        })
    return statuses

def send(role: str, message: str, source: str = "") -> dict:
    """向 CCS 发送消息。"""
    # 跨角色路由拦截（存根：当前仅记录日志，始终放行）
    if role != "self":
        try:
            from cross_role_router import CrossRoleRouter
            CrossRoleRouter().intercept(source or "cli", role, message)
        except Exception:
            pass  # 存根降级：DB 不可用等场景不阻塞消息发送

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
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "last_bus_msg_age": round(s.health.last_bus_msg_age, 0),
            "watchdog_ok": s.health.watchdog_ok,
            "restart_count": s.health.restart_count,
            "uptime_sec": int(time.time() - s.started_at) if alive else 0,
        }
    return result

def force_start_ccs(role_name: str, by_role: str = "",
                    init_prompt: str = "") -> dict:
    """强制启动 CCS（内部独立检查权限）。"""
    if by_role and not check_wake_permission(by_role, role_name):
        return {"success": False,
                "error": f"权限不足: {by_role} 无权启动 {role_name}"}
    if _is_alive(f"{TMUX_PREFIX}{role_name}"):
        return {"success": False, "error": f"CCS {role_name} 已在运行"}
    return start(role_name, init_prompt=init_prompt)

def wake_ccs(role_name: str, context: str = "",
             by_role: str = "") -> dict:
    """唤醒 CCS：优先检查已有 session，否则启动新 CCS。"""
    alive = _is_alive(f"{TMUX_PREFIX}{role_name}")
    if alive:
        if context:
            return send(role_name, context)
        return {"success": True, "action": "already_running", "role": role_name}
    return force_start_ccs(role_name, by_role=by_role)

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

def has_work(roles: list[dict]) -> Optional[dict]:
    """检查是否有角色有工作。返回优先级最高的角色。

    集成 session-pipeline 路由：bus 有积压时按消息优先级排序。
    """
    try:
        _PIPELINE_SRC = Path(os.environ.get('SESSION_PIPELINE_SRC', str(Path.home() / 'session-pipeline' / 'src')))
        if str(_PIPELINE_SRC) not in sys.path:
            sys.path.insert(0, str(_PIPELINE_SRC))
        from router import get_router, priority
        from bus_protocol import Blackboard
        router = get_router()
        bb = Blackboard()
        facts = bb.unconsumed()
        if facts:
            urgency: dict[str, float] = {}
            for r in roles:
                name = r.get("name", "")
                try:
                    consume_cats = router.role_consume_categories(name)
                except Exception:
                    continue
                if "*" in consume_cats:
                    urgency[name] = urgency.get(name, 0) + 100
                else:
                    for cat in consume_cats:
                        p = priority(cat)
                        urgency[name] = urgency.get(name, 0) + (10 - min(p, 9))
            roles = sorted(roles, key=lambda r: -urgency.get(r.get("name", ""), 0))
    except (ImportError, Exception):
        pass

    for role in roles:
        signals = role.get("input_signals", [])
        if not signals:
            continue
        for signal in signals:
            if check_signal(signal):
                auto_schedule(role.get("name", ""))
                return role
    return None

def auto_schedule(role_name: str) -> str | None:
    """为新角色检测到任务时自动创建 workflow。

    防重复：已有该角色的 pending/running task 时不创建。
    """
    try:
        from workflow_client import WorkflowClient
        with WorkflowClient("coordinator") as wf:
            # 检查是否已有未完成任务（防重复触发）
            existing = wf.list_tasks()
            for t in existing:
                if t.get("assignee") == role_name and t.get("status") in ("pending", "in_progress"):
                    return None
            # 角色→默认模板映射
            _ROLE_TEMPLATE = {
                "pm": "WL-02", "coordinator": "WL-02", "lr": "WL-02",
                "pg": "WL-01", "product_architect": "WL-04",
                "reviewer": "WL-01", "qa": "WL-03", "maintainer": "WL-05",
                "optimizer": "WL-05", "devops": "WL-01",
                "archivist": "WL-04", "curator": "WL-04", "consumer": "WL-02",
                "engineer": "WL-01",
            }
            template_id = _ROLE_TEMPLATE.get(role_name, "WL-01")
            task_id, wf_id = wf.create_task_v2(
                f"自动调度: {role_name}",
                assignee=role_name,
                template_id=template_id,
                initiator_role="coordinator",
                description=f"来自 {role_name} 的信号触发",
            )
            return wf_id
    except Exception:
        return None

def _start_feed_listener(role: str, feed_cat: str) -> None:
    """启动 feed listener 线程，监听指定 bus 分类的新消息。"""
    import socket as _socket
    import json as _json
    import threading

    def _run():
        tag = f"feed:{role}"
        s = None
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
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    t = threading.Thread(target=_run, daemon=True, name=f"feed:{role}")
    t.start()

def register(role: str, tmux_name: str, title: str = "") -> dict:
    """将手动创建的 tmux session 注册为 CCS。"""
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"tmux session '{tmux_name}' 不存在"}
    pid = _find_claude_pid(tmux_name)
    s = CcsSentinel(
        role=role, title=title or role, tmux_session=tmux_name,
        pid=pid, started_at=time.time(),
    )
    write_sentinel(s)
    return {"success": True, "role": role, "tmux_session": tmux_name, "pid": pid}

def workspace_create(name: str) -> dict:
    """创建或更新系统级 CCS 工作空间的 CLAUDE.MD。

    更新策略：只替换 marker 标记的系统区域（角色身份 + WORKFLOW_GUIDE），
    保留用户在 marker 外手动添加的内容。
    """
    path = Path(f"~/ccs-workspaces/{name}").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    claude_md = path / "CLAUDE.md"

    guide_path = Path.home() / ".hermes" / "templates" / "WORKFLOW_GUIDE.md"
    guide_content = ""
    if guide_path.exists():
        guide_content = guide_path.read_text().replace("{role_name}", name)

    sys_block = (
        f"{_WS_MARKER_START}\n"
        f"# {name}\n\n"
        f"## 身份\n\n"
        f"你是 {name}，系统级 CCS。你通过以下方式接收指令：\n"
        f"| 驱动方式 | 触发源 | 说明 |\n"
        f"|---------|--------|------|\n"
        f"| ① /loop | 自循环 | 定时自动巡检 |\n"
        f"| ② ccs-send | 其他 CCS 发消息 | 按需分析 |\n"
        f"| ③ feed push | bus 新消息实时推送 | 即时检测 |\n\n"
        f"---\n\n"
        f"{guide_content}\n"
        f"{_WS_MARKER_END}\n"
    )

    if not claude_md.exists():
        claude_md.write_text(sys_block)
        return {"success": True, "workspace": str(path), "action": "created"}

    content = claude_md.read_text(encoding="utf-8")
    if _WS_MARKER_START in content and _WS_MARKER_END in content:
        start_idx = content.rindex(_WS_MARKER_START)
        end_idx = content.rindex(_WS_MARKER_END) + len(_WS_MARKER_END)
        new_content = content[:start_idx] + sys_block + content[end_idx:]
    else:
        new_content = content.rstrip() + "\n\n" + sys_block

    claude_md.write_text(new_content, encoding="utf-8")
    return {"success": True, "workspace": str(path), "action": "updated"}

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

