#!/usr/bin/env python3
# TODO(红区欠账): 本文件超 600 行待拆分——判据/归属用例与 429 回收契约用例可分卷（AGENTS.md §9）
"""opencode_quota：429 判据 / 归属 / rotate / 唤醒（契约 C1/C2/C3）。

③ 429 判据命中（喂一行真实形态日志）；
④ 归属失败时不唤醒但换 IP。
外加：冷却窗去重、C3 文案一字不差、归属成功路径、
429 回收新契约（忙则全跳过、楔死延迟确认才回收、杀冷却 1800s、状态落盘防竞态）。
"""

import sys
import time
from pathlib import Path

_launcher_src = Path(__file__).resolve().parents[2] / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

import os
os.environ.setdefault("SESSION_PIPELINE_SKIP_TTL_PRUNER", "1")

import json

import pytest

from ops import opencode_quota as q

# 真实形态日志行（取自 ~/.local/share/opencode/log/opencode.log 2026-10-25T18:00:10.492Z）
REAL_LINE = (
    'timestamp=2026-10-25T18:00:10.492Z level=ERROR run=0ef008b3 message="stream error" '
    'providerID=opencode modelID=deepseek-v4.1-flash-free session.id=ses_e8bdaa045ffeluQYva2bN4QICR '
    'small=false agent=build mode=primary error.error="AI_APICallError: Free usage limit reached. '
    'Free models work in OpenCode (https://opencode.ai); subscribe to OpenCode Go to keep going: '
    'https://opencode.ai/go"'
)

_C3 = ("上一轮 API 错误中断（0 产出）。继续：重发被中断的那次请求，"
       "不要重新规划，不要写自检报告。")


def _fresh_hit(sid="ses_test001"):
    return {"session_id": sid, "started_at": "2099-01-01T00:00:00.000Z",
            "started_at_epoch": 4070908800.0,
            "reason": "rate429"}


# ── ③ 429 判据命中（真实形态日志）────────────────────────────────

def test_quota_criterion_hits_real_form_line():
    hit = q.parse_quota_line(REAL_LINE)
    assert hit is not None, "真实形态 429 行必须命中判据"
    assert hit["session_id"] == "ses_e8bdaa045ffeluQYva2bN4QICR"
    assert hit["started_at"] == "2026-10-25T18:00:10.492Z"
    # asip 白名单 reason + 数值时间戳（request_started_at 只收数）
    assert hit["reason"] == "rate429"
    assert isinstance(hit["started_at_epoch"], float) and hit["started_at_epoch"] > 1.7e9


def test_quota_criterion_rejects_other_lines():
    # 9router 命中的 429（providerID=9router）不算免费额度判据
    line_9r = ('timestamp=2026-11-13T21:35:46.531Z level=ERROR run=57099f01 '
               'message="stream error" providerID=9router modelID=9router_hermes '
               'session.id=ses_e292be71bffewcezgOov0dlKJ3 small=true agent=title mode=primary '
               'error.error="AI_RetryError: Failed after 3 attempts. Last error: '
               '[429]: free usage"')
    assert q.parse_quota_line(line_9r) is None
    # 非 ERROR 级别不命中
    assert q.parse_quota_line(REAL_LINE.replace("level=ERROR", "level=INFO")) is None


# ── B2：包装形态命中 / 原生回归 / 非额度 RetryError 不误命中 ──────

# 包装形态真实样例（取自 ~/.local/share/opencode/log/opencode.log 2026-10-26T13:45:49.658Z）
REAL_LINE_WRAPPED = (
    'timestamp=2026-10-26T13:45:49.658Z level=ERROR run=a057b827 message="stream error" '
    'providerID=opencode modelID=deepseek-v4.1-flash-free session.id=ses_e878a548fffe7LW5n63JZfNWDB '
    'small=true agent=title mode=primary error.error="AI_RetryError: Failed after 3 attempts. '
    'Last error: Free usage limit reached. Free models work in OpenCode (https://opencode.ai); '
    'subscribe to OpenCode Go to keep going: https://opencode.ai/go"'
)


