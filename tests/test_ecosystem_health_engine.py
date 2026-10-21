#!/usr/bin/env python3
"""wf:engine 判活回归 — 三条路径都要钉住。

2026-10-22 03:1x 实测：手工 daemon 占着 ~/.hermes/run/workflow-daemon.lock，
systemd 单元显示 active、三套巡检全绿，16 个实例停在 running 没人推进（#428132 同型）。
判据必须问「MainPID 有没有真持有那把锁」，而不是比锁文件内容——内容是上次成功的写入，
进程死后不清，重启窗口里还会被 open("w") 截断成空。

运行: python3 -m pytest tests/test_ecosystem_health_engine.py -q
"""
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import ecosystem_health as eh  # noqa: E402


def _fake_systemctl(main_pid: str):
    def _run(argv, **kwargs):
        out = "active\n" if "is-active" in argv else main_pid + "\n"
        return SimpleNamespace(stdout=out)
    return _run


def _fake_home(tmp_path, active: int = 0, last_log_ts: float = None):
    """建一个最小生产库：活跃实例数 + 最后一条 workflow_logs。"""
    (tmp_path / ".hermes" / "run").mkdir(parents=True)
    state = tmp_path / ".hermes" / "state"
    state.mkdir(parents=True)
    conn = sqlite3.connect(state / "workflows.db")
    conn.execute("CREATE TABLE workflow_instances (instance_id TEXT, status TEXT)")
    conn.execute("CREATE TABLE workflow_logs (ts REAL)")
    conn.executemany("INSERT INTO workflow_instances VALUES (?,?)",
                     [(f"wf_{i}", "running") for i in range(active)])
    if last_log_ts:
        conn.execute("INSERT INTO workflow_logs VALUES (?)", (last_log_ts,))
    conn.commit()
    conn.close()


def test_fails_when_lock_held_by_someone_else(tmp_path, monkeypatch):
    """锁文件点名了另一个 pid，而 MainPID 没持有锁 → fail 且点名占锁者。"""
    _fake_home(tmp_path)
    (tmp_path / ".hermes" / "run" / "workflow-daemon.lock").write_text("999999")
    monkeypatch.setattr(eh, "HOME", tmp_path)
    monkeypatch.setattr(eh.subprocess, "run", _fake_systemctl("1234"))
    item = eh._check_workflow_engine()
    assert not item.passed, "MainPID 未持锁时必须 fail"
    assert "999999" in item.detail, f"detail 应点名占锁 pid，实际: {item.detail}"


def test_fails_when_lock_truncated_and_unheld(tmp_path, monkeypatch):
    """锁文件被截断成空（daemon 正在取锁的窗口）时不能判绿。"""
    _fake_home(tmp_path)
    (tmp_path / ".hermes" / "run" / "workflow-daemon.lock").write_text("")
    monkeypatch.setattr(eh, "HOME", tmp_path)
    monkeypatch.setattr(eh.subprocess, "run", _fake_systemctl("1234"))
    item = eh._check_workflow_engine()
    assert not item.passed, "锁无人持有时必须 fail"
    assert "未持有锁" in item.detail, f"detail 应说明未持有锁，实际: {item.detail}"


def test_passes_when_main_pid_holds_lock_and_chain_moves(tmp_path, monkeypatch):
    """MainPID 真持有锁 + 活跃实例 + 最近有步骤日志 → pass。"""
    _fake_home(tmp_path, active=3, last_log_ts=time.time() - 60)
    lock = tmp_path / ".hermes" / "run" / "workflow-daemon.lock"
    me = str(os.getpid())
    with lock.open("w") as holder:  # 本进程持有 → /proc/self/fd 里能看到它
        holder.write(me)
        holder.flush()
        monkeypatch.setattr(eh, "HOME", tmp_path)
        monkeypatch.setattr(eh.subprocess, "run", _fake_systemctl(me))
        item = eh._check_workflow_engine()
    assert item.passed, f"应判绿，实际: {item.detail}"
    assert "活跃实例=3" in item.detail, item.detail
