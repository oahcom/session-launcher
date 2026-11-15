#!/usr/bin/env python3
"""opencode 原生直连：provider 写入（契约 C4）+ HTTPS_PROXY 注入（闸口出口）。

① provider 写入保留 9router 段与用户手写键；
② HTTPS_PROXY 注入只对 opencode 行、claude 行零改动。
"""

import json
import sys
from pathlib import Path

_launcher_src = Path(__file__).resolve().parents[2] / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

import os
os.environ.setdefault("SESSION_PIPELINE_SKIP_TTL_PRUNER", "1")
os.environ.setdefault("CLAUDECODE_PERM_FLAGS", "")

import core


# ── ① provider 写入：保留 9router 段与用户手写键 ─────────────────

# 契约 C4（2026-11-15 起）：baseURL 指向第八闸口 L7 拦截层（127.0.0.1:7899/v1），
# 429/transport 由闸口判出换 ok 渠道透明重试；仍属 oc-native 原生协议，不经 9router。
_C4_OC_NATIVE = {
    "npm": "@ai-sdk/openai-compatible",
    "name": "OpenCode Native",
    "options": {"baseURL": "http://127.0.0.1:7899/v1"},
    "models": {"deepseek-v4.1-flash-free": {
        "name": "DSv4.1 Flash Free",
        "limit": {"context": 200000, "output": 32000},
    }},
}


def test_provider_write_preserves_9router_and_user_keys(tmp_path):
    f = tmp_path / "opencode.jsonc"
    original_9router = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "9Router",
        "options": {"baseURL": "http://localhost:20127/v1", "apiKey": "9router-local"},
        "models": {"9router_hermes": {
            "name": "9Router Hermes",
            "limit": {"context": 300000, "output": 64000},
        }},
    }
    f.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "permission": "allow",
        "provider": {"9router": original_9router, "my-own": {"npm": "x"}},
        "user_custom_key": {"foo": 1},
    }, indent=2), encoding="utf-8")

    ok = core._write_opencode_provider(path=f)
    assert ok is True

    out = json.loads(f.read_text(encoding="utf-8"))
    # 契约 C4 字段一字不差
    assert out["provider"]["oc-native"] == _C4_OC_NATIVE
    # 9router 段原样保留（用户裁决 D2：不许改写、不许删）
    assert out["provider"]["9router"] == original_9router
    # 用户手写键保留
    assert out["provider"]["my-own"] == {"npm": "x"}
    assert out["user_custom_key"] == {"foo": 1}
    assert out["permission"] == "allow"


def test_provider_write_skips_broken_json(tmp_path):
    f = tmp_path / "opencode.jsonc"
    garbage = "{ this is not json"
    f.write_text(garbage, encoding="utf-8")
    ok = core._write_opencode_provider(path=f)
    assert ok is False
    assert f.read_text(encoding="utf-8") == garbage, "JSON 坏了不许覆盖，只告警"


