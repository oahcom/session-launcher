#!/usr/bin/env python3
"""wf:engine 判活回归 — 锁被别的进程持有时必须报 fail。

2026-10-22 03:1x 实测：手工 daemon 占着 ~/.hermes/run/workflow-daemon.lock，
systemd 单元显示 active、三套巡检全绿，16 个实例停在 running 没人推进。
本条锁住「比锁内容」之外的真探：只比文件内容会把死进程的残留 PID 当成"还活着"。

运行: python3 -m pytest tests/test_ecosystem_health_engine.py -q
"""
import fcntl
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import ecosystem_health as eh  # noqa: E402


def test_engine_check_fails_when_lock_held_by_other_pid(tmp_path, monkeypatch):
    run_dir = tmp_path / ".hermes" / "run"
    run_dir.mkdir(parents=True)
    lock = run_dir / "workflow-daemon.lock"
    lock.write_text("999999")  # 占锁进程的 pid：与 MainPID 不同

    holder = lock.open("a+")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 本进程模拟"手工 daemon"
        monkeypatch.setattr(eh, "HOME", tmp_path)

        def _fake_run(argv, **kwargs):
            out = "active\n" if "is-active" in argv else "1234\n"  # MainPID 是另一个
            return SimpleNamespace(stdout=out)

        monkeypatch.setattr(eh.subprocess, "run", _fake_run)
        item = eh._check_workflow_engine()
        assert not item.passed, "锁被别人持有时必须 fail"
        assert "999999" in item.detail, f"detail 应点名占锁 pid，实际: {item.detail}"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_engine_check_fails_when_lock_not_held_at_all(tmp_path, monkeypatch):
    """锁文件空/无持有者时不能判绿。

    daemon 取锁时先 open("w") 截断，再 flock——重启窗口内文件是空的；
    只比文件内容（lock_pid 为空就放行）会在这个窗口报绿，而链其实没人推进。
    """
    run_dir = tmp_path / ".hermes" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "workflow-daemon.lock").write_text("")  # 无持有者
    monkeypatch.setattr(eh, "HOME", tmp_path)

    def _fake_run(argv, **kwargs):
        return SimpleNamespace(stdout="active\n" if "is-active" in argv else "1234\n")

    monkeypatch.setattr(eh.subprocess, "run", _fake_run)
    item = eh._check_workflow_engine()
    assert not item.passed, "锁无人持有时必须 fail"
    assert "锁未被持有" in item.detail, f"detail 应说明锁未被持有，实际: {item.detail}"
