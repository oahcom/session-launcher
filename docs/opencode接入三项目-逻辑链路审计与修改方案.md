# OpenCode 接入 Session 三项目 — 逻辑链路审计与修改方案（对齐稿，未动手）

> 范围：hermes-session-roles（定义层）/ session-launcher（执行层）/ session-pipeline（路由层）
> 证据环境：WSL Ubuntu（/home/administrator/），实测时间 2026-11-13
> 性质：先对齐再动手的分析稿。只讲逻辑与证据，不贴代码；修改方案按「最根本 → 最表层」排序，每条给出为何影响、如何影响、为何能解决。

---

## 一、任务的精确化

任务表述是「把 opencode 接入三项目，与 claude 同级，后续可以选择不同 agent 启动」。拆开后是三个可检验的要求：

1. **同级**：opencode 不是外挂工具（现状 codex 那种独立子命令、独立形态的孤岛），而是走与 claude 完全相同的生命周期——启动、判活、投递、重启、上下文注入、状态可观测，六条链全部可用。
2. **可选择**：启动时能决定本次会话由哪个引擎承载（claude 或 opencode），且这个选择是持久化的——任何自动重启路径拉起的必须仍是原来那个引擎，不允许翻转。
3. **接的是三项目**：不只是 launcher 一家的事。定义层（persona）要能声明默认引擎，执行层要能分派，路由层（pipeline 的拉起/投递/存活统计）不能把非 claude 会话漏掉或误杀。

一个必须先澄清的术语歧义：「agent」在本任务里指**引擎级**（claude CLI / opencode CLI 这两种承载进程的程序）；而 opencode 自己内部还有 `--agent` 参数和 `opencode agent` 子命令（build/plan 等内置角色），那是 opencode 的**内层概念**，与本任务的引擎选择是两个层级。本方案一律用「引擎（engine）」称呼前者。

---

## 二、现状逻辑链路（六条链，逐条给证据）

### 链 A：启动链（进程出生的地方）

入口是 ccs.py 的 start 子命令 → core.start()。StartOptions（core.py:144）封装启动参数，**当前没有任何引擎字段**——这条链天生只会产出 claude 会话。流程为：

准备工作区（创建 ~/ccs-workspaces/<role>）→ 角色知识注入（写 workspace 的 CLAUDE.md）→ 写 .claude/settings.json（MCP 与权限隔离）→ 启动 tmux 会话。

启动 tmux 的实现在 core.py:625 的 _launch_tmux。它构造一条 shell 包裹脚本（wrapper）：一个 while-true 死循环里反复拉起 `claude --model 9router_hermes` 加工具集、权限旗标、effort 参数，进程退出后按存活时长退避重拉。claude 这个**进程名字面量**烤死在命令行里；模型网关环境变量（ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY，来自 ~/.claude/9router-env.sh，core.py:541）以 tmux 环境变量注入，并在启动后做「进程环境必须带齐这两个变量」的断言（core.py:759）——因为缺了它们 claude 会静默卡在登录页而不是报错。

启动后的就绪探测（core.py:728）轮询 pane 输出里是否出现 Claude TUI 的输入提示符「❯」或 claude ready 字样。

**为何影响**：这是唯一创造进程身份的地方。argv、环境变量、就绪判据全部是 claude 形状，任何引擎替换都必须从这里分派，否则后面五条链全部在为错误的进程服务。

### 链 B：消息投递链（外部怎么把任务喂给角色）

路由层的 role_lifecycle 把工作流步骤的 prompt 通过子进程调用 `ccs.py send <role> <prompt>`（role_lifecycle.py:884、1113）——两项目之间没有直接 API，只经 CLI 子进程。进入 core.send() 后依次过：跨角色三源验证与敏感命令门禁、gateway 重启红线拦截、人类来源的意图提醒注入，然后交给 tmux_ops 的投递原语。

投递原语（tmux_ops.py:421 起）不是裸写入，而是一套 Claude TUI 协议：先读 pane 尾部 8 行判忙（tmux_ops.py:241，判据是状态栏文案「esc to interrupt」或「Retrying attempt」——实测标定过，只在生成期出现，空闲期为零匹配）；闲则发 C-u 清输入行；再按 500 字符分块粘贴文本；发 Enter 提交；最后补一个提交确认环节（tmux_ops.py:349）——从输入框里「最后两条横线之间」的区域（composer，tmux_ops.py:334）取文本，判断消息是否已离开输入框，滞留则补 Enter。

