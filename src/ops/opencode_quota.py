#!/usr/bin/env python3
"""opencode_quota.py — opencode 免费额度 429 监测器（契约 C1/C2/C3）。

watchdog 周期调用 check_once()：

1. tail `~/.local/share/opencode/log/opencode.log`，命中 C2 判据
   （行内同时含 `level=ERROR`、`providerID=opencode`、`Free usage limit reached`，
   且 error.error= 含 AI_APICallError 原生形态或 AI_RetryError 包装形态之一）；
2. 经 `opencode.db`（**只读**）`session.id → directory` 归属 ccs 工作区/角色；
3. 调 asip rotate 换出口 IP（C1：POST /rotate-opencode-gate over Unix Socket，
   头 X-Asip-Token）——归属失败也换（只换 IP 不唤醒，宁可漏唤醒不误唤醒）；
4. 归属成功则对该会话发 C3 唤醒文案（复用 launcher send 通道）。

重复抑制：同一 session.id 冷却窗（COOLDOWN_SEC=60s）内不重复换 IP + 不重复唤醒。
rotate 的 socket/token 路径读 config（opencode_gate.rotate_socket /
opencode_gate.rotate_token_file，env OPENCODE_GATE_ROTATE_SOCK /
OPENCODE_GATE_TOKEN_FILE 覆盖），缺配置显式降级（告警 + 跳过 rotate）。

与 asip 侧（impl-gate 2026-11-14 回复）对齐的语义：
- reason 只认 rate429|abuse403|transport|proactive 白名单 → 429 命中恒传
  rate429；transport 断连命中（T1）恒传 transport；
- request_started_at 传数值时间戳（epoch 秒，非数 asip 侧按 None 丢弃）；
- 回包 switched=false 且 reason∈{no_healthy_node, borrowed_skip} → 不唤醒
  （防在同 IP 上烧重试），60s 冷却即退避；reason=coalesced 视为可安全同端口重试。
  borrowed 可选字段不传（本监测器无法识别非本闸口会话，按常规报障处理）。
"""

import http.client
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
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

# C2 判据 needle（两组，B2 修复后覆盖原生 + 包装两种真实形态）：
# 公共组（全部命中）：level=ERROR + providerID=opencode + Free usage limit reached；
# 错误形态组（任一命中）：error.error= 含 AI_APICallError（原生：额度错误直接暴露）
#   或 AI_RetryError（包装：Failed after N attempts. Last error: Free usage limit reached）。
# 分两组的理由：一行日志只含一种错误形态，两条形态 needle 若塞进同一 all() 元组，
#   两种形态会同时匹配不到；公共组的 Free usage limit reached 保证非额度 AI_RetryError
#   （如 token 过期）不误命中。形态 needle 按前缀匹配，容忍真实日志里该值后继续接
#   ". Free models work in OpenCode…"（含收尾引号的完整 needle 在真实日志上命中数为 0）。
_COMMON_NEEDLES = (
    "level=ERROR",
    "providerID=opencode",
    "Free usage limit reached",
)
_ERROR_FORM_NEEDLES = (
    'error.error="AI_APICallError',
    'error.error="AI_RetryError',
)

# T1 transport 判据（2026-11-15 闸口死节点事故）：池节点死/半死的表现是
# 「socket 断连」而非 429 —— 429 判据不命中、没人换节点，全部 opencode
# 会话一起卡死在死节点上。真实形态（opencode.log 2026-11-14T19:24:45Z）：
# level=ERROR + providerID=opencode + error.error="AI_APICallError: Cannot
# connect to API: The socket connection was closed unexpectedly…"。
# 公共组 providerID=opencode 把 message=process 的伴生行、models.dev 目录
# 拉取失败等无 provider 行挡在外面（它们不是会话请求面）。
_TRANSPORT_COMMON_NEEDLES = ("level=ERROR", "providerID=opencode")
_TRANSPORT_FORM_NEEDLES = (
    "Cannot connect to API",
    "socket connection was closed",
)

_TS_RE = re.compile(r"\btimestamp=(\S+)")
_SID_RE = re.compile(r"\bsession\.id=(ses_[A-Za-z0-9]+)")