def test_quota_criterion_hits_wrapped_retry_form():
    """B2：AI_RetryError 包装形态（Last error: Free usage limit reached）必须命中。"""
    hit = q.parse_quota_line(REAL_LINE_WRAPPED)
    assert hit is not None, "包装形态 429 行必须命中判据（B2）"
    assert hit["session_id"] == "ses_e878a548fffe7LW5n63JZfNWDB"
    assert hit["started_at"] == "2026-10-26T13:45:49.658Z"
    assert hit["reason"] == "rate429"
    assert isinstance(hit["started_at_epoch"], float) and hit["started_at_epoch"] > 1.7e9
    # 另一真实包装形态行（旧版误判为不命中，B2 起必须命中）
    line_retry = ('timestamp=2026-10-25T17:56:34.639Z level=ERROR run=044586ef '
                  'message="stream error" providerID=opencode modelID=glm-5.3-flash-free '
                  'session.id=ses_e8bcb18fdffeMNO28MAHudf9wG small=true agent=title '
                  'mode=primary error.error="AI_RetryError: Failed after 3 attempts. '
                  'Last error: Free usage limit reached."')
    assert q.parse_quota_line(line_retry) is not None


def test_quota_criterion_still_hits_native_form():
    """B2 回归：原生 AI_APICallError 形态在判据改造后必须照旧命中。"""
    hit = q.parse_quota_line(REAL_LINE)
    assert hit is not None, "原生形态 429 行必须照旧命中（B2 回归）"
    assert hit["session_id"] == "ses_e8bdaa045ffeluQYva2bN4QICR"
    assert hit["reason"] == "rate429"


def test_quota_criterion_ignores_non_quota_retry_error():
    """B2：不含 Free usage limit reached 的 AI_RetryError（如 token 过期）不得误命中。"""
    line_token = ('timestamp=2026-10-26T14:02:11.123Z level=ERROR run=9f00aa11 '
                  'message="stream error" providerID=opencode modelID=deepseek-v4.1-flash-free '
                  'session.id=ses_e878a548fffe7LW5n63JZfNWDB small=true agent=title mode=primary '
                  'error.error="AI_RetryError: Failed after 3 attempts. Last error: '
                  'AI_APICallError: OAuth token expired, please re-authenticate"')
    assert q.parse_quota_line(line_token) is None


# ── ④ 归属失败：不唤醒但换 IP ───────────────────────────────────

def test_attribution_failure_rotates_without_wake(monkeypatch):
    q._last_handled.clear()
    calls = {"rotate": 0, "wake": 0}

    def _rot(sid, **kw):
        calls["rotate"] += 1
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: "/home/administrator")  # 非 ccs 工作区 → 归属失败
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: calls.__setitem__("wake", calls["wake"] + 1) or {})
    res = q.handle_hit(_fresh_hit())
    assert calls["rotate"] == 1, "归属失败也必须换 IP"
    assert calls["wake"] == 0, "归属失败不许唤醒（宁可漏唤醒，不误唤醒）"
    assert res["action"] == "rotate_only"


def test_attribution_failure_when_session_missing(monkeypatch):
    q._last_handled.clear()
    calls = {"rotate": 0, "wake": 0}

    def _rot(sid, **kw):
        calls["rotate"] += 1
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory", lambda sid: None)  # db 查不到
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: calls.__setitem__("wake", calls["wake"] + 1) or {})
    res = q.handle_hit(_fresh_hit("ses_missing01"))
    assert calls["rotate"] == 1 and calls["wake"] == 0
    assert res["action"] == "rotate_only"


# ── 归属成功：rotate + 唤醒，文案一字不差 ────────────────────────

