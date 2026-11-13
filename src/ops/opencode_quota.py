#!/usr/bin/env python3
"""opencode_quota.py — opencode 免费额度 429 监测器（契约 C1/C2/C3）。

watchdog 周期调用 check_once()：

1. tail `~/.local/share/opencode/log/opencode.log`，命中 C2 判据
   （行内同时含 `level=ERROR`、`providerID=opencode`、
   `error.error="AI_APICallError: Free usage limit reached`）；
2. 经 `opencode.db`（**只读**）`session.id → directory` 归属 ccs 工作区/角色；
3. 调 asip rotate 换出口 IP（C1：POST /rotate-opencode-gate over Unix Socket，
   头 X-Asip-Token）——归属失败也换（只换 IP 不唤醒，宁可漏唤醒不误唤醒）；
4. 归属成功则对该会话发 C3 唤醒文案（复用 launcher send 通道）。

重复抑制：同一 session.id 冷却窗（COOLDOWN_SEC=60s）内不重复换 IP + 不重复唤醒。
rotate 的 socket/token 路径读 config（opencode_gate.rotate_socket /
opencode_gate.rotate_token_file，env OPENCODE_GATE_ROTATE_SOCK /
OPENCODE_GATE_TOKEN_FILE 覆盖），缺配置显式降级（告警 + 跳过 rotate）。
"""

import http.client
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("opencode_quota")

# ── 观测面路径（契约 C2：~/.local/share/opencode/...）──
LOG_PATH = Path.home() / ".local" / "share" / "opencode" / "log" / "opencode.log"
DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
WS_ROOT = Path.home() / "ccs-workspaces"

# C3 唤醒文案（与 session-pipeline role_lifecycle._RATE_LIMIT_NUDGE 一字不差）
WAKE_MSG = ("上一轮 API 错误中断（0 产出）。继续：重发被中断的那次请求，"
            "不要重新规划，不要写自检报告。")

# C2 判据 needle：error.error="AI_APICallError: Free usage limit reached
# （按前缀匹配，容忍真实日志里该值后继续接 ". Free models work in OpenCode…"；
#   含收尾引号的完整 needle 在真实日志上命中数为 0，见 REPORT）
_NEEDLES = (
    "level=ERROR",
    "providerID=opencode",
    'error.error="AI_APICallError: Free usage limit reached',
)

_TS_RE = re.compile(r"\btimestamp=(\S+)")
_SID_RE = re.compile(r"\bsession\.id=(ses_[A-Za-z0-9]+)")

COOLDOWN_SEC = 60.0
_FRESH_SEC = 600.0        # 只处理近 10 分钟的命中（防日志轮转后重放旧账）
_MIN_SCAN_INTERVAL = 10.0  # 全局扫描节流（watchdog 多线程并发调用时只扫一次）

_last_handled: dict[str, float] = {}
_cool_lock = threading.Lock()
_scan_lock = threading.Lock()
_last_scan = 0.0
_scan = {"offset": None}  # None = 首次：跳到 EOF，只跟增量


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


