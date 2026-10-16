# session-launcher 问题单

审查时间：2026-10-16 | HEAD：`90db336` | 测试基线：`pytest tests/ -q` → 341 passed

标注说明：**[已验证]** = 本机实际执行复现；**[代码确认]** = 读码确认逻辑缺陷，未构造运行时复现。

---

## L1 [P0] feed_listener 注入的 `Enter` 被当作字面文本，消息永不提交 [已验证]

- 位置：`feed_listener.py:94`
- 证据：
  ```python
  ["tmux", "send-keys", "-l", "-t", tmux_target, payload, "Enter"],
  ```
  `-l` 关闭键名解析（tmux 3.4 man：*"The -l flag disables key name lookup and processes the keys as literal UTF-8 characters"*），`"Enter"` 因此被当作 5 个字符输入。
  隔离 socket 实测：`send-keys -l -t p 'PRE' 'Enter'` → 捕获 flushe 前为空；补发真 `send-keys -t p Enter` 后捕获到 `PREEnter$`（`Enter` 是字面文本，真回车在末尾）。
- 失败场景：所有 feed 消息堆在 CLI 输入行不提交，字面 `Enter` 污染输入行并粘到下一条消息前。同类正确写法已在 `src/tmux_ops.py:250-253`（分两次调用）。
- 逃逸原因：`tests/unit/test_tmux_ops.py:113-116` 只断言 `-l` 存在，不断言 Enter。
- 修复方向：拆成两次调用 —— `-l` 只发 payload，再单独 `send-keys -t <target> Enter`。

## L2 [P0] 门禁矩阵用 persona 覆盖内置 `"*"` 通配，coordinator 无法向多数角色发红消息 [已验证]

- 位置：`src/routing/gatekeeper.py:101,121,215`；触发源 `personas/session-roles/persona_04_coordinator.json` 的 `workgroup`
- 证据：`matrix = {"coordinator": {"*"}}` 之后 `if roles: matrix[name] = roles` **无条件覆盖**。实测：
  ```
  PYTHONPATH=src python3 -c "import routing.gatekeeper as g; print(g.WORKGROUP_MATRIX['coordinator'])"
  → {'pm', 'pg', 'devops', 'lr'}
  ```
  而 coordinator persona 声明的 `collab.track.notify_roles=["closer"]`、`guard.watch=["pm","engineer"]` 里，closer 与 engineer 均不在该集合内。
- 失败场景：coordinator 向 engineer/qa/reviewer/writer/curator/scout 发任一条含"分配/决策/审批"的红消息 → 被 `:215` 判 `not in workgroup matrix, red message blocked`。角色按自身契约发通知也被自己的门禁拒。
- 修复方向：`matrix.setdefault(name, roles)` 保留内置通配；或在 persona workgroup 显式加入 `"*"`。

## L3 [P1] `_tmux_send` 吞掉全部异常且无日志，`send()` 仍回报成功 [已验证]

- 位置：`src/tmux_ops.py:256-258`
- 证据：`except Exception:` → `if attempt < retries - 1: time.sleep(retry_delay)`，函数末尾无 `raise`/`return`/日志。
  桩测（`send-keys` 恒 rc=1、`has-session` 正常）→ `_tmux_send` 静默返回 None，`core.send()` 随即 `_reset_send_fail_streak(role)` 并返回 `{'success': True, 'sent_chars': 11}`。
- 失败场景：投递实际丢弃，调用方（pipeline / partner / 引擎）以为成功，3 连败 fallback 永不触发。
- 修复方向：超限后 `return False` 或 raise，由 `core.send` 转成 `success: False`。

## L4 [P1] ondemand 哨兵 `tmux_session=""` 使 `_is_alive("")` 恒真，健康检查谎报存活 [已验证]

- 位置：`src/core.py:342`（建哨兵 `tmux_session=""`）、`:912`（`alive = _is_alive(s.tmux_session)`）
- 证据：`tmux has-session -t ""` 在 tmux server 存活时 rc=0。实测写入 ondemand 哨兵后 `health_check("zz_audit_probe")` → `"alive": true`，同刻 `tmux has-session ccs-zz_audit_probe` → False。
- 失败场景：`ccs health <role>` 把从未启动的 ondemand 角色报成存活。（`status`/`dashboard` 走 `list_sentinels` 且被 `tmux_session or pid>0` 过滤，不受影响。）
- 修复方向：`_is_alive` 对空串直接 `return False`。

## L5 [P1] feed_listener 跨进程不回收 + 单元 `KillMode=process`，孤儿进程常驻 [已验证]