def test_attribution_success_rotates_and_wakes(monkeypatch):
    q._last_handled.clear()
    seen = {}

    def _rot(sid, **kw):
        seen["rotated"] = sid
        seen["rotate_kwargs"] = kw
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "wake_role",
                        lambda role, inst=0: seen.setdefault("wake", (role, inst)) or {})
    res = q.handle_hit(_fresh_hit("ses_ok01"))
    assert res["action"] == "rotate_and_wake"
    assert seen["rotated"] == "ses_ok01"
    assert seen["wake"] == ("pg", 0)
    # C3 文案一字不差
    assert q.WAKE_MSG == _C3


# ── 冷却窗：同一 session.id 60s 内不重复换 IP + 不重复唤醒 ────────

def test_cooldown_suppresses_repeat_rotate_and_wake(monkeypatch):
    q._last_handled.clear()
    calls = {"rotate": 0, "wake": 0}

    def _rot(sid, **kw):
        calls["rotate"] += 1
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory", lambda sid: "/home/administrator")
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: calls.__setitem__("wake", calls["wake"] + 1) or {})
    q.handle_hit(_fresh_hit("ses_cd01"))
    q.handle_hit(_fresh_hit("ses_cd01"))
    q.handle_hit(_fresh_hit("ses_cd01"))
    assert calls["rotate"] == 1, "冷却窗内不重复换 IP"
    assert calls["wake"] == 0
    # 冷却窗过期后可再次处理
    q._last_handled["ses_cd01"] = time.monotonic() - q.COOLDOWN_SEC - 1
    q.handle_hit(_fresh_hit("ses_cd01"))
    assert calls["rotate"] == 2


# ── no_healthy_node：不唤醒防烧重试（impl-gate 语义）──────────────

def test_no_healthy_node_skips_wake(monkeypatch):
    q._last_handled.clear()
    calls = {"wake": 0}
    monkeypatch.setattr(q, "rotate_gate",
                        lambda sid, **kw: {"ok": True, "switched": False,
                                           "reason": "no_healthy_node"})
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: calls.__setitem__("wake", calls["wake"] + 1) or {})
    res = q.handle_hit(_fresh_hit("ses_nohp01"))
    assert calls["wake"] == 0, "no_healthy_node 不许唤醒（同 IP 重试只会再撞 429）"
    assert res["action"] == "rotate_only"
    assert res["wake_skipped"] == "no_healthy_node"


# ── rotate 载荷：reason=rate429 + 数值时间戳（asip 白名单/类型）────

def test_hit_carries_rate429_and_numeric_timestamp(monkeypatch):
    q._last_handled.clear()
    seen = {}
    monkeypatch.setattr(q, "rotate_gate",
                        lambda sid, **kw: seen.setdefault("kw", kw) or
                        {"ok": True, "switched": True, "reason": ""})
    monkeypatch.setattr(q, "session_directory", lambda sid: None)
    q.handle_hit(_fresh_hit("ses_pl01"))
    assert seen["kw"]["reason"] == "rate429", "reason 必须是 asip 白名单值"
    ts = seen["kw"]["request_started_at"]
    assert ts is None or isinstance(ts, (int, float)), "request_started_at 只收数值"


# ── rotate 配置缺失：显式降级不换 IP ─────────────────────────────

def test_rotate_degrades_when_config_missing(monkeypatch):
    monkeypatch.setattr(q, "_cfg_or_env", lambda *a: None)
    res = q.rotate_gate("ses_x", "rate429")
    assert res["ok"] is False, "缺配置必须显式降级（不硬编码）"
    assert res["reason"] == "config_missing"


# ── opencode 唤醒 = 杀子进程 + 续发（楔死解除）──────────────────
# 【改写声明】旧断言 test_opencode_wake_kills_respawns_then_sends 断言命中当轮
# 立即 restart（order == [rotate, restart, wake]），与新契约「命中当轮绝不杀、
# 楔死延迟确认才回收」直接冲突，按下述新语义改写；楔死路径的杀+重生+补投
# 见 test_wedged_engine_reaped_next_round。

