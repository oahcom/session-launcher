#!/usr/bin/env python3
"""
feed_listener.py — 实时监听 bus 新消息，检测辩论结束等关键词。

通过 Sister Socket Server 的 feed socket 接收实时推送。
零轮询，毫秒级延迟。

用法:
    python3 feed_listener.py                    # 默认监听
    python3 feed_listener.py --on-debate-end    # 仅检测辩论结束
    python3 feed_listener.py --notify           # 检测到时写 bus notice

架构:
    bus_protocol.py write() → feed socket → 本脚本 → bus notice
"""
import argparse
import json
import socket
import subprocess
import sys
import time
from typing import Optional


from pathlib import Path
_HOME = Path.home()
FEED_SOCKET = _HOME / ".hermes" / "run" / "sister_bus_feed.sock" if (_HOME / ".hermes" / "run" / "sister_bus_feed.sock").exists() else "/tmp/sister_bus_feed.sock"
_BUS_CLIENT = _HOME / ".hermes" / "scripts" / "bus_client.py"
BUS_CLIENT = str(_BUS_CLIENT) if _BUS_CLIENT.exists() else str(Path.home() / ".hermes" / "scripts" / "bus_client.py")

# 辩论结束关键词
DEBATE_END_KEYWORDS = (
    "终局", "最终", "收束", "结束", "收官", "结论",
    "总结", "共识", "定论", "闭幕", "完结", "收尾",
)


def _bus_write(cat: str, text: str, src: str = "feed-listener"):
    """写入 bus。"""
    subprocess.run(
        ["python3", BUS_CLIENT, "write", cat, text, "--src", src],
        capture_output=True, timeout=15
    )


def _on_new_fact(event: dict):
    """处理新消息事件。socket_server 发送格式为 {"event":"message","msg":{...}}。"""
    # 提取 msg 内层数据
    msg = event.get("msg", event)
    cat = msg.get("cat", "")
    title = msg.get("title", "")
    src = msg.get("src", "")
    fid = msg.get("id", "")

    print(f"[{cat}] #{fid} [{src}] {title[:80]}")

    # 检测辩论结束
    if cat == "debate" and any(k in title for k in DEBATE_END_KEYWORDS):
        _bus_write("notice",
                   f"[feed-listener] 辩论已结束: {title[:100]}",
                   src="feed-listener")
        print(f"  → 检测到辩论结束，已写 bus notice")


def connect() -> socket.socket:
    """连接到 feed socket。失败时抛异常，由调用方决定重试。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(FEED_SOCKET)
    s.sendall(b'{"cmd":"SUBSCRIBE","agent":"feed"}\n')
    return s


def _inject_to_tmux(tmux_target: str, event: dict):
    """将 feed 消息注入到 tmux 会话。

    task_spec / workflow 消息发送 task 触发指令。
    其他消息发送 /goal context。
    """
    msg = event.get("msg", event)
    cat = msg.get("cat", "")
    title = msg.get("title", "")
    text = msg.get("text", "")
    body = msg.get("evidence", "") or msg.get("body", "")
    if cat in ("task_spec", "workflow", "scheduler"):
        snippet = (body or title)[:200]
        payload = f"/goal [{cat}] 新任务: {title}\n{snippet}\n请立即: wf check → 执行 → wf complete"
    else:
        payload = f"/goal [{cat}] {title}"
        if body:
            payload += f"\n{body[:200]}"
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_target, payload, "Enter"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def run(notify: bool = False, on_debate_end: bool = False, tmux_target: str = ""):
    """主循环：阻塞等待 feed socket 消息，断线自动重连。

    tmux_target: 指定后自动将消息注入到对应 tmux 会话（如 "ccs-architect"）。
    """
    # PID 锁：防止同一 tmux_target 的重复进程（O_EXCL 原子创建，消除 TOCTOU 竞态）
    if tmux_target:
        import os, signal as _sig
        pid_dir = os.path.expanduser("~/.hermes/run/feed_listeners")
        os.makedirs(pid_dir, exist_ok=True)
        pid_file = os.path.join(pid_dir, f"{tmux_target}.pid")
        try:
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                old_pid = int(open(pid_file).read().strip())
                os.kill(old_pid, 0)  # 检查进程存活
                print(f"[feed_listener] 已有存活实例 PID={old_pid}，退出")
                return
            except (ValueError, OSError, FileNotFoundError):
                pass  # 旧进程已死，覆盖写
                Path(pid_file).write_text(str(os.getpid()))
        import atexit, functools
        def _cleanup():
            try: os.unlink(pid_file)
            except OSError: pass
        atexit.register(lambda: [os.unlink(pid_file) for _ in [1] if os.path.exists(pid_file)])
        _sig.signal(_sig.SIGTERM, lambda *_: (atexit._run_exitfuncs(), sys.exit(0)))
    print(f"正在连接 {FEED_SOCKET}...")
    s = None

    while True:
        try:
            if s is None:
                s = connect()
                print(f"✅ 已连接 feed socket，监听中... (Ctrl+C 退出)")
                if tmux_target:
                    print(f"  消息将注入 tmux: {tmux_target}")
            chunk = s.recv(4096)
            if not chunk:
                print("连接断开，5秒后重试...")
                time.sleep(5)
                s = None
                continue
            buf = chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line:
                    try:
                        event = json.loads(line)
                        _on_new_fact(event)
                        if tmux_target:
                            _inject_to_tmux(tmux_target, event)
                    except json.JSONDecodeError:
                        pass
        except KeyboardInterrupt:
            print("\n已断开")
            break
        except (ConnectionError, BrokenPipeError, OSError) as e:
            print(f"连接错误: {e}，5秒后重试...")
            time.sleep(5)
            s = None
        except Exception as e:
            print(f"异常: {e}，5秒后重试...")
            time.sleep(5)
            s = None

    if s:
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="实时监听 bus 新消息")
    parser.add_argument("--notify", action="store_true",
                        help="检测到辩论结束时写 bus notice")
    parser.add_argument("--on-debate-end", action="store_true",
                        help="仅检测辩论结束关键词")
    parser.add_argument("--tmux-target", default="",
                        help="将消息注入到 tmux 会话（如 ccs-architect）")
    args = parser.parse_args()
    run(notify=args.notify, on_debate_end=args.on_debate_end, tmux_target=args.tmux_target)
