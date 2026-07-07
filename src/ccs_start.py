#!/usr/bin/env python3
"""
# DEPRECATED: 请使用 ccs.py
ccs_start.py — CCS 生命周期管理器（旧版独立启动器，逐步迁入 ccs.py）

内置协作基础设施：
  --partner <role>  自动守护伙伴存活（主从模式）
  --bus-track <cat> 轮次追踪 + 死锁检测（协作模式）
  --auto-restart    伙伴挂了自动重启
  --no-attach       后台运行不 attach
  --prompt          启动后自动注入的初始 prompt

协作模式通过 CLI 参数注入，不依赖 prompt 文本：
  - 主从模式: python3 ccs_start.py primary --partner replica --auto-restart
  - 对等模式: python3 ccs_start.py a --bus-track debate
  - 仲裁模式: python3 ccs_start.py monitor --bus-track debate --auto-restart

内部实现：
  - 伙伴守护线程: 定期 tmux has-session 检查
  - 轮次追踪线程: poll bus 最新时间戳，超时自动发提醒
  - 哨兵文件写入: /tmp/ccs-sentinels/<role>.json
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CCS_TMUX_PREFIX = "ccs-"
CCS_SENTINEL_DIR = Path("/tmp/ccs-sentinels")
CCS_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()


# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════

def _find_claude_session_id(tmux_name: str) -> Optional[str]:
    """从 CCS 独立工作目录中查找最新的 claude session ID。"""
    try:
        # 每个 CCS 有自己的 session 文件
        base = Path("/tmp") / "ccs_sessions"
        if not base.exists():
            return None

        # 找所有 *.jsonl，取最新的
        latest_file = None
        latest_time = 0
        for f in base.rglob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime > latest_time:
                latest_time = mtime
                latest_file = f

        if not latest_file:
            return None
        return latest_file.stem
    except Exception:
        return None
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


def _is_alive(tmux_name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _write_sentinel(role: str, title: str, tmux_name: str, pid: Optional[int], extra: dict = None):
    CCS_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "role": role,
        "title": title,
        "tmux_session": tmux_name,
        "pid": pid,
        "started_at": time.time(),
        "lifecycle": "infinite",
    }
    if extra:
        data.update(extra)
    (CCS_SENTINEL_DIR / f"{role}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )


def _tmux_send(tmux_name: str, message: str):
    """分批发送消息 + Enter。"""
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


def _bus_write(cat: str, text: str, src: str = "", evidence: str = ""):
    cmd = ["python3", str(BUS_CLIENT), "write", cat, text]
    if src:
        cmd += ["--src", src]
    if evidence:
        cmd += ["--evidence", evidence]
    subprocess.run(cmd, capture_output=True, timeout=15)


def _bus_read_latest(cat: str) -> Optional[dict]:
    """读取 bus 某分类最新一条消息。"""
    try:
        r = subprocess.run(
            ["python3", str(BUS_CLIENT), "read", "--cat", cat, "--limit", "1", "--json"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        facts = data.get("facts", data) if isinstance(data, dict) else data
        return facts[0] if facts else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
#  守护线程：伙伴存活检查
# ═══════════════════════════════════════════════════════════

def _watchdog_loop(this_role: str, partner_role: str, auto_restart: bool,
                   restart_prompt: str, interval: int = 30, restart_delay: int = 5):
    """定期检查伙伴存活，挂了则自动重启。"""
    partner_tmux = f"{CCS_TMUX_PREFIX}{partner_role}"
    log = lambda msg: print(f"[watchdog] {datetime.now():%H:%M:%S} {msg}", flush=True)

    while True:
        try:
            time.sleep(interval)
            if _is_alive(partner_tmux):
                continue

            log(f"partner {partner_role} 已死")

            if not auto_restart:
                _bus_write("notice", f"[{this_role}] 伙伴 {partner_role} 已死，未自动重启",
                           src=this_role)
                continue

            log(f"正在重启 {partner_role}...")
            # 清理旧哨兵（仅在 auto_restart 时删除）
            sentinel = CCS_SENTINEL_DIR / f"{partner_role}.json"
            sentinel.unlink(missing_ok=True)
            _bus_write("notice", f"[{this_role}] 重启 {partner_role}，原因: 进程退出",
                       src=this_role)
            time.sleep(restart_delay)

            # 重启逻辑: 直接调用本脚本（递归但不递归——新进程）
            cmd = [
                sys.executable, __file__, partner_role,
                f"（重启自 {this_role}）",
                "--no-attach",
            ]
            if restart_prompt:
                cmd += ["--prompt", restart_prompt]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"已发起 {partner_role} 重启")
        except Exception as e:
            log(f"异常: {e}，等待下一轮重试")
            time.sleep(interval)


def _start_watchdog(this_role: str, partner_role: str, auto_restart: bool,
                    restart_prompt: str = "", interval: int = 30) -> threading.Thread:
    t = threading.Thread(
        target=_watchdog_loop,
        args=(this_role, partner_role, auto_restart, restart_prompt, interval),
        daemon=False,
    )
    t.start()
    return t


# ═══════════════════════════════════════════════════════════
#  轮次追踪：bus 死锁检测
# ═══════════════════════════════════════════════════════════

def _turn_tracker_loop(this_role: str, bus_cat: str, timeout_sec: int = 300,
                       interval: int = 10):
    """定期检查 bus 轮次时间戳，超时发提醒。"""
    log = lambda msg: print(f"[tracker] {datetime.now():%H:%M:%S} {msg}", flush=True)
    last_reminder = 0

    while True:
        time.sleep(interval)
        latest = _bus_read_latest(bus_cat)
        if not latest:
            continue

        ts = latest.get("timestamp", 0)
        age = time.time() - ts
        src = latest.get("src", "")

        # 已超时且自己不是上一轮作者
        if age > timeout_sec and src != this_role:
            now = time.time()
            # 30 分钟内只提醒一次
            if now - last_reminder > 1800:
                target = src if src else "对方"
                _bus_write(
                    bus_cat,
                    f"[{this_role}] 死锁检测: {bus_cat} 最后消息 {int(age)}s 前 (by {src})，请继续",
                    src=this_role,
                )
                log(f"死锁提醒: {bus_cat} 超时 {int(age)}s，已发提醒给 {target}")
                last_reminder = now


def _start_turn_tracker(this_role: str, bus_cat: str, timeout_sec: int = 300,
                        interval: int = 10) -> threading.Thread:
    t = threading.Thread(
        target=_turn_tracker_loop,
        args=(this_role, bus_cat, timeout_sec, interval),
        daemon=False,
    )
    t.start()
    return t


# ═══════════════════════════════════════════════════════════
#  核心：创建 CCS
# ═══════════════════════════════════════════════════════════

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partner: str = "", auto_restart: bool = False,
          bus_track: str = "", bus_timeout: int = 300, workspace: str = "") -> dict:
    tmux_name = f"{CCS_TMUX_PREFIX}{role}"

    # 检查已存在
    if _is_alive(tmux_name):
        return {"success": False, "error": "已存在", "tmux_session": tmux_name}

    # 1. 启动 tmux + claude（系统级 CCS 使用独立工作空间）
    workspace_dir = Path(f"~/ccs-workspaces/{workspace}").expanduser() if workspace else None
    if workspace_dir and workspace_dir.exists():
        cmd = (
            f"claude --cd={workspace_dir}"
            " --model 9router_hermes"
            " --dangerously-skip-permissions"
            " --effort max"
            " --permission-mode bypassPermissions"
        )
        print(f"📁 系统级 CCS: 使用独立工作空间 {workspace_dir}")
    else:
        cmd = (
            "claude --model 9router_hermes"
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

    # 4. 获取 claude session ID 并写哨兵
    pid = _find_claude_pid(tmux_name)
    session_id = _find_claude_session_id(tmux_name)
    sentinel_extra = {}
    if partner:
        sentinel_extra["partner"] = partner
    if bus_track:
        sentinel_extra["bus_track"] = bus_track
    if session_id:
        sentinel_extra["session_id"] = session_id
    _write_sentinel(role, title or role, tmux_name, pid, sentinel_extra)

    # 5. 启动守护线程（仅 detach 模式，non-detach 会被 os.execvp 杀死）
    if detach:
        if partner:
            restart_prompt = f"你是 {partner}，被 {role} 重启。执行 /loop 持续工作。"
            _start_watchdog(role, partner, auto_restart, restart_prompt, interval=30)
            print(f"✅ 守护线程: 监控 {partner} 存活")

        if bus_track:
            _start_turn_tracker(role, bus_track, timeout_sec=bus_timeout, interval=10)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")
    elif partner or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True,
        "role": role,
        "tmux_session": tmux_name,
        "pid": pid,
        "partner": partner or None,
        "bus_track": bus_track or None,
    }

    if not detach:
        print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
        os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
    else:
        print(f"🎯 后台运行 (tmux attach -t {tmux_name} 进入)")

    return result


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="CCS 生命周期管理器 — 内置协作基础设施",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
协作模式示例:
  # 主从模式：verifier 守护 rebutter
  ccs_start.py verifier --partner rebutter --auto-restart --no-attach

  # 对等模式：两个 CCS 互相追踪 bus
  ccs_start.py pro --bus-track debate --no-attach
  ccs_start.py rebutter --bus-track debate --no-attach

  # 仲裁模式：监控者守护两个 CCS
  ccs_start.py monitor --partner verifier --partner rebutter --no-attach
""")
    parser.add_argument("role", help="角色名（自动加 ccs- 前缀）")
    parser.add_argument("title", nargs="?", default="", help="角色标题")
    parser.add_argument("--no-attach", action="store_true", help="后台运行，不 attach")
    parser.add_argument("--prompt", default="", help="初始 prompt（自动发送）")
    parser.add_argument("--partner", default=None, action="append",
                       help="守护伙伴（可多次指定）")
    parser.add_argument("--auto-restart", action="store_true",
                       help="伙伴挂了自动重启")
    parser.add_argument("--bus-track", default="",
                       help="追踪 bus 分类的轮次（防死锁）")
    parser.add_argument("--bus-timeout", type=int, default=300,
                       help="轮次超时秒数（默认 300）")
    args = parser.parse_args()

    # 主从模式: 第一个 partner 是被守护方，其余是额外守护
    primary_partner = args.partner[0] if args.partner else ""

    start(
        role=args.role,
        title=args.title,
        detach=args.no_attach,
        init_prompt=args.prompt,
        partner=primary_partner,
        auto_restart=args.auto_restart,
        bus_track=args.bus_track,
        bus_timeout=args.bus_timeout,
    )


if __name__ == "__main__":
    main()
