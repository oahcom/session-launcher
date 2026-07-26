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
import os
import sys

from core import (
    start, stop, status, send, output, health_check, register,
    workspace_create, workspace_list, force_start_ccs, wake_ccs,
    start_codex_session, run_codex_task, cdx_status,
    get_role, inject_role_knowledge_into_workspace, _invalidate_role_cache,
    list_roles, get_config_value,
)


def main():
    parser = argparse.ArgumentParser(
        description="CCS 生命周期管理器 — 内置协作基础设施",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
新增命令:
  config get/set/edit    查看/修改 ccs_config.json（全局配置中心）
  auto-send list/add/rm  管理角色的自动发送消息
  roles [--available]    列出所有可启动角色

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

启动优化:
  ccs.py start <role>              启动 CCS 后自动发送 ccs_config.json 配置的消息
  ccs.py start <role> --no-auto-send  跳过自动发送（只保留角色 prompt）
""")
    sub = parser.add_subparsers(dest="command")

    # ── start ──
    p_start = sub.add_parser("start", help="创建并启动 CCS")
    p_start.add_argument("role", help="角色名（自动加 ccs- 前缀）")
    p_start.add_argument("title", nargs="?", default="", help="角色标题")
    p_start.add_argument("--no-attach", action="store_true",
                         help="后台运行，不 attach（非交互环境自动生效）")
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
                         help="工作空间名（默认同 role），设置后 tmux 在对应 ~/ccs-workspaces/<name>/ 启动")
    p_start.add_argument("--drive", default="",
                         help="驱动方式: loop / feed / ondemand（默认从 persona JSON 读取）")
    p_start.add_argument("--feed-cat", default="",
                         help="feed push 监听的 bus 分类（如 debate），实时接收新消息")
    p_start.add_argument("--route-policy", default="sticky",
                         choices=["sticky", "round-robin", "priority"],
                         help="路由策略")
    p_start.add_argument("--no-auto-send", action="store_true",
                         help="跳过 ccs_config.json 的自动发送消息")

    # ── config ──
    p_cfg = sub.add_parser("config", help="查看/修改 ccs_config.json")
    cfg_sub = p_cfg.add_subparsers(dest="config_command")
    cfg_get = cfg_sub.add_parser("get", help="读取配置值")
    cfg_get.add_argument("key", help="点号路径, 如 auto_send.interval_sec")
    cfg_set = cfg_sub.add_parser("set", help="写入配置值")
    cfg_set.add_argument("key", help="点号路径, 如 auto_send.interval_sec")
    cfg_set.add_argument("value", help="JSON 值（数字/布尔/字符串）")
    cfg_sub.add_parser("edit", help="用 $EDITOR 打开配置文件")

    # ── auto-send ──
    p_as = sub.add_parser("auto-send", help="管理 auto_send 消息")
    as_sub = p_as.add_subparsers(dest="auto_send_command")
    as_list = as_sub.add_parser("list", help="列出角色的 auto_send 消息")
    as_list.add_argument("role", nargs="?", default="", help="角色名（空=全部）")
    as_add = as_sub.add_parser("add", help="为角色添加 auto_send 消息")
    as_add.add_argument("role", help="角色名")
    as_add.add_argument("message", help="消息内容")
    as_rm = as_sub.add_parser("rm", help="删除角色的 auto_send 消息（整角色或多条）")
    as_rm.add_argument("role", help="角色名（或 default）")
    as_rm.add_argument("--index", "-i", type=int, nargs="*", default=None,
                       help="要删除的消息索引（不传则删除整个角色配置）")

    # ── roles（列出可启动角色）──
    p_roles = sub.add_parser("roles", help="列出所有可启动角色")
    p_roles.add_argument("--available", action="store_true",
                         help="只看未运行的角色")

    # ── stop ──
    p_stop = sub.add_parser("stop", help="终止 CCS")
    p_stop.add_argument("role", help="角色名")


    # ── status ──
    sub.add_parser("status", help="列出所有 CCS 状态")

    # ── send ──
    p_send = sub.add_parser("send", help="向 CCS 发消息")
    p_send.add_argument("role", help="角色名")
    p_send.add_argument("message", help="消息内容")
    p_send.add_argument("--from", dest="from_role", default="",
                        help="来源角色名（三源验证用）")

    # ── output ──
    p_out = sub.add_parser("output", help="查看 CCS 输出")
    p_out.add_argument("role", help="角色名")
    p_out.add_argument("--tail", type=int, default=20, help="行数")

    # ── stream ──
    p_stream = sub.add_parser("stream", help="流式输出 CCS 输出")
    p_stream.add_argument("role", help="角色名")
    p_stream.add_argument("--follow", action="store_true", default=True,
                          help="持续跟踪输出")
    p_stream.add_argument("--tail", type=int, default=50, help="显示最后N行")

    # ── dashboard ──
    sub.add_parser("dashboard", help="聚合健康仪表板 + 路由拓扑")

    # ── health ──
    p_health = sub.add_parser("health", help="健康检查")
    p_health.add_argument("role", nargs="?", default="",
                          help="角色名（空=全部）")

    # ── register ──
    p_reg = sub.add_parser("register", help="注册手动 tmux 为 CCS")
    p_reg.add_argument("role", help="角色名")
    p_reg.add_argument("tmux_name", help="tmux session 名")
    p_reg.add_argument("title", nargs="?", default="", help="角色标题")

    # ── workspace ──
    p_ws = sub.add_parser("workspace", help="管理工作空间")
    ws_sub = p_ws.add_subparsers(dest="ws_command")
    ws_create = ws_sub.add_parser("create", help="创建新工作空间")
    ws_create.add_argument("name", help="工作空间名（如 ccs-monitor）")
    ws_sub.add_parser("list", help="列出所有工作空间")

    # ── send-direct ──
    p_direct = sub.add_parser("send-direct",
                               help="直接发送消息（不走 bus，<1ms）")
    p_direct.add_argument("from_role", help="发送方角色")
    p_direct.add_argument("to_role", help="接收方角色")
    p_direct.add_argument("message", help="消息内容")

    # ═══════════════ 从 launcher 迁移的命令 ═══════════════

    # ── wake ──
    p_wake = sub.add_parser("wake", help="唤醒 CCS 角色")
    p_wake.add_argument("role", help="目标角色名")
    p_wake.add_argument("--by-role", required=True, help="调用方角色")
    p_wake.add_argument("--context", default="", help="唤醒附带上下文")

    # ── status-role ──
    p_sr = sub.add_parser("status-role", help="查询单个角色状态")
    p_sr.add_argument("role", help="角色名")

    # ── send-safe（自动唤醒后发送） ──
    p_ss = sub.add_parser("send-safe", help="安全发送消息（自动唤醒）")
    p_ss.add_argument("role", help="目标角色名")
    p_ss.add_argument("message", help="消息内容")
    p_ss.add_argument("--by-role", required=True, help="调用方角色")
    p_ss.add_argument("--no-auto-wake", action="store_true", help="不自动唤醒")

    # ── codex ──
    p_cdx = sub.add_parser("codex", help="Codex session 管理")
    cdx_sub = p_cdx.add_subparsers(dest="cdx_command")

    cdx_start = cdx_sub.add_parser("start", help="启动 Codex session")
    cdx_start.add_argument("role", help="角色名")

    cdx_exec = cdx_sub.add_parser("exec", help="在 Codex session 上执行任务")
    cdx_exec.add_argument("role", help="角色名")
    cdx_exec.add_argument("message", help="任务描述")
    cdx_exec.add_argument("--timeout", type=int, default=300,
                          help="超时秒数 (默认 300)")

    cdx_sub.add_parser("status", help="列出所有运行中的 Codex sessions")

    # ── reload-knowledge ──
    p_reload = sub.add_parser("reload-knowledge",
                               help="刷新角色 KNOWLEDGE 块（无需重启 CCS）")
    p_reload.add_argument("role", nargs="?", default="",
                          help="角色名（空=全部）")

    args = parser.parse_args()

    # ═══════════ 命令分发 ═══════════

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
            drive=args.drive,
            feed_cat=args.feed_cat,
            workspace=args.workspace,
            no_auto_send=args.no_auto_send,
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
        result = send(args.role, args.message, source=args.from_role)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "output":
        print(output(args.role, tail=args.tail))

    elif args.command == "stream":
        from ccs_socket import CCSStreamer

        client = CCSStreamer(args.role)
        # ponytail: CCSStreamer.start() returns None; the if/while/True/sleep
        # block was dead code.  The daemon (sister_agent_daemon.py) is the
        # real runtime; stream is only used for on-demand tailing.
        client.start(lambda chunk: print(chunk, end="", flush=True))

    elif args.command == "health":
        result = health_check(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "dashboard":
        from core import dashboard
        print(dashboard())

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

    elif args.command == "send-direct":
        from ccs_socket import CCSClient
        import asyncio

        async def do_send():
            client = CCSClient(args.from_role)
            if await client.connect():
                await client.send_to(args.to_role, args.message)
                await client.close()
                print(f"已发送: {args.from_role} -> {args.to_role}")
            else:
                print("连接失败，确保 sister_bus_ccs.sock 正在运行")
                sys.exit(1)

        asyncio.run(do_send())

    # ═══════════ 从 launcher 迁移的命令 ═══════════

    elif args.command == "wake":
        result = wake_ccs(args.role, context=args.context, by_role=args.by_role)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("success"):
            sys.exit(1)

    elif args.command == "status-role":
        from routing.partner import PartnerClient
        pc = PartnerClient("launcher")
        s = pc.resolve(args.role)
        print(json.dumps(s, ensure_ascii=False, indent=2))

    elif args.command == "send-safe":
        from routing.partner import PartnerClient
        pc = PartnerClient(args.by_role)
        result = pc.force_send(args.role, args.message,
                               auto_wake=not args.no_auto_wake)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "codex":
        if args.cdx_command == "start":
            result = start_codex_session(args.role)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.cdx_command == "exec":
            result = run_codex_task(args.role, args.message, args.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.cdx_command == "status":
            stats = cdx_status()
            if not stats:
                print("没有运行中的 Codex sessions")
            else:
                print(f"运行中的 Codex sessions: {len(stats)}")
                for s in stats:
                    uptime_m = int(s["uptime_sec"] / 60)
                    mark = "✅" if s["alive"] else "❌"
                    print(f"  [{s['role']:12}] {s['title']}  {mark}  "
                          f"运行 {uptime_m}分  pid={s['pid']}")
        else:
            print("用法: ccs.py codex {start|exec|status}")
            sys.exit(1)

    elif args.command == "reload-knowledge":
        _invalidate_role_cache()
        from pathlib import Path as _Path
        if args.role:
            role = get_role(args.role)
            if not role:
                print(f"❌ 角色 {args.role} 不存在")
                sys.exit(1)
            result = inject_role_knowledge_into_workspace(role)
            print(f"  {args.role}: {result}")
        else:
            # 全部角色
            ws_root = _Path.home() / "ccs-workspaces"
            if ws_root.exists():
                for d in sorted(ws_root.iterdir()):
                    if not (d / "CLAUDE.md").exists():
                        continue
                    name = d.name.removeprefix("ccs-")
                    role_def = get_role(name)
                    if role_def:
                        r = inject_role_knowledge_into_workspace(role_def)
                        print(f"  {d.name}: {r}")
                    else:
                        print(f"  {d.name}: 角色定义未找到，跳过")
            else:
                print("没有 workspace 目录")

    # ═══════════ config 命令 ═══════════
    elif args.command == "config":
        from ops.ccs_config import load as _cfg_load, set_value as _cfg_set, _path as _cfg_path
        if args.config_command == "get":
            v = get_config_value(args.key)
            if v is None:
                print(f"未找到: {args.key}")
                sys.exit(1)
            print(json.dumps(v, ensure_ascii=False, indent=2) if isinstance(v, (dict, list)) else v)
        elif args.config_command == "set":
            try:
                parsed = json.loads(args.value)
            except (json.JSONDecodeError, TypeError):
                parsed = args.value  # 字符串原值
            result = _cfg_set(args.key, parsed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("success"):
                sys.exit(1)
        elif args.config_command == "edit":
            editor = os.environ.get("EDITOR", "vim")
            p = _cfg_path()
            os.execvp(editor, [editor, str(p)])
        else:
            print("用法: ccs.py config {get|set|edit}")
            sys.exit(1)

    # ═══════════ auto-send 命令 ═══════════
    elif args.command == "auto-send":
        from ops.ccs_config import (
            add_auto_send_message, remove_auto_send_message,
            get_auto_send_messages, load as _cfg_load2,
        )
        if args.auto_send_command == "list":
            cfg = _cfg_load2()
            roles_map = cfg.get("auto_send", {}).get("messages", {}).get("roles", {})
            default_msgs = cfg.get("auto_send", {}).get("messages", {}).get("default", [])
            if args.role:
                msgs = get_auto_send_messages(args.role)
                if not msgs:
                    print(f"角色 {args.role} 没有 auto_send 消息")
                else:
                    role_raw = roles_map.get(args.role, [])
                    for i, m in enumerate(msgs):
                        src = "default" if m in default_msgs and m not in role_raw else "role"
                        print(f"  [{i}] {m} ({src})")
            else:
                print(f"default ({len(default_msgs)} 条):")
                for i, m in enumerate(default_msgs):
                    print(f"  [{i}] {m}")
                for role, msgs in sorted(roles_map.items()):
                    if role == "default":
                        continue  # default 已在上面单独显示
                    print(f"\n{role} ({len(msgs)} 条):")
                    for i, m in enumerate(msgs):
                        print(f"  [{i}] {m}")
        elif args.auto_send_command == "add":
            result = add_auto_send_message(args.role, args.message)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.auto_send_command == "rm":
            result = remove_auto_send_message(args.role, args.index)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("success"):
                sys.exit(1)
        else:
            print("用法: ccs.py auto-send {list|add|rm}")
            sys.exit(1)

    # ═══════════ roles 命令 ═══════════
    elif args.command == "roles":
        roles = list_roles()
        if not roles:
            print("没有找到角色定义")
            sys.exit(1)
        if args.available:
            roles = [r for r in roles if not r["alive"]]
        print(f"共 {len(roles)} 个角色{'（仅未运行）' if args.available else ''}")
        print(f"  {'名称':<20} {'标题':<18} {'分类':<8} {'状态':<8} {'生命周期':<10}")
        print(f"  {'─'*68}")
        for r in roles:
            status_mark = "▶ 运行中" if r["alive"] else "○ 就绪"
            print(f"  {r['name']:<20} {r['title']:<18} {r['category']:<8} "
                  f"{status_mark:<8} {r['lifecycle']:<10}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