COOLDOWN_SEC = 60.0
_FRESH_SEC = 600.0        # 只处理近 10 分钟的命中（防日志轮转后重放旧账）
_MIN_SCAN_INTERVAL = 10.0  # 全局扫描节流（watchdog 多线程并发调用时只扫一次）

_last_handled: dict[str, float] = {}
_transport_last_ts = 0.0   # transport 全局抑制（闸口级事件，跨 session 只换一次）
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


def rotate_gate(session_id: str, reason: str, request_started_at: float | None = None,
                retry_after_ms: int | None = None) -> dict:
    """C1：POST /rotate-opencode-gate over Unix Socket，头 X-Asip-Token。

    socket/token 路径读 config（opencode_gate.rotate_socket /
    opencode_gate.rotate_token_file），缺任一 → 显式降级（告警 + 不换 IP）。
    req_id 传 session.id（契约 C1）。
    返回 {"ok", "switched", "reason"}：switched=False 且 reason∈{no_healthy_node,
    borrowed_skip} 时调用方不得唤醒（同 IP 上重试只会再撞 429）。
    """
    sock_path = _cfg_or_env("OPENCODE_GATE_ROTATE_SOCK", "opencode_gate.rotate_socket")
    token_file = _cfg_or_env("OPENCODE_GATE_TOKEN_FILE", "opencode_gate.rotate_token_file")
    if not sock_path or not token_file:
        _log.warning("rotate 配置缺失（opencode_gate.rotate_socket/rotate_token_file"
                     " 或 OPENCODE_GATE_ROTATE_SOCK/OPENCODE_GATE_TOKEN_FILE），"
                     "显式降级：本轮不换 IP session=%s", session_id)
        return {"ok": False, "switched": None, "reason": "config_missing"}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as e:
        _log.warning("读取 asip token 失败 %s: %s，不换 IP", token_file, e)
        return {"ok": False, "switched": None, "reason": "token_unreadable"}
    if not token:
        _log.warning("asip token 为空（%s），不换 IP", token_file)
        return {"ok": False, "switched": None, "reason": "token_empty"}
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
        raw = resp.read()
        if not (200 <= resp.status < 300):
            _log.warning("rotate-opencode-gate HTTP %s session=%s", resp.status, session_id)
            return {"ok": False, "switched": None, "reason": f"http_{resp.status}"}
        switched: bool | None = None
        reason_val = ""
        try:
            data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            if isinstance(data, dict):
                if isinstance(data.get("switched"), bool):
                    switched = data["switched"]
                reason_val = str(data.get("reason") or "")
        except (ValueError, TypeError):
            pass  # 回包非 JSON：按已换成功处理（HTTP 2xx）
        _log.info("rotate-opencode-gate 成功 session=%s switched=%s reason=%s",
                  session_id, switched, reason_val)
        return {"ok": True, "switched": switched, "reason": reason_val}
    except OSError as e:
        _log.warning("rotate-opencode-gate 连接失败 %s: %s", sock_path, e)
        return {"ok": False, "switched": None, "reason": "transport_error"}
    finally:
        conn.close()


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


# ── opencode 楔死解除（杀子进程 → pane 重生循环拉起）───────────────
SENTINEL_DIR = Path.home() / ".hermes" / "run" / "ccs-sentinels"
_OPENCODE_CMD_MARK = ".opencode/bin/opencode"


def _sentinel_engine(role: str) -> str | None:
    """读哨兵事实源的 engine。缺文件/缺键/读失败 → None（保守：不杀只发文案）。"""
    try:
        d = json.loads((SENTINEL_DIR / f"{role}.json").read_text(encoding="utf-8"))
        eng = d.get("engine")
        return eng if isinstance(eng, str) else None
    except (OSError, ValueError):
        return None


def _tmux_session_of(role: str) -> str:
    """角色 → tmux 会话名（哨兵 tmux_session 优先，回退 ccs-<role>）。"""
    try:
        d = json.loads((SENTINEL_DIR / f"{role}.json").read_text(encoding="utf-8"))
        name = d.get("tmux_session")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError):
        pass
    return "ccs-" + role


