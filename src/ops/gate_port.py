#!/usr/bin/env python3
"""gate_port.py — 从 opencode_quota 迁出的活代码，非退役对象。

旧监测链路（ops/opencode_quota.py：429/transport 日志判据、rotate、唤醒、
杀引擎回收）已随用户裁决「全部退役」整体下线；本模块只保留仍在用的
闸口端口动态发现：GET /slots（同一 UDS + token）取 kind=="opencode" 的
port——core._opencode_gate_port 端口三级发现的最后一级。
函数体与迁出前一字不变（含注释），仅此模块 docstring 与 logger 名更新。
"""

import http.client
import json
import logging
import os
import socket
from pathlib import Path

_log = logging.getLogger("gate_port")


# ── rotate（契约 C1）─────────────────────────────────────────────

class _UnixHTTPConnection(http.client.HTTPConnection):
    """Unix Socket 上的极简 HTTP 连接（asip rotate 端点）。"""

    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _cfg_or_env(env_name: str, cfg_key: str) -> str | None:
    """env 覆盖 config（ops.ccs_config 点路径）。都没有 → None（显式缺失）。"""
    v = os.environ.get(env_name)
    if v and v.strip():
        return v.strip()
    try:
        from ops.ccs_config import get_value
        v = get_value(cfg_key)
    except Exception as e:  # noqa: BLE001
        _log.debug("读 config %s 失败: %s", cfg_key, e)
        return None
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def discover_gate_port() -> int | None:
    """GET /slots（同一 UDS + token）取 kind=="opencode" 那条的 port。

    impl-gate 建议的端口发现方式：opencode 闸口端口随 slot 编号顺延
    （默认 7898 只是 slot-8 的当前值），写死会随 consumer_slots/slots 变更失效。
    发现不到/端点未上线 → None（调用方显式降级为不注入）。
    """
    sock_path = _cfg_or_env("OPENCODE_GATE_ROTATE_SOCK", "opencode_gate.rotate_socket")
    token_file = _cfg_or_env("OPENCODE_GATE_TOKEN_FILE", "opencode_gate.rotate_token_file")
    if not sock_path or not token_file:
        return None
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    conn = _UnixHTTPConnection(sock_path, timeout=3.0)
    try:
        conn.request("GET", "/slots", headers={"X-Asip-Token": token})
        resp = conn.getresponse()
        raw = resp.read()
        if not (200 <= resp.status < 300):
            return None
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    finally:
        conn.close()
    items = data if isinstance(data, list) else (
        data.get("slots") if isinstance(data, dict) else None)
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("kind") == "opencode":
            port = item.get("port")
            if isinstance(port, int) and 1 <= port <= 65535:
                return port
    return None