def test_provider_migration_swaps_only_oc_native_baseurl(tmp_path):
    """迁移钉 D2：旧 oc-native 的 baseURL 切 L7 新值，9router/用户键逐字节保留。"""
    f = tmp_path / "opencode.jsonc"
    original_9router = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "9Router",
        "options": {"baseURL": "http://localhost:20127/v1", "apiKey": "9router-local"},
        "models": {"9router_hermes": {
            "name": "9Router Hermes",
            "limit": {"context": 300000, "output": 64000},
        }},
    }
    old_oc_native = {  # 迁移前的旧契约 C4（baseURL 仍指原生端点）
        "npm": "@ai-sdk/openai-compatible",
        "name": "OpenCode Native",
        "options": {"baseURL": "https://api.opencode.ai/v1"},
        "models": {"deepseek-v4.1-flash-free": {
            "name": "DSv4.1 Flash Free",
            "limit": {"context": 200000, "output": 32000},
        }},
    }
    input_data = {
        "$schema": "https://opencode.ai/config.json",
        "permission": "allow",
        "provider": {
            "9router": original_9router,
            "my-own": {"npm": "x"},
            "oc-native": old_oc_native,
        },
        "user_custom_key": {"foo": 1},
    }
    in_raw = json.dumps(input_data, indent=2, ensure_ascii=False)
    f.write_text(in_raw, encoding="utf-8")

    ok = core._write_opencode_provider(path=f)
    assert ok is True

    out_raw = f.read_text(encoding="utf-8")
    out = json.loads(out_raw)

    # 只把 oc-native 的 baseURL 写成 L7 拦截层新值（整段为契约 C4）
    assert out["provider"]["oc-native"] == _C4_OC_NATIVE
    assert out["provider"]["oc-native"]["options"]["baseURL"] == "http://127.0.0.1:7899/v1"
    assert "https://api.opencode.ai/v1" not in out_raw

    # 9router 段与用户手写键逐字节保留（D2：不许改写、不许删）——
    # 输出全文 = 输入全文仅替换 baseURL 一处字符串，其余字节全等
    assert out_raw == in_raw.replace(
        "https://api.opencode.ai/v1", "http://127.0.0.1:7899/v1"
    ), "除 oc-native.baseURL 外不得有任何字节变化"
    assert out["provider"]["9router"] == original_9router
    assert out["provider"]["my-own"] == {"npm": "x"}
    assert out["user_custom_key"] == {"foo": 1}
    assert out["permission"] == "allow"


# ── ② HTTPS_PROXY 注入：只对 opencode 行，claude 行零改动 ─────────

def test_proxy_injected_for_opencode_row(monkeypatch):
    monkeypatch.setenv("OPENCODE_GATE_PORT", "18080")
    cmd = core._opencode_inner_cmd()
    assert "HTTPS_PROXY=http://127.0.0.1:18080" in cmd
    assert "HTTP_PROXY=http://127.0.0.1:18080" in cmd
    assert "NO_PROXY=127.0.0.1,localhost" in cmd
    assert cmd.rstrip().endswith(".opencode/bin/opencode")
    assert "ANTHROPIC_" not in cmd, "opencode 行不得注入 ANTHROPIC_*"


def test_proxy_degrades_explicitly_when_port_missing(monkeypatch):
    monkeypatch.delenv("OPENCODE_GATE_PORT", raising=False)
    monkeypatch.setattr(core, "_cfg_get_value", lambda key: None)
    # 阻断 /slots 动态发现（生产 config 现有 socket/token 默认值，测试不得打生产 UDS）
    import ops.opencode_quota as _q
    monkeypatch.setattr(_q, "discover_gate_port", lambda: None)
    cmd = core._opencode_inner_cmd()
    assert "HTTPS_PROXY" not in cmd and "HTTP_PROXY" not in cmd \
        and "NO_PROXY" not in cmd, "读不到端口必须显式降级为不注入"
    assert cmd == str(Path.home() / ".opencode" / "bin" / "opencode")


def test_claude_row_zero_change(monkeypatch):
    monkeypatch.setenv("OPENCODE_GATE_PORT", "18080")
    cmd = core._ccs_inner_cmd()
    assert "HTTPS_PROXY" not in cmd and "HTTP_PROXY" not in cmd and "NO_PROXY" not in cmd, \
        "claude 行不得注入闸口代理 env（P4 零回归）"
    assert cmd.startswith("claude --model 9router_hermes")
    assert "--permission-mode bypassPermissions" in cmd
    assert "--effort high" in cmd
    # 启动配方注册表数据不动（红线 2）
    assert core._ENGINE_REGISTRY["ccs"]["inject_router_env"] is True
    assert core._ENGINE_REGISTRY["ccs"]["inner_builder"] is core._ccs_inner_cmd
    assert core._ENGINE_REGISTRY["opencode"]["inner_builder"] is core._opencode_inner_cmd
