#!/usr/bin/env python3
"""
Session Launcher 端到端 Mock 测试。

验证信号检查委托逻辑、prompt 注入、生命周期哨兵写入。

运行: python3 tests/test_e2e_mock.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# --- 测试辅助函数 ---


def _make_role(
    name: str = "maintainer",
    title: str = "测试角色",
    lifecycle: str = "infinite",
    drive: str = "cron",
    cron_schedule: str = "*/15 * * * *",
    signal_source: str = "",
    signal_filter: str = "",
) -> dict:
    """创建测试用角色 JSON。"""
    return {
        "name": name,
        "title": title,
        "description": "测试用角色",
        "category": "测试",
        "lifecycle": lifecycle,
        "drive": drive,
        "cron_schedule": cron_schedule,
        "idle_action": "exit",
        "session_hint": "cron",
        "eval_criteria": ["条件1"],
        "input_signals": [
            {"source": signal_source or f"systemctl --user is-active {name}.service",
             "filter": signal_filter}
        ],
        "output_targets": ["bus cat=test"],
        "system_prompt": "你是 {persona_title}({persona_name})，测试用。\n\n## 专长\n- 测试\n\n## 行为准则\n1. 完成测试\n"
    }


# --- 信号映射测试（验证 launcher.check_signal 委托给 signals） ---


def test_check_signal_bus_unread():
    """bus_client.py 源 → signals.bus_unread。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=True) as m:
        assert check_signal({"source": "bus_client.py unread --all", "filter": ""}) is True
        m.assert_called_with("bus_unread", "")


def test_check_signal_bus_empty():
    """无未读返回 False。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=False):
        assert check_signal({"source": "bus_client.py unread --all", "filter": ""}) is False


def test_check_signal_systemctl_active():
    """systemctl 源 → signals.systemctl_active，False → 无工作。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=False):
        assert check_signal({"source": "systemctl --user is-active demo.service", "filter": ""}) is False


def test_check_signal_systemctl_inactive():
    """systemctl 源 → signals.systemctl_active，True → 有工作。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=True) as m:
        assert check_signal({"source": "systemctl --user is-active demo.service", "filter": ""}) is True
        m.assert_called_with("systemctl_active", "")


def test_check_signal_curl_200():
    """curl 源 → signals.http_health，False → 无工作。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=False):
        assert check_signal({"source": "curl http://localhost:8890", "filter": ""}) is False


def test_check_signal_curl_non200():
    """curl 源 → signals.http_health，True → 有工作。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=True) as m:
        assert check_signal({"source": "curl http://localhost:8890", "filter": ""}) is True
        m.assert_called_with("http_health", "")


def test_check_signal_journalctl_error():
    """journalctl 源 → signals.journalctl_errors。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=True) as m:
        assert check_signal({"source": "journalctl --user -u demo --since 1h --no-pager",
                             "filter": "ERROR|exception|Traceback"}) is True
        m.assert_called_with("journalctl_errors", "ERROR|exception|Traceback")


def test_check_signal_journalctl_clean():
    """无 ERROR 返回 False。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=False):
        assert check_signal({"source": "journalctl --user -u demo --since 1h --no-pager",
                             "filter": "ERROR|exception|Traceback"}) is False


def test_check_signal_git_staged():
    """git diff 源 → signals.git_staged。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=True) as m:
        assert check_signal({"source": "git diff --cached --name-only", "filter": ""}) is True
        m.assert_called_with("git_staged", "")


def test_check_signal_git_no_staged():
    """无 staged 返回 False。"""
    from launcher import check_signal
    with patch("signals.check_signal_by_name", return_value=False):
        assert check_signal({"source": "git diff --cached --name-only", "filter": ""}) is False


# --- has_work 测试 ---


def test_has_work_finds_active():
    """has_work 在有 active 信号的角色中返回第一个。"""
    from launcher import has_work
    roles = [
        _make_role(name="scout", signal_source="curl http://localhost:9999"),
        _make_role(name="maintainer"),
    ]

    def mock_check(signal):
        return "curl" in signal.get("source", "")

    with patch("launcher.check_signal", side_effect=mock_check):
        result = has_work(roles)
    assert result is not None
    assert result["name"] == "scout"


def test_has_work_none_active():
    """所有角色都无信号时返回 None。"""
    from launcher import has_work
    roles = [_make_role(), _make_role(name="scout")]
    with patch("launcher.check_signal", return_value=False):
        result = has_work(roles)
    assert result is None


# --- prompt 注入测试 ---


def test_inject_prompt_creates_file():
    """CLAUDE.md 不存在时创建新文件。"""
    from launcher import inject_prompt_into_claudemd, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        with patch("launcher.CLAUDE_MD", fake_path):
            role = _make_role()
            inject_prompt_into_claudemd(role)
            assert fake_path.exists()
            content = fake_path.read_text()
            assert SESSION_MARKER_START in content
            assert SESSION_MARKER_END in content
            assert role["name"] in content


def test_inject_prompt_replaces_marker():
    """已有 marker 时替换中间内容。"""
    from launcher import inject_prompt_into_claudemd, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        fake_path.write_text(f"原有内容\n{SESSION_MARKER_START}\n旧内容\n{SESSION_MARKER_END}\n更多内容")

        with patch("launcher.CLAUDE_MD", fake_path):
            inject_prompt_into_claudemd(_make_role(name="new_role", title="新角色"))
            content = fake_path.read_text()
            assert "原有内容" in content
            assert "更多内容" in content
            assert "旧内容" not in content
            assert "新角色" in content