**为何影响**：整条投递的正确性建立在「pane 里跑的是 Claude TUI」这个假设上。忙判据、输入框几何、提交判据三样都是 Claude 形状。假设破裂的后果有实证：生成中被投递的消息会被当成当前回合输入、覆盖在飞推理（2026-10-20 closer 实测：9 分 58 秒推理中途收到新任务，原审计结论丢失）；未提交的消息会滞留输入框（archivist 启动消息滞留 25 分钟）。

### 链 C：判活链（怎么知道进程真的活着）

三个项目各有一层判活，全部以 claude 进程名为锚：

- launcher 内 watchdog 的双检（ops/watchdog.py:34）：tmux 会话存在 **且** pane 子进程里 pgrep 得到名字含 claude 的进程。历史教训让它必须双检——wrapper 的 while 循环常驻会让 pane_pid 恒活，closer 曾因 PATH 缺失每 3 秒报 `claude: command not found` 长达 1.4 小时而无人察觉（R41）。
- launcher 的生态健康检查（ecosystem_health.py:239 _pane_has_claude）：遍历 pane 的后代进程，在 /proc/<pid>/cmdline 里找 claude 字样，找不到即判定为「僵尸会话」（pane 活但 agent 未启动）。
- launcher 的陈旧重启（tmux_ops.py:308 _restart_if_stale）：要求进程 comm（内核记录的可执行名）严格等于 claude。
- 路由层的存活快照（liveness.py:86 tmux_snapshot）：一次 tmux list-panes 拿全部 pane，**只收会话名以 ccs- 开头的行**，剥掉前缀当角色名；role_lifecycle._is_agent_alive（role_lifecycle.py:605）据此判活，死后由 _check_and_start_role 调 `ccs.py start` 拉起（role_lifecycle.py:690）。

**为何影响**：判活是自愈系统的感官。感官里写着 claude，opencode 会话就会被系统性误判为死/僵尸，然后触发自动重启——而重启路径拉的是 claude（链 A），于是发生**引擎翻转**：一个 opencode 角色被「救活」成 claude 角色，身份、上下文、模型配置全部错位，且无告警。

### 链 D：上下文注入链（角色的专业知识怎么进脑子）

hermes-session-roles 的 persona JSON（实测 19 个文件，键含 prompt_refs / mcp_servers / permissions / lifecycle / drive 等，**无引擎字段**）经 roles.py 的 inject_role_knowledge_into_workspace（roles.py:420）把角色契约与 base.md 通用红线写进 workspace 的 **CLAUDE.md**（带 WORKSPACE_SYS 标记块）；MCP 与权限写进 workspace 的 **.claude/settings.json**（core.py:246）；技能靠 ~/.claude/skills/ 目录被 Claude Code 启动时自动扫描。CLAUDE.md 的机制是 Claude Code 的「工作目录指令文件自动发现」：进程启动时自动读入 cwd 下的该文件作为系统级指令。

**为何影响**：整条「上下文隔离、互不污染」的项目核心价值，物理上落在 CLAUDE.md 这个**claude 约定的文件名**上。实测（本次探针：同目录放置 AGENTS.md 与 CLAUDE.md 两份不同标记词的指令文件，让 opencode 回答「你的工作区指令文件里写了什么标记词」）opencode 只复述了 **AGENTS.md** 的标记词——opencode 的工作区指令文件是 AGENTS.md 约定，不是 CLAUDE.md。若照搬现状，opencode 角色会**空脑运行**：进程活着、消息进得去，但角色契约、红线、经验教训一样都读不到，产出质量塌方且无报错。

### 链 E：观测与评估链（事后怎么审计产出）

路由层的 transcript 解析（role_lifecycle.py:62 _TRANSCRIPT_ROOT）、评估器（eval_checker.py:32）、工作流生命周期（wf_lifecycle.py:188）、意图分类器（ccls_daemon_intent_classifier.py:128）都从 **~/.claude/projects/-home-administrator-ccs-workspaces-<role>** 读会话转录。这是 Claude Code 自己的会话落盘目录。

**为何影响**：评估与审计体系按 claude 的落盘布局取数。opencode 的会话痕迹存在它自己的存储（~/.opencode/ 下 opencode.db 等）。不接通则 opencode 会话的产出在评估面**静默缺席**——不是评了差分，而是根本没被评，形成假阴性。

### 链 F：既有第二引擎（codex）的真实位置——重验旧结论

