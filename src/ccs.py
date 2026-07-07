#!/usr/bin/env python3
"""
ccs.py — CCS 生命周期管理器（主入口）

协作模式：
  --partner <role> --auto-restart     主从模式：守护伙伴存活
  --bus-track <cat>                   对等模式：轮次追踪 + 死锁检测
  两者可组合：既守护伙伴又追踪轮次
"""
import argparse
import json
import sys

from core import (
    start, stop, status, send, output, health_check, register,
    workspace_create, workspace_list,
)
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="CCS 生命周期管理器 — 内置协作基础设施",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
协作模式示例:
  # 主从模式：verifier 守护 rebutter
  ccs.py start verifier --partner rebutter --auto-restart --no-attach

  # 对等模式：两个 CCS 互相追踪 bus debate
  ccs.py start pro --bus-track debate --no-attach
  ccs.py start rebutter --bus-track debate --no-attach

  # 组合模式：既守护伙伴又追踪轮次
  ccs.py start verifier --partner rebutter --auto-restart --bus-track debate --no-attach

  # 仲裁模式：监控者守护两个 CCS
  ccs.py start monitor --partner verifier --partner rebutter --bus-track debate --no-attach
""")
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="创建并启动 CCS")
    p_start.add_argument("role", help="角色名（自动加 ccs- 前缀）")
    p_start.add_argument("title", nargs="?", default="", help="角色标题")
    p_start.add_argument("--no-attach", action="store_true", help="后台运行，不 attach")
    p_start.add_argument("--prompt", default="", help="初始 prompt（自动发送）")
    p_start.add_argument("--partner", default=None, action="append",
                         help="守护伙伴（可多次指定）")
    p_start.add_argument("--auto-restart", action="store_true",
                         help="伙伴挂了自动重启")
    p_start.add_argument("--bus-track", default="",
                         help="追踪 bus 分类的轮次（防死锁）")
    p_start.add_argument("--bus-timeout", type=int, default=300,
                         help="轮次超时秒数（默认 300）")
    p_start.add_argument("--workspace", default="",
                         help="系统级 CCS 工作空间名（如 ccs-monitor），使用独立 CLAUDE.md")
    p_start.add_argument("--drive", default="loop",
                         help="驱动方式: loop / feed / both（默认 loop）")
    p_start.add_argument("--feed-cat", default="",
                         help="feed push 监听的 bus 分类（如 debate），实时接收新消息")

    # stop
    p_stop = sub.add_parser("stop", help="终止 CCS")
    p_stop.add_argument("role", help="角色名")

    # status
    sub.add_parser("status", help="列出所有 CCS 状态")

    # send
    p_send = sub.add_parser("send", help="向 CCS 发消息")
    p_send.add_argument("role", help="角色名")
    p_send.add_argument("message", help="消息内容")

    # output
    p_out = sub.add_parser("output", help="查看 CCS 输出")
    p_out.add_argument("role", help="角色名")
    p_out.add_argument("--tail", type=int, default=20, help="行数")

    # health
    p_health = sub.add_parser("health", help="健康检查")
    p_health.add_argument("role", nargs="?", default="", help="角色名（空=全部）")

    # register
    p_reg = sub.add_parser("register", help="注册手动 tmux 为 CCS")
    p_reg.add_argument("role", help="角色名")
    p_reg.add_argument("tmux_name", help="tmux session 名")
    p_reg.add_argument("title", nargs="?", default="", help="角色标题")

    # workspace
    p_ws = sub.add_parser("workspace", help="管理工作空间")
    ws_sub = p_ws.add_subparsers(dest="ws_command")
    ws_create = ws_sub.add_parser("create", help="创建新工作空间")
    ws_create.add_argument("name", help="工作空间名（如 ccs-monitor）")
    ws_sub.add_parser("list", help="列出所有工作空间")

    args = parser.parse_args()

    if args.command == "start":
        result = start(
            role=args.role,
            title=args.title,
            detach=args.no_attach,
            init_prompt=args.prompt,
            partners=args.partner,
            auto_restart=args.auto_restart,
            bus_track=args.bus_track,
            bus_timeout=args.bus_timeout,
            workspace=args.workspace,
            drive=args.drive,
            feed_cat=args.feed_cat,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "stop":
        result = stop(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "status":
        result = status()
        if not result:
            print("没有运行中的 CCS")
        else:
            print(f"运行中的 CCS: {len(result)}")
            for s in result:
                uptime_m = s["uptime_sec"] // 60
                health = s["health"]
                print(f"  [{s['role']:12}] {s['title']}  "
                      f"{'✅' if s['alive'] else '❌'}  "
                      f"运行 {uptime_m}分  pid={s['pid']}  "
                      f"partner={s['partner'] or '-'}  bus={s['bus_track'] or '-'}")
                print(f"      health: watchdog={'✅' if health['watchdog_ok'] else '❌'} "
                      f"bus_age={health['bus_msg_age']}s restarts={health['restart_count']}")

    elif args.command == "send":
        result = send(args.role, args.message)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "output":
        print(output(args.role, tail=args.tail))

    elif args.command == "health":
        result = health_check(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "register":
        result = register(args.role, args.tmux_name, args.title)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "workspace":
        if args.ws_command == "create":
            result = workspace_create(args.name)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("success"):
                sys.exit(1)
        elif args.ws_command == "list":
            result = workspace_list()
            if not result:
                print("没有工作空间")
            else:
                for ws in result:
                    print(f"  {ws['name']:20} {ws['path']}")
        else:
            print("用法: ccs.py workspace {create|list}")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()