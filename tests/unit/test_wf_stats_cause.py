#!/usr/bin/env python3
"""单元测试: workflow/client.py:_cancel_cause — 取消归因桶。

纯函数，不碰数据库。桶语义与 workflow_stats() 的 cancelled_by_cause 一致，
需求记录见 ~/hermes/workspace/R147-僵尸步取证与取消归因/需求.md。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from workflow.client import _cancel_cause  # noqa: E402

CASES = [
    # (detail, reason, 期望桶)
    ("僵尸回收: running 状态 s1 无推进记录 4.1h (timeout_count=0)", None, "zombie_stall"),
    ("僵尸回收: running 运行24h > 24h全局阈值", None, "zombie_24h"),
    ("僵尸回收: running 运行1271h > 72h全局阈值", None, "zombie_72h"),
    ("僵尸回收: 当前步骤 s4 已终态(cancelled)但行级仍 running", None,
     "zombie_row_mismatch"),
    ("无活跃工作流", None, "starvation"),
    ("角色不响应自动取消", None, "role_unresponsive"),
    ("workflow delivery_blocked", None, "delivery_blocked"),
    ("reason=重复派发: 同源 DESIGN.md 已产出(bus #376300)", None, "dedup"),
    ("reason=误路由: 知识采集类任务经 WL-full_dev_pipeline 错发 product_architect", None,
     "misroute"),
    ("reason=孤子步已终止", None, "orphan_subflow"),
    ("reason=PM自评#377395: WL-02 fanout无需求映射", None, "manual"),
    ("workflow cancelled", None, "unspecified"),   # 默认文案，无真实原因
    ("", None, "no_log"),                          # 无取消日志，无 reason
    (None, None, "no_log"),                        # 同上：detail 缺失
    ("workflow cancelled", "连续6分钟无活跃工作流", "starvation"),  # 默认文案 → 回退 reason
    ("s1: 角色不响应自动取消", "任何理由都不改桶", "role_unresponsive"),  # detail 优先
]


def test_cancel_cause_buckets():
    for detail, reason, expected in CASES:
        got = _cancel_cause(detail, reason)
        assert got == expected, (
            f"_cancel_cause({detail!r}, {reason!r}) = {got!r}, 期望 {expected!r}")


if __name__ == "__main__":
    test_cancel_cause_buckets()
    print("PASS", len(CASES), "cases")
