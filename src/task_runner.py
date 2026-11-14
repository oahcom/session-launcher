"""ccs task 的执行体：专用实例上的 scout → engineer → reviewer → closer 四段转交。

完成判定只认本次 run_dir 下的 done 文件，内容为「专用实例名 + 产物 SHA256」两段：
同角色的其他实例也会收到广播请求，只写 done 无法区分是谁写的；只认实例名则
done 之后的产物修改不会被发现，信号与产物可能不一致。
"""
import fcntl
import hashlib
import os
import shutil
import time

from tmux_ops import MAX_INSTANCES, _is_alive, _tmux_kill, _tmux_output, make_tmux_name

OUTPUT_ROOT = "/home/administrator/ccs-output"
SCOUT_TIMEOUT = int(os.environ.get("CCS_TASK_SCOUT_TIMEOUT", "2400"))
ENGINEER_TIMEOUT = int(os.environ.get("CCS_TASK_ENGINEER_TIMEOUT", "2400"))
REVIEWER_TIMEOUT = int(os.environ.get("CCS_TASK_REVIEWER_TIMEOUT", "2700"))
CLOSER_TIMEOUT = int(os.environ.get("CCS_TASK_CLOSER_TIMEOUT", "2700"))
# 宽限期：角色常在 deadline 之后才把 done 写完（实测 scout 21:28:57 被判 timeout，
# 21:29:49 才写出合法 done）。到点立即判死会让下游拿到 timeout，而产物其实有效。
GRACE_INTERVAL = int(os.environ.get("CCS_TASK_GRACE", "180"))
BUSY_RETRIES = 6
BUSY_INTERVAL = 10
POLL_INTERVAL = 10
# 额度墙：角色 turn 被 9router 限额打断后会话静止，没人替它输入就会干等到 timeout。
CONTINUE_SIGNATURES = ("FreeUsageLimitError", "usage limit reached", "503")
CONTINUE_INJECTS = 3
CONTINUE_COOLDOWN = 120

# role -> (超时, 契约标题, 阶段专属要求)；字典顺序即执行顺序
STAGES = {
    "scout": (SCOUT_TIMEOUT, "调研结果", ""),
    "engineer": (ENGINEER_TIMEOUT, "实现", ""),
    "reviewer": (REVIEWER_TIMEOUT, "审查结果",
                 "审查阶段：只审查上述产出并给出结论与问题清单，不重写整个实现。"),
    "closer": (CLOSER_TIMEOUT, "最终交付",
               "收尾要求：把三个阶段的产出整合为一份可直接使用的最终交付文档，"
               "补齐缺漏，不引入未经前序产物的虚构内容。"),
}
ROLES = tuple(STAGES)


def _pick_instance() -> int:
    """1..MAX_INSTANCES-1 里第一个四种专用实例都不存活的编号；0 表示无空闲。"""
    for i in range(1, MAX_INSTANCES):
        if not any(_is_alive(make_tmux_name(role, i)) for role in ROLES):
            return i
    return 0


