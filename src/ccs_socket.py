#!/usr/bin/env python3
"""
ccs_socket.py — CCS 直接通信客户端（复用 sister_bus socket server）

CCS 通过 sister_bus /tmp/sister_bus_ccs.sock 通信。
SUBSCRIBE 注册为 "ccs-{role}"，PUBLISH 按目标路由。

用法:
    ccs.py send-direct alice bob hi     # 直连 <1ms
    ccs.py stream bob                   # 流式输出
"""

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("ccs-socket")

# 复用 sister_bus socket server（由 systemd 管理）
from paths import SISTER_BUS_CCS_SOCK
SISTER_BUS_SOCK = SISTER_BUS_CCS_SOCK


class CCSClient:
    """CCS Socket 客户端 — 连接 sister_bus_ccs.sock

    线程安全的连接池：同一 (role, loop) 复用连接，最多 hold 60s 空闲断连。
    """
    _pools: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter, float]] = {}

    def __init__(self, role: str):
        self.role = role
        self.agent = f"ccs-{role}"
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self, max_retries: int = 3) -> bool:
        """连接 socket，失败时自动重试（指数退避）。

        优先复用连接池中的存活连接，跳过重复 SUBSCRIBE。
        池中连接空闲 >60s 自动失效。

        Args:
            max_retries: 最大重试次数（默认 3），0 表示不重试。
        """
        import asyncio
        _POOL_IDLE_TIMEOUT = 60.0
        now = time.time()
        # 尝试复用池中连接
        pool_key = self.role
        pooled = self._pools.get(pool_key)
        if pooled:
            r, w, ts = pooled
            if not w.is_closing() and (now - ts) < _POOL_IDLE_TIMEOUT:
                self.reader, self.writer = r, w
                log.debug("connect(%s): reusing pooled conn", self.role)
                return True
            # 过期或已关闭，丢弃
            try:
                w.close()
                await w.wait_closed()
            except Exception as e:
                log.warning("connect(%s): 清理过期连接出错: %s", self.role, e)
            self._pools.pop(pool_key, None)

        # 新建连接
        for attempt in range(max_retries + 1):
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(str(SISTER_BUS_SOCK))
                cmd = {"cmd": "SUBSCRIBE", "agent": self.agent}
                await self._send(cmd)
                resp = await asyncio.wait_for(self.reader.readline(), timeout=5)
                data = json.loads(resp.decode().strip())
                ok = data.get("event") == "subscribed"
                if ok:
                    self._pools[pool_key] = (self.reader, self.writer, time.time())
                    return True
                log.warning("connect(%s): unexpected response %s", self.role, data)
            except (OSError, asyncio.TimeoutError, ConnectionRefusedError) as e:
                log.warning("connect(%s) attempt %d/%d failed: %s", self.role, attempt, max_retries, e)
            except Exception as e:
                log.warning("connect(%s) attempt %d/%d error: %s", self.role, attempt, max_retries, e)
            if attempt < max_retries:
                wait = 0.5 * (2 ** (attempt - 1))  # 指数退避: 0.5s, 1s, 2s
                await asyncio.sleep(wait)
        return False

    async def _send(self, cmd: dict):
        data = (json.dumps(cmd, ensure_ascii=False) + "\n").encode()
        self.writer.write(data)
        await self.writer.drain()

    async def send_to(self, target_role: str, text: str) -> bool:
        """发送消息给另一个 CCS。target_role 是角色名（不含 ccs- 前缀）。"""
        target = f"ccs-{target_role}"
        await self._send({
            "cmd": "PUBLISH",
            "to": target,
            "msg": {"text": text, "type": "chat", "_from_role": self.role}
        })
        return True

    async def listen(self, cb: Callable[[dict], None]):
        """监听接收的消息。阻塞直到连接断开。"""
        while True:
            try:
                line = await self.reader.readline()
                if not line:
                    break
                msg = json.loads(line.decode().strip())
                if msg.get("event") == "message":
                    cb(msg.get("msg", {}))
            except json.JSONDecodeError as e:
                log.warning("listen(%s): 畸形消息，跳过: %s", self.role, e)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.warning("listen(%s): 连接断开: %s", self.role, e)
                break

    async def close(self):
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                log.warning("close(%s): %s", self.role, e)
            finally:
                self._pools.pop(self.role, None)
                self.reader = self.writer = None


# ── 流式输出（tmux capture-pane）────

class CCSStreamer:
    """CCS 流式输出 — 实时捕获 tmux pane 增量"""

    def __init__(self, role: str):
        self.role = role
        self.tmux = f"ccs-{role}"
        self._running = False
        self._last = ""

    def start(self, cb: Callable[[str], None]):
        self._running = True
        def _poll():
            while self._running:
                try:
                    r = subprocess.run(
                        ["tmux", "capture-pane", "-p", "-t", f"{self.tmux}:0.0", "-S", "-500"],
                        capture_output=True, text=True, timeout=3)
                    if r.returncode == 0:
                        cur = r.stdout
                        if cur != self._last:
                            if self._last and cur.startswith(self._last):
                                cb(cur[len(self._last):])
                            else:
                                cb(cur)
                            self._last = cur
                except subprocess.TimeoutExpired:
                    pass
                except Exception as e:
                    log.warning("stream poll error: %s", e)
                time.sleep(0.5)
        t = threading.Thread(target=_poll, daemon=True)
        t.start()

    def stop(self):
        self._running = False


# ── CLI 工具 ──

async def send_direct(from_role: str, to_role: str, text: str):
    cli = CCSClient(from_role)
    if await cli.connect():
        await cli.send_to(to_role, text)
        print(f"{from_role} → {to_role}: {text[:80]}")
        await cli.close()
    else:
        print(f"❌ 无法连接 sister_bus_ccs.sock ({SISTER_BUS_SOCK})")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="CCS Socket 工具")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("send")
    s.add_argument("from_role")
    s.add_argument("to_role")
    s.add_argument("message")

    args = p.parse_args()
    if args.cmd == "send":
        asyncio.run(send_direct(args.from_role, args.to_role, args.message))
