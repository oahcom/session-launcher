#!/usr/bin/env python3
"""护栏测试：确保 CCS 角色 claude 启动时必须带 9Router 路由 env。

根因回归防护：e5d5ccf 曾把 env 注入逻辑当"死代码"删除，
导致 headless/systemd 角色 claude 缺失 ANTHROPIC_* 而卡在 /login。
本测试把"启动出的 claude 必须带路由 env"固化为会失败的契约：
  1. _router_env 单测（含文件缺失回落）
  2. _launch_tmux 契约：断言 new-session 经 -e 注入两项变量
  3. 真机集成（可选，TEST_LAUNCH_REAL=1 时启用）：起真实 claude 抓 /proc env
删除 _router_env 或漏掉 -e 注入 → 本测试变红，而非线上静默卡死。
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from core import _router_env, _launch_tmux


class TestRouterEnvSource:
    def test_returns_both_keys(self):
        env = _router_env()
        assert env["ANTHROPIC_BASE_URL"]
        assert env["ANTHROPIC_API_KEY"]

    def test_keys_present_in_known_router(self):
        env = _router_env()
        assert "localhost:20128" in env["ANTHROPIC_BASE_URL"]
        assert env["ANTHROPIC_API_KEY"] == "9router-local"

    def test_fallback_when_source_missing(self, monkeypatch, tmp_path):
        # 指向不存在的 source 文件，验证兜底为已知默认值而非崩溃
        missing = tmp_path / "no_such_env.sh"
        monkeypatch.setattr("core._ROUTER_ENV_FILE", missing)
        env = _router_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:20128/v1"
        assert env["ANTHROPIC_API_KEY"] == "9router-local"

    def test_reads_values_from_source(self, monkeypatch, tmp_path):
        # source 文件里有真实值 → 优先于兜底
        src = tmp_path / "9router-env.sh"
        src.write_text(
            'export ANTHROPIC_BASE_URL="http://example:9999/v1"\n'
            'export ANTHROPIC_API_KEY="secret-key"\n'
        )
        monkeypatch.setattr("core._ROUTER_ENV_FILE", src)
        env = _router_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://example:9999/v1"
        assert env["ANTHROPIC_API_KEY"] == "secret-key"

    def test_empty_value_falls_back(self, monkeypatch, tmp_path):
        # source 文件内 ANTHROPIC_API_KEY= 为空（截断/损坏）→ 回落默认值，
        # 否则 claude 带空 key 静默卡 /login（原 bug 回归）。
        src = tmp_path / "9router-env.sh"
        src.write_text(
            'export ANTHROPIC_BASE_URL="http://localhost:20128/v1"\n'
            'export ANTHROPIC_API_KEY=\n'
        )
        monkeypatch.setattr("core._ROUTER_ENV_FILE", src)
        env = _router_env()
        assert env["ANTHROPIC_API_KEY"] == "9router-local"


class TestLaunchTmuxInjectsEnv:
    """契约测试：_launch_tmux 必须把路由 env 通过 -e 传给 tmux new-session。"""

    @patch("core._router_env")
    @patch("core.subprocess.run")
    @patch("core._find_claude_pid")
    @patch("core.time.sleep")
    def test_new_session_carries_anthropic_env(
        self, mock_sleep, mock_find_pid, mock_run, mock_router_env
    ):
        mock_router_env.return_value = {
            "ANTHROPIC_BASE_URL": "http://localhost:20128/v1",
            "ANTHROPIC_API_KEY": "9router-local",
        }
        mock_find_pid.return_value = 12345
        ok = MagicMock()
        ok.returncode = 0
        ok.stdout = ""
        ok.stderr = ""
        mock_run.return_value = ok

        _launch_tmux("ccs-testrole", Path("/tmp"))

        # 找到 tmux new-session 那次调用
        new_session_calls = [
            c.args[0] for c in mock_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "tmux"
            and "new-session" in c.args[0]
        ]
        assert new_session_calls, "tmux new-session 未被调用"
        args = new_session_calls[0]
        # 断言两项变量经 -e 注入（顺序无关）
        assert "-e" in args
        joined = " ".join(args)
        assert "ANTHROPIC_BASE_URL=http://localhost:20128/v1" in joined
        assert "ANTHROPIC_API_KEY=9router-local" in joined


def _real_launch_env_check():
    """真机集成：起真实角色 pane，抓 claude 进程 env 验证。需 TEST_LAUNCH_REAL=1。"""
    import subprocess
    import time
    import shutil

    role = "qa"
    tmux_name = f"ccs-{role}"
    ws = Path.home() / "ccs-workspaces" / role
    # 清理可能残留
    subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                   stderr=subprocess.DEVNULL, check=False)
    time.sleep(1)
    err = _launch_tmux(tmux_name, ws)
    assert err is None, f"_launch_tmux 失败: {err}"
    time.sleep(4)  # 等 claude 起来
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        # 找 pane 下的 claude 进程
        out = subprocess.run(
            ["pgrep", "-P", pane_pid], capture_output=True, text=True, timeout=5).stdout.strip()
        claude_pid = out.splitlines()[0] if out.strip() else None
        assert claude_pid, "未找到 claude 子进程"
        env = subprocess.run(
            ["bash", "-c", f"tr '\\0' '\\n' < /proc/{claude_pid}/environ"],
            capture_output=True, text=True, timeout=5).stdout
        assert "ANTHROPIC_BASE_URL=" in env, "claude 进程缺 ANTHROPIC_BASE_URL"
        assert "ANTHROPIC_API_KEY=" in env, "claude 进程缺 ANTHROPIC_API_KEY"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                       stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    if os.environ.get("TEST_LAUNCH_REAL") == "1":
        _real_launch_env_check()
        print("REAL LAUNCH OK")
    else:
        print("set TEST_LAUNCH_REAL=1 to run real launch integration")
