#!/usr/bin/env python3
"""wf — 一步达 WorkflowClient CLI (like ccs)"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workflow_client import WorkflowClient, check  # noqa: E402


def _require_role() -> str:
    """获取角色名；非交互环境（stdin 关闭）时给出可操作提示而非 traceback。

    原实现 `role or input("role: ")` 在 daemon/脚本/CI 调用（stdin=/dev/null）
    时抛 EOFError 裸栈。已有 --role 与 $CCS_ROLE 两条注入路径，此处只兜底报错。
    """
    if not sys.stdin.isatty():
        print("缺少角色：请用 --role/-r 指定（须置于子命令前，如 `wf -r engineer stats`）"
              "或设置 $CCS_ROLE", file=sys.stderr)
        sys.exit(2)
    return input("role: ")


def main():
    p = argparse.ArgumentParser(prog="wf", formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--role", "-r", default=os.getenv("CCS_ROLE", ""), help="角色 (默认 $CCS_ROLE)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检查是否有待办任务")

    p_create = sub.add_parser("create", help="创建任务")
    p_create.add_argument("title")
    p_create.add_argument("--desc", "-d", default="", help="任务描述（替换 {task_definition} 占位符）")
    p_create.add_argument("--assignee", "-a", default="")
    # --template 可选：角色的职责是判断需求，不该先知道内部模板名。
    # 不传时交给 TemplateRegistry.recommend() 按标题+发起角色+bus 分类自动选
    # （create_task_v2 的 template_id="" 分支本就支持）。原 required=True 使
    # “PM 说‘做个网页’→建工作流”这条入口物理不可用。
    p_create.add_argument("--template", "-t", default="",
                          help="工作流模板；不传则按需求自动推荐")
    p_create.add_argument("--initiator", "-i", default="")

    p_complete = sub.add_parser("complete", help="完成任务")
    p_complete.add_argument("wf_id")
    p_complete.add_argument("--summary", "-s", default="done")

    p_fail = sub.add_parser("fail", help="标记失败")
    p_fail.add_argument("wf_id")
    p_fail.add_argument("--reason", "-r", default="")

    p_notify = sub.add_parser("notify", help="发 bus 通知")
    p_notify.add_argument("cat")
    p_notify.add_argument("title")
    p_notify.add_argument("--evidence", "-e", default="")

    p_task = sub.add_parser("task", help="查看任务详情")
    p_task.add_argument("task_id")

    p_logs = sub.add_parser("logs", help="查看日志")
    p_logs.add_argument("--wf", default="")
    p_logs.add_argument("--task", default="")

    p_cancel = sub.add_parser("cancel", help="取消工作流")
    p_cancel.add_argument("wf_id")
    p_cancel.add_argument("--reason", "-r", default="")

    p_my = sub.add_parser("my", help="我的任务列表")
    p_my.add_argument("--status", "-s", default="")

    p_ls = sub.add_parser("ls", help="列出任务")
    p_ls.add_argument("--status", "-s", default="")
    p_ls.add_argument("--assignee", "-a", default="")

    sub.add_parser("kanban", help="看板视图")
    sub.add_parser("stats", help="统计")

    p_cleanup = sub.add_parser("cleanup", help="列出/取消僵尸工作流")
    p_cleanup.add_argument("--minutes", type=int, default=120, help="running 超时阈值(分钟)")
    p_cleanup.add_argument("--limit", type=int, default=50)
    p_cleanup.add_argument("--yes", action="store_true", help="执行取消(默认 dry-run)")

    # 审批入口：引擎为 handoff/review/subflow 步骤签发 approval_token 并通过
    # ccs send 推给审批人，但没有 CLI 能把它提交回来 —— 角色只能眼看步骤卡在
    # step_done_ready，最终靠 daemon 的 TTL+30min 兜底自动 approve（质量闸门失效）。
    # 实测 24h：step_done_ready 410 次 vs step_confirmed 5 次。
    p_confirm = sub.add_parser("confirm", help="审批步骤（提交 approval_token）")
    p_confirm.add_argument("wf_id")
    p_confirm.add_argument("step_id")
    p_confirm.add_argument("--token", "-t", required=True, help="引擎下发的审批密钥")
    p_confirm.add_argument("--reject", action="store_true", help="驳回而非通过")
    p_confirm.add_argument("--reason", "-r", default="")

    # 拉取审批密钥：推送路径（ccs send）失败时的唯一可用入口。降级 bus 事实里
    # 只写「请用 wf approval 取回」，不写明文 token（共享 bus 不得承载凭证）。
    p_appr = sub.add_parser("approval", help="拉取自己是审批人的步骤密钥")
    p_appr.add_argument("wf_id")
    p_appr.add_argument("step_id")
    p_appr.add_argument("--json", action="store_true")

    # 模板匹配是词法匹配，而角色拿到的是自然语言需求（"做一个高质量网页"）
    # vs 内部行话（"产品经理需求拆解"）—— 2-gram 无交集就会推荐为空。
    # 与其堆同义词表，不如把选择权交给角色的知识（prompt 就是这么设计的）。
    p_tpl = sub.add_parser("templates", help="列出可用工作流模板（供角色选型）")
    p_tpl.add_argument("--json", action="store_true")
    p_tpl.add_argument("--match", "-m", default="", help="按需求描述粗匹配排序")

    args = p.parse_args()
    role = args.role or os.getenv("CCS_ROLE", "")
    if not args.cmd:
        p.print_help()
        return

    if args.cmd == "check":
        if role:
            with WorkflowClient(role) as wf:
                t = wf.check_task()
                print(json.dumps(t, indent=2, ensure_ascii=False) if t else "无待办")
        else:
            for r in ("pm", "pg", "qa", "engineer", "lr", "arch", "scout",
                       "devops", "reviewer", "maintainer", "coordinator", "cx", "whale"):
                t = check(r)
                if t:
                    print(f"{r}: {t.get('instance_id','?')} — {t.get('template_id','?')}")

    elif args.cmd == "create":
        r = args.role or args.assignee
        if not r:
            # rc=0 + stdout 会把这句话当成"建好了"：调用方（角色/脚本）按返回码判成败，
            # 需求静默丢弃 = 链根本没起。与 complete 的终态分支同口径：写 stderr + 非 0。
            print("--role 或 --assignee 必填", file=sys.stderr)
            sys.exit(2)
        init = args.initiator or r
        with WorkflowClient(r) as wf:
            desc = args.desc or ""
            tid, wid = wf.create_task_v2(args.title, r, args.template, init, description=desc)
            print(f"task={tid}  wf={wid}")

    elif args.cmd == "complete":
        # 返回 None = 实例已是终态，本次什么都没写。角色把「打印了完成」当完成判据，
        # 昔日的无条件 print 让空操作也 rc=0 —— 完成信号因此永远为真。
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            _terminated = wf.complete(args.wf_id, args.summary) is None
        if _terminated:
            print(f"未写入: {args.wf_id} 已是终态，complete 为空操作", file=sys.stderr)
            sys.exit(1)
        print(f"完成: {args.wf_id}")

    elif args.cmd == "fail":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            _terminated = wf.fail(args.wf_id, args.reason) is None
        if _terminated:
            print(f"未写入: {args.wf_id} 已是终态，fail 为空操作", file=sys.stderr)
            sys.exit(1)
        print(f"失败标记: {args.wf_id}")


    elif args.cmd == "notify":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            wf.notify(args.cat, args.title, evidence=args.evidence)
            print(f"通知已发: {args.cat} / {args.title}")

    elif args.cmd == "task":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            t = wf.get_task(args.task_id)
            if not t:
                # stdout + rc=0 会被当成"查到了但没内容"：角色按 rc 判成败，
                # 契约查不到就静默提交（实测角色只持 wf_id，见 client.get_task 注释）。
                print(f"未找到: {args.task_id}（task_id 或 wf_id）", file=sys.stderr)
                sys.exit(1)
            print(json.dumps(t, indent=2, ensure_ascii=False))

    elif args.cmd == "logs":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            # 实例不存在时 get_logs 返回空表，rc=0 会被读成"这条链没有日志"。
            if args.wf and not wf.get(args.wf):
                print(f"未找到: {args.wf}（wf_id）", file=sys.stderr)
                sys.exit(1)
            logs = wf.get_logs(wf_id=args.wf or None, task_id=args.task or None)
            for l in logs:
                print(f"[{l.get('ts','')}] {l.get('action','')} — {l.get('detail','')}")

    elif args.cmd == "cancel":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            _terminated = wf.cancel(args.wf_id, args.reason) is None
        if _terminated:
            print(f"未写入: {args.wf_id} 已是终态，cancel 为空操作", file=sys.stderr)
            sys.exit(1)
        print(f"已取消: {args.wf_id}")

    elif args.cmd == "confirm":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            wf.confirm(args.wf_id, args.step_id, token=args.token,
                       approved=not args.reject, reason=args.reason)
            print(f"{'已驳回' if args.reject else '已通过'}: {args.wf_id}/{args.step_id}")

    elif args.cmd == "approval":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            res = wf.pull_approval(args.wf_id, args.step_id)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, default=str))
            sys.exit(0 if res.get("ok") else 1)
        if not res.get("ok"):
            print(f"无法拉取: {res.get('error')}", file=sys.stderr)
            sys.exit(1)
        print(res["token"])
        print(f"剩余有效期: {int(res.get('ttl_remaining', 0) // 60)} 分钟", file=sys.stderr)

    elif args.cmd == "templates":
        import sys as _sys
        from paths import SESSION_PIPELINE_SRC as _psrc
        if str(_psrc) not in _sys.path:
            _sys.path.insert(0, str(_psrc))
        from template_registry import TemplateRegistry
        reg = TemplateRegistry()
        try:
            rows = reg.list(active_only=True)
            if args.match:
                # 粗匹配仅做排序参考；未命中排在后面，仍全部列出——
                # 选型靠角色判断，不靠词法打分。
                ranked = {x["template_id"] for x in
                          reg.recommend(args.match, initiator_role=role) or []}
                rows = sorted(rows, key=lambda t: t.get("template_id") not in ranked)
            if args.json:
                print(json.dumps([{
                    "template_id": t.get("template_id"),
                    "description": t.get("description", ""),
                    "trigger_scene": t.get("trigger_scene", []),
                    "executors": t.get("allowed_executors", []),
                } for t in rows], ensure_ascii=False, indent=2))
            else:
                for t in rows:
                    ex = ",".join(t.get("allowed_executors") or []) or "-"
                    print(f"{t.get('template_id','?'):26s} [{ex}] {str(t.get('description',''))[:70]}")
        finally:
            reg.close()


    elif args.cmd == "my":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            tasks = wf.list_my_tasks(status=args.status or None)
            for t in tasks:
                print(f"{t.get('instance_id','?'):24s} {str(t.get('status','?')):10s} {str(t.get('template_id','?')):12s} {t.get('current_step_id','?')}")

    elif args.cmd == "ls":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            tasks = wf.list_tasks(status=args.status or None, assignee=args.assignee or None)
            for t in tasks:
                print(f"{t.get('task_id','?'):24s} {t.get('status','?'):10s} {t.get('assignee','?'):12s} {t.get('title','')}")

    elif args.cmd == "kanban":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            b = wf.kanban_board()
            for lane in b:
                print(f"\n## {lane['lane']} ({lane['count']})")
                for item in lane['items'][:5]:
                    print(f"  {item.get('instance_id','?'):24s} {item.get('current_step_id',''):6s} {item.get('assignee','')}")

    elif args.cmd == "cleanup":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            zombies = wf.find_zombies(args.minutes, args.limit)
            if not zombies:
                print("无僵尸工作流")
                return
            for z in zombies:
                print(f"  {z['instance_id']} | {z['template_id']} | {z['assignee']} | "
                      f"step={z['current_step_id']} | running={z['running_minutes']:.0f}min")
            if args.yes:
                cancelled = [z["instance_id"] for z in zombies
                             if wf.cancel(z["instance_id"], "zombie cleanup")]
                skipped = len(zombies) - len(cancelled)
                for wf_id in cancelled:
                    print(f"  已取消: {wf_id}")
                if skipped:
                    print(f"  {skipped} 个已是终态，未改动", file=sys.stderr)
                wf.notify("architecture", f"wf cleanup 已取消 {len(cancelled)} 个僵尸工作流",
                          evidence="\n".join(cancelled))
            else:
                print(f"\n--dry-run: {len(zombies)} 个候选，加 --yes 执行取消")

    elif args.cmd == "stats":
        r = role or os.getenv("CCS_ROLE", "") or _require_role()
        with WorkflowClient(r) as wf:
            s = wf.workflow_stats()
            for k, v in s.items():
                print(f"{k}: {v}")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        # 角色拿 `wf complete` 当"写个完成信号"，拼错/过期的 wf_id 此前会被
        # 打印成「完成: <id>」并 rc=0 —— 角色的完成判据因此永远为真。
        print(f"错误: {e.args[0] if e.args else e}", file=sys.stderr)
        sys.exit(2)
