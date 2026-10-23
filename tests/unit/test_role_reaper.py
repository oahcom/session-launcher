"""role_reaper：只启动「pane 不在 + 有等够时间且不是 notice 的 @role 派单」的 ondemand 角色。"""
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import core  # noqa: E402
import role_reaper  # noqa: E402


class _Fact:
    def __init__(self, text, age_sec, cat="code_review", src="engineer"):
        self.t, self.ts, self.cat, self.src = text, time.time() - age_sec, cat, src


class _Bus:
    def __init__(self, facts):
        self.facts, self.written = facts, []

    def unconsumed(self, consumer=None, limit=0, **_):
        # 复刻生产 SQL 的 ORDER BY ts DESC + LIMIT：否则「旧派单被新广播挤掉」
        # 这条回归测不出来（真实 Blackboard.unconsumed 先排序再截断）。
        rows = sorted(self.facts.get(consumer, []), key=lambda f: f.ts, reverse=True)
        return rows[:limit] if limit else rows

    def write(self, cat, text, src=""):
        self.written.append((cat, text))


def _wire(monkeypatch, tmp_path, roles, facts, wake=None):
    bus = _Bus(facts)
    monkeypatch.setattr(role_reaper, "COOLDOWN_FILE", str(tmp_path / "cd.json"))
    monkeypatch.setitem(sys.modules, "bus_protocol",
                        types.SimpleNamespace(Blackboard=lambda: bus))
    monkeypatch.setattr(core, "list_roles", lambda: roles)
    calls = []
    monkeypatch.setattr(
        core, "wake_ccs",
        wake or (lambda role, by_role="": (calls.append(role), {"success": True})[1]))
    return bus, calls


def _roles(*names, lifecycle="ondemand", alive=False):
    return [{"name": n, "alive": alive, "lifecycle": lifecycle} for n in names]


def test_engine_task_spec_dispatch_is_found(monkeypatch, tmp_path):
    """机器派单标题是 needs_implementation @role 工作流任务: …（首 token 不是收件人）。"""
    bus, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                       {"devops": [_Fact("needs_implementation @devops 工作流任务: wf_a/s1", 3600,
                                         cat="task_spec")]})
    assert [r["action"] for r in role_reaper.reap()] == ["started"]
    assert calls == ["devops"]
    assert "已启动 pane devops" in bus.written[0][1]


def test_coordinator_prose_dispatch_is_found(monkeypatch, tmp_path):
    """协调者派单是 [coordinator] 派单 @engineer: … —— 收件人在中文之后。"""
    _, calls = _wire(monkeypatch, tmp_path, _roles("engineer"),
                     {"engineer": [_Fact("[coordinator] 派单 @engineer: step_handler.py:1334", 900,
                                         src="coordinator")]})
    assert [r["action"] for r in role_reaper.reap()] == ["started"]
    assert calls == ["engineer"]


def test_bracket_chain_counts(monkeypatch, tmp_path):
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("[@engineer→@devops] eb1b300 交接", 1200)]})
    assert [r["action"] for r in role_reaper.reap()] == ["started"]
    assert calls == ["devops"]


def test_mention_also_wakes_by_design(monkeypatch, tmp_path):
    """过唤醒是有意的：漏唤醒没有兜底，过唤醒有窗口/冷却/cap 三道兜底。"""
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("[@pipeline/@engineer] 5 条 @devops 派单丢失", 1800)]})
    assert [r["action"] for r in role_reaper.reap()] == ["started"]
    assert calls == ["devops"]


def test_notice_alone_does_not_wake(monkeypatch, tmp_path):
    """notice 是角色自己的自动告警（24h 占定向积压 26/40），不构成启动理由。"""
    _, calls = _wire(monkeypatch, tmp_path, _roles("ccs-monitor"),
                     {"ccs-monitor": [_Fact("@ccs-monitor [eval_checker] 检查失败", 1200,
                                            cat="notice")]})
    assert role_reaper.reap() == [] and calls == []


def test_self_written_fact_does_not_wake(monkeypatch, tmp_path):
    """自己 @ 自己写的告警，bus_client unread 都不显示，更不该启动 pane。"""
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("@devops [context_warn] 上下文将满", 1200,
                                       src="devops")]})
    assert role_reaper.reap() == [] and calls == []