def test_clear_injected_prompt():
    """清除注入块后 marker 不应存在。"""
    from launcher import clear_injected_prompt, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        fake_path.write_text(f"原有\n{SESSION_MARKER_START}\n内容\n{SESSION_MARKER_END}\n")

        with patch("launcher.CLAUDE_MD", fake_path):
            clear_injected_prompt()
            content = fake_path.read_text()
            assert SESSION_MARKER_START not in content
            assert SESSION_MARKER_END not in content
            assert "原有" in content


# --- 生命周期哨兵测试 ---


def test_write_lifecycle_sentinel():
    """ondemand 角色应创建哨兵文件。"""
    from launcher import write_lifecycle_sentinel

    role = _make_role(name="developer", lifecycle="ondemand")
    with patch("launcher.subprocess.run",
               return_value=MagicMock(stdout="2026-06-30T12:00:00+0800\n")):
        write_lifecycle_sentinel(role)

    sentinel = Path("/tmp/session-launcher/developer.active")
    assert sentinel.exists()
    data = json.loads(sentinel.read_text())
    assert data["role"] == "developer"
    assert data["lifecycle"] == "ondemand"
    sentinel.unlink(missing_ok=True)


def test_cleanup_stale_sentinels():
    """进程已退出的哨兵应被清理。"""
    from launcher import cleanup_stale_sentinels
    sentinel_dir = Path("/tmp/session-launcher")
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    bad_sentinel = sentinel_dir / "ghost.active"
    bad_sentinel.write_text(json.dumps({
        "role": "ghost", "title": "幽灵", "lifecycle": "ondemand",
        "pid": 999999999, "started_at": "2026-06-30"
    }))

    cleaned = cleanup_stale_sentinels()
    assert "ghost" in cleaned
    assert not bad_sentinel.exists()


# --- main 流程测试 ---


def test_main_no_work():
    """无工作时 main 应清除注入并退出。"""
    from launcher import main, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_md = Path(tmp) / "CLAUDE.md"
        fake_md.write_text(f"原有\n{SESSION_MARKER_START}\n旧角色内容\n{SESSION_MARKER_END}\n")

        with patch("launcher.CLAUDE_MD", fake_md):
            with patch("launcher.has_work", return_value=None):
                try:
                    main()
                except SystemExit as e:
                    assert e.code == 0

        content = fake_md.read_text()
        assert SESSION_MARKER_START not in content


def test_main_shell_output_format():
    """有工作时 main 输出正确的 export 行。"""
    from launcher import main
    import launcher
    import io

    test_roles_dir = Path("/tmp/test_session_roles")
    roles_subdir = test_roles_dir / "personas" / "session-roles"
    roles_subdir.mkdir(parents=True, exist_ok=True)
    (roles_subdir / "persona_00_test.json").write_text(json.dumps(
        _make_role(name="tester", title="测试员")
    ))

    original_root = launcher.SESSION_ROLES_ROOT
    try:
        launcher.SESSION_ROLES_ROOT = test_roles_dir
        test_role = _make_role(name="tester", title="测试员")

        with patch("launcher.has_work", return_value=test_role):
            with tempfile.TemporaryDirectory() as tmp:
                fake_md = Path(tmp) / "CLAUDE.md"
                with patch("launcher.CLAUDE_MD", fake_md):
                    captured = io.StringIO()
                    old_stdout = sys.stdout
                    try:
                        sys.stdout = captured
                        try:
                            launcher.main()
                        except SystemExit:
                            pass
                    finally:
                        sys.stdout = old_stdout

                    out = captured.getvalue()
                    export_lines = [l for l in out.splitlines() if l.startswith("export ")]
                    assert len(export_lines) >= 3, f"应有至少 3 行 export，实际: {export_lines}"
                    assert any("SESSION_ROLE=" in l for l in export_lines)
                    assert any("SESSION_LIFECYCLE=" in l for l in export_lines)
                    assert any("SESSION_DRIVE=" in l for l in export_lines)
    finally:
        launcher.SESSION_ROLES_ROOT = original_root
        shutil.rmtree(test_roles_dir, ignore_errors=True)


# --- 自检模式 ---


def _run_selfcheck():
    """不使用 pytest 框架的自检模式。"""
    import traceback

    tests = [
        ("bus 有未读", test_check_signal_bus_unread),
        ("bus 无未读", test_check_signal_bus_empty),
        ("systemctl 全部 active", test_check_signal_systemctl_active),
        ("systemctl 有 inactive", test_check_signal_systemctl_inactive),
        ("curl 200", test_check_signal_curl_200),
        ("curl 非 200", test_check_signal_curl_non200),
        ("journalctl 有错误", test_check_signal_journalctl_error),
        ("journalctl 无错误", test_check_signal_journalctl_clean),
        ("有 staged", test_check_signal_git_staged),
        ("无 staged", test_check_signal_git_no_staged),
        ("has_work 找到首个", test_has_work_finds_active),
        ("has_work 无工作", test_has_work_none_active),
        ("prompt 创建文件", test_inject_prompt_creates_file),
        ("prompt 替换 marker", test_inject_prompt_replaces_marker),
        ("clear 注入", test_clear_injected_prompt),
        ("哨兵写入", test_write_lifecycle_sentinel),
        ("哨兵清理", test_cleanup_stale_sentinels),
        ("无工作 main", test_main_no_work),
        ("shell 输出格式", test_main_shell_output_format),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception:
            print(f"  ❌ {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n结果: {passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    sys.exit(_run_selfcheck())
