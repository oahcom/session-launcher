"""feed_listener 自身不再为已死的角色会话服务。"""
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed_listener import _target_alive


def _probe(monkeypatch, *, rc=None, exc=None):
    """把 tmux has-session 的返回值/异常替换掉，再调被测函数。

    不调真 tmux：本机 19 个角色在跑，has-session 常超出 5s 预算 → FailOpen 返回
    True，断言变成环境噪音（实测 11:29 失败、11:26 跳过，同一份代码）。
    """
    def fake_run(*_a, **_kw):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args=[], returncode=rc)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return _target_alive("ccs-nonexistent-role-9f2a")


def test_dead_target_is_not_alive(monkeypatch):
    """不存在的会话不是活目标：监听器应退出，释放 bus 订阅。"""
    assert _probe(monkeypatch, rc=1) is False


def test_alive_target_keeps_serving(monkeypatch):
    """会话还在 → 监听器继续服务。"""
    assert _probe(monkeypatch, rc=0) is True


def test_probe_failure_falls_open(monkeypatch):
    """探测本身失败时按"还在"处理：宁可不退，也不要误杀正在服务的监听器。"""
    assert _probe(monkeypatch, exc=subprocess.TimeoutExpired(cmd="tmux", timeout=5)) is True
    assert _probe(monkeypatch, exc=OSError("tmux 未安装")) is True