- 位置：`src/core.py:1003-1007`（启动）、`:729-736`（stop 只 pop 本进程 `_FEED_PROCS`）；`/etc/systemd/system/session-pipeline.service:19`
- 证据：
  - `_FEED_PROCS` 是**进程内** dict（`core.py:8`），而 `ccs.py start` 与 `ccs.py stop` 是两次独立进程调用 → stop 时字典为空，回收永不生效。
  - 实测 `ps` → **9 个 PPID=1 的 feed_listener**，`tmux ls` → **8 个 session**。多出的 `ccs-closer`（pid 303555，存活 5h41m）目标会话早已不存在，仍持续重连。
  - `systemctl show session-pipeline.service -p KillMode` → `process`（只杀主 PID）。systemd journal 今日记录 **12 次** `Unit process ... remains running after unit stopped`（459586 / 504368 各 6 次），systemd 仅 "Ignoring"。
- 失败场景：当前 9 个 listener 中，`ccs-closer` 那个（存活 5h41m）目标会话已不存在，仍持续重连 —— 这是**唯一已观测到后果**的一条。459586/504368 两个孤儿（PPID=1）的目标会话 `ccs-coordinator`/`ccs-product_architect` 仍存活，**未证实**它们会随时间累积成同一 target 的重复监听者（同日实测每个 target 恰好 1 个）—— 风险是真实的，因果链未闭合。
- 修复方向：stop 同时按 `~/.hermes/run/feed_listeners/<tmux>.pid` 读 PID 并 kill（顺带解决 L1：`ccs-closer` 目标消失后仍以 L1 的字面 `Enter` 持续尝试注入）；单元 `KillMode=control-group` 见 `session-pipeline/ISSUES.md` G11。

## L6 [P2] 门禁速率限制把绿/黄消息一并计入，挤爆合法红消息额度 [已验证]

- 位置：`src/routing/gatekeeper.py:224-229`（统计）、`:189`（每条无条件落库）
- 证据：`SELECT COUNT(*) FROM workflow_logs WHERE actor=? AND action='cross_role_send' AND ts>?` → `if count > SENSITIVE_RATE_LIMIT`。实测预置 6 条 `cross_role_send` 后，合法红消息 `intercept('engineer','qa',...)` 直接返回 False。
- 失败场景：docstring 声称统计"🔴 消息"，实际统计全部跨角色流量。正常协作量即可耗尽敏感消息额度。
- 修复方向：detail 记录敏感度，SQL 按 red 过滤。

## L7 [P2] `_check_bus_source` 的 `"partial"` 被当作验证通过，三源验证可凑数 [代码确认]

- 位置：`src/routing/gatekeeper.py:256-259`、`:310-312`
- 证据：`if bus_src: matches += 1`，而 `_check_bus_source` 在"搜到结果但不含 claimed_source"时 `return "partial"`。
- 失败场景：任何 bus 搜索命中（哪怕与声称来源无关）+ 一个同名 tmux 会话即达 2 源 → 伪造来源放行。
- 修复方向：仅 `bus_src == "matched"` 时 `matches += 1`。

## L8 [P2] `send` 连败门槛以 role 为键，忽略 instance_id [已验证]

- 位置：`src/core.py:808`、`:85-91`
- 证据：`_inc_send_fail_streak(role)`（错误信息却打印 `{role}[{instance_id}]`），持久化文件键只有 `{role}`。
- 失败场景：实例 1 的三次探测失败消耗主实例 0 的额度，主实例首次抖动即被判"3 连败确认"并写 `[ccs_send_fallback]`（实测 3 次 `engineer[1]` 后 streak 归零并写 fallback）。
- 修复方向：键改 `f"{role}[{instance_id}]"`。

## L9 [P2] 红线 1 拦截通知的 bus 写入失败被静默吞掉 [代码确认]

- 位置：`src/core.py:830-837`
- 证据：`subprocess.run([... "security", "[gateway_guard] 拦截 ..."])` 外层 `except Exception: pass`。
- 失败场景：`BUS_CLIENT` 缺失或 bus 故障时，拦截 gateway 重启指令这一安全事件不留任何痕迹。
- 修复方向：`except` 内至少 `_log.error`。

## L10 [P2] 文档/自检入口指向已删除模块与不存在的参数 [已验证]

- 位置：`AGENTS.md:46`、`CLAUDE.md:55`、`CLAUDE.md:78`
- 证据：`role_manager` 与 `src/ops/mcp_settings.py` 均已删除（`ls src/ops/` 无此文件）；`ecosystem_health.py --check` → `error: unrecognized arguments: --check`（真实参数为 `--json`/`--ci`）；`CLAUDE.md:78` 的 `sentinel` 模块亦不存在。
- 失败场景：照抄文档自检的操作者拿到 ImportError / argparse 报错。
- 修复方向：删 `role_manager`、`sentinel`、`mcp_settings.py` 行；`--check` 改 `--json`。

---

## 跨项目

- **L2** 的触发源在 hermes-session-roles（persona workgroup），两处均可修，见 `~/hermes-session-roles/ISSUES.md` R6。
- **L5** 的单元 `KillMode=process` 属 session-pipeline 部署配置。