def test_opencode_hit_defers_kill_to_confirmation(monkeypatch):
    """命中当轮只 rotate+唤醒；杀引擎延迟到楔死确认（回收已登记待观察队列）。"""
    q._last_handled.clear()
    q._pending_kills.clear()
    order = []
    monkeypatch.setattr(q, "rotate_gate",
                        lambda sid, **kw: order.append("rotate") or
                        {"ok": True, "switched": True, "reason": ""})
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "_sentinel_engine", lambda role: "opencode")
    monkeypatch.setattr(q, "_opencode_child_pids", lambda role: [40001])
    monkeypatch.setattr(q, "_busy", lambda *a, **k: False)
    monkeypatch.setattr(q, "restart_opencode",
                        lambda role, inst=0: order.append("restart") or {})
    monkeypatch.setattr(q, "wake_role",
                        lambda role, inst=0: order.append("wake") or {})
    res = q.handle_hit(_fresh_hit("ses_oc01"))
    assert order == ["rotate", "wake"], \
        "命中当轮不杀：换 IP 先于唤醒，回收延迟到楔死确认"
    assert res["restarted"] == {"scheduled": True}, "回收已登记待观察队列"
    assert res["action"] == "rotate_and_wake"


def test_ccs_wake_unchanged_no_kill(monkeypatch):
    q._last_handled.clear()
    calls = {"restart": 0, "wake": 0}
    monkeypatch.setattr(q, "rotate_gate",
                        lambda sid, **kw: {"ok": True, "switched": True, "reason": ""})
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "_sentinel_engine", lambda role: "ccs")
    monkeypatch.setattr(q, "restart_opencode",
                        lambda *a, **k: calls.__setitem__(
                            "restart", calls["restart"] + 1) or {})
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: calls.__setitem__(
                            "wake", calls["wake"] + 1) or {})
    q.handle_hit(_fresh_hit("ses_ccs01"))
    assert calls["restart"] == 0, "ccs 会话绝不许杀进程（P4 零回归）"
    assert calls["wake"] == 1, "ccs 唤醒行为零变化（只发 C3）"


def test_unknown_engine_no_kill(monkeypatch):
    q._last_handled.clear()
    calls = {"restart": 0}
    monkeypatch.setattr(q, "rotate_gate",
                        lambda sid, **kw: {"ok": True, "switched": True, "reason": ""})
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "_sentinel_engine", lambda role: None)
    monkeypatch.setattr(q, "restart_opencode",
                        lambda *a, **k: calls.__setitem__(
                            "restart", calls["restart"] + 1) or {})
    monkeypatch.setattr(q, "wake_role", lambda *a, **k: {})
    q.handle_hit(_fresh_hit("ses_unk01"))
    assert calls["restart"] == 0, "engine 缺证据只发文案，绝不杀"


def test_kill_criteria_positive_and_negative():
    assert q._is_opencode_cmd("/home/administrator/.opencode/bin/opencode") is True
    assert q._is_opencode_cmd("bash -c while true; do opencode; done") is False
    assert q._is_opencode_cmd("claude --model 9router_hermes") is False
    assert q._is_opencode_cmd("") is False


def test_restart_opencode_no_process_degrades(monkeypatch):
    monkeypatch.setattr(q, "_opencode_child_pids", lambda role: [])
    res = q.restart_opencode("pg")
    assert res["respawned"] is False
    assert res["reason"] == "no opencode process"


# ── 一次性宿主：offset/冷却跨进程持久化（run_once）────────────────

