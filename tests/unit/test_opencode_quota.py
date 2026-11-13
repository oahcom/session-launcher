#!/usr/bin/env python3
"""opencode_quota：429 判据 / 归属 / rotate / 唤醒（契约 C1/C2/C3）。

③ 429 判据命中（喂一行真实形态日志）；
④ 归属失败时不唤醒但换 IP。
外加：冷却窗去重、C3 文案一字不差、归属成功路径。
"""

import sys
import time
from pathlib import Path

_launcher_src = Path(__file__).resolve().parents[2] / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

import os
os.environ.setdefault("SESSION_PIPELINE_SKIP_TTL_PRUNER", "1")

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
    # AI_RetryError 包装的 Free usage limit 不含 AI_APICallError needle → 不命中
    line_retry = ('timestamp=2026-10-25T17:56:34.639Z level=ERROR run=044586ef '
                  'message="stream error" providerID=opencode modelID=glm-5.3-flash-free '
                  'session.id=ses_e8bcb18fdffeMNO28MAHudf9wG small=true agent=title '
                  'mode=primary error.error="AI_RetryError: Failed after 3 attempts. '
                  'Last error: Free usage limit reached."')
    assert q.parse_quota_line(line_retry) is None
    # 非 ERROR 级别不命中
    assert q.parse_quota_line(REAL_LINE.replace("level=ERROR", "level=INFO")) is None


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
