#!/usr/bin/env python3
"""
ccs_socket.py — CCS 直接通信 + 流式输出

独立 Socket Server（不依赖 sister_socket_server）。
每个 CCS 通过 agent 名订阅，PUBLISH 按 agent 路由，<1ms。

用法:
    ccs.py socket start              # 启动 CCS Socket Server
    ccs.py send-direct alice bob hi  # A → B 直连
    ccs.py stream bob                # 流式显示 B 输出
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SOCKET_DIR = Path("/tmp/ccs-sockets")
AGENTS_FILE = SOCKET_DIR / "agents.json"


# ── CCS Socket Server（独立，不依赖 sister_socket_server）────

class CSSocketServer:
    """动态 agent Socket Server — 任何 CCS 角色都能连接"""

    def __init__(self, socket_path: Path = SOCKET_DIR / "ccs.sock"):
        self.socket_path = socket_path
        self.subscribers: dict[str, set[asyncio.StreamWriter]] = {}
        self._servers: list[asyncio.AbstractServer] = []
        self._start_time = time.time()
        self._msg_count = 0

    async def start(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._handler, path=str(self.socket_path))
        self._servers.append(server)
        self.socket_path.chmod(0o600)
        print(f"Socket server 监听 {self.socket_path}")

    async def stop(self):
        for s in self._servers:
            s.close()
            await s.wait_closed()
        self.socket_path.unlink(missing_ok=True)
        AGENTS_FILE.unlink(missing_ok=True)

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        agent = "unknown"
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=600)
                if not line:
                    break
                cmd = json.loads(line.decode().strip())
                resp = await self._dispatch(cmd, agent, writer)
                if resp and "agent" in resp.get("data", {}):
                    agent = resp["data"]["agent"]
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            for a in list(self.subscribers.keys()):
                self.subscribers[a].discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, cmd: dict, agent: str, writer: asyncio.StreamWriter) -> dict | None:
        t = cmd.get("cmd", "")
        if t == "SUBSCRIBE":
            a = cmd.get("agent", agent)
            if a not in self.subscribers:
                self.subscribers[a] = set()
            self.subscribers[a].add(writer)
            # 注册到 agents.json
            self._save_agent(a)
            await self._send(writer, {"event": "subscribed", "agent": a})
            return {"data": {"agent": a}}
        elif t == "PUBLISH":
            target = cmd.get("to", "")
            msg = cmd.get("msg", {})
            msg["_from"] = agent
            msg["_ts"] = time.time()
            self._msg_count += 1
            await self._push(target, {"event": "message", "msg": msg})
            return {"data": {"ok": True}}
        elif t == "PING":
            await self._send(writer, {"event": "pong", "uptime": round(time.time() - self._start_time, 1)})
        elif t == "STATS":
            await self._send(writer, {
                "event": "stats",
                "agents": {k: len(v) for k, v in self.subscribers.items()},
                "msg_count": self._msg_count,
            })
        return None

    async def _push(self, agent: str, data: dict):
        payload = (json.dumps(data, ensure_ascii=False) + "\n").encode()
        dead = []
        for w in self.subscribers.get(agent, set()):
            try:
                w.write(payload)
                await w.drain()
            except (ConnectionError, OSError):
                dead.append(w)
        for w in dead:
            self.subscribers[agent].discard(w)

    async def _send(self, writer: asyncio.StreamWriter, data: dict):
        try:
            payload = (json.dumps(data, ensure_ascii=False) + "\n").encode()
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    def _save_agent(agent: str):
        try:
            agents = {}
            if AGENTS_FILE.exists():
                agents = json.loads(AGENTS_FILE.read_text())
            agents[agent] = time.time()
            AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            AGENTS_FILE.write_text(json.dumps(agents, indent=2))
        except Exception:
            pass


class CCSClient:
    """CCS Socket 客户端"""

    def __init__(self, role: str, socket_path: Path = SOCKET_DIR / "ccs.sock"):
        self.role = role
        self.socket_path = socket_path
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> bool:
        try:
            self.reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path))
            await self._send({"cmd": "SUBSCRIBE", "agent": self.role})
            return True
        except Exception:
            return False

    async def _send(self, cmd: dict):
        data = (json.dumps(cmd, ensure_ascii=False) + "\n").encode()
        self.writer.write(data)
        await self.writer.drain()

    async def send_to(self, target: str, text: str) -> bool:
        await self._send({"cmd": "PUBLISH", "to": target, "msg": {"text": text, "type": "chat"}})
        return True

    async def listen(self, cb: Callable):
        while True:
            line = await self.reader.readline()
            if not line:
                break
            msg = json.loads(line.decode().strip())
            if msg.get("event") == "message":
                cb(msg.get("msg", {}))

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()


# ── 流式输出（pexpect 实时捕获）────

class CCSStreamer:
    """CCS 流式输出"""

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
                except Exception:
                    pass
                time.sleep(0.5)
        t = threading.Thread(target=_poll, daemon=True)
        t.start()

    def stop(self):
        self._running = False


def start_server():
    """启动 CCS Socket Server（后台进程）"""
    script = Path(__file__).resolve()
    proc = subprocess.Popen(
        [sys.executable, str(script), "server-foreground"],
        cwd=str(script.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    if proc.poll() is None:
        print(f"CCS Socket server PID={proc.pid}")
    else:
        print(f"CCS Socket server 启动失败 (exit={proc.returncode})")


async def run_server_foreground():
    server = CSSocketServer()
    await server.start()
    pid_path = SOCKET_DIR / "server.pid"
    pid_path.write_text(str(os.getpid()))
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        await server.stop()
    finally:
        pid_path.unlink(missing_ok=True)


async def send_direct(from_role: str, to_role: str, text: str):
    cli = CCSClient(from_role)
    if await cli.connect():
        await cli.send_to(to_role, text)
        print(f"{from_role} → {to_role}: {text[:80]}")
        await cli.close()
    else:
        print(f"❌ 无法连接 CCS Socket Server（{SOCKET_DIR / 'ccs.sock'}）")
        sys.exit(1)


def list_agents() -> list[str]:
    if AGENTS_FILE.exists():
        return list(json.loads(AGENTS_FILE.read_text()).keys())
    return []


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "server-foreground":
        asyncio.run(run_server_foreground())
    else:
        import argparse
        p = argparse.ArgumentParser(description="CCS Socket 工具")
        sub = p.add_subparsers(dest="cmd")
        sub.add_parser("server")
        sub.add_parser("status")
        s = sub.add_parser("send")
        s.add_argument("from_role")
        s.add_argument("to_role")
        s.add_argument("message")

        args = p.parse_args()
        if args.cmd == "server":
            start_server()
        elif args.cmd == "status":
            agents = list_agents()
            print(f"已注册 agent: {agents}")
        elif args.cmd == "send":
            asyncio.run(send_direct(args.from_role, args.to_role, args.message))