def test_run_once_persists_offset_and_cooldown(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    q._last_handled.clear()
    log = tmp_path / "opencode.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(q, "LOG_PATH", log)
    monkeypatch.setattr(q, "_STATE_PATH", tmp_path / "state.json")
    rot = {"n": 0}

    def _rot(sid, **kw):
        rot["n"] += 1
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "_sentinel_engine", lambda role: "ccs")
    monkeypatch.setattr(q, "wake_role", lambda *a, **k: {})

    def _line(sid):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")
        return ('timestamp=%s level=ERROR providerID=opencode modelID=x '
                'session.id=%s error.error="AI_APICallError: '
                'Free usage limit reached. x"' % (ts, sid))

    # 首轮：offset 从 EOF 起，不处理存量
    log.write_text(_line("ses_old01") + "\n", encoding="utf-8")
    assert q.run_once() == [], "首次运行跳 EOF，不重放历史"
    # 增量一条新命中 → 换 IP 1 次
    with log.open("a", encoding="utf-8") as f:
        f.write(_line("ses_run01") + "\n")
    res = q.run_once()
    assert rot["n"] == 1, "只处理增量行"
    assert res and res[0]["session_id"] == "ses_run01"
    # 同 sid 再来一条（冷却持久化）→ 不再换 IP
    with log.open("a", encoding="utf-8") as f:
        f.write(_line("ses_run01") + "\n")
    res3 = q.run_once()
    assert rot["n"] == 1, "冷却表持久化：跨进程不重复换 IP"
    assert res3 and res3[0].get("action") == "suppressed"


# ── T1 transport：断连形态判据 / 判定矩阵 / 只换不唤醒 / 全局冷却 ────

# 真实形态（opencode.log 2026-11-14T19:24:45.419Z，2026-11-15 闸口死节点事故）
REAL_LINE_TRANSPORT = (
    'timestamp=2026-11-14T19:24:45.419Z level=ERROR run=511a658b message="stream error" '
    'providerID=opencode modelID=deepseek-v4.1-flash-free '
    'session.id=ses_e247c1d4cffe6cIeOctV8YWv4T small=false agent=build mode=primary '
    'error.error="AI_APICallError: Cannot connect to API: The socket connection was '
    'closed unexpectedly. For more information, pass `verbose: true` in the second '
    'argument to fetch() (cause: Error: The socket connection was closed unexpectedly. '
    'For more information, pass `verbose: true` in the second argument to fetch()))"'
)

# 伴生行（message=process，无 providerID）——不是会话请求面，不许命中
REAL_LINE_TRANSPORT_PROCESS = (
    'timestamp=2026-11-14T19:24:45.424Z level=ERROR run=511a658b message=process '
    'session.id=ses_e247c1d4cffe6cIeOctV8YWv4T '
    'messageID=msg_1db83e2dc001V8htRNpjqtvF7x error="Cannot connect to API: The socket '
    'connection was closed unexpectedly." stack="AI_APICallError: Cannot connect to API"'
)


def test_transport_criterion_hits_real_line():
    hit = q.parse_transport_line(REAL_LINE_TRANSPORT)
    assert hit is not None, "真实断连形态必须命中 transport 判据（T1）"
    assert hit["session_id"] == "ses_e247c1d4cffe6cIeOctV8YWv4T"
    assert hit["reason"] == "transport"
    assert isinstance(hit["started_at_epoch"], float) and hit["started_at_epoch"] > 1.7e9


def test_transport_criterion_disjoint_with_quota_criterion():
    # 429 行不算 transport；transport 行不算 429（两判据互斥，不双发）
    assert q.parse_transport_line(REAL_LINE) is None
    assert q.parse_quota_line(REAL_LINE_TRANSPORT) is None
    # 无 providerID 的伴生行、非 ERROR 行不命中
    assert q.parse_transport_line(REAL_LINE_TRANSPORT_PROCESS) is None
    assert q.parse_transport_line(
        REAL_LINE_TRANSPORT.replace("level=ERROR", "level=INFO")) is None


def test_transport_verdict_matrix(monkeypatch):
    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: True)
    assert q.gate_vs_direct_verdict() == "skip_transient"
    # 闸口断 + 直连通 → 唯一值得换节点的场景
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: proxy is None)
    assert q.gate_vs_direct_verdict() == "rotate"
    # 双断：本机网络问题，换闸口节点没用
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: False)
    assert q.gate_vs_direct_verdict() == "skip_network"
    # 端口发现不到：显式降级不盲换
    monkeypatch.setattr(q, "discover_gate_port", lambda: None)
    assert q.gate_vs_direct_verdict() == "skip_no_port"


