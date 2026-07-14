"""test_signal_parser.py — 信号解析器新旧格式兼容性测试。"""
import json
import os
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from unittest.mock import patch
from signal_parser import parse_signal, _check_bus, _check_memory, _check_disk


class TestParseSignal:
    """parse_signal 统一入口测试。"""

    def test_unknown_type_silent(self):
        """未知 type 不报错，返回 False。"""
        assert parse_signal({"type": "nonexistent", "spec": {}}) is False

    def test_empty_spec_silent(self):
        """空 spec 不报错。"""
        with patch("events.parser.subprocess.run") as mock_run:
            mock_run.return_value.stdout = '{"facts": []}'
            mock_run.return_value.returncode = 0
            assert parse_signal({"type": "bus", "spec": {}}) is False

    def test_missing_type_silent(self):
        """无 type 字段不报错（旧格式检测失败时）。"""
        result = parse_signal({"filter": "test"})
        assert result is False  # 无法判断类型，安全返回 False


class TestFormatCompatibility:
    """新旧格式兼容性（不触发真实检查，仅验证逻辑路径）。"""

    def test_new_format_dispatches_to_check_bus(self):
        """新格式 bus 派发到 _check_bus。"""
        with patch("events.parser.subprocess.run") as mock_run:
            mock_run.return_value.stdout = '{"facts": []}'
            mock_run.return_value.returncode = 0
            result = parse_signal({"type": "bus", "spec": {"category": "security"}})
            assert result is False

    def test_old_format_deprecation_warning(self):
        """旧格式应触发 DeprecationWarning。"""
        with pytest.warns(DeprecationWarning, match="旧格式"):
            parse_signal({"source": "bus_client.py read --cat security"})

    def test_old_format_still_works(self):
        """旧格式兼容层正常运行（不抛异常）。"""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with patch("events.parser.subprocess.run") as mock_run:
                mock_run.return_value.stdout = '{"facts": []}'
                mock_run.return_value.returncode = 0
                result = parse_signal({"source": "bus_client.py read --cat security"})
                assert result is False


class TestCheckBus:
    """_check_bus 单元测试。"""

    def test_empty_category(self):
        """空 category 不报错。"""
        with patch("events.parser.subprocess.run") as mock_run:
            mock_run.return_value.stdout = '{"facts": []}'
            mock_run.return_value.returncode = 0
            result = _check_bus({"category": ""}, "")
            assert result is False

    def test_with_category(self):
        """有 category 时正常调用。"""
        result = _check_bus({"category": "security"}, "")
        assert result is False  # 真实环境，安全降级

    def test_timeout_silent(self):
        """超时不报错。"""
        result = _check_bus({"category": "nonexistent_cat_xyz"}, "", timeout=1)
        assert result is False


class TestCheckMemory:
    """_check_memory 单元测试。"""

    def test_default_threshold(self):
        """默认阈值 500MB 正常执行。"""
        result = _check_memory({})
        # 不关心结果，只验证不报错
        assert isinstance(result, bool)

    def test_custom_threshold(self):
        """自定义阈值。"""
        result = _check_memory({"threshold_mb": 100})
        assert isinstance(result, bool)


class TestCheckDisk:
    """_check_disk 单元测试。"""

    def test_default_threshold(self):
        """默认阈值 90% 正常执行。"""
        result = _check_disk({})
        assert isinstance(result, bool)

    def test_custom_threshold(self):
        """自定义阈值。"""
        result = _check_disk({"threshold_pct": 50})
        assert isinstance(result, bool)
