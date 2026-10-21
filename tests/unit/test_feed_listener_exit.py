"""feed_listener 自身不再为已死的角色会话服务。"""
import subprocess, sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed_listener import _target_alive


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
