#!/usr/bin/env python3
"""wf — 一步达 WorkflowClient CLI (like ccs)"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workflow_client import WorkflowClient, check  # noqa: E402


def main():
    p = argparse.ArgumentParser(prog="wf", formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--role", "-r", default=os.getenv("CCS_ROLE", ""), help="角色 (默认 $CCS_ROLE)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检查是否有待办任务")

    p_create = sub.add_parser("create", help="创建任务")
    p_create.add_argument("title")
    p_create.add_argument("--desc", "-d", default="", help="任务描述（替换 {task_definition} 占位符）")
    p_create.add_argument("--assignee", "-a", default="")
    p_create.add_argument("--template", "-t", required=True)
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
            print("--role 或 --assignee 必填")
            return
        init = args.initiator or r
        with WorkflowClient(r) as wf:
            desc = args.desc or ""
            tid, wid = wf.create_task_v2(args.title, r, args.template, init, description=desc)
            print(f"task={tid}  wf={wid}")

    elif args.cmd == "complete":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            wf.complete(args.wf_id, args.summary)
            print(f"完成: {args.wf_id}")

    elif args.cmd == "fail":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            wf.fail(args.wf_id, args.reason)
            print(f"失败标记: {args.wf_id}")

    elif args.cmd == "notify":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            wf.notify(args.cat, args.title, evidence=args.evidence)
            print(f"通知已发: {args.cat} / {args.title}")

    elif args.cmd == "task":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            t = wf.get_task(args.task_id)
            print(json.dumps(t, indent=2, ensure_ascii=False) if t else "未找到")

    elif args.cmd == "logs":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            logs = wf.get_logs(wf_id=args.wf or None, task_id=args.task or None)
            for l in logs:
                print(f"[{l.get('ts','')}] {l.get('action','')} — {l.get('detail','')}")

    elif args.cmd == "cancel":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            wf.cancel(args.wf_id, args.reason)
            print(f"已取消: {args.wf_id}")

    elif args.cmd == "my":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            tasks = wf.list_my_tasks(status=args.status or None)
            for t in tasks:
                print(f"{t.get('instance_id','?'):24s} {str(t.get('status','?')):10s} {str(t.get('template_id','?')):12s} {t.get('current_step_id','?')}")

    elif args.cmd == "ls":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            tasks = wf.list_tasks(status=args.status or None, assignee=args.assignee or None)
            for t in tasks:
                print(f"{t.get('task_id','?'):24s} {t.get('status','?'):10s} {t.get('assignee','?'):12s} {t.get('title','')}")

    elif args.cmd == "kanban":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            b = wf.kanban_board()
            for lane in b:
                print(f"\n## {lane['lane']} ({lane['count']})")
                for item in lane['items'][:5]:
                    print(f"  {item.get('instance_id','?'):24s} {item.get('current_step_id',''):6s} {item.get('assignee','')}")

    elif args.cmd == "cleanup":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            zombies = wf.find_zombies(args.minutes, args.limit)
            if not zombies:
                print("无僵尸工作流")
                return
            for z in zombies:
                print(f"  {z['instance_id']} | {z['template_id']} | {z['assignee']} | "
                      f"step={z['current_step_id']} | running={z['running_minutes']:.0f}min")
            if args.yes:
                for z in zombies:
                    wf.cancel(z["instance_id"], "zombie cleanup")
                    print(f"  已取消: {z['instance_id']}")
                wf.notify("architecture", "wf cleanup 已取消 {len(zombies)} 个僵尸工作流",
                          evidence="\n".join(z["instance_id"] for z in zombies))
            else:
                print(f"\n--dry-run: {len(zombies)} 个候选，加 --yes 执行取消")

    elif args.cmd == "stats":
        r = role or input("role: ")
        with WorkflowClient(r) as wf:
            s = wf.workflow_stats()
            for k, v in s.items():
                print(f"{k}: {v}")


if __name__ == "__main__":
    main()