def _deliver(role: str, message: str, instance_id: int) -> tuple[bool, str]:
    """start + send。busy 最多重试 6 次，每次等 10 秒。"""
    from core import send, start

    if instance_id <= 0:   # 0 是常驻实例，本流程永不动它
        return False, f"拒绝向常驻实例投递 {role}#{instance_id}"
    # no_auto_send：新实例的 ccs_config 自动消息会在启动瞬间让它进入推理，
    # 6×10s 的重试窗口全被别人的任务吃掉（实测 scout#1/#2 连续 busy 至失败）。
    r = start(role, detach=True, instance_id=instance_id, no_auto_send=True)
    if r.get("success") is not True:
        return False, f"启动 {role}#{instance_id} 失败：{r.get('error') or r}"

    for attempt in range(BUSY_RETRIES + 1):
        r = send(role, message, source="cli", instance_id=instance_id)
        if r.get("success") is True:
            return True, ""
        if r.get("reason") != "busy":
            return False, f"投递 {role}#{instance_id} 失败：{r.get('error') or r}"
        if attempt < BUSY_RETRIES:
            time.sleep(BUSY_INTERVAL)
    return False, f"投递 {role}#{instance_id} 失败：busy 重试 {BUSY_RETRIES} 次仍未投递"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _done_state(done: str, artifact: str, expected_owner: str):
    """done 的三态判定：'done' / None（还没写）/ 不合规的具体指纹（供两次比对）。

    返回指纹而非直接 failed，是为了区分「正在写」与「写错了」：指纹相同说明内容
    已稳定且仍不合规，才可判死；指纹变化说明还有人在写，继续等。
    """
    if not os.path.exists(done):
        return None
    with open(done, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    parts = raw.split()
    if len(parts) != 2:
        return ("words", raw)
    owner, claimed = parts
    if owner != expected_owner:
        return ("owner", owner)
    if not os.path.exists(artifact) or os.path.getsize(artifact) == 0:
        return ("artifact",)
    actual = _sha256_file(artifact)
    return "done" if actual == claimed else ("sha", claimed, actual)


def _wait_stage(
    run_dir: str,
    role: str,
    expected_owner: str,
    instance_id: int,
    timeout_sec: int,
) -> str:
    """返回 done / timeout / failed。done 必须恰好两段：expected_owner + <role>.md 的 SHA256。"""
    done = os.path.join(run_dir, f"{role}.done")
    artifact = os.path.join(run_dir, f"{role}.md")
    deadline = time.monotonic() + timeout_sec
    # 到点不立刻判死：done/failed 的判定原样保留，只把「何时放弃」推到 deadline+宽限。
    grace_deadline = deadline + GRACE_INTERVAL
    announced_grace = False
    injects = 0
    last_inject = 0.0
    last_bad = None
    while True:
        state = _done_state(done, artifact, expected_owner)
        if state == "done":
            return "done"
        if state is None:
            last_bad = None
        elif state == last_bad:
            # 同一份不合规内容隔一拍仍在，才算真坏：#478543 实测 done 由 shell
            # 重定向壳出，首次读到空/半截后 2s 内就能改写成合规两段。
            return "failed"
        else:
            last_bad = state
        # 额度墙自愈：命中签名就补一句「继续」，次数与间隔都封顶；判定与 deadline 不受影响。
        if injects < CONTINUE_INJECTS and time.monotonic() - last_inject >= CONTINUE_COOLDOWN:
            try:
                pane = _tmux_output(make_tmux_name(role, instance_id), tail=15) or ""
            except Exception:
                pane = ""
            if pane and any(sig in pane for sig in CONTINUE_SIGNATURES):
                from core import send
                send(role, "继续", source="cli", instance_id=instance_id)
                injects += 1
                last_inject = time.monotonic()
                print(f"{role} 疑似额度墙，已注入继续（第 {injects} 次）")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_INTERVAL, remaining))
            continue
        if time.monotonic() >= grace_deadline:
            pane = _tmux_output(make_tmux_name(role, instance_id), tail=50)
            with open(os.path.join(run_dir, f"{role}.pane.txt"), "w",
                      encoding="utf-8") as f:
                f.write(pane + "\n")
            return "timeout"
        if not announced_grace:
            print(f"{role} 已过 {timeout_sec}s 上限，进入宽限期"
                  f"（最长再等 {GRACE_INTERVAL}s）...")
            announced_grace = True
        time.sleep(min(POLL_INTERVAL, max(grace_deadline - time.monotonic(), 0)))


def _contract(role: str, run_dir: str, owner: str, subject: str) -> list[str]:
    """产出契约：产物 <role>.md，done 为「实例名 + 产物 SHA256」两段。"""
    md = os.path.join(run_dir, f"{role}.md")
    return [
        f"1. {subject}写入 {md}，写完即定稿，之后不得再修改",
        f"2. 计算 SHA256：sha256sum {md}",
        f"3. 完成后创建 {os.path.join(run_dir, role + '.done')}，内容严格为两段、以单个空格分隔：",
        f"   <实例名> <{md} 的64位小写 sha256>",
        f"   即：{owner} $(sha256sum {md} | cut -d' ' -f1)",
        f"4. 未完成不得创建 {role}.done；done 之后修改 {md} 会导致校验失败",
    ]


