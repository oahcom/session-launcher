#!/usr/bin/env python3
"""不记名 start 每次开新实例（自动分配最小空闲实例号，活实例永不复用）。

钉住四件事：
① next_free_instance 四态：空集→0；{ccs-anon,ccs-anon-2}→1（取最小空位）；
   0..16 全占→None；只占 0→1；
② resolve_start_instance_id 语义：显式 --instance-id（含 0）恒尊重、永不分配；
   注册角色（persona）→ 0（主实例语义，哪怕 0 被占也是 attach/复用而非另分配）；
   不记名→分配最小空闲；17 槽全满→(None, err 含「实例已满」+清理指引)；
③ CLI 边界：subprocess 钉显式 --instance-id 0/5 不分配 + 裸 start dry-run
   三字段自洽（instance_id ↔ planned tmux 名 ↔ tmux_session，不断言具体值——
   测试机 tmux 状态不定）；进程内 CLI 分发钉「ccs-anon 被占 → 分配 1」
   （红绿反证：resolve 不记名分支恒 0 突变下本例必红）；
④ core.start 本体语义不动（instance_id 传什么用什么——既有
   test_engine_default_opencode 的 ccs-anon 锚不受影响）。

全部走 monkeypatch / dry-run / subprocess，不碰真实 tmux/哨兵。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_launcher_src = Path(__file__).resolve().parents[2] / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

os.environ.setdefault("SESSION_PIPELINE_SKIP_TTL_PRUNER", "1")
os.environ.setdefault("CLAUDECODE_PERM_FLAGS", "")

import core
import ops.sentinel as sentinel
from tmux_ops import MAX_INSTANCES, make_tmux_name, next_free_instance


def _slots(role="anon"):
    """0..MAX_INSTANCES 全槽位的 tmux 名集合（17 个）。"""
    return {make_tmux_name(role, i) for i in range(MAX_INSTANCES + 1)}


# ── ① next_free_instance 四态 ──────────────────────────────────

def test_next_free_empty_returns_zero(monkeypatch):
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: set())
    assert next_free_instance("anon") == 0


def test_next_free_smallest_gap_wins(monkeypatch):
    # 0 和 2 被占 → 最小空位是 1（不是 3）
    monkeypatch.setattr(sentinel, "_list_tmux_sessions",
                        lambda: {"ccs-anon", "ccs-anon-2"})
    assert next_free_instance("anon") == 1


def test_next_free_all_slots_taken_returns_none(monkeypatch):
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: _slots())
    assert next_free_instance("anon") is None


def test_next_free_only_main_taken_returns_one(monkeypatch):
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: {"ccs-anon"})
    assert next_free_instance("anon") == 1


# ── ② resolve_start_instance_id 语义 ───────────────────────────

def test_resolve_explicit_zero_is_pinned(monkeypatch):
    # 显式 0 = 钉主实例：即便不记名、即便 0 被占，也绝不自动分配
    monkeypatch.setattr(core, "get_role", lambda name: None)
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: {"ccs-anon"})
    assert core.resolve_start_instance_id("anon", 0) == (0, None)


def test_resolve_explicit_five_is_pinned(monkeypatch):
    monkeypatch.setattr(core, "get_role", lambda name: None)
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: set())
    assert core.resolve_start_instance_id("anon", 5) == (5, None)


def test_resolve_registered_role_defaults_to_main(monkeypatch):
    # 注册角色缺省=主实例 0：哪怕 0 被占也不另分配（主实例 attach/复用语义）
    monkeypatch.setattr(core, "get_role", lambda name: {"name": name})
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: {"ccs-engineer"})
    assert core.resolve_start_instance_id("engineer", None) == (0, None)


def test_resolve_anon_allocates_smallest_free(monkeypatch):
    monkeypatch.setattr(core, "get_role", lambda name: None)
    monkeypatch.setattr(sentinel, "_list_tmux_sessions",
                        lambda: {"ccs-anon", "ccs-anon-1"})
    inst, err = core.resolve_start_instance_id("anon", None)
    assert err is None
    assert inst == 2, "0/1 被占应分配最小空位 2"


def test_resolve_full_returns_structured_error(monkeypatch):
    monkeypatch.setattr(core, "get_role", lambda name: None)
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: _slots())
    inst, err = core.resolve_start_instance_id("anon", None)
    assert inst is None
    assert err is not None
    assert "实例已满" in err
    assert "ccs stop" in err, f"错误文案须带清理指引: {err}"


# ── ③ CLI 边界 ─────────────────────────────────────────────────

def _run_cli(*argv):
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    return subprocess.run(
        [sys.executable, str(_launcher_src / "ccs.py"), *argv],
        capture_output=True, text=True, timeout=60, env=env)


def _parse_payload(stdout: str) -> dict:
    # ccs.py 先打人读行再打 indent=2 JSON；从首个 "{" 行起解析
    lines = stdout.splitlines()
    begin = next(i for i, ln in enumerate(lines) if ln.strip() == "{")
    return json.loads("\n".join(lines[begin:]))


def test_cli_explicit_zero_pins_main_instance():
    r = _run_cli("start", "--dry-run", "--instance-id", "0")
    assert r.returncode == 0, r.stderr
    payload = _parse_payload(r.stdout)
    assert payload["instance_id"] == 0
    assert "ccs-anon" in payload["planned_actions"][0]


def test_cli_explicit_five_pins_extended_instance():
    r = _run_cli("start", "--dry-run", "--instance-id", "5")
    assert r.returncode == 0, r.stderr
    payload = _parse_payload(r.stdout)
    assert payload["instance_id"] == 5
    assert "ccs-anon-5" in payload["planned_actions"][0]


def test_cli_bare_start_dry_run_self_consistent():
    r = _run_cli("start", "--dry-run")
    assert r.returncode == 0, r.stderr
    payload = _parse_payload(r.stdout)
    inst = payload["instance_id"]
    assert isinstance(inst, int) and 0 <= inst <= MAX_INSTANCES
    expected = make_tmux_name("anon", inst)
    assert expected in payload["planned_actions"][0], \
        f"planned tmux 名与 instance_id 不自洽: {expected} vs {payload['planned_actions'][0]}"
    if "tmux_session" in payload:
        assert payload["tmux_session"] == expected


def test_cli_dispatch_allocates_when_main_busy(monkeypatch, capsys):
    """进程内走 ccs.py 分发：ccs-anon 已占 → 裸 start 应分配 1，绝不复用 0。

    红绿反证锚：resolve 不记名分支恒 return 0 突变下本例必红。
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(core, "_opencode_gate_port", lambda: None)
    monkeypatch.setattr(core, "get_role", lambda name: None)
    monkeypatch.setattr(sentinel, "_list_tmux_sessions", lambda: {"ccs-anon"})
    monkeypatch.setattr(sys, "argv", ["ccs.py", "start", "--dry-run"])
    import ccs
    ccs.main()
    payload = _parse_payload(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["instance_id"] == 1, f"ccs-anon 已占应分配 1: {payload}"
    assert "ccs-anon-1" in payload["planned_actions"][0]
