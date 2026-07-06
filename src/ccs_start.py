#!/usr/bin/env python3
"""
ccs_start.py — 直接在 tmux 里跑原生 claude（交互模式），写哨兵

用法:
  python3 src/ccs_start.py <role> [title]       # 启动 ccs-<role>，进入 claude 交互
  python3 src/ccs_start.py <role> --detach      # 后台启动，不 attach
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


CCS_TMUX_PREFIX = "ccs-"
CCS_SENTINEL_DIR = Path("/tmp/ccs-sentinels")


def _find_claude_pid(tmux_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = result.stdout.strip()
        if pane_pid.isdigit():
            result2 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", "claude"],
                capture_output=True, text=True, timeout=3
            )
            if result2.stdout.strip():
                return int(result2.stdout.strip().split("\n")[0])
            return int(pane_pid)
    except Exception:
        pass
    return None


def start(role: str, title: str = "", detach: bool = False, init_prompt: str = ""):
    tmux_name = f"{CCS_TMUX_PREFIX}{role}"

    # 检查已存在
    result = subprocess.run(
        ["tmux", "has-session", "-t", tmux_name],
        capture_output=True, timeout=5
    )
    if result.returncode == 0:
        if detach:
            print(f"⚠ {tmux_name} 已在运行，直接 attach")
            subprocess.run(["tmux", "attach", "-t", tmux_name])
        else:
            print(f"✅ {tmux_name} 已在运行，直接 attach")
            subprocess.run(["tmux", "attach", "-t", tmux_name])
        return

    # 启动 tmux + 原生 claude（交互模式，不带 -p）
    cmd = (
        "claude --model 9router_hermes"
        " --dangerously-skip-permissions"
        " --effort max"
        " --permission-mode bypassPermissions"
    )
    tmux_cmd = [
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-e", "FORCE_PERSONA=0",
        "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
    ]
    result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print(f"❌ tmux 启动失败: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    if init_prompt:
        import time
        time.sleep(1)
        for i in range(10):
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

        for i in range(0, len(init_prompt), 500):
            chunk = init_prompt[i:i+500]
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", chunk],
                capture_output=True, timeout=3
            )
            time.sleep(0.05)
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
            capture_output=True, timeout=3
        )
        print(f"✅ 初始 prompt 已注入")

    print(f"✅ tmux session {tmux_name} 已创建，等待 claude 就绪...")

    # 等 claude 就绪（最长 15 秒）
    ready = False
    for i in range(15):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0", "-S-3"],
                capture_output=True, text=True, timeout=3
            )
            if "❯" in out.stdout:
                ready = True
                print(f"✅ claude 就绪（{i+1}s）")
                break
        except Exception:
            pass

    # 写哨兵
    pid = _find_claude_pid(tmux_name)
    CCS_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = CCS_SENTINEL_DIR / f"{role}.json"
    sentinel.write_text(json.dumps({
        "role": role,
        "title": title or role,
        "tmux_session": tmux_name,
        "pid": pid,
        "started_at": time.time(),
        "lifecycle": "infinite",
    }, ensure_ascii=False, indent=2))

    print(f"✅ 哨兵已写入: {sentinel}")

    if not detach:
        print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出不杀进程)")
        os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
    else:
        print(f"🎯 后台运行中。用 `tmux attach -t {tmux_name}` 进入")


def main():
    parser = argparse.ArgumentParser(description="启动原生 claude CCS")
    parser.add_argument("role", help="角色名（自动加 ccs- 前缀）")
    parser.add_argument("title", nargs="?", default="", help="角色标题")
    parser.add_argument("--detach", action="store_true", help="后台运行，不 attach")
    parser.add_argument("--prompt", default="", help="启动后自动发送的初始 prompt")
    args = parser.parse_args()

    start(args.role, args.title, args.detach, args.prompt)


if __name__ == "__main__":
    main()