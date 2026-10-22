#!/usr/bin/env python3
"""wf complete 的摘要质量提示：默认/过短摘要要在 stderr 出现，好摘要不出现。

背景（2026-10-22 实测）：1,136 个 completed 步骤里 367 个（32.3%）没有 exit_messages，
默认值 "done" 被引擎当成该步全部产出。提示放在动作发生处，不改变完成语义。
"""
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import wf  # noqa: E402


def test_default_summary_warns():
    msg = wf._summary_warning("done")
    assert msg and "rc=0" in msg, "默认摘要必须提示正确形态"


def test_empty_summary_warns():
    assert wf._summary_warning("") is not None


def test_short_summary_warns():
    assert wf._summary_warning("已修") is not None


def test_evidence_summary_is_quiet():
    assert wf._summary_warning("修 doccheck 解析 | rc=0 | 11 mismatch -> CLEAN") is None
