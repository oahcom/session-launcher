"""feed_listener 自身不再为已死的角色会话服务。"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed_listener import _target_alive

# 探活给 tmux 的预算是 3s，FailOpen：探不到就当活着。负载高的机器上 tmux 可能要
# 十几秒才应答，此时两种断言都失去意义（不是代码行为变了）。
_PROBE_BUDGET_S = 3.0


def _tmux_latency() -> float:
    t0 = time.monotonic()
    try:
        subprocess.run(["tmux", "ls", "-F", "#{session_name}"], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        # 60s 都不应答时返回 inf，让下面的 fixture 走 skip 而不是 error：
        # 探活预算 3s 已经不可满足，两个断言都失去意义（实测 11:05/11:15 两次）。
        return float("inf")
    return time.monotonic() - t0


@pytest.fixture(scope="module", autouse=True)
def _require_responsive_tmux():
    lat = _tmux_latency()
    if lat > _PROBE_BUDGET_S:
        pytest.skip(f"tmux 应答 {lat:.1f}s > {_PROBE_BUDGET_S}s 探活预算，跳过（环境限制）")


def test_target_dead_listener_should_exit():
    """不存在的会话不是活目标：监听器应退出，释放 bus 订阅。"""
    assert _target_alive("ccs-nonexistent-role-9f2a") is False


def test_target_alive_listener_keeps_serving():
    """当前至少存活一个 CCS 会话，它被判为活。"""
    out = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                         capture_output=True, text=True, check=True)
    names = [n for n in out.stdout.split() if n.startswith("ccs-")]
    assert names, "本机无 ccs-* 会话，跳过活目标判断（环境限制）"
    assert _target_alive(names[0]) is True
