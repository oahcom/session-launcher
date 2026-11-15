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
    start, resolve_start_instance_id, stop, status, send, output, health_check, register,
    workspace_create, workspace_list, wake_ccs,
    start_codex_session, run_codex_task, cdx_status,
    get_role, inject_role_knowledge_into_workspace, _invalidate_role_cache,
    list_roles, get_config_value, StartOptions,
    ENGINE_DOMAIN, _engine_with_source, _engine_pid_and_source,
)
from tmux_ops import _restart_if_stale, make_tmux_name


def main():
    parser = argparse.ArgumentParser(
        description="CCS 生命周期管理器 — 内置协作基础设施",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
新增命令:
  config get/set/edit    查看/修改 ccs_config.json（全局配置中心）
  auto-send list/add/rm  管理角色的自动发送消息
  roles [--available]    列出所有可启动角色

协作模式（已废弃，由 workflow 引擎取代）:
  # 协作能力（伙伴守护/轮次追踪/返工门禁/循环审计）已全部收编到
  # session-pipeline pipeflow 引擎：
  #   - 角色存活守护 → 引擎每 tick _ensure_role_alive 自动拉起
  #   - bus 轮次追踪   → 模板 exit_condition 精确匹配推进
  #   - 超时防死锁     → timeout_count 重推 + coordinator 升级 + auto-cancel
  #   - 返工门禁       → review 步骤被拒 → 自动创建 rework subflow
  #   - 循环审计       → loop.producer_roles + value_gate
  # 使用方式：定义 workflow 模板（~/.hermes/workflows/*.json），
  # 见 session-pipeline/docs/collab-modes-implementation.md
  #
  # 旧参数（--partner/--bus-track/--auto-restart）保留兼容但不再推荐，
  # 守护线程随 CLI 进程退出失效，请改用 workflow 模板。

会话承载引擎（--engine / persona.engine）:
  ccs         claude --model 9router_hermes（存量 persona 全部是它）
  opencode    ~/.opencode/bin/opencode（缺省引擎；原生直连 deepseek-v4.1-flash-free，
              出口走 asip 第八闸口 7898；撞 429 自动换 IP + 重生续发）
  codex       本期未接入主启动面（孤岛入口：ccs.py codex start）
  声明优先级：--engine 按次覆盖 > persona 的 engine 字段 > 缺省 opencode。
  不记名角色（无 persona 或裸 ccs start）默认 opencode 承载、无 persona 权限/MCP 限制。
  重启只读哨兵事实源 ~/.hermes/run/ccs-sentinels/<role>.json，不回读
  persona —— 所以重启/陈旧拉起都不会翻转引擎。
  示例:
  ccs.py start engineer --engine opencode --no-attach   # opencode 承载
  ccs.py start engineer --prompt "开工"                 # persona 默认引擎（ccs）
  ccs.py status-role engineer        # 单角色 JSON（含 engine/engine_source）

启动优化:
  ccs.py start <role>              启动 CCS 后自动发送 ccs_config.json 配置的消息
  ccs.py start <role> --no-auto-send  跳过自动发送（只保留角色 prompt）
""")
    sub = parser.add_subparsers(dest="command")

    # ── start ──
    p_start = sub.add_parser(
        "start", help="创建并启动 CCS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  ccs.py start                                  # 不记名会话（每次开新实例，自动编号，opencode 承载）
  ccs.py start engineer                          # persona 默认引擎（ccs）
  ccs.py start engineer --engine opencode        # 强制 opencode 承载
  ccs.py start engineer --engine opencode --no-attach --prompt "开工"
  ccs.py start engineer --instance-id 1          # 扩展实例
  ccs.py start engineer --dry-run                # 只打印将执行的命令

引擎说明:
  --engine {ccs,opencode,codex}  会话承载引擎，按次覆盖（优先级最高）；
  未指定取 persona.engine，再缺省 opencode。codex 本期拒绝（rc=1），
  走孤岛入口 ccs.py codex start。重启只读哨兵，不会翻转引擎。
  opencode 会话出口走 asip 第八闸口（HTTPS_PROXY 注入，429 自动换 IP）。""")
    p_start.add_argument("role", nargs="?", default="anon",
                         help="角色名（自动加 ccs- 前缀）；缺省 anon=不记名会话"
                              "（每次 start 自动分配新实例：ccs-anon / ccs-anon-1 / …）")
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
                         help="驱动配置: cron / goal / infinite / ondemand（默认从 persona JSON 读取）")
    p_start.add_argument("--no-auto-send", action="store_true",
                         help="跳过 ccs_config.json 的自动发送消息")
    p_start.add_argument("--instance-id", type=int, default=None,
                         help="实例编号: 0=主实例, >0=扩展实例；缺省=注册角色取 0，"
                              "不记名角色自动分配最小空闲实例（每次 start 开新实例）")
    p_start.add_argument("--instances", type=int, default=0,
                         help="批量启动 N 个实例（从 1 到 N），仅 detach 模式")
    p_start.add_argument("--dry-run", action="store_true",
                         help="只打印将执行的 tmux 命令/参数/路径，不实际执行；返回结构化 JSON (dry_run=true, planned_actions[])")
    p_start.add_argument("--engine", default=None, choices=list(ENGINE_DOMAIN),
                         help="会话承载引擎（按次覆盖，优先级最高；未指定时取 persona.engine，"
                              "再缺省 opencode。codex 本期未接入主启动面）")

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
    p_stop.add_argument("--instance-id", type=int, default=0,
                        help="实例编号（默认 0=主实例）")
    p_stop.add_argument("--dry-run", action="store_true",
                         help="只打印将执行的操作，不实际执行")


    # ── status ──
    sub.add_parser("status", help="列出所有 CCS 状态")

    # ── send ──
    p_send = sub.add_parser("send", help="向 CCS 发消息")
    p_send.add_argument("role", help="角色名")
    p_send.add_argument("message", help="消息内容")
    # 默认来源：优先 $CCS_ROLE（launcher 启动时注入，见 core.py "-e CCS_ROLE=<role>"）。
    # 角色 prompt 教的用法是不带 --from 的裸 `ccs send <角色> 消息`
    # （ccs-workspaces/*/CLAUDE.md），旧默认 "cli" 会让**角色发出的消息被
    # 当成人类请求**：user_intent_reminder 据此注入提醒 + 写痕迹，而 bus 对同内容
    # UPSERT 刷新 ts → 痕迹永不过期 → 被提醒方反复空转（实测单条 fact 重写 276 次）。
    # 未设 CCS_ROLE 时才是真人类手敲 CLI，保留 "cli"。
    p_send.add_argument("--from", dest="from_role",
                        default=os.getenv("CCS_ROLE") or "cli",
                        help="来源角色名（默认 $CCS_ROLE，未设则 cli）")
    p_send.add_argument("--instance-id", type=int, default=0,
                        help="实例编号（默认 0=主实例）")
    p_send.add_argument("--dry-run", action="store_true",
                         help="只打印将执行的操作，不实际执行")

    # ── task ──
    p_task = sub.add_parser("task", help="接收任务并打印执行计划")
    p_task.add_argument("description", help="任务描述")

    # ── output ──
    p_out = sub.add_parser("output", help="查看 CCS 输出")
    p_out.add_argument("role", help="角色名")
    p_out.add_argument("--tail", type=int, default=20, help="行数")
    p_out.add_argument("--instance-id", type=int, default=0,
                        help="实例编号（默认 0=主实例）")

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
    # ── p0draft ── (R70 Round 4: grow P0_draft ladder — mark_p0_draft 入口)
    p_p0 = sub.add_parser("p0draft", help="标注 P0_draft 任务 (engineer/pg/qa/devops/reviewer/scout/writer)")
    p_p0.add_argument("task_id", help="任务 ID")
    p_p0.add_argument("reason", help="升级理由 (≥15 字符，填写为什么这个任务是 P0)")

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

    # ── reap ──
    p_reap = sub.add_parser("reap", help="唤起有定向派单但 pane 不在的角色")
    p_reap.add_argument("--dry-run", action="store_true", help="只报告不唤醒")

    # ── status-role ──
    p_sr = sub.add_parser(
        "status-role", help="查询单个角色状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""输出 JSON 字段：
  running/alive      会话是否在跑（tmux 存活）
  pid/pid_source     引擎进程 pid 与其来源（sentinel|tmux|fallback）
  engine             承载引擎 ccs|opencode|codex|unknown
  engine_source      引擎身份的来源（sentinel=事实源；tmux-prefix-fallback=推断值，
                     推断值不得冒充事实，评估面按显式降级处置）
  tmux_session/pid   宿主 tmux 会话与 pane pid
示例: ccs.py status-role engineer""")
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
    p_reload.add_argument("--restart-idle", action="store_true", help="变了且空闲则重启 claude 进程")

    args = parser.parse_args()

    # 单测调 ccs = 往真角色 pane 投递 + 由 ccs 写生产 workflows.db（实测 scout 1h 收 28
    # 条虚构实例告警，转录累计 169 处）。用 pytest 自设的 env 判定，与测试树是否隔离、
    # 是否旧 worktree 无关；rc=0 保持调用方的「已送达」语义，测试自洽。
    if os.environ.get("PYTEST_CURRENT_TEST") and args.command in (
            "start", "stop", "send", "send-safe", "send-direct", "wake",
            "reap", "reload-knowledge"):
        print(json.dumps({"success": True, "reason": "suppressed-under-pytest"}))
        return

    # ═══════════ 命令分发 ═══════════

    if args.command == "start":
        if args.dry_run:
            inst, inst_err = resolve_start_instance_id(args.role, args.instance_id)
            if inst_err:
                print(json.dumps({"success": False, "error": inst_err}, ensure_ascii=False))
                sys.exit(1)
            result = start(StartOptions(
                role=args.role,
                title=args.title,
                detach=args.no_attach,
                init_prompt=args.prompt,
                partners=args.partner,
                auto_restart=args.auto_restart,
                bus_track=args.bus_track,
                bus_timeout=args.bus_timeout,
                drive=args.drive,
                workspace=args.workspace,
                no_auto_send=args.no_auto_send,
                instance_id=inst,
                dry_run=True,
                engine=args.engine,
            ))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # 预检失败（如 engine=codex 显式拒绝/取值非法）必须 rc≠0：此前 dry-run
            # 分支无条件退出 0，预检脚本只看退出码会把"不能起"当成"可以起"。
            if not result.get("success"):
                sys.exit(1)
        elif args.instances > 0:
            # 批量启动 N 个实例
            results = []
            for i in range(1, args.instances + 1):
                result = start(StartOptions(
                    role=args.role,
                    title=f"{args.title or args.role}#{i}",
                    detach=True,
                    init_prompt=args.prompt,
                    partners=args.partner,
                    auto_restart=args.auto_restart,
                    bus_track=args.bus_track,
                    bus_timeout=args.bus_timeout,
                    drive=args.drive,
                    workspace=args.workspace,
                    no_auto_send=args.no_auto_send,
                    instance_id=i,
                    engine=args.engine,
                ))
                results.append(result)
                if not result.get("success"):
                    print(f"⚠ 实例 {i} 启动失败: {result.get('error')}")
            print(json.dumps(results, ensure_ascii=False, indent=2))
            successes = sum(1 for r in results if r.get("success"))
            print(f"启动 {successes}/{args.instances} 个实例")
            # 批量启动部分失败也必须 rc≠0：此前恒退出 0，哨兵写入失败/就绪失配
            # 被"启动 N/M"一行吞掉，调用方（脚本/systemd）以为全成功。
            if successes < args.instances:
                sys.exit(1)
        else:
            inst, inst_err = resolve_start_instance_id(args.role, args.instance_id)
            if inst_err:
                print(json.dumps({"success": False, "error": inst_err}, ensure_ascii=False))
                sys.exit(1)
            result = start(StartOptions(
                role=args.role,
                title=args.title,
                detach=args.no_attach,
                init_prompt=args.prompt,
                partners=args.partner,
                auto_restart=args.auto_restart,
                bus_track=args.bus_track,
                bus_timeout=args.bus_timeout,
                drive=args.drive,
                workspace=args.workspace,
                no_auto_send=args.no_auto_send,
                instance_id=inst,
                engine=args.engine,
            ))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("success"):
                sys.exit(1)

    elif args.command == "stop":
        if args.dry_run:
            result = stop(args.role, instance_id=args.instance_id, dry_run=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = stop(args.role, instance_id=args.instance_id)
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
                inst = f"[{s['instance_id']}]" if s.get("instance_id") else ""
                alive = s['alive']
                print(f"  [{s['role']:12}{inst}] {s['title']}  "
                      f"{'✅' if alive else '❌'}  "
                      f"运行 {uptime_m}分  pid={s['pid']}  "
                      f"partner={s['partner'] or '-'}  bus={s['bus_track'] or '-'}  "
                      f"engine={s['engine']}({s['engine_source']})")
                if not health.get("watchdog_measured", True):
                    wd = "—"   # 从未测量 ≠ 健康。显示 ✅ 会把默认值当成检查结果。
                else:
                    wd = "✅" if health["watchdog_ok"] else "❌"
                print(f"      health: watchdog={wd} "
                      f"bus_age={health['bus_msg_age']}s restarts={health['restart_count']}")

    elif args.command == "send":
        if args.dry_run:
            result = send(args.role, args.message, source=args.from_role,
                          instance_id=args.instance_id, dry_run=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = send(args.role, args.message, source=args.from_role,
                          instance_id=args.instance_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result.get("success"):
                sys.exit(1)

    elif args.command == "task":
        from task_runner import run_task
        sys.exit(run_task(args.description))

    elif args.command == "output":
        print(output(args.role, tail=args.tail, instance_id=args.instance_id))

    elif args.command == "health":
        print(json.dumps(health_check(args.role), ensure_ascii=False, indent=2))

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
                    print(f"  {ws['role']:20} {ws['path']}")
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

    elif args.command == "reap":
        from role_reaper import reap
        result = reap(dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(r.get("action") == "start_failed" for r in result):
            sys.exit(1)  # 让 systemd 把「起不来」记成 failed，而不是每 15 分钟绿一次

    elif args.command == "status-role":
        from routing.partner import PartnerClient
        pc = PartnerClient("launcher")
        s = pc.resolve(args.role)
        # T1A 裁决 B：engine 灌进 JSON 数据面（D 轨取数口）。
        # routing/partner.py 是黑名单文件不动，在此按事实源合并 engine/engine_source：
        # 哨兵显式键="sentinel"；读不到按前缀反推兜底并标注，不静默编造。
        _eng, _eng_src = _engine_with_source(args.role)
        s["engine"] = _eng
        s["engine_source"] = _eng_src
        # T1A-fix：pid 必须是引擎进程 pid。PartnerClient.resolve 的 _find_pid 默认
        # 按 process_name="claude" 查（routing/partner.py 是黑名单不动的既有行为），
        # opencode 会话查不到 claude 子进程 → 回落 pane_pid = wrapper bash
        # （实测 status_role: pid=2532697 bash，opencode 实为 2532698）。此处用注册表
        # comm 精确查找覆盖；取不到时保留原值并标注 pid_source，不静默冒充。
        if s.get("alive"):
            _pid, _pid_src = _engine_pid_and_source(
                make_tmux_name(args.role, 0), _eng)
            if _pid is None:
                _pid, _pid_src = s.get("pid"), "pane-fallback"
            s["pid"] = _pid
            s["pid_source"] = _pid_src
        else:
            s["pid_source"] = "sentinel"
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
            if args.restart_idle: _restart_if_stale(f"ccs-{args.role}", _Path.home()/"ccs-workspaces"/args.role/"CLAUDE.md")
        else:
            # 全部角色
            ws_root = _Path.home() / "ccs-workspaces"
            if ws_root.exists():
                for d in sorted(ws_root.iterdir()):
                    if not (d / "CLAUDE.md").exists():
                        continue
                    # 目录名可能就是角色名（ccs-monitor），直接剥前缀会查不到。
                    role_def = get_role(d.name) or get_role(d.name.removeprefix("ccs-"))
                    if role_def:
                        r = inject_role_knowledge_into_workspace(role_def)
                        print(f"  {d.name}: {r}")
                        if args.restart_idle: _restart_if_stale(f"ccs-{d.name}", d / "CLAUDE.md")
                    else:
                        print(f"  {d.name}: 角色定义未找到，跳过")
            else:
                print("没有 workspace 目录")

    # ═══════════ config 命令 ═══════════
    elif args.command == "config":
        from ops.ccs_config import set_value as _cfg_set, _path as _cfg_path
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

    # ═══════════ p0draft 命令 ═══════════
    # R70 Round 4: grow dead P0 ladder API — wire mark_p0_draft into ccs CLI
    elif args.command == "p0draft":
        try:
            # 动态導入，避免把 pipeline 依賴拉到 ccs.py 模組級
            import sys as _sys
            _pipe_src = os.path.realpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "session-pipeline", "src"))
            if _pipe_src not in _sys.path:
                _sys.path.insert(0, _pipe_src)
            from p0_exemption import P0Exemption  # type: ignore
            p0 = P0Exemption(role="ccs")  # 由 CCS CLI 觸發，role 標記為 ccs
            result = p0.mark_p0_draft(args.task_id, args.reason, role="ccs")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("p0_state") != "draft":
                sys.exit(1)
        except PermissionError as e:
            print(f"P0_draft 权限拒绝: {e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            print(f"P0_draft 标记失败: {e}", file=sys.stderr)
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
