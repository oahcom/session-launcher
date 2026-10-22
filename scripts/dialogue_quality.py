#!/usr/bin/env python3
"""对话质量口径：按「回合」而不是按「消息块」统计角色转录。

WHY 这个脚本存在：2026-10-21 的判据把「只带文本、不调工具的 assistant 块」当低质量
文本（1,900 条），据此给 prompts/base.md 加了一条规则。2026-10-22 复测：其中 81.5%
是**同回合内紧接着调工具的过渡句**，回合末文本块里 53% 是网关错误而不是角色产出——
判据的分母错了（同 skill §34：判据自己比它抓到的问题更容易出错）。

口径（唯一真相，改这里、别各自重算）：
- 回合 = 一条 user 字符串消息 → 下一条 user 字符串消息之前（tool_result 是 list，不算开局）。
- 零工具回合 = 该回合内没有任何 tool_use 块。
- 回合末文本块 = 最后一个带文本、不带工具的 assistant 块；它之后同回合还有工具调用 →
  过渡句(preamble)，否则算收尾块(final)。过渡句不是缺陷，不计入任何质量分母。
- API Error / upstream connection lost 开头的收尾块单列，不计入「角色没给证据」。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import Counter, defaultdict

_ERR = re.compile(r"^\s*(API Error|upstream connection lost)")
_EVID = re.compile(r"~/|/home/|\.\w{1,4}\b|:[0-9]{1,4}\b|git |pytest|sqlite3|"
                   r"journalctl|#\d{5,}|wf_[0-9a-f]{6,}|\bPASS\b|\bFAIL\b")
PAT = os.path.expanduser("~/.claude/projects/-home-administrator-ccs-workspaces-*")


def _turns(path):
    recs = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
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
        # 哨兵必须是 None：新回合的 cur 是空 list，判真值会让整回合一条都收不进来
        # （实测：全窗口回合数=0，看着像"没有转录"，其实是这一行）。
        if cur is not None:
            cur.append(r)
    if cur:
        out.append((head, cur))
    return out


def measure(cut, pattern=PAT):
    s = defaultdict(Counter)
    for d in glob.glob(pattern):
        role = os.path.basename(d).split("workspaces-")[-1]
        for f in glob.glob(d + "/*.jsonl"):
            for head, recs in _turns(f):
                if recs[0]["timestamp"] < cut:
                    continue
                r0 = s[role]
                r0["turns"] += 1
                blocks, tools = [], 0
                for r in recs:
                    if r.get("type") != "assistant":
                        continue
                    c = (r.get("message") or {}).get("content")
                    if not isinstance(c, list):
                        continue
                    has = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c)
                    txt = " ".join(b.get("text") or "" for b in c
                                   if isinstance(b, dict) and b.get("type") == "text").strip()
                    tools += 1 if has else 0
                    blocks.append((has, txt))
                if not tools:
                    r0["zero_tool_turns"] += 1
                r0["turn_tools"] += tools
                tail = None
                for has, txt in blocks:
                    if txt and not has:
                        tail = txt
                    if has:
                        tail = None
                if tail is None:
                    continue
                if _ERR.search(tail):
                    r0["final_api_error"] += 1
                    continue
                r0["final_text"] += 1
                if not _EVID.search(tail):
                    r0["final_no_evidence"] += 1
    return s


def self_check():
    """判据自证：必须能区分过渡句 / 收尾 / 网关错误，且分母不是 0。"""
    tmp = "/tmp/_dq_selfcheck/roles/probe"
    os.makedirs(tmp, exist_ok=True)
    body = "\n".join([
        json.dumps({"type": "user", "timestamp": "2030-01-01T00:00:00Z",
                    "message": {"content": "做点事"}}),
        json.dumps({"type": "assistant", "timestamp": "2030-01-01T00:00:01Z",
                    "message": {"content": [{"type": "text", "text": "先查证。"}]}}),
        json.dumps({"type": "assistant", "timestamp": "2030-01-01T00:00:02Z",
                    "message": {"content": [{"type": "tool_use", "name": "bash"}]}}),
        json.dumps({"type": "user", "timestamp": "2030-01-01T00:01:00Z",
                    "message": {"content": "再来"}}),
        json.dumps({"type": "assistant", "timestamp": "2030-01-01T00:01:01Z",
                    "message": {"content": [{"type": "text",
                                             "text": "API Error: upstream connection lost"}]}}),
        json.dumps({"type": "user", "timestamp": "2030-01-01T00:02:00Z",
                    "message": {"content": "最后一问"}}),
        json.dumps({"type": "assistant", "timestamp": "2030-01-01T00:02:01Z",
                    "message": {"content": [{"type": "text", "text": "已完成，无新动作。"}]}}),
    ])
    with open(tmp + "/probe.jsonl", "w", encoding="utf-8") as fh:
        fh.write(body)
    t = measure("2029-01-01", os.path.dirname(tmp) + "/probe")["probe"]
    assert t["turns"] == 3, t
    # 零工具回合 = 2：网关错误那轮确实没干活，算零工具；但它在 final_api_error 里单列，
    # 想看"角色偷懒"就把 final_api_error 从分母里减掉，别把它读成懒惰。
    assert t["zero_tool_turns"] == 2, t
    assert t["final_api_error"] == 1, t           # 网关错误单列
    assert t["final_no_evidence"] == 1, t         # 只有"已完成，无新动作"算无证据收尾
    print("self-check PASS", dict(t))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--cut", help="ISO 起点(UTC)，与 --hours 二选一")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        import time
        cut = a.cut or time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time() - a.hours * 3600))
        s = measure(cut)
        tot = Counter()
        for r in s.values():
            tot.update(r)
        print("cut={} 回合={} 零工具回合={} ({:.1f}%) 收尾文本={} 无证据={} 网关错误={}".format(
            cut, tot["turns"], tot["zero_tool_turns"],
            100 * tot["zero_tool_turns"] / max(1, tot["turns"]),
            tot["final_text"], tot["final_no_evidence"], tot["final_api_error"]))
        for role, r in sorted(s.items(), key=lambda kv: -kv[1]["zero_tool_turns"])[:8]:
            print("  {:20s} 回合={:4d} 零工具={:3d} 收尾={:3d} 无证据={:3d}".format(
                role, r["turns"], r["zero_tool_turns"], r["final_text"], r["final_no_evidence"]))
