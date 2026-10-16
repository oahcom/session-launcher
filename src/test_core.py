#!/usr/bin/env python3
"""
test_core.py — core.py 自检测试（纯 stdlib，无外部依赖）

覆盖 core.py 关键函数：start 参数校验、send 失败计数、(health_)status、
inject_role_knowledge_into_workspace 注入契约、_invalidate_role_cache、
load_roles、_validate_role_name、_build_role_prompt、_router_env 等。

运行: python3 test_core.py
退出码: 0=全部通过, 1=有失败
"""

import sys
import json
import time
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# 将 src 目录加入路径以便导入 core
sys.path.insert(0, str(Path(__file__).parent))

import core
import routing.roles as roles_mod
import tmux_ops

TMP = Path(tempfile.mkdtemp())


def _reset_role_cache():
    """清空 load_roles 的内部缓存，确保每次读最新。"""
    roles_mod._LOADED_ALL_ROLES = None
    roles_mod._ROLE_CACHE.clear()


def teardown():
    shutil.rmtree(TMP, ignore_errors=True)


# ─────────────────────────────────────────────
# 1. start() 的 workspace 名校验（非法名直接拒绝，不启动 tmux）
# ─────────────────────────────────────────────
def test_start_rejects_invalid_workspace():
    # _validate_role_name 拒绝含路径遍历/空格的名称
    assert core._validate_role_name("../etc") is False
    assert core._validate_role_name("a b") is False
    assert core._validate_role_name("ok_name-1") is True
    # 非法 workspace -> start 返回 failure，不触发 launch
    with patch.object(core, "_validate_role_name", side_effect=core._validate_role_name):
        res = core.start("engineer", workspace="../evil")
    assert res.get("success") is False
    assert "非法 workspace" in res.get("error", "")
    print("✓ test_start_rejects_invalid_workspace")


# ─────────────────────────────────────────────
# 2. send() 的 3 连败门槛：前 2 次只报探测失败，第 3 次才写 fallback
# ─────────────────────────────────────────────
def test_send_fail_streak_threshold():
    streak_file = TMP / "send_fail_streak.json"
    with patch.object(core, "_SEND_FAIL_STREAK_FILE", streak_file), \
         patch("core._is_alive", return_value=False), \
         patch("tmux_ops.make_tmux_name", return_value="ccs-test"), \
         patch("core._write_bus_notice") as notice:
        # 第 1、2 次：不写 fallback
        for _ in range(2):
            r = core.send("test", "hello", source="pipeline")
            assert r["success"] is False
            assert "暂缓 fallback" in r["error"]
        assert notice.call_count == 0
        # 第 3 次：达阈值，写 fallback
        r = core.send("test", "hello", source="pipeline")
        assert r["success"] is False
        assert notice.call_count == 1
        # 成功后清零
        core._reset_send_fail_streak("test")
    print("✓ test_send_fail_streak_threshold")


# ─────────────────────────────────────────────
# 3. status() / health_check() 对哨兵列表的封装结构
# ─────────────────────────────────────────────
def test_status_and_health_check_structure():
    sentinel = MagicMock()
    sentinel.role = "engineer"
    sentinel.instance_id = 0
    sentinel.title = "Engineer"
    sentinel.tmux_session = "ccs-engineer"
    sentinel.pid = 12345
    sentinel.started_at = time.time()
    sentinel.lifecycle = "infinite"
    sentinel.partners = []
    sentinel.bus_track = ""
    sentinel.health.watchdog_ok = True
    sentinel.health.last_bus_msg_age = 0.0
    sentinel.health.restart_count = 0

    with patch("core.list_sentinels", return_value=[sentinel]), \
         patch("core._is_alive", return_value=True):
        statuses = core.status()
        assert isinstance(statuses, list) and len(statuses) == 1
        s = statuses[0]
        assert s["role"] == "engineer" and s["alive"] is True
        assert "uptime_sec" in s and "health" in s

        hc = core.health_check()
        assert "engineer" in hc
        assert hc["engineer"]["alive"] is True
        assert "watchdog_ok" in hc["engineer"]
    print("✓ test_status_and_health_check_structure")


# ─────────────────────────────────────────────
# 4. inject_role_knowledge_into_workspace 写入 KNOWLEDGE 块到 workspace CLAUDE.md
# ─────────────────────────────────────────────
def test_inject_role_knowledge_into_workspace():
    ws = TMP / "inject_ws"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# original\n")
    role = {"name": "inject_role", "title": "Inject"}

    with patch.object(roles_mod, "_resolve_ws_paths", return_value=[ws / "CLAUDE.md"]), \
         patch.object(roles_mod, "_role_assembler_output", return_value="KNOWLEDGE CONTENT"), \
         patch("core.SESSION_ROLES_ROOT", TMP):
        out = core.inject_role_knowledge_into_workspace(role)
    content = (ws / "CLAUDE.md").read_text() if (ws / "CLAUDE.md").exists() else ""
    assert "KNOWLEDGE:START" in content, "应注入 KNOWLEDGE 块"
    print("✓ test_inject_role_knowledge_into_workspace")