def test_transport_rotate_only_never_wakes_or_kills(monkeypatch):
    q._transport_last_ts = 0.0
    seen = {"rotate": 0, "wake": 0, "restart": 0, "kw": None}

    def _rot(sid, **kw):
        seen["rotate"] += 1
        seen["kw"] = kw
        return {"ok": True, "switched": True, "reason": ""}

    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: proxy is None)
    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "wake_role",
                        lambda *a, **k: seen.__setitem__("wake", seen["wake"] + 1) or {})
    monkeypatch.setattr(q, "restart_opencode",
                        lambda *a, **k: seen.__setitem__("restart", seen["restart"] + 1) or {})
    res = q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    assert seen["rotate"] == 1
    assert seen["wake"] == 0 and seen["restart"] == 0, \
        "transport 只换节点，绝不杀进程/唤醒（断连不楔 TUI）"
    assert seen["kw"]["reason"] == "transport", "reason 传 asip 白名单值 transport"
    assert res["action"] == "rotate_only"


def test_transport_skips_without_rotation_when_gate_alive(monkeypatch):
    q._transport_last_ts = 0.0
    calls = {"rotate": 0}
    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: True)  # 闸口通
    monkeypatch.setattr(q, "rotate_gate",
                        lambda *a, **k: calls.__setitem__("rotate", calls["rotate"] + 1) or {})
    res = q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    assert calls["rotate"] == 0, "闸口活着：瞬时抖动不许瞎换节点"
    assert res["reason"] == "skip_transient"


def test_transport_skips_when_local_network_down(monkeypatch):
    q._transport_last_ts = 0.0
    calls = {"rotate": 0}
    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: False)  # 双断
    monkeypatch.setattr(q, "rotate_gate",
                        lambda *a, **k: calls.__setitem__("rotate", calls["rotate"] + 1) or {})
    res = q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    assert calls["rotate"] == 0, "本机网络挂了：换闸口节点没用"
    assert res["reason"] == "skip_network"


def test_transport_global_cooldown_one_rotate_per_window(monkeypatch):
    q._transport_last_ts = 0.0
    calls = {"rotate": 0}
    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: proxy is None)
    monkeypatch.setattr(q, "rotate_gate",
                        lambda *a, **k: calls.__setitem__("rotate", calls["rotate"] + 1) or {})
    q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    assert calls["rotate"] == 1, "闸口级事件：60s 窗内多会话报错只换一次"
    q._transport_last_ts = time.time() - q.COOLDOWN_SEC - 1
    q.handle_transport_hit(q.parse_transport_line(REAL_LINE_TRANSPORT))
    assert calls["rotate"] == 2