哨兵数据结构（ops/sentinel.py:78）有 engine 字段，默认 "ccs"，codex 路径写 "codex"；tmux 会话名前缀有两套（ccs- 与 cdx-，tmux_ops.py:60/62）；codex 有独立模块 codex_ops.py 与独立子命令 `ccs.py codex start/exec/status`；进程查找函数已参数化进程名（tmux_ops.py:179，默认 claude，codex 传 codex）。

但**必须重验**「codex 已是与 claude 平级引擎」这个可能被当成锚点的旧印象，本次逐点核对后结论是：**平级不成立，codex 是平行孤岛**：
- 会话创建走的是独立入口 start_codex_session，不走 core.start；
- 消息投递 core.send 用 make_tmux_name 生成会话名，而 make_tmux_name（tmux_ops.py:68）只产出 ccs- 前缀——**ccs send 永远打不到 cdx- 会话**；codex 的任务执行是 run_codex_task 的一次性 exec 形态，不是 TUI 投递；
- 路由层存活快照只统计 ccs-*（liveness.py 过滤条件），cdx- 会话对 pipeline 不可见；
- 自动重启路径（watchdog._restart_partner、pipeline._check_and_start_role）一律调 core.start，只会拉 claude。

**为何影响**：这决定了「与 claude 同级」的正确解读——不是再复制一个 codex 式孤岛（那样「可以选择不同 agent 启动」在核心链路上仍是假的），而是让引擎成为 start 路径内部的一个**分派维度**。哨兵的 engine 字段已经存在，缺的是「谁写它、谁读它」的闭环。

---

## 三、本次实测的新事实（opencode 侧，全部为真机验证）

1. **安装与版本**：WSL 内 ~/.opencode/bin/opencode，v1.19.3，ELF 原生二进制；配置在 ~/.config/opencode/opencode.jsonc。
2. **模型网关已通**：配置里已有 9router 供应商（openai-compatible，baseURL 指向 9Router 的本地端口，模型 9router_hermes）——与 claude 用的是**同一个 9Router 网关**。TUI 状态栏实测显示「Build · 9Router Hermes」，端到端请求 1 分 23 秒返回，证明模型链路开箱即通，无需 ANTHROPIC_* 环境变量（那套是 claude 的认证方式）。
3. **TUI 形态可用**：tmux 内起 opencode，tmux send-keys 打字 + Enter 提交可正常触发对话并收到回复（探针问答成功）——与 claude 同构的「tmux 会话承载交互式 TUI」形态成立。
4. **忙标记不同**：生成期状态栏是「esc interrupt」（无 to）加转点动画，空闲期消失。现状判忙文案是「esc to interrupt」——**直接照搬会漏判 opencode 的生成期**，导致链 B 的「生成中拒投」保护失效。
5. **输入框几何不同**：composer 用竖线边框与底边框线，不是 Claude 的双横线区域——链 B 的提交确认判据（取双横线之间的文本）在 opencode 上取不到区域，会退化为反复补 Enter 的空转。
6. **指令文件是 AGENTS.md**：见链 D 探针。同一份内容落 CLAUDE.md 对 opencode 无效。
7. **无头模式当前不可用**：`opencode run` 一次性执行实测日志报「Failed to fetch models.dev」超时，标准输出为空（exit 0 但无内容）——因此第一期**不能**仿照 codex 的 run_codex_task 式一次性 exec 路线，TUI-in-tmux 是唯一被验证的承载形态。
8. **内层 agent 机制存在**：`opencode agent list` 显示 build（primary）等，`--agent` 可选——这是引擎内部的子代理选择，留给二期作为可选参数，不影响引擎级选择的设计。

---

## 四、根因排序与修改方案（按最根本 → 最表层）

排序原则：离「身份从哪来」越近越根本。以下每条按【是什么问题 → 为何影响 → 如何影响（含实证） → 修改方案 → 为何能解决】组织。

### 第 1 层（最根本）：引擎不是一等概念，身份无处声明、无处持久

【问题】全系统没有一个地方回答「这个会话由哪个引擎承载」。persona 无引擎字段、StartOptions 无引擎字段、ccs.py start 无引擎参数；哨兵虽有 engine 字段但只在 codex 孤岛里写、没人按它分派行为。于是六条链各自用字符串字面量猜（claude）。

【为何影响】身份层缺失是其余所有硬编码的共同根因——正因为没有 engine 可查，判活、投递、上下文才只好写死 claude。