# ─────────────────────────────────────────────
# 5. _invalidate_role_cache 清空缓存
# ─────────────────────────────────────────────
def test_invalidate_role_cache():
    roles_mod._ROLE_CACHE["x"] = {"name": "x"}
    roles_mod._LOADED_ALL_ROLES = [{"name": "x"}]
    core._invalidate_role_cache()
    assert roles_mod._ROLE_CACHE == {}
    assert roles_mod._LOADED_ALL_ROLES is None
    print("✓ test_invalidate_role_cache")


# ─────────────────────────────────────────────
# 6. load_roles 返回 list（mock 底层 loader 隔离文件系统）
# ─────────────────────────────────────────────
def test_load_roles_returns_list():
    fake = [{"name": "a"}, {"name": "b"}]
    _reset_role_cache()
    with patch.object(roles_mod, "_sl_load_roles", return_value=fake), \
         patch.object(roles_mod, "_run_shared_loader_validate", return_value=True):
        roles = core.load_roles()
    assert isinstance(roles, list)
    assert len(roles) == 2
    print("✓ test_load_roles_returns_list")


# ─────────────────────────────────────────────
# 7. _build_role_prompt 由 role dict 生成字符串
# ─────────────────────────────────────────────
def test_build_role_prompt():
    role = {"name": "pm", "title": "PM", "drive": "continuous"}
    p = core._build_role_prompt(role)
    assert isinstance(p, str) and "PM" in p
    print("✓ test_build_role_prompt")


# ─────────────────────────────────────────────
# 8. _router_env 解析 export 行 + 缺失时回落默认
# ─────────────────────────────────────────────
def test_router_env_parsing_and_fallback():
    import re
    fake_file = MagicMock()
    fake_file.exists.return_value = True
    fake_file.read_text.return_value = (
        'export ANTHROPIC_BASE_URL="http://localhost:20128/v1"\n'
        'export ANTHROPIC_API_KEY="secret"\n'
        'export OTHER="ignored"\n'
    )
    with patch.object(core, "_ROUTER_ENV_FILE", fake_file), \
         patch.object(core, "_ROUTER_ENV_RE", core._ROUTER_ENV_RE):
        env = core._router_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:20128/v1"
    assert env["ANTHROPIC_API_KEY"] == "secret"
    assert "OTHER" not in env

    # 文件缺失 -> 默认回落
    missing = MagicMock()
    missing.exists.return_value = False
    with patch.object(core, "_ROUTER_ENV_FILE", missing):
        env2 = core._router_env()
    assert env2["ANTHROPIC_BASE_URL"] == "http://localhost:20128/v1"
    assert env2["ANTHROPIC_API_KEY"] == "9router-local"
    print("✓ test_router_env_parsing_and_fallback")


# ─────────────────────────────────────────────
# 10. stop() 回收 feed 子进程 + 删除哨兵（幂等）
# ─────────────────────────────────────────────
def test_stop_recycles_feed_proc_and_sentinel():
    fake_proc = MagicMock()
    fake_proc.poll.return_value = 0  # 已退出
    core._FEED_PROCS["ccs-engineer"] = fake_proc

    with patch("tmux_ops.make_tmux_name", return_value="ccs-engineer"), \
         patch("core._is_alive", return_value=True), \
         patch("core._tmux_kill"), \
         simple_patch_stop_helpers():
        res = core.stop("engineer")
    assert res["success"] is True
    assert "ccs-engineer" not in core._FEED_PROCS
    print("✓ test_stop_recycles_feed_proc_and_sentinel")


def simple_patch_stop_helpers():
    """stop() 内的守护线程/哨兵回收用 nullctx 批量 patch。"""
    from contextlib import ExitStack
    from unittest.mock import patch
    stack = ExitStack()
    stack.enter_context(patch("ops.watchdog.stop_watchdog"))
    stack.enter_context(patch("ops.tracker.stop_tracker"))
    stack.enter_context(patch("core.delete_sentinel"))
    return stack


# ─────────────────────────────────────────────
# 11. StartOptions 数据类默认值 + register_hook 触发
# ─────────────────────────────────────────────
def test_start_options_and_hooks():
    o = core.StartOptions(role="qa", instance_id=2)
    assert o.role == "qa" and o.instance_id == 2
    assert o.detach is False and o.partners is None

    fired = []
    core.register_hook("before_send", lambda **kw: fired.append(kw))
    core._trigger_hooks("before_send", role="qa")
    assert len(fired) == 1 and fired[0]["role"] == "qa"
    print("✓ test_start_options_and_hooks")


TESTS = [
    test_start_rejects_invalid_workspace,
    test_send_fail_streak_threshold,
    test_status_and_health_check_structure,
    test_inject_role_knowledge_into_workspace,
    test_invalidate_role_cache,
    test_load_roles_returns_list,
    test_build_role_prompt,
    test_router_env_parsing_and_fallback,
    # test_ondemand_timeout_and_stale_cleanup 已随其定义一并移除：
    # 该函数在文件中无定义（另有 10 个 def 对应 11 个引用），导致 import 时
    # NameError、整个 test_core 收集失败，连带 pytest 在 src/ 下直接中断。
    test_stop_recycles_feed_proc_and_sentinel,
    test_start_options_and_hooks,
]


def run():
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*40}\n通过 {passed} / 失败 {failed} ({len(TESTS)} 个测试)\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    teardown()
    sys.exit(0 if ok else 1)
