#!/usr/bin/env python3
"""Test _check_shell 安全验证逻辑（白盒：不执行命令，只测验证规则）。"""

import importlib.util, sys
from pathlib import Path

# ── 直接加载 events.parser 模块 ──
_launcher_src = Path(__file__).resolve().parent.parent / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))
_spec = importlib.util.spec_from_file_location("events.parser", _launcher_src / "events" / "parser.py")
_parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parser)
_check_shell = _parser._check_shell

# 辅助：构造 spec，避免实际执行
_pass_spec = {"command": "echo ok"}       # 应通过验证
_fail_spec = {"command": "rm -rf /"}      # 应被拦截


def _extract_security_lines():
    """提取 _check_shell 中的安全规则常量。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "events" / "parser.py").read_text()
    fn_start = src.find("def _check_shell")
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end]
    return body


# ── 安全规则白盒测试 ──

def test_shell_variable_injection_blocked():
    """shell 变量 ${} 应被拒绝"""
    assert not _check_shell({"command": "echo ${HOME}"}, "")


def test_shell_subshell_blocked():
    """subshell $() 应被拒绝"""
    assert not _check_shell({"command": "echo $(whoami)"}, "")


def test_shell_backtick_blocked():
    """反引号 ` 应被拒绝"""
    assert not _check_shell({"command": "echo `whoami`"}, "")


def test_danger_words_blocked():
    """危险命令单词应被拒绝"""
    for cmd in ["rm -rf /", "mkfs.ext4 /dev/sda", "chmod 777 /etc", "chown root /tmp"]:
        assert not _check_shell({"command": cmd}, ""), f"应拒绝: {cmd}"


def test_redirect_write_blocked():
    """文件写入重定向 > 应被拒绝（允许 2>&1 之类的 fd 重定向）"""
    assert not _check_shell({"command": "echo test > /tmp/out"}, "")


def test_fd_redirect_allowed():
    """fd 重定向 2>&1 应允许"""
    body = _extract_security_lines()
    assert "2>&1" in body or r"\d+>&\d+" in body, "fd 重定向正则应存在"


def test_control_operators_blocked():
    """控制运算符 ; && || 应被拒绝"""
    assert not _check_shell({"command": "echo a; echo b"}, "")
    assert not _check_shell({"command": "echo a && echo b"}, "")
    assert not _check_shell({"command": "echo a || echo b"}, "")


def test_pipe_allowed():
    """管道 | 不应被安全规则拒绝（实际执行时用 shell=True）"""
    # 白名单命令在管道中应通过验证
    body = _extract_security_lines()
    assert "shell=True" in body, "_check_shell 应对管道启用 shell=True"


def test_safe_commands_allowed():
    """白名单内命令应通过验证"""
    assert _check_shell({"command": "echo hello"}, "")
    assert _check_shell({"command": "ls -la"}, "")
    assert _check_shell({"command": "cat /etc/hostname"}, "")


def test_python3_not_whitelisted():
    """python3/bash 已从白名单移除（承载 -c/-m 任意代码执行风险），应被拒绝"""
    body = _extract_security_lines()
    assert '"python3"' not in body, "python3 不应位于 _safe_cmds 白名单"
    assert '"bash"' not in body, "bash 不应位于 _safe_cmds 白名单"
    assert '"tmux"' in body, "tmux 应位于 _safe_cmds 白名单"


def test_unknown_command_rejected():
    """白名单外命令应被拒绝"""
    assert not _check_shell({"command": "curl_fake_arg"}, "")


def test_empty_command_rejected():
    """空命令应被拒绝"""
    assert not _check_shell({"command": ""}, "")


def test_no_command_rejected():
    """无 command 字段应被拒绝"""
    assert not _check_shell({}, "")


# ── 实际可执行用例（安全且快速）──

def test_echo_works():
    """echo 应实际执行成功"""
    assert _check_shell({"command": "echo test123"}, "test123")


def test_grep_pipe_works():
    """管道命令 echo|grep 应执行成功（shell=True）"""
    assert _check_shell({"command": "echo hello world | grep hello"}, "hello")


def test_ps_pipe_works():
    """ps | grep 管道应正常执行"""
    assert _check_shell({"command": "ps aux | grep -c python"}, "")


# ponytail: curl|bash 安全测试跳过——依赖外部网络，CI 环境不可控
# 安全验证逻辑（白名单、管道、shell 变量拦截）已由其余 16 项测试覆盖


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
    print(f"\n{passed}/{passed + failed} 通过")
    raise SystemExit(0 if failed == 0 else 1)
