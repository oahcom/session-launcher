#!/usr/bin/env python3
"""
Session Launcher 端到端 Mock 测试。

模拟所有外部依赖（systemctl / journalctl / curl / bus_client），
验证信号检查、角色匹配、prompt 注入、生命周期哨兵写入的逻辑正确性。

运行: python3 -m pytest tests/test_e2e_mock.py -x -v
     python3 tests/test_e2e_mock.py  （无 pytest 时自检模式）
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
    signal_type: str = "systemctl",
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


def _mock_subprocess_run(stdout: str, returncode: int = 0) -> MagicMock:
    """生成 subprocess.run 的 mock 返回值。"""
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


# --- 信号检查测试 ---


def test_check_signal_bus_unread():
    """bus 有未读消息时返回 True。"""
    from launcher import check_signal
    signal = {"source": "bus_client.py unread --all", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("5 unread messages\n")):
        assert check_signal(signal) is True, "有未读应返回 True"


def test_check_signal_bus_empty():
    """bus 无未读消息时返回 False。"""
    from launcher import check_signal
    signal = {"source": "bus_client.py unread --all", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("0 unread\n")):
        assert check_signal(signal) is False, "0 未读应返回 False"


def test_check_signal_systemctl_active():
    """服务全部 active 时返回 False（没有异常）。"""
    from launcher import check_signal
    signal = {"source": "systemctl --user is-active demo.service", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("active\n")):
        assert check_signal(signal) is False, "全部 active 应无工作"


def test_check_signal_systemctl_inactive():
    """有服务 inactive 时返回 True（有异常需要处理）。

    systemctl is-active inactive 服务返回 exit code 3，不是 0。
    """
    from launcher import check_signal
    signal = {"source": "systemctl --user is-active demo.service", "filter": ""}
    inactive_mock = _mock_subprocess_run("inactive\n")
    inactive_mock.returncode = 3  # 真实行为：systemctl is-active inactive → 3
    with patch("launcher._safe_run", return_value=inactive_mock):
        assert check_signal(signal) is True, "有 inactive 应返回 True"


def test_check_signal_curl_200():
    """端点返回 200 时无工作。"""
    from launcher import check_signal
    signal = {"source": "curl http://localhost:8890 http://localhost:20128", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("200")):
        assert check_signal(signal) is False, "全部 200 应无工作"


def test_check_signal_curl_non200():
    """端点非 200 时有工作。"""
    from launcher import check_signal
    signal = {"source": "curl http://localhost:8890", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("503")):
        assert check_signal(signal) is True, "非 200 应有工作"


def test_check_signal_journalctl_error():
    """journalctl 有错误时返回 True。"""
    from launcher import check_signal
    signal = {"source": "journalctl --user -u demo --since 1h --no-pager",
              "filter": "ERROR|exception|Traceback"}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run(
            "Jun 30 12:00:00 host python[1234]: ERROR: something broke\n")):
        assert check_signal(signal) is True, "有 ERROR 应返回 True"


def test_check_signal_journalctl_clean():
    """journalctl 无错误时返回 False。"""
    from launcher import check_signal
    signal = {"source": "journalctl --user -u demo --since 1h --no-pager",
              "filter": "ERROR|exception|Traceback"}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run(
            "Jun 30 12:00:00 host python[1234]: INFO: all good\n")):
        assert check_signal(signal) is False, "无 ERROR 应返回 False"


def test_check_signal_git_staged():
    """有 staged 代码时返回 True。"""
    from launcher import check_signal
    signal = {"source": "git diff --cached --name-only", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("src/foo.py\nsrc/bar.py\n")):
        assert check_signal(signal) is True, "有 staged 应返回 True"


def test_check_signal_git_no_staged():
    """无 staged 代码时返回 False。"""
    from launcher import check_signal
    signal = {"source": "git diff --cached --name-only", "filter": ""}
    with patch("launcher._safe_run", return_value=_mock_subprocess_run("")):
        assert check_signal(signal) is False, "无 staged 应返回 False"


# --- has_work 测试 ---


def test_has_work_finds_active():
    """has_work 在有 active 信号的角色中返回第一个。"""
    from launcher import has_work, check_signal
    roles = [
        _make_role(name="scout", signal_type="curl",
                   signal_source="curl http://localhost:9999"),
        _make_role(name="maintainer", signal_type="systemctl"),
    ]

    original_check = check_signal

    def mock_check_signal(signal):
        source = signal.get("source", "")
        if "curl" in source:
            return True  # scout 有工作
        return False

    with patch("launcher.check_signal", side_effect=mock_check_signal):
        result = has_work(roles)

    assert result is not None, "应找到有工作的角色"
    assert result["name"] == "scout", "应返回第一个有工作的角色"


def test_has_work_none_active():
    """所有角色都无信号时返回 None。"""
    from launcher import has_work
    roles = [_make_role(), _make_role(name="scout")]
    with patch("launcher.check_signal", return_value=False):
        result = has_work(roles)
    assert result is None, "无工作应返回 None"


# --- prompt 注入测试 ---


def test_inject_prompt_into_claudemd_creates_file():
    """CLAUDE.md 不存在时创建新文件。"""
    from launcher import inject_prompt_into_claudemd, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        with patch("launcher.CLAUDE_MD", fake_path):
            role = _make_role()
            inject_prompt_into_claudemd(role)
            assert fake_path.exists(), "应创建文件"
            content = fake_path.read_text()
            assert SESSION_MARKER_START in content, "应有起始 marker"
            assert SESSION_MARKER_END in content, "应有结束 marker"
            assert role["name"] in content, "应有角色名"


def test_inject_prompt_into_claudemd_replaces_marker():
    """CLAUDE.md 已有 marker 时替换中间内容。"""
    from launcher import inject_prompt_into_claudemd, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        fake_path.write_text(f"原有内容\n{SESSION_MARKER_START}\n旧内容\n{SESSION_MARKER_END}\n更多内容")

        with patch("launcher.CLAUDE_MD", fake_path):
            new_role = _make_role(name="new_role", title="新角色")
            inject_prompt_into_claudemd(new_role)
            content = fake_path.read_text()
            assert "原有内容" in content, "原有内容应保留"
            assert "更多内容" in content, "marker 之后的内容应保留"
            assert "旧内容" not in content, "旧内容应被替换"
            assert "新角色" in content, "新角色名应在注入块中"


def test_clear_injected_prompt():
    """清除注入块后 marker 和内容都不应存在。"""
    from launcher import clear_injected_prompt, SESSION_MARKER_START, SESSION_MARKER_END
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "CLAUDE.md"
        fake_path.write_text(f"原有\n{SESSION_MARKER_START}\n内容\n{SESSION_MARKER_END}\n")

        with patch("launcher.CLAUDE_MD", fake_path):
            clear_injected_prompt()
            content = fake_path.read_text()
            assert SESSION_MARKER_START not in content, "起始 marker 应被清除"
            assert SESSION_MARKER_END not in content, "结束 marker 应被清除"
            assert "原有" in content, "非 marker 内容应保留"


# --- 生命周期哨兵测试 ---


def test_write_lifecycle_sentinel():
    """ondemand 角色应创建哨兵文件。"""
    from launcher import write_lifecycle_sentinel
    import launcher
    original_sentinel_dir = launcher.Path("/tmp/session-launcher")

    role = _make_role(name="developer", lifecycle="ondemand")
    with patch("launcher.subprocess.run",
               return_value=_mock_subprocess_run("2026-06-30T12:00:00+0800")):
        write_lifecycle_sentinel(role)

    sentinel = Path("/tmp/session-launcher/developer.active")
    assert sentinel.exists(), "哨兵文件应存在"
    data = json.loads(sentinel.read_text())
    assert data["role"] == "developer"
    assert data["lifecycle"] == "ondemand"
    sentinel.unlink(missing_ok=True)


def test_cleanup_stale_sentinels():
    """进程已退出的哨兵应被清理。"""
    from launcher import cleanup_stale_sentinels
    sentinel_dir = Path("/tmp/session-launcher")
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    # 写一个引用不存在的 PID 的哨兵
    bad_sentinel = sentinel_dir / "ghost.active"
    bad_sentinel.write_text(json.dumps({
        "role": "ghost", "title": "幽灵", "lifecycle": "ondemand",
        "pid": 999999999, "started_at": "2026-06-30"
    }))

    cleaned = cleanup_stale_sentinels()
    assert "ghost" in cleaned, "幽灵角色应被清理"
    assert not bad_sentinel.exists(), "幽灵哨兵文件应被删除"


# --- 综合测试：main 流程 ---


def test_main_no_work():
    """无工作时 main 应清除注入并退出。"""
    from launcher import main, CLAUDE_MD
    with tempfile.TemporaryDirectory() as tmp:
        fake_md = Path(tmp) / "CLAUDE.md"
        fake_md.write_text("原有内容")
        # 注入一个旧的 marker 再验证清除
        from launcher import SESSION_MARKER_START, SESSION_MARKER_END
        fake_md.write_text(f"原有\n{SESSION_MARKER_START}\n旧角色内容\n{SESSION_MARKER_END}\n")

        import launcher
        with patch("launcher.CLAUDE_MD", fake_md):
            with patch("launcher.has_work", return_value=None):
                try:
                    main()
                except SystemExit as e:
                    assert e.code == 0, "无工作时 exit(0)"

        content = fake_md.read_text()
        assert SESSION_MARKER_START not in content, "无工作时应清除注入"


# --- __main__ 入口测试（shell 兼容性） ---


def test_main_shell_output_format():
    """__main__ 的输出格式应能被 shell eval 直接读取。"""
    from launcher import load_roles, inject_prompt_into_claudemd, CLAUDE_MD, SESSION_MARKER_START

    # 注入测试角色（放在正确的子目录下）
    test_roles_dir = Path("/tmp/test_session_roles")
    roles_subdir = test_roles_dir / "personas" / "session-roles"
    roles_subdir.mkdir(parents=True, exist_ok=True)
    (roles_subdir / "persona_00_test.json").write_text(json.dumps(
        _make_role(name="tester", title="测试员")
    ))

    import launcher
    original_root = launcher.SESSION_ROLES_ROOT

    try:
        launcher.SESSION_ROLES_ROOT = test_roles_dir

        # 模拟 has_work 返回测试角色
        test_role = _make_role(name="tester", title="测试员")

        import io
        with patch("launcher.has_work", return_value=test_role):
            with tempfile.TemporaryDirectory() as tmp:
                fake_md = Path(tmp) / "CLAUDE.md"
                with patch("launcher.CLAUDE_MD", fake_md):
                    captured = io.StringIO()
                    import sys
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
                    assert len(export_lines) >= 3, \
                        f"应有至少 3 行 export，实际: {export_lines}"
                    assert any("SESSION_ROLE=" in l for l in export_lines), \
                        "应有 SESSION_ROLE"
                    assert any("SESSION_LIFECYCLE=" in l for l in export_lines), \
                        "应有 SESSION_LIFECYCLE"
                    assert any("SESSION_DRIVE=" in l for l in export_lines), \
                        "应有 SESSION_DRIVE"
    finally:
        launcher.SESSION_ROLES_ROOT = original_root
        shutil.rmtree(test_roles_dir, ignore_errors=True)


# --- 自检模式（无 pytest 时运行） ---


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
        ("prompt 创建文件", test_inject_prompt_into_claudemd_creates_file),
        ("prompt 替换 marker", test_inject_prompt_into_claudemd_replaces_marker),
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
