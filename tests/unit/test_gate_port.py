#!/usr/bin/env python3
"""gate_port：闸口端口动态发现（opencode_quota 迁出活代码的行为锚）。

旧文件 tests/unit/test_opencode_quota.py 没有直接测 discover_gate_port 的用例
（transport 用例里仅把它 stub 成依赖）；本文件对迁出的活代码逐项钉
「行为一字不变」：
① 显式降级语义（config 缺失 / token 不可读或为空 → None）——语义迁自
   旧文件 test_rotate_degrades_when_config_missing 的「缺配置显式降级」锚；
② /slots 取 kind=="opencode" 的 port（list 与 {"slots": [...]} 两种载荷）；
③ HTTP 非 2xx / 坏载荷 / 端口非法 / 传输异常 → None（不盲报端口）；
④ _cfg_or_env：env 覆盖 config、回退 config、双缺 → None。
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

from ops import gate_port as g


def _fake_conn(monkeypatch, status=200, raw=b"[]", exc=None):
    """替换 _UnixHTTPConnection：返回固定回包或抛传输异常（不碰真 UDS）。"""

    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return raw

    class _Conn:
        def __init__(self, socket_path, timeout=5.0):
            pass

        def request(self, *args, **kwargs):
            if exc is not None:
                raise exc

        def getresponse(self):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(g, "_UnixHTTPConnection", _Conn)


def _stub_cfg(monkeypatch, sock="/tmp/oc-gate.sock", token="/tmp/oc-gate.token"):
    monkeypatch.setattr(
        g, "_cfg_or_env",
        lambda env_name, cfg_key: sock if "SOCK" in env_name else token)


def _write_token(tmp_path) -> str:
    p = tmp_path / "token"
    p.write_text("tk-1\n", encoding="utf-8")
    return str(p)


# ── ① 显式降级（语义迁自 test_rotate_degrades_when_config_missing）──

def test_discover_degrades_when_config_missing(monkeypatch):
    monkeypatch.setattr(g, "_cfg_or_env", lambda *a: None)
    assert g.discover_gate_port() is None, "缺配置必须显式降级（不硬编码端口）"


def test_discover_degrades_when_token_unreadable_or_empty(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch, token=str(tmp_path / "missing.token"))
    assert g.discover_gate_port() is None, "token 文件不可读 → None"
    empty = tmp_path / "empty.token"
    empty.write_text("", encoding="utf-8")
    _stub_cfg(monkeypatch, token=str(empty))
    assert g.discover_gate_port() is None, "token 为空 → None"


# ── ② /slots 取 kind=="opencode" 的 port ─────────────────────────

def test_discover_reads_opencode_slot_port(tmp_path, monkeypatch):
    token = _write_token(tmp_path)
    _stub_cfg(monkeypatch, token=token)
    _fake_conn(monkeypatch, status=200,
               raw=json.dumps([{"kind": "ccs", "port": 7897},
                               {"kind": "opencode", "port": 7898}]).encode())
    assert g.discover_gate_port() == 7898, "list 载荷取 kind==opencode 的 port"
    _fake_conn(monkeypatch, status=200,
               raw=json.dumps({"slots": [{"kind": "opencode", "port": 7899}]}).encode())
    assert g.discover_gate_port() == 7899, '{"slots": [...]} 载荷同样认'


# ── ③ 非 2xx / 坏载荷 / 端口非法 / 传输异常 → None ────────────────

def test_discover_none_on_http_error(tmp_path, monkeypatch):
    token = _write_token(tmp_path)
    _stub_cfg(monkeypatch, token=token)
    _fake_conn(monkeypatch, status=500, raw=b"{}")
    assert g.discover_gate_port() is None, "HTTP 非 2xx → None"


def test_discover_none_on_bad_payload_or_kind_miss(tmp_path, monkeypatch):
    token = _write_token(tmp_path)
    _stub_cfg(monkeypatch, token=token)
    _fake_conn(monkeypatch, status=200, raw=b"not json")
    assert g.discover_gate_port() is None, "坏 JSON → None"
    _fake_conn(monkeypatch, status=200, raw=json.dumps({"slots": "oops"}).encode())
    assert g.discover_gate_port() is None, "slots 非 list → None"
    _fake_conn(monkeypatch, status=200,
               raw=json.dumps([{"kind": "ccs", "port": 7897}]).encode())
    assert g.discover_gate_port() is None, "无 kind==opencode → None"


def test_discover_none_on_invalid_port(tmp_path, monkeypatch):
    token = _write_token(tmp_path)
    _stub_cfg(monkeypatch, token=token)
    _fake_conn(monkeypatch, status=200,
               raw=json.dumps([{"kind": "opencode", "port": "7898"}]).encode())
    assert g.discover_gate_port() is None, "port 非 int → None"
    _fake_conn(monkeypatch, status=200,
               raw=json.dumps([{"kind": "opencode", "port": 70000}]).encode())
    assert g.discover_gate_port() is None, "port 越界 → None"


def test_discover_none_on_transport_error(tmp_path, monkeypatch):
    token = _write_token(tmp_path)
    _stub_cfg(monkeypatch, token=token)
    _fake_conn(monkeypatch, exc=OSError("connection refused"))
    assert g.discover_gate_port() is None, "传输异常 → None（调用方显式降级）"


# ── ④ _cfg_or_env：env 覆盖 config、双缺 → None ──────────────────

def test_cfg_or_env_env_overrides_config(monkeypatch):
    import ops.ccs_config as ccs_config
    monkeypatch.setenv("OPENCODE_GATE_ROTATE_SOCK", "/env/sock")
    called = {"n": 0}

    def _get_value(key):
        called["n"] += 1
        return "/cfg/sock"

    monkeypatch.setattr(ccs_config, "get_value", _get_value)
    assert g._cfg_or_env("OPENCODE_GATE_ROTATE_SOCK",
                         "opencode_gate.rotate_socket") == "/env/sock"
    assert called["n"] == 0, "env 有效时不得回退 config"

    monkeypatch.delenv("OPENCODE_GATE_ROTATE_SOCK")
    assert g._cfg_or_env("OPENCODE_GATE_ROTATE_SOCK",
                         "opencode_gate.rotate_socket") == "/cfg/sock"
    assert called["n"] == 1, "env 缺失才回退 config"

    monkeypatch.setattr(ccs_config, "get_value", lambda key: None)
    assert g._cfg_or_env("OPENCODE_GATE_ROTATE_SOCK",
                         "opencode_gate.rotate_socket") is None, "双缺 → None"