def _message(description: str, run_dir: str, role: str, owner: str,
             prior: list[tuple[str, str]]) -> str:
    """本阶段投递文本：原始任务 + 前序各阶段状态/产物/超时 pane + 本阶段契约。"""
    lines = [f"任务：{description}"]
    for prev, status in prior:
        lines += ["", f"{prev} 状态: {status}",
                  f"{prev} 产出: {os.path.join(run_dir, prev + '.md')}"]
        if status == "timeout":
            lines.append(f"{prev} pane 尾部: {os.path.join(run_dir, prev + '.pane.txt')}")
    brief = STAGES[role][2]
    if brief:
        lines += ["", brief]
    lines += ["", "产出契约（必须遵守）："]
    return "\n".join(lines + _contract(role, run_dir, owner, STAGES[role][1]))


def _publish_final(run_dir: str) -> None:
    """closer 的契约产物是 closer.md；交付名固定为 final.md，故复制一份。
    不用 rename：closer.done 里的 SHA256 指向 closer.md，移走会让它失效。"""
    shutil.copyfile(os.path.join(run_dir, "closer.md"),
                    os.path.join(run_dir, "final.md"))


def _cleanup(names: list[str]) -> None:
    """只清本次 ccs-<role>-<id> 四个会话；清理失败不影响退出码。"""
    for name in names:
        try:
            _tmux_kill(name)
        except Exception as e:
            print(f"清理会话 {name} 失败: {e}")
        else:
            print(f"清理会话: {name}")


def _run_stages(description: str, run_dir: str, instance_id: int,
                names: dict[str, str]) -> int:
    prior: list[tuple[str, str]] = []
    for role in ROLES:
        msg = _message(description, run_dir, role, names[role], prior)
        ok, err = _deliver(role, msg, instance_id)
        if not ok:
            print(f"{err}\nrun_dir: {run_dir}")
            return 1
        timeout_sec = STAGES[role][0]
        print(f"等待 {role}（每 {POLL_INTERVAL}s 检查 {role}.done，最长 {timeout_sec}s）...")
        status = _wait_stage(run_dir, role, names[role], instance_id, timeout_sec)
        print(f"{role} 状态: {status}")
        if status == "timeout":
            print(f"注意：{role} 判定 timeout，但其产出可能在稍后补达，可人工检查 {run_dir}")
        if status == "failed":
            print(f"{role} 完成信号或产物归属无效\nrun_dir: {run_dir}")
            return 1
        prior.append((role, status))
    if status == "timeout":
        print(f"closer 超时\nrun_dir: {run_dir}")
        return 1
    _publish_final(run_dir)
    print()
    print("任务完成")
    print(f"最终交付: {os.path.join(run_dir, 'final.md')}")
    print(f"run_dir: {run_dir}")
    return 0


def run_task(description: str) -> int:
    # 整个四段流程持锁：_pick_instance 只查「此刻有无存活专用实例」，查完到 start
    # 之间任何第二个 ccs task 都会拿到同一编号、写进同一 run_dir（#477341：4 个实例
    # 各刷一次 scout.done，下游对同一份产出验三遍）。锁必须跨越 pick 与全部阶段，
    # 只锁 pick 的话第二个进程会在阶段之间插进来，照样串。
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    with open(os.path.join(OUTPUT_ROOT, ".ccs-task.lock"), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个 ccs task 正在执行，本次不启动")
            return 1
        return _run_task_locked(description)


def _run_task_locked(description: str) -> int:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_dir = os.path.join(OUTPUT_ROOT, run_id)
    os.makedirs(run_dir, exist_ok=True)
    print(f"run_id: {run_id}")
    print(f"run_dir: {run_dir}")

    instance_id = _pick_instance()
    if not instance_id:
        print("无空闲任务实例")
        return 1
    names = {role: make_tmux_name(role, instance_id) for role in ROLES}
    print(f"专用实例: {' / '.join(names.values())}")
    try:
        return _run_stages(description, run_dir, instance_id, names)
    finally:
        _cleanup(list(names.values()))
