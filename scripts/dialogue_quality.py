#!/usr/bin/env python3
"""对话质量口径：按「回合」统计角色转录（唯一实现，别在别处重算）。

WHY：2026-10-21 用「只带文本、不调工具的 assistant 块」当低质量文本（1,900 条），据此改了
prompts/base.md。2026-10-22 复测：81.5% 是**同回合内紧接着调工具的过渡句**，回合末文本过半
是网关错误 → 分母错了。口径：
- 回合一 = 一条字符串正文的 user 消息 → 下一条之前；tool_result 是 list，不开局。
- 会话续接 / task-notification 等系统语料单列 system，不进零工具分母
  （实测 24h 它们贡献了 129/252 个"零工具回合"，混进去懒惰率虚高一倍）。
- 收尾块 = 最后一个带文本、不带工具的 assistant 块；其后同回合还有工具 → 过渡句（不计）。
- 网关错误先看记录元数据 isApiErrorMessage，文本行首匹配只作后备
  （有 "Prompt is too long · … API Error: …" 这种带前缀的，行首匹配会漏）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime

_ERR = re.compile(r"^\s*(API Error|upstream connection lost)")
_EVID = re.compile(r"~/|/home/|\b\w+\.(py|md|json|sh|db|log|txt|ya?ml)\b"
                   r"|git |pytest|sqlite3|journalctl|curl |#\d{5,}|wf_[0-9a-f]{6,}"
                   r"|\bPASS\b|\bFAIL\b")
_SYS = ("This session is being continued", "<task-notification>",
        "Caveat: The messages below were generated")
_KINDS = (("api_nudge", "API 错误中断"), ("reminder", "【用户意图提醒"),
          ("notice", "[notice"), ("workflow", "[workflow]"), ("banner", "## "))


def _kind(head: str) -> str:
    h = (head or "").strip()
    if any(s in h[:60] for s in _SYS):
        return "system"
    return next((k for k, p in _KINDS if h.startswith(p) or p in h[:40]), "task/other")


def _ts(rec):
    try:
        return datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def _turns(path):
    """→ [(head, records)]；只有字符串正文的 user 记录才开新回合。"""
    recs = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue          # 并发写入的半行
            if r.get("timestamp"):
                recs.append(r)
    recs.sort(key=lambda x: x["timestamp"])
    out, cur, head = [], None, ""
    for r in recs:
        if r.get("type") == "user":
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str) and c.strip():
                if cur:
                    out.append((head, cur))
                cur, head = [], c
        if cur is not None:
            cur.append(r)
    return out + ([(head, cur)] if cur else [])


def measure(cut: float, pattern=None):
    dirs = glob.glob(pattern or str(os.path.expanduser("~/.claude/projects/-*ccs-workspaces-*")))
    s = defaultdict(Counter)
    for d in dirs:
        role = os.path.basename(d).split("workspaces-")[-1]
        for f in glob.glob(d + "/*.jsonl"):
            for head, recs in _turns(f):
                t0 = _ts(recs[0])
                if t0 is None or t0 < cut:
                    continue
                r0, tools, tail, tail_err, model = s[role], 0, None, False, ""
                for r in recs:
                    if r.get("type") != "assistant":
                        continue
                    c = (r.get("message") or {}).get("content")
                    if not isinstance(c, list):
                        continue
                    _m = (r.get("message") or {}).get("model") or ""
                    if _m and "synthetic" not in _m:
                        model = _m
                    has = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c)
                    texts = [b.get("text") or "" for b in c
                             if isinstance(b, dict) and b.get("type") == "text"]
                    txt = " ".join(texts).strip()
                    tools += has
                    if has:
                        tail, tail_err = None, False
                    elif txt:
                        # 判据只认**最后一块非空文本**：同回合里"错误→正常交付"最终算交付、
                        # "正常→错误"算错误，且与错误块/正常块是否拆在两条记录无关
                        # （按记录累积或按任意块匹配都会让结果随记录边界翻转）。
                        blocks = [t for t in texts if t.strip()]
                        # 元数据标记优先：实测 3093 条 isApiErrorMessage 记录里 0 条含多块，
                        # 该形态在生产数据里不出现；这里把它定成"整条记录算错误"的显式规则，
                        # 由 self-check 的混合用例钉住，避免以后被当成随机行为改来改去。
                        tail = txt
                        tail_err = bool(r.get("isApiErrorMessage")) or bool(
                            blocks and _ERR.match(blocks[-1]))
                k = _kind(head)
                r0["k:" + k] += 1
                r0["tools"] += tools
                if k == "system":
                    r0["system"] += 1
                    continue
                if not tools:
                    r0["zero"] += 1
                    r0["k:" + k + ":zero"] += 1
                if tail is None:
                    continue
                if tail_err:      # 判据在上面按"最后一块"算好了，这里不再看整串行首
                    r0["api_error"] += 1
                    # 端点归因：只知道"网络不好"没法处置。2026-10-22 实测 24h 的 429
                    # 全部来自 ollama/gpt-oss:120b —— 换模型即可解，不必查网关。
                    _ek = ("429" if "429" in tail else
                           "conn_lost" if "connection lost" in tail.lower() else "other")
                    _tag = re.search(r"\[([^\]]{3,40})\]", tail)
                    _tagv = _tag.group(1) if _tag else ""
                    # 模型名才是端点线索；`[400]` 这类状态码 tag 不是端点（codex 指出）
                    _who = (model if model and "synthetic" not in model
                            else (_tagv if _tagv and not _tagv.isdigit() else "?")).split("/")[-1][:28]
                    r0["err:%s:%s" % (_ek, _who)] += 1
                else:
                    r0["final"] += 1
                    if not _EVID.search(tail):
                        r0["no_evidence"] += 1
    return s, len(dirs)


def _rec(kind, ts, content, **kw):
    return dict({"type": kind, "timestamp": ts, "message": {"content": content}}, **kw)


def self_check():
    """判据自证：口径改错必须变红（tool_result 不得开局；元数据错误不得算正文）。"""
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "-x-ccs-workspaces-probe")
        os.makedirs(d)
        rows = [
            _rec("user", "2030-01-01T00:00:00Z", "做点事"),
            _rec("assistant", "2030-01-01T00:00:01Z", [{"type": "text", "text": "先查证。"}]),
            _rec("assistant", "2030-01-01T00:00:02Z", [{"type": "tool_use", "name": "bash"}]),
            # tool_result 是 list 正文：错口径会让它开新回合
            _rec("user", "2030-01-01T00:00:03Z", [{"type": "tool_result", "content": "ok"}]),
            _rec("user", "2030-01-01T00:01:00Z", "再来"),
            # 元数据标记的网关错误：文本无 "API Error" 前缀，行首匹配会漏
            _rec("assistant", "2030-01-01T00:01:01Z",
                 [{"type": "text", "text": "Prompt is too long · compaction failed"}],
                 isApiErrorMessage=True),
            _rec("user", "2030-01-01T00:01:30Z", "再来2"),
            # 同记录"错误→正常交付"：最终算交付（结果不随记录边界翻转）
            _rec("assistant", "2030-01-01T00:01:31Z",
                 [{"type": "text", "text": "API Error: upstream connection lost"},
                  {"type": "text", "text": "正常交付：见 bus #439999"}]),
            _rec("user", "2030-01-01T00:01:40Z", "再来3"),
            # 反向：同记录"正常→错误" 最终算网关错误
            _rec("assistant", "2030-01-01T00:01:41Z",
                 [{"type": "text", "text": "正常交付：见 bus #439998"},
                  {"type": "text", "text": "API Error: upstream connection lost"}]),
            _rec("user", "2030-01-01T00:01:50Z", "再来4"),
            # 元数据 + 正常块混排：按规则整条算错误（生产 3093 条里 0 条这种形态）
            _rec("assistant", "2030-01-01T00:01:51Z",
                 [{"type": "text", "text": "API Error: upstream connection lost"},
                  {"type": "text", "text": "正常交付：见 bus #439997"}],
                 isApiErrorMessage=True),
            _rec("user", "2030-01-01T00:02:00Z", "最后一问"),
            _rec("assistant", "2030-01-01T00:02:01Z", [{"type": "text", "text": "已完成，无新动作。"}]),
            _rec("user", "2030-01-01T00:03:00Z", "This session is being continued from a previous…"),
            _rec("assistant", "2030-01-01T00:03:01Z", [{"type": "text", "text": "继续。"}]),
        ]
        with open(os.path.join(d, "probe.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows))
        t = measure(0.0, os.path.join(td, "-*ccs-workspaces-*"))[0]["probe"]
    assert t["system"] == 1, t           # 会话续接单列
    assert t["k:task/other"] == 6, t     # tool_result 没有开新回合（错口径会变 7）
    assert t["zero"] == 5, t             # 4 个无工具轮 + 1 个交付轮
    assert t["api_error"] == 3, t        # 元数据 2（含混排）+ "正常→错误" 1
    assert t["final"] == 2, t            # "错误→正常交付" 与 "已完成" 各一
    assert t["no_evidence"] == 1, t      # 只有"已完成，无新动作"
    print("self-check PASS", dict(t))


def _report(hours, cut: float, s) -> None:
    tot = Counter()
    for r in s.values():
        tot.update(r)
    rt = sum(v for k, v in tot.items()
             if k.startswith("k:") and not k.endswith(":zero") and k != "k:system")
    print("[{}] cut={} 回合={} 系统语料={} 零工具={}({:.1f}%) 收尾={} 无证据={} 网关错误={}".format(
        hours, datetime.fromtimestamp(cut).isoformat(timespec="seconds"), rt, tot["system"],
        tot["zero"], 100 * tot["zero"] / max(1, rt), tot["final"],
        tot["no_evidence"], tot["api_error"]))
    kinds = sorted({k[2:] for r in s.values() for k in r
                    if k.startswith("k:") and not k.endswith(":zero") and k != "k:system"},
                   key=lambda k: -tot["k:" + k])
    print("按注入类别（零工具率）：" + "  ".join(
        "{}={}({:.0f}%)".format(k, tot["k:" + k], 100 * tot["k:" + k + ":zero"] / max(1, tot["k:" + k]))
        for k in kinds))
    errs = sorted(((k, v) for k, v in tot.items() if k.startswith("err:")), key=lambda kv: -kv[1])
    if errs:
        print("网关错误按端点: " + "  ".join("{}={}".format(k[4:], v) for k, v in errs[:6]))
    for role, r in sorted(s.items(), key=lambda kv: -kv[1]["zero"])[:8]:
        print("  {:20s} 回合={:4d} 零工具={:3d} 收尾={:3d} 无证据={:3d}".format(
            role, sum(v for k, v in r.items() if k.startswith("k:") and not k.endswith(":zero")
                      and k != "k:system"), r["zero"], r["final"], r["no_evidence"]))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, nargs="+", default=[6.0, 24.0],
                    help="可给多个窗口；默认 6h+24h（24h 会把已结束的事故算成现状）")
    ap.add_argument("--cut", help="ISO 起点(UTC)，给定时忽略 --hours")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
        raise SystemExit(0)
    now = time.time()
    for h, cut in ([(0, a.cut)] if a.cut else [(h, None) for h in a.hours]):
        c = (datetime.fromisoformat(cut.replace("Z", "+00:00")).timestamp()
             if cut else now - h * 3600)
        s, ndirs = measure(c)
        if not ndirs:
            raise SystemExit("找不到角色转录目录 —— 判据基准错了，不看 0 结果")
        _report("{}h".format(h) if h else "custom", c, s)