def test_running_instance_suppresses_wake(monkeypatch, tmp_path):
    """alive 只查 instance 0；以 ccs-role-1 在跑时再起一个会有两份 bus consumer。"""
    roles = [{"name": "devops", "alive": False, "lifecycle": "ondemand",
              "instances": [1]}]
    _, calls = _wire(monkeypatch, tmp_path, roles,
                     {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    assert role_reaper.reap() == [] and calls == []


def test_infinite_lifecycle_is_left_to_ops(monkeypatch, tmp_path):
    """infinite 角色被停是运维事件，顺手拉起会掩盖它。"""
    _, calls = _wire(monkeypatch, tmp_path, _roles("coordinator", lifecycle="infinite"),
                     {"coordinator": [_Fact("@coordinator 请裁决", 1200)]})
    assert role_reaper.reap() == [] and calls == []


def test_alive_role_is_skipped(monkeypatch, tmp_path):
    bus, calls = _wire(monkeypatch, tmp_path, _roles("devops", alive=True),
                       {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    assert role_reaper.reap() == [] and calls == [] and bus.written == []


def test_age_window_boundaries_are_inclusive(monkeypatch, tmp_path):
    """正好 600s 与正好 6h 都算，超出任一端就不算（时间冻结，避免毫秒漂移）。"""
    now = 1_700_000_000.0
    monkeypatch.setattr(role_reaper.time, "time", lambda: now)
    for age, expect in ((600, 1), (21600, 1), (599, 0), (21601, 0)):
        fact = _Fact("@devops 边界", 0)
        fact.ts = now - age
        _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                         {"devops": [fact]})
        role_reaper.COOLDOWN_FILE = str(tmp_path / ("cd%d.json" % age))  # 每轮独立冷却
        got = role_reaper.reap()
        assert len(calls) == expect, (age, got)


def test_second_line_mention_does_not_count(monkeypatch, tmp_path):
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("广播标题\n@devops 在第二行", 3600)]})
    assert role_reaper.reap() == [] and calls == []


def test_untagged_fact_is_not_a_reason(monkeypatch, tmp_path):
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("广播消息没有 @ 定向", 3600)]})
    assert role_reaper.reap() == [] and calls == []


def test_cooldown_blocks_second_start(monkeypatch, tmp_path):
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    role_reaper.reap()
    assert role_reaper.reap() == [] and calls == ["devops"]
    assert json.loads((tmp_path / "cd.json").read_text())["devops"] > 0


def test_most_backlogged_role_goes_first(monkeypatch, tmp_path):
    """cap 只该挡「凑巧靠前」的，不该挡最堵的那个。"""
    facts = {"a": [_Fact("@a 单", 3600)],
             "b": [_Fact("@b 单%d" % i, 3600) for i in range(3)]}
    _, calls = _wire(monkeypatch, tmp_path, _roles("a", "b"), facts)
    rep = role_reaper.reap(max_wakes=1)
    assert calls == ["b"]
    assert [r["action"] for r in rep] == ["started", "throttled"]


def test_failed_start_still_backs_off_and_leaves_a_trace(monkeypatch, tmp_path):
    """起不来的角色若不记冷却，会每 15 分钟被重试且 bus 里零痕迹。"""
    bus, _ = _wire(monkeypatch, tmp_path, _roles("devops"),
                   {"devops": [_Fact("@devops 请巡检磁盘", 3600)]},
                   wake=lambda role, by_role="": {"success": False, "error": "权限不足"})
    assert [r["action"] for r in role_reaper.reap()] == ["start_failed"]
    assert role_reaper.reap() == []
    assert "启动失败 devops" in bus.written[0][1] and "权限不足" in bus.written[0][1]


def test_fact_beyond_newest_100_is_still_found(monkeypatch, tmp_path):
    """回归：unconsumed 按 ts DESC 排序，LIMIT 会把等待唤醒的旧定向派单挤掉。"""
    noise = [_Fact("近期广播 %d" % i, 1) for i in range(120)]
    _, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                     {"devops": noise + [_Fact("@devops 五小时前的派单", 5 * 3600)]})
    assert [r["action"] for r in role_reaper.reap()] == ["started"]
    assert calls == ["devops"]


def test_dry_run_reports_without_starting(monkeypatch, tmp_path):
    bus, calls = _wire(monkeypatch, tmp_path, _roles("devops"),
                       {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    assert [r["action"] for r in role_reaper.reap(dry_run=True)] == ["would_start"]
    assert calls == [] and bus.written == []


def test_dry_run_honours_cap(monkeypatch, tmp_path):
    """dry-run 必须报告真跑会做什么：3 个候选、cap=2 → 2 个 would_start + 1 个 throttled。"""
    facts = {"r%d" % i: [_Fact("@r%d 单" % i, 3600)] for i in range(3)}
    _, calls = _wire(monkeypatch, tmp_path, _roles("r0", "r1", "r2"), facts)
    rep = role_reaper.reap(dry_run=True, max_wakes=2)
    assert sorted(r["action"] for r in rep) == ["throttled", "would_start", "would_start"]
    assert calls == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
