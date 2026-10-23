"""role_reaper：只唤起「pane 不在 + 有等够时间的 @role 定向派单」的角色。"""
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import core  # noqa: E402
import role_reaper  # noqa: E402


class _Fact:
    def __init__(self, text, age_sec):
        self.t, self.ts = text, time.time() - age_sec


class _Bus:
    def __init__(self, facts):
        self.facts, self.written = facts, []

    def unconsumed(self, consumer=None, limit=0, **_):
        # 复刻生产 SQL 的 ORDER BY ts DESC + LIMIT：否则「旧派单被新广播挤掉」
        # 这条回归测不出来（真实 Blackboard.unconsumed 就是先排序再截断）。
        rows = sorted(self.facts.get(consumer, []), key=lambda f: f.ts, reverse=True)
        return rows[:limit] if limit else rows

    def write(self, cat, text, src=""):
        self.written.append((cat, text))


def _wire(monkeypatch, tmp_path, roles, facts):
    bus = _Bus(facts)
    monkeypatch.setattr(role_reaper, "COOLDOWN_FILE", str(tmp_path / "cd.json"))
    monkeypatch.setitem(sys.modules, "bus_protocol",
                        types.SimpleNamespace(Blackboard=lambda: bus))
    monkeypatch.setattr(core, "list_roles", lambda: roles)
    woke = []
    monkeypatch.setattr(
        core, "wake_ccs",
        lambda role, by_role="": (woke.append(role), {"success": True})[1])
    return bus, woke


def test_dead_role_with_old_directed_fact_is_woken(monkeypatch, tmp_path):
    bus, woke = _wire(monkeypatch, tmp_path,
                      [{"name": "devops", "alive": False}],
                      {"devops": [_Fact("@devops 请巡检磁盘\n第二行", 3600)]})
    assert [r["action"] for r in role_reaper.reap()] == ["woke"]
    assert woke == ["devops"]
    assert "唤起 devops" in bus.written[0][1]


def test_alive_role_is_skipped(monkeypatch, tmp_path):
    bus, woke = _wire(monkeypatch, tmp_path,
                      [{"name": "devops", "alive": True}],
                      {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    assert role_reaper.reap() == [] and woke == [] and bus.written == []


def test_young_fact_waits_for_min_age(monkeypatch, tmp_path):
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("@devops 刚发的", 30)]})
    assert role_reaper.reap() == [] and woke == []


def test_untagged_fact_is_not_a_reason(monkeypatch, tmp_path):
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("广播消息没有 @ 定向", 3600)]})
    assert role_reaper.reap() == [] and woke == []


def test_cooldown_blocks_second_wake(monkeypatch, tmp_path):
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    role_reaper.reap()
    assert role_reaper.reap() == [] and woke == ["devops"]
    assert json.loads((tmp_path / "cd.json").read_text())["devops"] > 0


def test_max_wakes_caps_one_run(monkeypatch, tmp_path):
    roles = [{"name": "a", "alive": False}, {"name": "b", "alive": False}]
    facts = {"a": [_Fact("@a 单", 3600)], "b": [_Fact("@b 单", 3600)]}
    _, woke = _wire(monkeypatch, tmp_path, roles, facts)
    rep = role_reaper.reap(max_wakes=1)
    assert woke == ["a"]
    assert [r["action"] for r in rep] == ["woke", "throttled"]


def test_stale_fact_is_not_a_wake_reason(monkeypatch, tmp_path):
    """20h 前的遗留派单唤起来只会让角色复述过时指令，不该当唤醒理由。"""
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("@devops 昨天的派单", 20 * 3600)]})
    assert role_reaper.reap() == [] and woke == []


def test_fact_beyond_newest_100_is_still_found(monkeypatch, tmp_path):
    """回归：unconsumed 按 ts DESC 排序，LIMIT 会把等待唤醒的旧定向派单挤掉。"""
    noise = [_Fact("近期广播 %d" % i, 1) for i in range(120)]
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": noise + [_Fact("@devops 五小时前的派单", 5 * 3600)]})
    assert [r["action"] for r in role_reaper.reap()] == ["woke"]
    assert woke == ["devops"]


def test_dry_run_reports_without_waking(monkeypatch, tmp_path):
    bus, woke = _wire(monkeypatch, tmp_path,
                      [{"name": "devops", "alive": False}],
                      {"devops": [_Fact("@devops 请巡检磁盘", 3600)]})
    assert [r["action"] for r in role_reaper.reap(dry_run=True)] == ["would_wake"]
    assert woke == [] and bus.written == []


def test_prose_mention_is_not_a_dispatch(monkeypatch, tmp_path):
    """「[@all] devops 0 行落盘…」是提及不是派单 —— 拿它唤醒等于凭空起进程。"""
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("[@pipeline/@engineer] 5 条 @devops 派单丢失", 1200),
                                _Fact("[@all] devops 0 行落盘不是空转", 1800)]})
    assert role_reaper.reap() == [] and woke == []


def test_bracket_recipient_chain_counts(monkeypatch, tmp_path):
    _, woke = _wire(monkeypatch, tmp_path,
                    [{"name": "devops", "alive": False}],
                    {"devops": [_Fact("[@engineer→@devops] eb1b300 交接", 1200)]})
    assert [r["action"] for r in role_reaper.reap()] == ["woke"]
    assert woke == ["devops"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
