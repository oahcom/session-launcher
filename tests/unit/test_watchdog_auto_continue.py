"""check_auto_continue: 忙/失败时不得写审计、不得写节流戳、不得报成功。

回归背景：「已发送 /continue」是运维判断"唤醒已发生"的唯一证据。_tmux_send
忙时拒投（返回 "busy"，不碰 pane）、重试耗尽返回 False；旧实现丢弃返回值，
两种情况都照样写审计 + 节流戳 + 返回 True —— 假审计 + 下次真唤醒被节流压制。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest


def _run(send_result):
    from ops import watchdog
    watchdog._AUTO_CONTINUE_SENT.clear()
    with patch("core._is_alive", return_value=True), \
         patch("core._tmux_send", return_value=send_result), \
         patch.object(watchdog, "_audit_monitor") as audit:
        ok = watchdog.check_auto_continue("qa")
    return ok, audit.call_count, dict(watchdog._AUTO_CONTINUE_SENT)


@pytest.mark.parametrize("send_result,label", [("busy", "角色忙拒投"), (False, "重试耗尽")])
def test_undelivered_continue_writes_no_audit(send_result, label):
    ok, audits, sent = _run(send_result)
    assert ok is False, f"{label}: 未送达不得返回 True，实际 {ok!r}"
    assert audits == 0, f"{label}: 未送达不得写 auto-continue 审计，实际 {audits} 次"
    assert not sent, f"{label}: 未送达不得写节流戳（会压制下次真唤醒），实际 {sent}"


def test_delivered_continue_is_recorded():
    ok, audits, sent = _run(True)
    assert ok is True and audits == 1 and sent, \
        f"真送达应记录审计与节流戳，实际 ok={ok!r} audits={audits} sent={sent}"