【如何影响】任何"换引擎"的改动都会变成在 20 余处字面量里做替换，且重启路径（watchdog 与 pipeline 各一处）拉错引擎的故障面无法从结构上关闭（见第 2、3 层）。

【修改方案】建立引擎身份的三层声明：persona JSON 增加 engine 字段（角色默认）→ ccs.py start 增加 --engine 覆盖（按次选择）→ 启动时把最终值写入哨兵 engine 字段（事实源）。所有后续分派只读哨兵，不再从进程名或前缀反推。

【为何能解决】「后续可以选择不同 agent 启动」的语义落在 persona 默认 + CLI 覆盖两个入口，且身份持久化后，重启、判活、投递共享同一事实源——引擎翻转从结构上不可能发生。

### 第 2 层：启动面（进程出生）写死 claude

【问题】_launch_tmux 的 wrapper 命令行、环境注入、就绪判据都是 claude 形状（链 A）。

【如何影响】start 是唯一产出口，不改它则一切免谈；照搬现状起 opencode 会拉错二进制或拉起后就绪探测永远不命中。

【修改方案】启动面按引擎分派：claude 分支保持现状 wrapper 一字不动（回归风险为零）；opencode 分支用同构的 wrapper 拉起 opencode TUI（实测形态），就绪判据换成 opencode 的输入框占位提示/版本角标出现；模型与权限走 opencode 自己的配置面（已就绪），跳过 ANTHROPIC_* 注入与断言。

【为何能解决】进程身份从源头正确；claude 路径零改动意味着存量 19 角色不受影响；opencode 的模型链路已被实测证明免配置（同一 9Router）。

### 第 3 层：判活与自愈以 claude 进程名为锚

【问题】watchdog 的 pgrep、健康的 cmdline 扫描、陈旧重启的 comm 严格匹配、以及**一切自动重启入口**（watchdog._restart_partner、pipeline._check_and_start_role）都固定拉 claude（链 C）。

【如何影响】误判 + 误救 = 引擎翻转。具体时序：opencode 会话被判「无 claude 进程」→ 判死/判僵尸 → 自动重启走 core.start → 起成 claude → 角色身份、上下文文件、模型配置三重错位，而所有监控指标都显示"已恢复"。

【修改方案】进程识别统一走「按引擎取进程名」（进程查找函数已参数化，codex 先例可用）；三处判活（watchdog、健康检查、陈旧重启）按哨兵 engine 分派判据；所有自动重启入口从哨兵读 engine 并透传给 start。

【为何能解决】感官与行为同源（都读 engine），误判面消失；即使判死，救回来的仍是原引擎，自愈不再破坏身份。

### 第 4 层：TUI 交互协议是 Claude 形状

【问题】忙判据文案、composer 几何、提交确认三样绑定 Claude TUI（链 B），实测 opencode 三样全不同（第三节 4、5 条）。

【如何影响】两头都出事：忙漏判 → 生成中被投递 → 在飞推理被覆盖（closer 实证）；提交判据失效 → 滞留消息不可见 + 补 Enter 空转。

【修改方案】投递原语按引擎分派三个判据：claude 沿用现文案与双横线几何；opencode 用实测标记（「esc interrupt」）与竖线 composer 几何。粘贴与 Enter 提交本身两引擎通用，不动。

【为何能解决】两类事故（覆盖推理、消息滞留）的防护在 opencode 上恢复等效强度；因为只换判据不动投递骨架，claude 行为零漂移。

### 第 5 层：上下文落盘文件名绑定 CLAUDE.md

【问题】角色知识写 CLAUDE.md，opencode 读 AGENTS.md（链 D + 实测）。

【如何影响】opencode 角色空脑运行：拿不到角色契约、红线、教训，产出质量塌方且无任何报错——正是本项目最警惕的「进程在转 ≠ 角色在高质量产出」。

【修改方案】知识注入按引擎落盘到对应指令文件（同一内容、同源生成）：claude → CLAUDE.md，opencode → AGENTS.md。MCP/权限面按引擎写对应配置（claude 的 settings.json 路径不动，opencode 用其配置面）。

【为何能解决】上下文隔离这一核心价值对两引擎等效成立；内容同源保证两引擎拿到的角色知识逐字一致，不会漂移。

### 第 6 层：观测/评估链只认 claude 转录目录

【问题】四个消费点（transcript 解析、eval_checker、wf_lifecycle、意图分类器）硬编码 ~/.claude/projects 布局（链 E）。

【如何影响】opencode 会话的产出在评估面静默缺席（假阴性），质量信号失真。

