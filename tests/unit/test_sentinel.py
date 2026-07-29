#!/usr/bin/env python3
"""单元测试: ops/sentinel.py — 9 个冒泡路径覆盖

路径索引:
  P1  write_sentinel      文件写入成功
  P2  delete_sentinel     文件存在 → True
  P3  delete_sentinel     文件不存在 → False
  P4  read_sentinel       文件存在 → from_dict 成功
  P5  read_sentinel       文件损坏 → tmux 回退成功
  P6  read_sentinel       无文件 + 无 tmux → None
  P7  get_cross_session_memory  文件不存在 → {}
  P8  get_cross_session_memory  文件有效 → 返回 dict
  P9  list_sentinels      僵尸哨兵被过滤
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest

from ops.sentinel import (
    CcsSentinel,
    write_sentinel,
    delete_sentinel,
    read_sentinel,
    list_sentinels,
    get_cross_session_memory,
    get_all_cross_session_memories,
    record_cross_session_action,
    _MEMORY_DIR,
)


@pytest.fixture(autouse=True)
def isolate_sentinel_dirs(tmp_path):
    """隔离哨兵/健康/跨 session 内存目录，避免污染真实环境。"""
    import ops.sentinel as _sent

    sentinel_dir = tmp_path / "sentinels"
    sentinel_dir.mkdir()
    health_dir = tmp_path / "health"
    health_dir.mkdir()
    memory_dir = tmp_path / "cross-session"
    memory_dir.mkdir()

    _sent.SENTINEL_DIR = sentinel_dir
    _sent._HEALTH_DIR = health_dir
    _sent._MEMORY_DIR = memory_dir

    yield {"sentinel_dir": sentinel_dir, "health_dir": health_dir, "memory_dir": memory_dir}

    _sent.SENTINEL_DIR = Path.home() / ".hermes" / "run" / "ccs-sentinels"
    _sent._HEALTH_DIR = Path.home() / ".hermes" / "run" / "ccs-health"
    _sent._MEMORY_DIR = Path.home() / ".hermes" / "run" / "ccs-cross-session-memory"


def _make_sentinel(role="engineer", instance_id=0):
    """构造 CcsSentinel 测试实例。"""
    return CcsSentinel(
        role=role,
        title=f"Test {role}",
        instance_id=instance_id,
        tmux_session=f"ccs-{role}" if instance_id == 0 else f"ccs-{role}-{instance_id}",
        pid=12345,
        started_at=time.time(),
        lifecycle="infinite",
        drive="loop",
        partners=[],
        bus_track="",
        bus_timeout=300,
        session_id="",
        engine="ccs",
    )


class TestWriteSentinel:
    """P1: write_sentinel 文件写入成功"""

    def test_write_creates_file_with_correct_content(self):
        s = _make_sentinel()
        path = write_sentinel(s)

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["role"] == "engineer"
        assert data["instance_id"] == 0
        assert data["lifecycle"] == "infinite"
        assert path.name == "engineer.json"

    def test_write_instance_id_nonzero_uses_key_format(self):
        s = _make_sentinel(instance_id=2)
        path = write_sentinel(s)

        assert path.name == "engineer-2.json"
        data = json.loads(path.read_text())
        assert data["instance_id"] == 2

    def test_write_overwrites_existing_file(self):
        import ops.sentinel as _sent
        s = _make_sentinel()
        write_sentinel(s)
        s.title = "Updated Title"
        write_sentinel(s)

        actual = json.loads((_sent.SENTINEL_DIR / f"{s.sentinel_key}.json").read_text())
        assert actual["title"] == "Updated Title"


class TestDeleteSentinel:
    """P2/P3: delete_sentinel 文件存在/不存在路径"""

    def test_delete_existing_file_returns_true(self):
        import ops.sentinel as _sent
        s = _make_sentinel()
        write_sentinel(s)

        result = delete_sentinel("engineer")
        assert result is True
        assert not (_sent.SENTINEL_DIR / "engineer.json").exists()

    def test_delete_nonexistent_file_returns_false(self):
        result = delete_sentinel("nonexistent")
        assert result is False

    def test_delete_with_instance_id(self):
        s = _make_sentinel(instance_id=3)
        write_sentinel(s)

        result = delete_sentinel("engineer", instance_id=3)
        assert result is True

    def test_delete_removes_health_file_too(self):
        import ops.sentinel as _sent
        s = _make_sentinel()
        write_sentinel(s)
        health_path = _sent._HEALTH_DIR / "engineer.json"
        health_path.write_text('{"watchdog_ok": true}')

        delete_sentinel("engineer")
        assert not health_path.exists()

    def test_delete_strips_json_suffix(self):
        """防御：调用方传了 .json 后缀时自动去除。"""
        s = _make_sentinel()
        write_sentinel(s)

        result = delete_sentinel("engineer.json")
        assert result is True


class TestReadSentinel:
    """P4/P5/P6: read_sentinel 三条路径"""

    def test_read_from_file_success(self):
        """P4: 文件存在，JSON 合法 → from_dict 成功。"""
        s = _make_sentinel()
        write_sentinel(s)

        result = read_sentinel("engineer")
        assert result is not None
        assert result.role == "engineer"
        assert result.title == "Test engineer"
        assert result.pid == 12345

    def test_read_corrupt_json_falls_back_to_tmux(self):
        """P5: 文件损坏 → tmux has-session 成功 → 返回构造 sentinel。"""
        import ops.sentinel as _sent
        _sent.SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
        (_sent.SENTINEL_DIR / "engineer.json").write_text("{corrupt!!")

        mock_result = MagicMock()
        mock_result.returncode = 0  # tmux has-session success

        with patch("ops.sentinel.subprocess.run", return_value=mock_result):
            with patch("ops.sentinel._get_pid", return_value=9999):
                with patch("ops.sentinel._get_started_at", return_value=1000.0):
                    with patch("ops.sentinel._get_role_json", return_value={"title": "Engineer", "lifecycle": "loop"}):
                        result = read_sentinel("engineer")

        assert result is not None
        assert result.role == "engineer"
        assert result.engine == "ccs"
        assert result.lifecycle == "loop"

    def test_read_no_file_no_tmux_returns_none(self):
        """P6: 无文件 + 无 tmux session → None。"""
        mock_result = MagicMock()
        mock_result.returncode = 1  # tmux has-session fails

        with patch("ops.sentinel.subprocess.run", return_value=mock_result):
            result = read_sentinel("nonexistent")

        assert result is None

    def test_read_with_instance_id_parses_correctly(self):
        """带 instance_id 的读取。"""
        s = _make_sentinel(instance_id=2)
        write_sentinel(s)

        result = read_sentinel("engineer", instance_id=2)
        assert result is not None
        assert result.instance_id == 2

    def test_read_key_with_json_suffix_strips_it(self):
        """调用方传 "engineer.json" 时自动去掉后缀。"""
        s = _make_sentinel()
        write_sentinel(s)

        result = read_sentinel("engineer.json")
        assert result is not None
        assert result.role == "engineer"

    def test_read_cdX_fallback_uses_codex_engine(self):
        """cdx 前缀 tmux session → engine="codex"。"""
        import ops.sentinel as _sent
        _sent.SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "has-session" in cmd:
                m.returncode = 0 if "cdx-" in cmd[-1] else 1
            else:
                m.returncode = 1
            m.stdout = ""
            return m

        with patch("ops.sentinel.subprocess.run", side_effect=fake_run):
            with patch("ops.sentinel._get_pid", return_value=8888):
                with patch("ops.sentinel._get_started_at", return_value=2000.0):
                    with patch("ops.sentinel._get_role_json", return_value=None):
                        result = read_sentinel("engineer")

        assert result is not None
        assert result.engine == "codex"


class TestGetCrossSessionMemory:
    """P7/P8: 跨 session 内存路径"""

    def test_get_memory_nonexistent_returns_empty(self):
        """P7: 文件不存在 → {}。"""
        result = get_cross_session_memory("nonexistent_role")
        assert result == {}

    def test_get_memory_valid_json_returns_data(self):
        """P8: 文件存在 + 合法 JSON → 返回 dict。"""
        record_cross_session_action("engineer", "started session", summary="test")
        result = get_cross_session_memory("engineer")
        assert result["last_action"] == "started session"
        assert "last_ts" in result

    def test_get_memory_corrupt_json_returns_empty(self):
        """文件损坏 → {}。"""
        import ops.sentinel as _sent
        _sent._MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (_sent._MEMORY_DIR / "bad.json").write_text("NOT JSON {{{")

        result = get_cross_session_memory("bad")
        assert result == {}

    def test_record_and_get_roundtrip(self):
        """record → get 完整往返。"""
        record_cross_session_action("qa", "ran tests", summary="all passed")
        result = get_cross_session_memory("qa")
        assert result["last_action"] == "ran tests"
        assert result["summary"] == "all passed"

    def test_get_all_memories(self):
        """get_all_cross_session_memories 汇总。"""
        record_cross_session_action("engineer", "deployed")
        record_cross_session_action("qa", "tested")
        result = get_all_cross_session_memories()
        assert "engineer" in result
        assert "qa" in result


class TestListSentinels:
    """P9: list_sentinels 僵尸过滤 + 合并"""

    def test_zombie_sentinel_filtered(self):
        """pid=None + 空 tmux_session → 被过滤。"""
        zombie = CcsSentinel(role="zombie", tmux_session="", pid=None)
        write_sentinel(zombie)

        with patch("ops.sentinel._list_tmux_sessions", return_value=set()):
            result = list_sentinels()

        roles = [s.role for s in result]
        assert "zombie" not in roles

    def test_valid_sentinel_included(self):
        """有效哨兵保留。"""
        s = _make_sentinel()
        write_sentinel(s)

        with patch("ops.sentinel._list_tmux_sessions", return_value=set()):
            result = list_sentinels()

        roles = [s.role for s in result]
        assert "engineer" in roles

    def test_tmux_supplements_file_data(self):
        """tmux session 有 PID 但无文件 → 合并到结果。"""
        import ops.sentinel as _sent

        def fake_parse(name):
            return ("qa", 0)

        mock_result = MagicMock()
        mock_result.stdout = "ccs-qa\n"

        with patch("ops.sentinel._list_tmux_sessions", return_value={"ccs-qa"}):
            with patch("ops.sentinel._parse_tmux_name", side_effect=fake_parse):
                with patch("ops.sentinel._get_pid", return_value=54321):
                    with patch("ops.sentinel._get_started_at", return_value=3000.0):
                        with patch("ops.sentinel._get_role_json", return_value={"title": "QA"}):
                            with patch("ops.sentinel.subprocess.run", return_value=mock_result):
                                result = list_sentinels()

        roles = [s.role for s in result]
        assert "qa" in roles

    def test_empty_directory_returns_empty_list(self):
        """无文件 + 无 tmux → []。"""
        with patch("ops.sentinel._list_tmux_sessions", return_value=set()):
            result = list_sentinels()
        assert result == []


class TestUpdateHealth:
    """update_health 更新健康文件"""

    def test_update_creates_health_file(self):
        import ops.sentinel as _sent
        _sent.update_health("engineer", watchdog_ok=False, restart_count=3)
        health_path = _sent._HEALTH_DIR / "engineer.json"
        assert health_path.exists()
        data = json.loads(health_path.read_text())
        assert data["watchdog_ok"] is False
        assert data["restart_count"] == 3

    def test_update_merges_existing(self):
        import ops.sentinel as _sent
        _sent.update_health("engineer", watchdog_ok=True, restart_count=1)
        _sent.update_health("engineer", restart_count=2)
        data = json.loads((_sent._HEALTH_DIR / "engineer.json").read_text())
        assert data["restart_count"] == 2
        assert data["watchdog_ok"] is True

    def test_update_with_instance_id(self):
        import ops.sentinel as _sent
        _sent.update_health("engineer", instance_id=5, watchdog_ok=False)
        assert (_sent._HEALTH_DIR / "engineer-5.json").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
