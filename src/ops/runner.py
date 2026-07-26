"""ops/runner.py — 运行循环 + 健康仪表板（从 core.py 提取）"""

import os
import sys
import time
import socket
import json
import threading
from pathlib import Path

from ops.sentinel import list_sentinels
from tmux_ops import _is_alive

from paths import ensure_paths as _ensure_paths
_ensure_paths()


def dashboard() -> str:
    """聚合健康仪表板 + 路由拓扑。"""
    lines = ["╔═════════════════════════════════════════════╗",
             "║     CCS 生态健康仪表板                       ║",
             "╚══════════════════════════════════════════════╝", ""]

    # ── 1. CCS 存活概览 ──
    sents = list_sentinels()
    alive_count = sum(1 for s in sents if _is_alive(s.tmux_session))
    lines.append(f"CCS 哨兵: {len(sents)}  存活: {alive_count}  死亡: {len(sents)-alive_count}")
    lines.append("")

    # ── 2. 每个 CCS 详情 ──
    lines.append(f"{'角色':16} {'状态':6} {'运行':8} {'PID':8} {'伙伴':14} {'bus':14} {'看门狗':6} {'重启':4}")
    lines.append("-" * 88)
    for s in sorted(sents, key=lambda x: x.role):
        alive = _is_alive(s.tmux_session)
        age = int(time.time() - s.started_at)
        partner = s.partners[0] if s.partners else "-"
        box = "✅" if alive else "❌"
        wd = "✅" if s.health.watchdog_ok else "❌"
        lines.append(f"{s.role:16} {box:6} {age//60:3}分{age%60:02d}s "
                     f"{s.pid or 0:<8} {partner:14} {s.bus_track or '-':14} {wd:6} {s.health.restart_count:<4}")

    # ── 3. 路由拓扑摘要 ──
    lines.append("")
    lines.append("── 路由拓扑 ──")
    try:
        r = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0, '/home/administrator/session-pipeline/src'); "
             "from router import get_router; r = get_router(); print(r.routing)"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            import json as _json
            import ast as _ast
            routing = _ast.literal_eval(r.stdout)
            for role, data in sorted(routing.items()):
                cat_count = len(data.get("produce", []))
                c = data.get("consume", [])
                consume_cat = "*" if "*" in c else str(len(c))
                lines.append(f"  {role:16} 产出 {cat_count}分类  消费 {consume_cat}分类")
    except Exception:
        lines.append("  (pipeline router 不可达)")

    return "\n".join(lines)


def _start_feed_listener(role: str, feed_cat: str) -> None:
    """启动 feed listener 线程，监听指定 bus 分类的新消息。"""
    import socket as _socket
    import json as _json
    import threading

    s = None

    def _connect():
        nonlocal s
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(30)
            s.connect("/tmp/sister_bus_feed.sock")
            s.sendall(b'{"cmd":"SUBSCRIBE","agent":"feed"}\n')
            return True
        except Exception:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
                s = None
            return False

    def _run():
        nonlocal s
        tag = f"feed:{role}"
        while True:
            if not _connect():
                time.sleep(5)
                continue
            buf = b""
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        if not line_bytes:
                            continue
                        try:
                            data = _json.loads(line_bytes.decode())
                        except _json.JSONDecodeError:
                            continue
                        if data.get("cmd") == "MESSAGE":
                            cat = data.get("cat", "")
                            if cat == feed_cat:
                                from tmux_ops import _tmux_send
                                _tmux_send(f"ccs-{role}", _json.dumps(data, ensure_ascii=False))
                except Exception:
                    break
            if s:
                try:
                    s.close()
                except Exception:
                    pass
                s = None
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if not hasattr(_start_feed_listener, "_threads"):
        _start_feed_listener._threads = []
    _start_feed_listener._threads.append(t)