【修改方案】第一期显式降级并声明：评估面只覆盖 claude 会话，opencode 会话标记为「未纳入评估」而非默认通过；二期再接 opencode 的会话导出（其 CLI 有 session/export 能力）。

【为何能解决】用显式声明替代静默缺口——降级可见、可审计，不会把「没评」误读成「评过且合格」。

### 第 7 层（最表层）：会话命名空间与前缀过滤

【问题】会话名前缀、解析、过滤散布多处（make_tmux_name 只产 ccs-、read_sentinel 回退试 ccs/cdx 两前缀、liveness 快照只收 ccs-、role_lifecycle 活跃集合按剥前缀取名）。

【修改方案】此处是一个需要拍板的设计选择（见第五节 D1），推荐方案是**沿用 ccs- 命名空间 + engine 只存哨兵**：send、feed、liveness、活跃集合全部零改动自然兼容 opencode 会话；代价是「从 tmux 名反推引擎」的旧推断函数必须改为哨兵优先。备选是新前缀（对称 codex 先例），代价是上述四处过滤同步扩展，且与「引擎是实例属性而非会话类型」的语义略拧。

【为何能解决（推荐方案）】命名空间与引擎解耦后，"选择不同 agent"落在实例属性上，路由层对新引擎天然透明；一处反推函数的修正换掉四到五处过滤器的扩散。

---

## 五、待对齐的决策点（确认后才动手）

| # | 决策点 | 推荐 | 理由 |
|---|--------|------|------|
| D1 | 会话命名空间 | 沿用 ccs- + engine 存哨兵 | send/liveness/feed/活跃集合零改动；引擎是实例属性不是会话类型；codex 的 cdx- 是历史孤岛不应被当成范式 |
| D2 | 选择入口 | persona 默认 + `--engine` 覆盖 + 哨兵记事实 | 角色级偏好与临时试跑两种诉求都覆盖；重启只认哨兵 |
| D3 | 第一期承载形态 | TUI-in-tmux | 唯一被实测验证的形态；opencode run 无头模式当前空输出不可依赖 |
| D4 | 上下文落盘 | 按引擎写对应指令文件，内容同源 | 实测 opencode 只认 AGENTS.md；同源生成防双份漂移 |
| D5 | 第一期范围 | 启动/判活/投递/重启/上下文五链同级；评估面显式降级 | 与 claude 同级的硬标准就是五链；评估面二期接通 |

附带说明：codex 现状的孤岛形态不动，但本方案的引擎分派（第 1–3 层）天然容纳 engine="codex" 的回归，未来若要求 codex 同级，是增量而非重构。

---

## 六、术语表（本文出现的专业名词）

- **引擎（engine）**：承载角色会话的那个 CLI 进程（claude / opencode / codex）。本任务的选择对象。
- **CCS**：Claude Code Session，项目对「一个角色一个独立会话」的命名，历史名，现泛指角色会话。
- **tmux session / pane**：终端复用器的会话与窗格；每个角色独占一个 session，agent 进程跑在 pane 里。
- **wrapper**：包住 agent 的 shell 死循环脚本，agent 退出后自动重拉并退避——它常驻导致「pane 活 ≠ agent 活」，是双检判活的动因。
- **哨兵（sentinel）**：~/.hermes/run/ccs-sentinels/ 下每会话一个 JSON，记录角色、tmux 名、pid、engine 等，是跨进程的会话事实源。
- **TUI**：终端交互界面；claude 与 opencode 都是 TUI 形态，投递靠模拟键盘输入。
- **composer**：TUI 底部的多行输入区；投递后要确认文本已离开该区域才算提交。
- **判活（liveness）**：确认 agent 真实存活的检查；单检只看 tmux 会话，双检还看 agent 进程。
- **忙判据**：从 pane 输出识别「正在生成、勿投消息」的标志（两家 TUI 文案不同）。
- **工作区指令文件**：agent 启动时自动读入的工作目录级指令（claude 认 CLAUDE.md，opencode 认 AGENTS.md）。
- **9Router**：本地模型路由网关，claude 与 opencode 共用的模型出口。
- **persona JSON**：hermes-session-roles 里每个角色的定义文件（职责、信号、权限、生命周期等）。
- **drive / lifecycle**：角色的驱动方式（infinite 常驻 / ondemand 待唤 / goal 目标制）与生命周期标记。
- **pipeflow / role_lifecycle**：路由层的工作流引擎与角色拉起/投递编排。