def test_run_once_persists_transport_cooldown(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    q._last_handled.clear()
    q._transport_last_ts = 0.0
    log = tmp_path / "opencode.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(q, "LOG_PATH", log)
    monkeypatch.setattr(q, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(q, "discover_gate_port", lambda: 7898)
    monkeypatch.setattr(q, "_probe_reachable", lambda proxy: proxy is None)
    rot = {"n": 0}
    monkeypatch.setattr(q, "rotate_gate",
                        lambda *a, **k: rot.__setitem__("n", rot["n"] + 1) or
                        {"ok": True, "switched": True, "reason": ""})

    def _tline(sid):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")
        return ('timestamp=%s level=ERROR providerID=opencode modelID=x '
                'session.id=%s error.error="AI_APICallError: Cannot connect to '
                'API: The socket connection was closed unexpectedly."' % (ts, sid))

    log.write_text(_tline("ses_old01") + "\n", encoding="utf-8")
    assert q.run_once() == [], "首轮跳 EOF"
    with log.open("a", encoding="utf-8") as f:
        f.write(_tline("ses_t01") + "\n")
    res = q.run_once()
    assert rot["n"] == 1 and res[0]["kind"] == "transport"
    # 模拟新进程：内存冷却清零，靠状态文件续窗
    q._transport_last_ts = 0.0
    with log.open("a", encoding="utf-8") as f:
        f.write(_tline("ses_t02") + "\n")
    res2 = q.run_once()
    assert rot["n"] == 1, "冷却持久化：跨进程 60s 内不重复换节点"
    assert res2 and res2[0].get("action") == "suppressed"


# ── 429 回收新契约：忙则全跳过、楔死延迟确认才回收、杀冷却 1800s ────

def _setup_recycle_mocks(monkeypatch, *, busy=False, stuck=False,
                         engine="opencode", pids=(40001,)):
    """handle_hit/回收链公共 mock 底座；返回动作计数器。"""
    seen = {"rotate": 0, "wake": 0, "restart": 0, "order": []}

    def _rot(sid, **kw):
        seen["rotate"] += 1
        seen["order"].append("rotate")
        return {"ok": True, "switched": True, "reason": ""}

    def _wake(role, inst=0):
        seen["wake"] += 1
        seen["order"].append("wake")
        return {"success": True}

    def _restart(role, inst=0):
        seen["restart"] += 1
        seen["order"].append("restart")
        return {"killed": list(pids), "respawned": True, "new_pid": 50002,
                "reason": None}

    monkeypatch.setattr(q, "rotate_gate", _rot)
    monkeypatch.setattr(q, "session_directory",
                        lambda sid: str(Path.home() / "ccs-workspaces" / "pg"))
    monkeypatch.setattr(q, "attribute_session", lambda d: ("pg", 0))
    monkeypatch.setattr(q, "_sentinel_engine", lambda role: engine)
    monkeypatch.setattr(q, "_opencode_child_pids", lambda role: list(pids))
    monkeypatch.setattr(q, "_busy", lambda *a, **k: busy)
    monkeypatch.setattr(q, "_looks_stuck", lambda *a, **k: stuck)
    monkeypatch.setattr(q, "wake_role", _wake)
    monkeypatch.setattr(q, "restart_opencode", _restart)
    return seen


def _age_pending(seconds: float = 61.0):
    """把待观察队列 hit_ts 拨后，模拟下一轮（≥KILL_CONFIRM_DELAY_SEC）。"""
    for cand in q._pending_kills.values():
        cand["hit_ts"] -= seconds


def test_busy_hit_rotates_but_skips_wake_and_kill(monkeypatch):
    """忙会话命中 429：rotate 照常，投递/回收全跳过（红绿反证：守卫移除后必须红）。"""
    q._last_handled.clear()
    q._pending_kills.clear()
    q._kill_last.clear()
    seen = _setup_recycle_mocks(monkeypatch, busy=True, stuck=True)
    res = q.handle_hit(_fresh_hit("ses_busy01"))
    assert seen["rotate"] == 1, "rotate 必须照常（换 IP 与杀引擎解耦）"
    assert seen["wake"] == 0, "忙则不投递"
    assert seen["restart"] == 0, "忙则绝不杀"
    assert not q._pending_kills, "忙会话不得入观察队列"
    assert res["action"] == "rotate_only" and res["wake_skipped"] == "busy"
    _age_pending()
    assert q._process_pending_kills() == []
    assert seen["restart"] == 0, "确认轮也不许杀（队列本就该为空）"


def test_not_stuck_candidate_dropped_next_round(monkeypatch):
    """不忙但在干活（_looks_stuck False）：命中当轮不杀，候选下一轮被丢弃。"""
    q._last_handled.clear()
    q._pending_kills.clear()
    q._kill_last.clear()
    seen = _setup_recycle_mocks(monkeypatch, busy=False, stuck=False)
    res = q.handle_hit(_fresh_hit("ses_act01"))
    assert seen["restart"] == 0, "命中当轮绝不杀（延迟确认）"
    assert seen["order"] == ["rotate", "wake"]
    assert res["restarted"] == {"scheduled": True}
    assert len(q._pending_kills) == 1, "候选已登记"
    _age_pending()
    results = q._process_pending_kills()
    assert seen["restart"] == 0, "pane 有变化/在干活：不杀"
    assert not q._pending_kills, "候选被丢弃（引擎自己恢复了/在正常重试）"
    assert results and results[0]["action"] == "kill_skipped"


def test_wedged_engine_reaped_next_round(monkeypatch):
    """楔死（不忙 + _looks_stuck True）：下一轮杀+重生+补投 WAKE_MSG。"""
    q._last_handled.clear()
    q._pending_kills.clear()
    q._kill_last.clear()
    seen = _setup_recycle_mocks(monkeypatch, busy=False, stuck=True)
    q.handle_hit(_fresh_hit("ses_wedge01"))
    assert seen["restart"] == 0, "命中当轮不杀"
    _age_pending()
    results = q._process_pending_kills()
    assert seen["restart"] == 1, "楔死确认后才回收"
    assert seen["wake"] == 2, "杀+重生后必须补投 WAKE_MSG"
    assert seen["order"] == ["rotate", "wake", "restart", "wake"], \
        "确认轮顺序：杀先于 C3（新进程首发走新出口）"
    assert results[0]["action"] == "kill_and_wake"
    assert results[0]["restarted"]["respawned"] is True
    assert q._kill_last.get("ses_wedge01"), "杀冷却时间戳已记录"


def test_kill_cooldown_suppresses_repeat_kill(tmp_path, monkeypatch):
    """杀冷却 1800s：楔死杀过一次后再命中只 rotate 不杀；kill_last_ts 跨重载仍抑制。"""
    from datetime import datetime, timezone
    q._last_handled.clear()
    q._pending_kills.clear()
    q._kill_last.clear()
    q._transport_last_ts = 0.0
    log = tmp_path / "opencode.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(q, "LOG_PATH", log)
    monkeypatch.setattr(q, "_STATE_PATH", tmp_path / "state.json")
    seen = _setup_recycle_mocks(monkeypatch, busy=False, stuck=True)

    def _line(sid):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")
        return ('timestamp=%s level=ERROR providerID=opencode modelID=x '
                'session.id=%s error.error="AI_APICallError: '
                'Free usage limit reached. x"' % (ts, sid))

    sid = "ses_kill01"
    log.write_text(_line("ses_old99") + "\n", encoding="utf-8")
    assert q.run_once() == [], "首轮跳 EOF"
    with log.open("a", encoding="utf-8") as f:
        f.write(_line(sid) + "\n")
    q.run_once()                       # 命中当轮：rotate+wake+登记候选
    assert seen["rotate"] == 1 and seen["restart"] == 0
    _age_pending()
    q.run_once()                       # 下一轮：楔死确认 → 杀
    assert seen["restart"] == 1 and seen["wake"] == 2
    # 模拟进程重启：内存态全清，靠状态文件续抑制
    q._kill_last.clear()
    q._pending_kills.clear()
    q._last_handled.clear()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state.get("kill_last_ts", {}).get(sid), "kill_last_ts 已持久化"
    # 让 rotate 冷却过期，同一 sid 能再次命中
    state["handled"] = {sid: time.monotonic() - q.COOLDOWN_SEC - 60}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(_line(sid) + "\n")
    q.run_once()                       # 冷却期内再次命中：只 rotate
    assert seen["rotate"] == 2 and seen["restart"] == 1, "冷却期内最多只换 IP 不杀"
    _age_pending()
    res = q.run_once()                 # 确认轮：杀冷却抑制
    assert seen["restart"] == 1, "kill_last_ts 跨重载仍抑制（1800s 内不许再杀）"
    assert res and res[0]["action"] == "kill_suppressed"


def test_save_state_unique_tmp_name(tmp_path, monkeypatch):
    """_save_state 用含 pid 的唯一 tmp 名防并发 ENOENT；连续两次写都成功。"""
    monkeypatch.setattr(q, "_STATE_PATH", tmp_path / "state.json")
    seen = {}
    real_replace = os.replace

    def _spy(src, dst):
        seen["src"] = str(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    q._save_state({"a": 1})
    assert f".tmp.{os.getpid()}" in seen["src"], "tmp 必须带 pid 唯一名防并发互踩"
    q._save_state({"a": 2})
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) \
        == {"a": 2}, "连续两次落盘都成功且后者生效"
