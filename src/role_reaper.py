"""唤起「有定向派单但 pane 不在」的角色。

WHY：ondemand 角色的契约是「按需启动」，但派单只投给已运行的 pane，唯一会
ccs start 死 pane 的是工作流引擎，而系统类派单不 mint workflow → 死 pane 无人拉起。
2026-10-22 实测 devops pane 消失 10.5h，5 条 @devops 派单躺在 bus 无人接（bus #442309）。
"""
import json
import os
import re
import time

COOLDOWN_FILE = os.path.expanduser("~/.hermes/run/wake_cooldown.json")
COOLDOWN_S = 1800
# 只把 6h 内的定向派单当唤醒理由：更老的是「claim 了没投递」的遗留垃圾
# （实测 devops 43 条里最老 28h、ccs-monitor 16h），唤起来只会让角色面对一堆
# 早已过时的指令，产出的是复述不是处理。老账另计，不该由唤醒吞掉。
MAX_AGE_S = 6 * 3600
_TAG_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_-]*)")


def _targets(head: str) -> list:
    """首行的「收件人」= 头部前缀里的 @，正文里的 @ 只是提及。

    实测两种都是正文提及、都不该算派单：「[@pipeline/@engineer] 5 条 @devops
    派单丢失…」与「[@all] devops 0 行落盘…」。唤醒要启动一个 claude 进程，
    精度比投递更值钱 —— pipeline 那边 _is_for_me 用全文 @（宁多投），这里不能照抄。
    """
    if head.startswith("["):
        cut = head.find("]")
        head = head[:cut + 1] if cut > 0 else head
    else:
        head = head.split()[0] if head.split() else ""
    return _TAG_RE.findall(head)


def _pending(facts, role: str, now: float, min_age: float) -> list:
    """收件人是 role、年龄落在 [min_age, MAX_AGE_S] 的未消费事实。"""
    out = []
    for f in facts:
        age = now - (f.ts or now)
        if role in _targets((f.t or "").split("\n")[0]) and min_age <= age <= MAX_AGE_S:
            out.append(f)
    return out


def reap(dry_run: bool = False, min_age: float = 600,
         max_wakes: int = 2) -> list:
    """每轮最多唤起 max_wakes 个角色，同一角色 COOLDOWN_S 内只唤醒一次。"""
    import core
    from bus_protocol import Blackboard

    now = time.time()
    try:
        with open(COOLDOWN_FILE) as fh:
            cooldown = json.load(fh)
    except (OSError, ValueError):
        cooldown = {}
    bb = Blackboard()
    report, candidates = [], []
    for r in core.list_roles():
        role = r.get("name") or ""
        if not role or r.get("alive"):
            continue
        if now - cooldown.get(role, 0) < COOLDOWN_S:
            continue
        try:
            # limit=0 不能省：unconsumed 按 ts DESC 取最新，加 LIMIT 会被近期
            # 广播（100+ 条）挤掉真正等待唤醒的旧定向派单 —— 旧的才是证据。
            # 30 天窗口全量实测 ~190 条，扫 7 个死角色不贵。
            facts = bb.unconsumed(consumer=role, limit=0)
        except Exception as exc:
            report.append({"role": role, "action": "bus_error",
                           "error": str(exc)})
            continue
        waiting = _pending(facts, role, now, min_age)
        if waiting:
            candidates.append((role, waiting))
    # 积压最多的先唤醒：cap 只该挡「凑巧靠前」的，不该挡最堵的那个。
    candidates.sort(key=lambda c: -len(c[1]))
    for i, (role, waiting) in enumerate(candidates):
        if dry_run:
            report.append({"role": role, "action": "would_wake",
                           "pending": len(waiting)})
            continue
        if i >= max_wakes:
            report.append({"role": role, "action": "throttled",
                           "pending": len(waiting)})
            continue
        res = core.wake_ccs(role, by_role="reaper")
        ok = bool(res.get("success"))
        report.append({"role": role, "action": "woke" if ok else "wake_failed",
                       "pending": len(waiting), "detail": res})
        if not ok:
            continue
        cooldown[role] = now
        try:
            os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
            with open(COOLDOWN_FILE, "w") as fh:
                json.dump(cooldown, fh)
        except OSError:
            pass
        age_min = int((now - min(f.ts for f in waiting)) / 60)
        try:
            bb.write("monitor_audit",
                     f"reap: 唤起 {role}，{len(waiting)} 条定向派单滞留 "
                     f"{age_min}min（pane 不在）", src="reaper")
        except Exception:
            pass
    return report