def rotate_gate(session_id: str, reason: str, request_started_at: str = "",
                retry_after_ms: int | None = None) -> bool:
    """C1：POST /rotate-opencode-gate over Unix Socket，头 X-Asip-Token。

    socket/token 路径读 config（opencode_gate.rotate_socket /
    opencode_gate.rotate_token_file），缺任一 → 显式降级（告警 + 不换 IP）。
    req_id 传 session.id（契约 C1）。
    """
    sock_path = _cfg_or_env("OPENCODE_GATE_ROTATE_SOCK", "opencode_gate.rotate_socket")
    token_file = _cfg_or_env("OPENCODE_GATE_TOKEN_FILE", "opencode_gate.rotate_token_file")
    if not sock_path or not token_file:
        _log.warning("rotate 配置缺失（opencode_gate.rotate_socket/rotate_token_file"
                     " 或 OPENCODE_GATE_ROTATE_SOCK/OPENCODE_GATE_TOKEN_FILE），"
                     "显式降级：本轮不换 IP session=%s", session_id)
        return False
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as e:
        _log.warning("读取 asip token 失败 %s: %s，不换 IP", token_file, e)
        return False
    if not token:
        _log.warning("asip token 为空（%s），不换 IP", token_file)
        return False
    body = json.dumps({
        "req_id": session_id,
        "reason": reason,
        "egress_ip": "",
        "request_started_at": request_started_at,
        "retry_after_ms": retry_after_ms,
        "session_id": session_id,
    }).encode("utf-8")
    conn = _UnixHTTPConnection(sock_path, timeout=5.0)
    try:
        conn.request("POST", "/rotate-opencode-gate", body=body,
                     headers={"X-Asip-Token": token,
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        if 200 <= resp.status < 300:
            _log.info("rotate-opencode-gate 成功 session=%s", session_id)
            return True
        _log.warning("rotate-opencode-gate HTTP %s session=%s", resp.status, session_id)
        return False
    except OSError as e:
        _log.warning("rotate-opencode-gate 连接失败 %s: %s", sock_path, e)
        return False
    finally:
        conn.close()


# ── 归属（契约 C2：session.id → directory → 角色）────────────────

def session_directory(session_id: str) -> str | None:
    """opencode.db（只读）查 session.directory；查不到返回 None。"""
    if not DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT directory FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error as e:
        _log.warning("opencode.db 只读查询失败: %s", e)
        return None


def attribute_session(directory: str | None) -> tuple[str, int] | None:
    """directory → (role, instance_id)；归属失败返回 None（只换 IP 不唤醒）。

    判据：~/ccs-workspaces/{role} 或 ~/ccs-workspaces/{role}/instances/{n}，
    且该角色在哨兵注册表里（防把用户手开的 opencode 会话误归属给角色）。
    自定义 workspace 名（ws_name≠role）无法反解 → 按归属失败处理（宁可漏唤醒）。
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve()
        root = WS_ROOT.expanduser().resolve()
        rel = p.relative_to(root)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if len(parts) == 1:
        role, instance_id = parts[0], 0
    elif len(parts) == 3 and parts[1] == "instances" and parts[2].isdigit():
        role, instance_id = parts[0], int(parts[2])
    else:
        return None
    try:
        from ops.validators import _ROLE_NAME_RE
        if not _ROLE_NAME_RE.match(role):
            return None
        from ops.sentinel import read_sentinel
        if read_sentinel(role, instance_id) is None:
            return None
    except Exception as e:  # noqa: BLE001
        _log.warning("归属确认失败 %s/%s: %s", role, instance_id, e)
        return None
    return role, instance_id


# ── 唤醒（契约 C3，复用 launcher send 通道）───────────────────────

def wake_role(role: str, instance_id: int = 0) -> dict:
    """对该会话发 C3 唤醒（core.send，source=pipeline 走自动化白名单）。"""
    from core import send
    return send(role, WAKE_MSG, source="pipeline", instance_id=instance_id)


# ── 日志 tail + 判据 ────────────────────────────────────────────

def parse_quota_line(line: str) -> dict | None:
    """C2 判据：行内同时含 level=ERROR / providerID=opencode /
    error.error="AI_APICallError: Free usage limit reached → 提取命中信息。

    命中但无 session.id 时返回 session_id="" 的命中（handle_hit 跳过并留痕）：
    无 session.id 就没有 req_id 可传，不能只凭空 rotate。
    """
    if not all(n in line for n in _NEEDLES):
        return None
    m_sid = _SID_RE.search(line)
    m_ts = _TS_RE.search(line)
    started_at = m_ts.group(1) if m_ts else ""
    return {
        "session_id": m_sid.group(1) if m_sid else "",
        "started_at": started_at,
        "reason": "AI_APICallError: Free usage limit reached",
    }


def _fresh(started_at: str) -> bool:
    if not started_at:
        return False
    dt = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(started_at, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return False
    age = time.time() - dt.replace(tzinfo=timezone.utc).timestamp()
    return -30.0 <= age <= _FRESH_SEC


def _read_new_lines(path: Path) -> list[str]:
    """按字节 offset tail 新增完整行。首次调用跳到 EOF（只跟增量）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    offset = _scan["offset"]
    if offset is None or size < offset:
        offset = size if _scan["offset"] is None else 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        _scan["offset"] = offset
        return []
    cut = data.rfind(b"\n")
    if cut == -1:
        return []  # 半行留到下轮
    _scan["offset"] = offset + cut + 1
    return data[:cut + 1].decode("utf-8", errors="replace").splitlines()


# ── 主流程 ─────────────────────────────────────────────────────

def handle_hit(hit: dict) -> dict:
    """冷却去重 → rotate 换 IP → 归属成功才唤醒（C2/C3）。"""
    sid = hit.get("session_id") or ""
    if not sid:
        return {"action": "skip", "reason": "no session id"}
    now = time.monotonic()
    with _cool_lock:
        # 顺手清掉冷却窗外的旧条目（长驻 watchdog 进程防字典无界增长）
        for k in [k for k, t in _last_handled.items()
                  if now - t > max(COOLDOWN_SEC * 10, _FRESH_SEC)]:
            _last_handled.pop(k, None)
        last = _last_handled.get(sid)
        if last is not None and now - last < COOLDOWN_SEC:
            return {"action": "suppressed", "session_id": sid}
        _last_handled[sid] = now
    rotated = rotate_gate(sid, reason=hit.get("reason", ""),
                          request_started_at=hit.get("started_at", ""))
    directory = session_directory(sid)
    target = attribute_session(directory)
    if target is None:
        _log.warning("session %s 归属失败（dir=%s），只换 IP 不唤醒", sid, directory)
        return {"action": "rotate_only", "session_id": sid, "rotated": rotated}
    role, instance_id = target
    woken = wake_role(role, instance_id)
    return {"action": "rotate_and_wake", "session_id": sid, "rotated": rotated,
            "role": role, "instance_id": instance_id, "woken": woken}


def check_once() -> list[dict]:
    """watchdog 周期入口：tail 日志 → 逐条 handle_hit。全局节流 + 线程安全。"""
    global _last_scan
    with _scan_lock:
        now = time.monotonic()
        if now - _last_scan < _MIN_SCAN_INTERVAL:
            return []
        _last_scan = now
        lines = _read_new_lines(LOG_PATH)
    results = []
    for line in lines:
        hit = parse_quota_line(line)
        if not hit or not _fresh(hit.get("started_at", "")):
            continue
        try:
            results.append(handle_hit(hit))
        except Exception as e:  # noqa: BLE001 — 单条失败不打断整批
            _log.warning("handle_hit 异常 %s: %s", hit.get("session_id"), e)
    return results
