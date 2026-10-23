"""唤起「有定向派单但 pane 不在」的 ondemand 角色。

WHY：派单只投已运行的 pane，系统类派单不 mint workflow，而 ccs start 死 pane 的唯一
调用者是工作流引擎 → ondemand 角色 pane 一死就再也没有派单可投。2026-10-22 实测 devops
pane 消失 10.5h，43 条 @devops 未消费，其中 17 条是引擎写的 task_spec 派单。
"""
import json
import os
import re
import time

COOLDOWN_FILE = os.path.expanduser("~/.hermes/run/wake_cooldown.json")
COOLDOWN_S = 1800
# 10 分钟地板：派单后立刻启动会和 pipeline 自己的投递撞车（同一条消息投两遍）。
MIN_AGE_S = 600
# 6h 天花板：更老的是「claim 了没投递」的遗留账（实测 devops 43 条最老 28h），
# 唤醒只会让角色对着过时指令产出复述。老账另计，不该由唤醒吞掉。
MAX_AGE_S = 6 * 3600
_TAG_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_-]*)")


def _pending(facts, role: str, now: float) -> list:
    """首行出现 @role、年龄落在 [MIN_AGE_S, MAX_AGE_S] 的未消费事实。

    只看首行，**@ 在行的哪个位置都算**。这是实测唯一可行的口径：机器派单是
    「needs_implementation @pg 工作流任务: wf_x」、协调者派单是「[coordinator] 派单
    @engineer: …」、链式派单是「[@a→@b] …」，收件人位置各不相同，而 bus 的 tags 列
    24h 内 751/765 为空 —— 没有结构化收件人可读。代价是「[@all] devops 0 行落盘」这类
    提及也会唤醒一次；过唤醒由年龄窗口 + 冷却 + 单轮 cap 兜住，漏唤醒没有兜底。
    口径与 pipeline 的 _is_for_me（全文 @）一致，不制造第二套「定向」定义。
    """
    out = []
    for f in facts:
        age = now - (f.ts or now)
        head = (f.t or "").split("\n")[0]
        # 排掉 notice：那是角色自己的 [context_warn]/[eval_checker] 自动告警，24h 内
        # 占定向积压 26/40 —— 启动一个 pane 去读自己的告警纯属空转。
        # 也排掉自写（f.src == role）：自己 @ 自己的消息，bus_client unread 同样不显示。
        if (f.cat != "notice" and (f.src or "") != role
                and MIN_AGE_S <= age <= MAX_AGE_S
                and role in _TAG_RE.findall(head)):
            out.append(f)
    return out


def reap(dry_run: bool = False, max_wakes: int = 2) -> list:
    """单轮最多启动 max_wakes 个 pane；同角色 COOLDOWN_S 内不再试。"""
    import core
    from bus_protocol import Blackboard

    now = time.time()
    try:
        with open(COOLDOWN_FILE) as fh:
            cooldown = json.load(fh)
    except (OSError, ValueError):
        cooldown = {}  # 文件坏了最多多启动一轮，单轮 cap 仍然生效
    bb = Blackboard()
    report, candidates = [], []
    for r in core.list_roles():
        role = r.get("name") or ""
        # 只碰 ondemand：infinite 角色被人工停掉是运维事件，顺手拉起会掩盖它。
        # instances 非空说明它正以 ccs-<role>-N 在跑，再起一个会有两份 bus consumer。
        if (not role or r.get("alive") or r.get("instances")
                or r.get("lifecycle") != "ondemand"):
            continue
        if now - cooldown.get(role, 0) < COOLDOWN_S:
            continue
        try:
            # limit=0 不能省：unconsumed 按 ts DESC 取最新，加 LIMIT 会被近期广播
            # （实测 >100 条）挤掉真正等待唤醒的旧派单 —— 旧的才是证据。
            facts = bb.unconsumed(consumer=role, limit=0)
        except Exception as exc:
            report.append({"role": role, "action": "bus_error",
                           "error": str(exc)})
            continue
        waiting = _pending(facts, role, now)
        if waiting:
            candidates.append((role, waiting))
    # 积压最多的先启动：cap 只该挡「凑巧靠前」的，不该挡最堵的那个。
    candidates.sort(key=lambda c: -len(c[1]))
    for i, (role, waiting) in enumerate(candidates):
        if i >= max_wakes:
            report.append({"role": role, "action": "throttled",
                           "pending": len(waiting)})
            continue
        if dry_run:
            report.append({"role": role, "action": "would_start",
                           "pending": len(waiting)})
            continue
        res = core.wake_ccs(role, by_role="reaper")
        # success 只说明 tmux session 建成了：core.start 的就绪轮询超时同样返回成功，
        # claude 卡登录时 pane 也 alive → 别把 started 读成「角色可用了」。
        ok = bool(res.get("success"))
        report.append({"role": role, "action": "started" if ok else "start_failed",
                       "pending": len(waiting), "detail": res})
        # 失败也记冷却：否则起不来的角色每 15 分钟被重试一次，journal 刷屏、
        # bus 里还没有痕迹，看不出它一直在失败。
        cooldown[role] = now
        try:
            tmp = COOLDOWN_FILE + ".tmp"
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(cooldown, fh)
            os.replace(tmp, COOLDOWN_FILE)  # 原地写会让人读到半截 json → 冷却全清零
        except OSError:
            pass  # 冷却丢了最多多启动一次，不值得中断本轮
        try:
            bb.write("monitor_audit",
                     f"reap: {'已启动 pane' if ok else '启动失败'} {role}，"
                     f"{len(waiting)} 条定向派单滞留 "
                     f"{int((now - min(f.ts for f in waiting)) / 60)}min"
                     + ("" if ok else f"；{res.get('error', '')}"), src="reaper")
        except Exception:
            pass  # 审计写失败不影响已经发生的启动
    return report