def _is_opencode_cmd(cmd: str) -> bool:
    """kill 判据（进程命令行含 .opencode/bin/opencode 才算，绝不误杀其它）。"""
    return _OPENCODE_CMD_MARK in (cmd or "")


def _opencode_child_pids(role: str) -> list[int]:
    """角色 tmux pane 子进程树里 opencode 进程的 pid 列表（只读）。"""
    session = _tmux_session_of(role)
    try:
        out = subprocess.run(["tmux", "list-panes", "-t", session,
                              "-F", "#{pane_pid}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    roots = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    found: list[int] = []
    frontier = list(roots)
    seen = set(roots)
    while frontier:
        cur = frontier.pop()
        try:
            k = subprocess.run(["pgrep", "-P", str(cur)],
                               capture_output=True, text=True, timeout=5)
            kids = [int(x) for x in k.stdout.split() if x.strip().isdigit()]
        except (OSError, subprocess.TimeoutExpired):
            kids = []
        for pid in kids:
            if pid in seen:
                continue
            seen.add(pid)
            frontier.append(pid)
            try:
                cmd = (Path(f"/proc/{pid}/cmdline").read_bytes()
                       .decode("utf-8", "replace"))
            except OSError:
                continue
            if _is_opencode_cmd(cmd):
                found.append(pid)
    return found


def restart_opencode(role: str, instance_id: int = 0) -> dict:
    """杀 pane 树里的 opencode 子进程并等重生循环拉起新进程。

    opencode 撞 429 后 TUI 进内部退避（Retry-After 可达 17h+），打字/esc
    唤不醒（实测）；唯一有效是杀进程 —— pane 的 `while true; do opencode; done`
    循环自动拉起新进程（会话上下文在 opencode.db，同 cwd 可续）。
    SIGTERM，5s 不退 SIGKILL；随后 ≤15s 轮询新 opencode 出现。
    """
    pids = _opencode_child_pids(role)
    if not pids:
        return {"killed": [], "respawned": False, "reason": "no opencode process"}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not any(Path(f"/proc/{p}").exists() for p in pids):
            break
        time.sleep(0.3)
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    deadline = time.time() + 15.0
    new_pid = None
    while time.time() < deadline:
        fresh = _opencode_child_pids(role)
        if fresh:
            new_pid = fresh[0]
            break
        time.sleep(0.5)
    return {"killed": pids, "respawned": new_pid is not None,
            "new_pid": new_pid,
            "reason": None if new_pid else "no respawn within 15s"}


# ── 一次性宿主的状态持久化（offset + 冷却跨进程）──────────────────
_STATE_PATH = Path.home() / ".hermes" / "run" / "opencode_quota_state.json"


def _load_state() -> dict:
    try:
        d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STATE_PATH)
    except OSError as e:
        _log.warning("opencode_quota 状态落盘失败（下轮可能重复处理）: %s", e)


def run_once() -> list[dict]:
    """一次性宿主单轮（ccs-monitor-daemon 60s 周期调用的入口）。

    与长驻 watchdog 线程里的 check_once 的差别：offset/冷却是进程内存态，
    一次性宿主每次新进程会重放日志、重复轮换 —— 故持久化到
    opencode_quota_state.json。首次运行跳到 EOF（不重放历史）。
    """
    global _last_scan, _scan, _transport_last_ts
    state = _load_state()
    with _cool_lock:
        _last_handled.clear()
        for k, v in (state.get("handled") or {}).items():
            if isinstance(v, (int, float)):
                _last_handled[k] = float(v)
        v = state.get("transport_last_ts")
        _transport_last_ts = float(v) if isinstance(v, (int, float)) else 0.0
    with _scan_lock:
        off = state.get("offset")
        _scan["offset"] = int(off) if isinstance(off, int) else None
        _last_scan = 0.0
    results = check_once()
    with _cool_lock:
        handled = dict(_last_handled)
        transport_ts = _transport_last_ts
    _save_state({"offset": _scan.get("offset"), "handled": handled,
                 "transport_last_ts": transport_ts})
    return results


# ── 日志 tail + 判据 ────────────────────────────────────────────

def _parse_ts(started_at: str) -> float | None:
    """ISO 日志时间戳 → epoch 秒（供 _fresh 与 request_started_at 数值传递）。"""
    if not started_at:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(started_at, fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def parse_quota_line(line: str) -> dict | None:
    """C2 判据（B2 修复：原生 AI_APICallError 与包装 AI_RetryError 两种形态都命中）：
    行内含 level=ERROR / providerID=opencode / Free usage limit reached，
    且 error.error= 含 AI_APICallError 或 AI_RetryError 之一 → 提取命中信息。

    reason 按 asip 白名单传 rate429（只认 rate429|abuse403|transport|proactive）——
    两种形态同为免费额度 429 语义，统一传 rate429；started_at_epoch 为数值时间戳
    （asip 侧 request_started_at 只收数）。
    命中但无 session.id 时返回 session_id="" 的命中（handle_hit 跳过并留痕）：
    无 session.id 就没有 req_id 可传，不能只凭空 rotate。
    """
    if not all(n in line for n in _COMMON_NEEDLES):
        return None
    if not any(n in line for n in _ERROR_FORM_NEEDLES):
        return None
    m_sid = _SID_RE.search(line)
    m_ts = _TS_RE.search(line)
    started_at = m_ts.group(1) if m_ts else ""
    return {
        "session_id": m_sid.group(1) if m_sid else "",
        "started_at": started_at,
        "started_at_epoch": _parse_ts(started_at),
        "reason": "rate429",
    }


def parse_transport_line(line: str) -> dict | None:
    """T1 transport 判据：ERROR + providerID=opencode + 断连形态之一 → 命中。

    reason 传 asip 白名单值 transport。无 session.id 也可命中（req_id 用
    synthetic 串）——transport 轮换是闸口级动作，不依赖会话归属；不唤醒
    （断连不像 429 把 TUI 楔进 17h 退避，用户重发即走新出口）。
    """
    if not all(n in line for n in _TRANSPORT_COMMON_NEEDLES):
        return None
    if not any(n in line for n in _TRANSPORT_FORM_NEEDLES):
        return None
    m_sid = _SID_RE.search(line)
    m_ts = _TS_RE.search(line)
    started_at = m_ts.group(1) if m_ts else ""
    return {
        "session_id": m_sid.group(1) if m_sid else "",
        "started_at": started_at,
        "started_at_epoch": _parse_ts(started_at),
        "reason": "transport",
    }


def _fresh(started_at: str) -> bool:
    ts = _parse_ts(started_at)
    if ts is None:
        return False
    age = time.time() - ts
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
    rotated = rotate_gate(sid, reason=hit.get("reason", "rate429"),
                          request_started_at=hit.get("started_at_epoch"))
    if (isinstance(rotated, dict) and rotated.get("switched") is False
            and rotated.get("reason") in ("no_healthy_node", "borrowed_skip")):
        # asip 语义：switched=false 时同端口重试只会再撞 429（no_healthy_node
        # 按 60s 冷却退避）。不唤醒防烧重试。
        _log.warning("session %s rotate 未换节点（%s），不唤醒防烧重试",
                     sid, rotated.get("reason"))
        return {"action": "rotate_only", "session_id": sid, "rotated": rotated,
                "wake_skipped": rotated.get("reason")}
    directory = session_directory(sid)
    target = attribute_session(directory)
    if target is None:
        _log.warning("session %s 归属失败（dir=%s），只换 IP 不唤醒", sid, directory)
        return {"action": "rotate_only", "session_id": sid, "rotated": rotated}
    role, instance_id = target
    # opencode 会话撞 429 后 TUI 进内部退避（Retry-After 可达 17h+），打字
    # 唤不醒（实测）——必须杀 opencode 子进程让 pane 重生循环拉起新进程再发
    # C3 续发。换 IP 先于杀进程（rotate 在上面已完成）：新进程首发走新出口。
    # engine 读哨兵事实源；缺证据只发文案、绝不杀（保守）。
    restarted: dict = {}
    if _sentinel_engine(role) == "opencode":
        restarted = restart_opencode(role, instance_id)
        if not restarted.get("respawned"):
            _log.warning("session %s opencode 重生未确认（%s），降级只发 C3",
                         sid, restarted.get("reason", "unknown"))
    woken = wake_role(role, instance_id)
    return {"action": "rotate_and_wake", "session_id": sid, "rotated": rotated,
            "role": role, "instance_id": instance_id, "woken": woken,
            "restarted": restarted}


# ── transport（T1）：断连形态 → 判定矩阵 → 只换不唤醒 ─────────────

PROBE_URL = "https://api.opencode.ai/"
_PROBE_TIMEOUT_SEC = 5.0


def _probe_reachable(proxy: str | None) -> bool:
    """经/不经代理探一次 HTTPS 可达性（任何 HTTP 响应都算通）。"""
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(PROBE_URL, timeout=_PROBE_TIMEOUT_SEC):
            return True
    except Exception:  # noqa: BLE001 — 断连/超时/DNS 全算不可达
        return False


def gate_vs_direct_verdict() -> str:
    """判定矩阵：只有「直连通、经闸口断」才值得换节点（防误杀）。

    - 闸口通 → skip_transient（会话侧断连是瞬时抖动，换节点是瞎换）
    - 闸口断 + 直连断 → skip_network（本机出口挂了，换闸口节点没用）
    - 闸口端口发现不到 → skip_no_port（显式降级，不盲换）
    - 闸口断 + 直连通 → rotate（节点死，换节点能救）
    """
    port = discover_gate_port()
    if not port:
        return "skip_no_port"
    if _probe_reachable(f"http://127.0.0.1:{port}"):
        return "skip_transient"
    return "rotate" if _probe_reachable(None) else "skip_network"


def handle_transport_hit(hit: dict) -> dict:
    """transport 命中：判定矩阵通过才 rotate（reason=transport），不唤醒。

    全局 60s 抑制（跨 session）：死节点是闸口级事件，多会话同时报错只
    换一次；抑制窗同时限定探测频率（每窗至多一次判定矩阵）。
    """
    global _transport_last_ts
    now = time.time()
    with _cool_lock:
        if _transport_last_ts and now - _transport_last_ts < COOLDOWN_SEC:
            return {"action": "suppressed", "kind": "transport",
                    "reason": "transport cooldown"}
        _transport_last_ts = now
    verdict = gate_vs_direct_verdict()
    if verdict != "rotate":
        _log.info("transport 命中但 %s，不换节点 session=%s",
                  verdict, hit.get("session_id") or "-")
        return {"action": "skip", "kind": "transport", "reason": verdict}
    sid = hit.get("session_id") or ("transport-%d" % int(now))
    rotated = rotate_gate(sid, reason="transport",
                          request_started_at=hit.get("started_at_epoch"))
    return {"action": "rotate_only", "kind": "transport", "session_id": sid,
            "rotated": rotated}


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
        if hit is not None:
            if not _fresh(hit.get("started_at", "")):
                continue
            try:
                results.append(handle_hit(hit))
            except Exception as e:  # noqa: BLE001 — 单条失败不打断整批
                _log.warning("handle_hit 异常 %s: %s", hit.get("session_id"), e)
            continue
        thit = parse_transport_line(line)
        if thit is None or not _fresh(thit.get("started_at", "")):
            continue
        try:
            results.append(handle_transport_hit(thit))
        except Exception as e:  # noqa: BLE001 — 单条失败不打断整批
            _log.warning("handle_transport_hit 异常 %s: %s",
                         thit.get("session_id"), e)
    return results


if __name__ == "__main__":
    # 持久宿主入口（ccs-monitor-daemon 60s 调一次；watchdog 线程路径不变）。
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _res = run_once()
    if _res:
        for _r in _res:
            print(json.dumps(_r, ensure_ascii=False, default=str))
        print("opencode_quota: processed %d hit(s)" % len(_res))
    else:
        print("opencode_quota: no hit this round")
    sys.exit(0)
