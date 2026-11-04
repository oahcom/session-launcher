"""r77-routing-daemon-whitelist 分支上的规格测试。

补两个缺口（本轮实测均无覆盖：`grep -rln '_wait_stage' tests/` 零命中）：

1. done 契约：裸 `done`（无 owner / 无 sha）不得被判通过 —— 这正是 #477341 的
   事故形态：scout.done 5 字节内容 `done`，产物随后被改写 16 分钟。
2. 实例互斥：`_pick_instance()` 在两个并发调用之间没有任何锁，
   两次调用都会返回同一个编号（#477341 观测到 4 个实例各刷一次同一 run_dir）。
"""
import hashlib
import os
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import task_runner  # noqa: E402


def _stage(tmp_path, role, md_text, done_text):
    (tmp_path / f"{role}.md").write_text(md_text, encoding="utf-8")
    if done_text is not None:
        (tmp_path / f"{role}.done").write_text(done_text, encoding="utf-8")
    return task_runner._wait_stage(str(tmp_path), role, f"ccs-{role}-3", 3, 1)


def test_bare_done_is_failed(tmp_path):
    """裸 done 无法自证「产物是哪一版」，必须判 failed 而不是 done。"""
    assert _stage(tmp_path, "scout", "内容 v2（已被改写）", "done") == "failed"


def test_done_with_stale_sha_is_failed(tmp_path):
    """done 里写的是上一版产物的 sha → failed（产物被改写后不许蒙混过闸）。"""
    old = hashlib.sha256(b"v1").hexdigest()
    assert _stage(tmp_path, "scout", "v2 已被改写", f"ccs-scout-3 {old}") == "failed"


def test_done_with_matching_sha_is_done(tmp_path):
    """唯一通过的形态：owner 对 + sha 对。"""
    body = "定稿内容"
    sha = hashlib.sha256(body.encode()).hexdigest()
    assert _stage(tmp_path, "scout", body, f"ccs-scout-3 {sha}") == "done"


def test_done_from_another_instance_is_failed(tmp_path):
    """别的实例写的 done 不算本实例的完成（同角色多实例广播同一请求）。"""
    body = "定稿内容"
    sha = hashlib.sha256(body.encode()).hexdigest()
    (tmp_path / "scout.md").write_text(body, encoding="utf-8")
    (tmp_path / "scout.done").write_text(f"ccs-scout-7 {sha}", encoding="utf-8")
    assert task_runner._wait_stage(str(tmp_path), "scout", "ccs-scout-3", 3, 1) == "failed"


def test_pick_instance_can_return_same_id_twice(monkeypatch):
    """确定性复现「两次调用拿到同一编号」—— 无锁是缺陷本身，不是偶发。

    模拟 _is_alive 恒 False（所有专用实例都空闲）：真实环境里两个 ccs task
    并发时各自查完、各自返回 1，随后互相覆盖同一个 run_dir。
    """
    monkeypatch.setattr(task_runner, "_is_alive", lambda name: False)
    assert task_runner._pick_instance() == 1
    assert task_runner._pick_instance() == 1     # 第二次仍是 1，锁只会在 run_task 里加



def test_second_run_task_refuses_while_first_holds_lock(tmp_path, monkeypatch):
    """同一时刻只允许一个 ccs task 进四段流程（#477341：4 实例串进同一 run_dir）。"""
    import fcntl

    monkeypatch.setattr(task_runner, "OUTPUT_ROOT", str(tmp_path))
    called = []
    monkeypatch.setattr(task_runner, "_run_task_locked", lambda d: called.append(d) or 0)

    lock_path = tmp_path / ".ccs-task.lock"
    with open(lock_path, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert task_runner.run_task("并发进来第二个") == 1
    assert called == [], "持锁期间不得进入 _run_task_locked"

    assert task_runner.run_task("锁释放后") == 0
    assert called == ["锁释放后"]
