#!/usr/bin/env python3
"""Test `_write_mcp_settings` — MCP 隔离写入逻辑。"""

import importlib.util, json, sys, os
from pathlib import Path

_launcher_src = Path(__file__).resolve().parent.parent / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

# 使用 __import__ 确保包结构正确解析（importlib exec_module 无法处理相对导入）
os.environ.setdefault("SESSION_PIPELINE_SKIP_TTL_PRUNER", "1")
os.environ.setdefault("CLAUDECODE_PERM_FLAGS", "")
sys.path.insert(0, str(_launcher_src))  # 重复确保顺序
import core


def test_inherits_global_settings():
    """workspace settings 继承全局 permissions/hooks/model"""
    ws = Path("/tmp/_mcp_test_inherit")
    ws.mkdir(parents=True, exist_ok=True)
    core._write_mcp_settings(ws, {"name": "test", "mcp_servers": []})
    out = json.loads((ws / ".claude" / "settings.json").read_text())
    assert "hooks" in out
    assert "model" in out
    assert "permissions" in out


def test_role_mcp_merged():
    """角色声明的 mcp_servers 在注册表有时应写入"""
    ws = Path("/tmp/_mcp_test_merge")
    ws.mkdir(parents=True, exist_ok=True)
    role = {"name": "test", "mcp_servers": ["example-server"]}
    core._write_mcp_settings(ws, role)
    out = json.loads((ws / ".claude" / "settings.json").read_text())
    ms = out.get("mcpServers", {})
    assert "example-server" in ms, f"example-server should be in mcpServers, got {list(ms.keys())}"


def test_unknown_server_skipped():
    """角色声明了注册表中不存在的 server 应静默跳过"""
    ws = Path("/tmp/_mcp_test_skip")
    ws.mkdir(parents=True, exist_ok=True)
    role = {"name": "test", "mcp_servers": ["nonexistent-server-12345"]}
    core._write_mcp_settings(ws, role)
    out = json.loads((ws / ".claude" / "settings.json").read_text())
    assert "nonexistent-server-12345" not in out.get("mcpServers", {})


def test_empty_mcp_servers_ok():
    """mcp_servers 为空时 settings 正常写入"""
    ws = Path("/tmp/_mcp_test_empty")
    ws.mkdir(parents=True, exist_ok=True)
    core._write_mcp_settings(ws, {"name": "test", "mcp_servers": []})
    out = json.loads((ws / ".claude" / "settings.json").read_text())
    assert isinstance(out.get("mcpServers", {}), dict)


def test_no_role_def_ok():
    """role_def 为 None 时 settings 正常写入"""
    ws = Path("/tmp/_mcp_test_nodef")
    ws.mkdir(parents=True, exist_ok=True)
    core._write_mcp_settings(ws, None)
    out = json.loads((ws / ".claude" / "settings.json").read_text())
    assert isinstance(out.get("mcpServers", {}), dict)


if __name__ == "__main__":
    tests = [n for n in dir() if n.startswith("test_")]
    passed, failed = 0, 0
    for name in sorted(tests):
        try:
            globals()[name]()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} 通过")
    raise SystemExit(0 if failed == 0 else 1)
