#!/usr/bin/env python3
"""引擎缺省 opencode + 不记名启动路径（契约 4.2 三层声明的缺省层）。

钉住四件事：
① 裸 start（role="anon"，无 persona）→ 引擎解析为 opencode；
② persona 显式声明 engine:"ccs"（如 engineer）→ 仍解析为 ccs（19 个存量
   persona 零回归锚：persona 层在缺省之后覆盖，缺省改动不得波及）；
③ --engine 显式覆盖优先级不变（ccs 覆盖缺省、opencode 覆盖 persona 各一条）；
④ 无 persona 的任意角色名 → get_role None → 不报错、不注入 persona
   （沿用既有行为，加断言钉住"无限制"路径不被新校验拦截）。

另钉两条零回归锚：codex 显式拒绝口径未动；dry-run 输出带引擎配方
（裸 start 显示 opencode 配方、persona 角色显示 persona 引擎配方）。
全部走 dry-run 或深度 mock，不碰真实 tmux/哨兵/全局 opencode 配置。
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
from core import StartOptions, start


def _stub_gate(monkeypatch):
    """配方拼装不探闸口端口：防 discover_gate_port 的 UDS/HTTP 探测进单测。"""
    monkeypatch.setattr(core, "_opencode_gate_port", lambda: None)


def _recipe_line(result: dict) -> str:
    return next(a for a in result["planned_actions"] if a.startswith("引擎"))


# ── ① 裸 start（不记名）→ 缺省 opencode ─────────────────────────

def test_anon_bare_start_defaults_to_opencode(monkeypatch):
    _stub_gate(monkeypatch)
    monkeypatch.setattr(core, "get_role", lambda name: None)
    r = start(StartOptions(role="anon", dry_run=True))
    assert r["success"] is True
    assert r["engine"] == "opencode"
    recipe = _recipe_line(r)
    assert "opencode" in recipe, f"裸 start 应走 opencode 配方: {recipe}"
    assert "claude --model" not in recipe
    # 不记名会话命名钉住：tmux ccs-anon
    assert "ccs-anon" in r["planned_actions"][0]


# ── ② persona engine:"ccs" → 仍 ccs（零回归锚）─────────────────

def test_persona_engine_ccs_still_resolves_ccs(monkeypatch):
    _stub_gate(monkeypatch)
    monkeypatch.setattr(core, "get_role",
                        lambda name: {"name": "engineer", "engine": "ccs"})
    r = start(StartOptions(role="engineer", dry_run=True))
    assert r["success"] is True
    assert r["engine"] == "ccs", "persona 显式 engine:ccs 不得被新缺省翻转"
    recipe = _recipe_line(r)
    assert "claude --model 9router_hermes" in recipe, recipe


# ── ③ --engine 显式覆盖优先级不变 ─────────────────────────────

def test_explicit_engine_opencode_overrides_persona_ccs(monkeypatch):
    _stub_gate(monkeypatch)
    monkeypatch.setattr(core, "get_role",
                        lambda name: {"name": "engineer", "engine": "ccs"})
    r = start(StartOptions(role="engineer", dry_run=True, engine="opencode"))
    assert r["success"] is True
    assert r["engine"] == "opencode", "--engine 覆盖必须优先于 persona.engine"


def test_explicit_engine_ccs_overrides_default_opencode(monkeypatch):
    _stub_gate(monkeypatch)
    monkeypatch.setattr(core, "get_role", lambda name: None)
    r = start(StartOptions(role="anon", dry_run=True, engine="ccs"))
    assert r["success"] is True
    assert r["engine"] == "ccs", "--engine 覆盖必须优先于缺省 opencode"
    assert "claude --model" in _recipe_line(r)


# ── ④ 无 persona 任意角色名 → 不报错、不注入 persona ────────────

def test_no_persona_role_starts_without_injection(monkeypatch):
    calls = {"inject": 0, "prompt": 0}
    monkeypatch.setattr(core, "_is_alive", lambda *a, **k: False)
    monkeypatch.setattr(core, "_check_memory_before_launch", lambda *a, **k: None)
    monkeypatch.setattr(core, "workspace_create",
                        lambda *a, **k: {"success": True, "action": "created"})
    monkeypatch.setattr(core, "_write_mcp_settings", lambda *a, **k: None)
    monkeypatch.setattr(core, "_write_opencode_mcp", lambda *a, **k: None)
    monkeypatch.setattr(core, "_write_opencode_provider", lambda *a, **k: True)
    monkeypatch.setattr(core, "_launch_tmux", lambda *a, **k: None)
    monkeypatch.setattr(core, "_inject_prompt_and_autosend", lambda *a, **k: None)
    monkeypatch.setattr(core, "write_sentinel", lambda *a, **k: None)
    monkeypatch.setattr(core, "_find_engine_pid_by_comm", lambda *a, **k: None)
    monkeypatch.setattr(core, "get_role", lambda name: None)

    def _no_inject(*a, **k):
        calls["inject"] += 1

    def _no_prompt(*a, **k):
        calls["prompt"] += 1
        return ""

    monkeypatch.setattr(core, "inject_role_knowledge_into_workspace", _no_inject)
    monkeypatch.setattr(core, "_build_role_prompt", _no_prompt)

    r = start(StartOptions(role="zzz-nobody", detach=True))
    assert r["success"] is True, f"无 persona 名不得报错: {r}"
    assert r["engine"] == "opencode", "无 persona 名走缺省 opencode"
    assert calls["inject"] == 0, "无 persona 不得注入角色知识（无限制路径）"
    assert calls["prompt"] == 0, "无 persona 不得构建角色 prompt"


# ── 零回归锚：codex 显式拒绝口径未动 ──────────────────────────

def test_codex_engine_still_rejected(monkeypatch):
    monkeypatch.setattr(core, "get_role", lambda name: None)
    r = start(StartOptions(role="anon", dry_run=True, engine="codex"))
    assert r["success"] is False
    assert "codex" in r["error"]
    assert "本期未接入主启动面" in r["error"]


# ── CLI 边界：裸 `ccs start` 映射 role=anon + 引擎 opencode ──────

def test_cli_bare_start_maps_to_anon_and_opencode():
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    r = subprocess.run(
        [sys.executable, str(_launcher_src / "ccs.py"), "start", "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    # ccs.py 先打人读行再打 indent=2 JSON；从首个 "{" 行起解析
    lines = r.stdout.splitlines()
    begin = next(i for i, ln in enumerate(lines) if ln.strip() == "{")
    payload = json.loads("\n".join(lines[begin:]))
    assert payload["role"] == "anon", "裸 ccs start 应映射不记名缺省名 anon"
    assert payload["engine"] == "opencode"
