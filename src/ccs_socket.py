#!/usr/bin/env python3
"""
ccs_socket.py — CCS 直接通信客户端（复用 sister_bus socket server）

CCS 通过 sister_bus /tmp/sister_bus_ccs.sock 通信。
SUBSCRIBE 注册为 "ccs-{role}"，PUBLISH 按目标路由。

用法:
    ccs.py send-direct alice bob hi     # 直连 <1ms
"""

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ccs-socket")

# 复用 sister_bus socket server（由 systemd 管理）。
# 不经 paths 转接：pipeline 的 src 与本仓同名模块(paths)会被 test_gateway 注入的
# sys.path 抢先解析，谁先 import 谁赢 —— 直连 hermes_bus.config 断掉对 import 顺序的依赖。
sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
from hermes_bus.config import SISTER_BUS_CCS_SOCK
SISTER_BUS_SOCK = SISTER_BUS_CCS_SOCK

# 连接池锁（防止多线程并发修改 _pools）
_pools_lock = threading.Lock()


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
        pool_key = self.role

        # 尝试复用池中连接（加锁）
        with _pools_lock:
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
            with _pools_lock:
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
                    with _pools_lock:
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
        """发送消息给另一个 CCS。target_role 是角色名（不含 ccs- 前缀）。

        注：role→role IPC 的意图对齐未在此处注入。PUBLISH 消息经 sister_bus
        socket server 服务端落盘到目标 tmux，注入点在该 server 层（跨 repo，不在
        session-launcher）。此处仅承载纯角色协商（_from_role=本角色），不携带用户
        意图，故无提醒注入——保持与 Round1 SCOPE_BOUNDARY.md 一致，避免死代码 opt-in。
        """
        target = f"ccs-{target_role}"
        await self._send({
            "cmd": "PUBLISH",
            "to": target,
            "msg": {"text": text, "type": "chat", "_from_role": self.role}
        })
        return True

    async def close(self):
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                log.warning("close(%s): %s", self.role, e)
            finally:
                with _pools_lock:
                    self._pools.pop(self.role, None)
                self.reader = self.writer = None



