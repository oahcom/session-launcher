#!/usr/bin/env python3
"""
CCS 输入意图判定 + 重复提醒 daemon (R70 Round 1 — deliverable #1, v2)

User spec:
  "deamon 当用户输入给 ccs 时判断这些是不是疑问或者任务，
   deamon 需要重复提醒 ccs（你真的解决的用户的问题了吗？你真的有严格按用户的提示去完成任务吗？)"

设计决策（v3 触发源纠偏 — 见 ~/.hermes/workspace/ccs-intent-daemon-realign/REQUIREMENT.md）：
- 触发源：pane 无新产出 → **该角色存在未对齐用户请求**。前者对 infinite 常驻角色
  等价于「角色空闲」（其设计上的正常态），会形成 注入→应答→pane变→重置预算→再注入
  的自激循环（实测 pm 250 轮 / coordinator 193 轮 / engineer 175 轮）。改后
  无用户请求即零注入，且回到用户原始要求「当用户输入信息给 ccs 时候…重复提醒」。
- 请求证据复用 core.send 既有写入，不新增状态文件（bus ON CONFLICT(t) 天然去重）。
- 提醒文案改回用户要求的「你收到的是用户直接指令」——前提已由 bus 痕迹确证。
- 删除 _recent_violations_summary 回灌（drift 告警语义另有缺陷，且属纯噪音）。
- P0#2 死去重：原 hash(constant_string) 使第二次起永远 False → 改为纯冷却门控。
- X1 缺单实例锁：加 fcntl.flock pidfile（对齐架构红线 3）。
- C1 任务动词只看前 12 字：改为全串扫描。
- C2 英文任务漏判：加英文任务动词前缀。
- D1 send-keys 多行：用 send-keys -l 逐行 literal 发送。
- P1 _states 无回收：每轮剔除已不存在的 session。

ponytail: 轮询 10s，覆盖 ~50 个 ccs-* tmux session；并发 >100 改 event-driven。
"""
from __future__ import annotations
import json, os, re, tempfile, time, subprocess, sys, fcntl, threading
from dataclasses import dataclass
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
BUS_CLIENT = os.path.join(HERMES_HOME, "scripts", "bus_client.py")
PID_FILE = Path(HERMES_HOME) / "run" / "ccls_intent_daemon.pid"
# 提醒预算落盘：daemon 按设计会因源码 mtime 变化退出并由 systemd 重拉（见 run()），
# 内存态 _states 随之清零 → 同一条痕迹再次领满额预算，实测每 ~3 分钟复现一次
# "预算耗尽(3次)"（2026-10-17 00:04/00:06/00:09/00:11）。落盘后重载可续算。
STATE_FILE = Path(HERMES_HOME) / "run" / "intent_reminder_state.json"
# 预算上限的模块级常量（_load_states 需要在类外引用它做 clamp，
# 而 IntentReminderDaemon._MAX_REMINDERS 在类内、定义于下方）。
# 两者必须同值 —— 有测试断言其一致性。
_MAX_REMINDERS_CAP = 3


# 意图判定单一真相源：复用 user_intent_reminder 的 classify_intent / Intent /
# repeat_reminder，不再各自维护副本（v3 收敛：消除重复定义与符号覆盖冲突）。
try:
    from user_intent_reminder import (  # type: ignore
        classify_intent, Intent, repeat_reminder, verify_trace, is_ack,
        sanitize_request)
except Exception:  # pragma: no cover - 同目录兜底
    from pathlib import Path as _P
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "user_intent_reminder",
        str(_P(__file__).resolve().parent / "user_intent_reminder.py"))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    classify_intent = _mod.classify_intent
    Intent = _mod.Intent
    repeat_reminder = _mod.repeat_reminder
    verify_trace = _mod.verify_trace
    is_ack = _mod.is_ack
    sanitize_request = _mod.sanitize_request

# 同目录复用 tmux 投递（脚本以本目录为 sys.path[0] 启动，直接 import 即可）
try:
    from tmux_ops import _tmux_send, parse_tmux_name, _role_is_busy  # type: ignore
except Exception:  # pragma: no cover - 加载失败时降级为不可注入
    def _tmux_send(tmux_name: str, message: str, **_kw) -> None:
        raise RuntimeError("tmux_ops 不可用")

    def parse_tmux_name(name: str):  # type: ignore
        return (name, 0)

    def _role_is_busy(tmux_name: str) -> bool:  # type: ignore
        return False  # 降级：判不出忙 → 照常投递（宁可投递也不静默停发）


def _inject_text(session: str, text: str) -> bool | None:
    """把多行文本作为**一条**消息注入 pane。返回 _tmux_send 的结果。

    不可逐行 send-keys + 逐行 Enter：那样每行独立成一条消息（实测
    coordinator 收到 22 字符的裸首行「【停滞自检 · 每轮必对齐】第1次重复自检：」），
    角色为每行各起一个回合应答。_tmux_send 整条投递（chunk + 单个 Enter），
    payload 内嵌换行在 CLI 输入框中是换行而非提交。

    **返回值必须逐层上传**：_tmux_send 失败时返回 False 而非抛异常，
    这里若丢弃它就等于把投递失败伪装成成功。
    """
    return _tmux_send(session, text)


def _tmux_sessions() -> list[str] | None:
    """枚举 ccs-* tmux sessions。成功返回表（可为空），失败返回 None。

    None 与 [] 必须区分：[] 是"tmux 成功但无 ccs- 会话"（真·全退出），
    None 是"tmux 探测失败"（超时/socket 抖动）。旧实现两者都返回 []，
    使 _empty_polls 在负载抖动时累计到阈值 → 清空 _states → 同一用户请求
    重领满额预算（实测每 ~3 分钟复现一次"预算耗尽(3次)"）。
    2026-10-19 实测：tmux ls 3s 超时在高负载下 11% 概率触发，而 tmux 实际存在。
    2026-10-21 复测：6 小时窗口 117 次超时（审计小时窗 76/93 行，loadavg 19.2），
    负载回到 3.2 后归零 —— 3s 是负载尖峰下的紧预算，本 daemon 是这条需求唯一
    的执行器，失明即当轮零提醒，故放到 10s（与 _INTERVAL 同数量级）。
    """
    try:
        out = subprocess.check_output(["tmux", "ls", "-F", "#{session_name}"],
                                      text=True, timeout=10)
        return [s for s in out.splitlines() if s.startswith("ccs-")]
    except Exception as e:
        print(f"[ccls_daemon] tmux ls 失败 → 无法判定（不累计 _empty_polls）: {e!r}",
              file=sys.stderr, flush=True)
        return None

def _transcript_mtime(session: str) -> float:
    """角色最近一次落盘时刻（epoch，0=读不到）。只 stat，不解析文件。

    用来判"提醒之后角色是否真的回过一轮"：回过就不再提醒。
    实测 qa 2 小时 34 条零工具调用的回复，33 条是第 2、3 次提醒催出来的
    「已完成/无新增动作」——第 1 次没能让它动手，第 2、3 次只会让它复述。
    """
    role = IntentReminderDaemon._role_of(session)
    # Claude Code 把角色名里的 _ 也换成 -（ccs-workspaces-knowledge-curator）；
    # 不替换则下划线角色（knowledge_curator/product_architect/security_auditor）
    # 恒读到 0.0 → 抑制分支永不生效。
    d = Path.home() / ".claude" / "projects" / f"-home-administrator-ccs-workspaces-{role.replace('_', '-')}"
    try:
        return max((os.path.getmtime(f) for f in d.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


@dataclass
class _SessionState:
    """Per-session reminder state."""
    request_ts: float = 0.0   # 该角色最近一次用户请求时刻（bus 注入痕迹 timestamp）
    last_remind_ts: float = 0.0
    # 上次提醒的 epoch（last_remind_ts 是 monotonic，不能跟落盘时间比）。
    # 不持久化：重启后归零 → 允许立即提醒一次，无害。
    last_remind_epoch: float = 0.0
    remind_count: int = 0
    # 该请求的意图（evidence intent= 字段）。取不到时调用方降级 TASK。
    intent: "Intent | None" = None
    # 已就"角色生成中"打过一次日志（防每 tick 刷屏）。不持久化：重启后重报一次无妨。
    deferred_logged: bool = False
    # 已就"痕迹无请求正文"打过一次日志。必须与 deferred_logged 分开：忙路径每转闲
    # 都会把 deferred_logged 清回 False，共用会让这条"只报一次"的日志每 10s 刷一行
    # （审查 P2#7 实测 5 tick → 5 行）。
    no_digest_logged: bool = False


def _load_states() -> dict[str, _SessionState]:
    """读回落盘的提醒预算。损坏/缺失一律返回空表（降级为原内存行为，不阻塞启动）。

    只保留与本进程活着的会话无关的语义：键是 tmux session 名，由调用方决定去留。

    **不读回 last_remind_ts**：它是 time.monotonic()（自开机计时），而本文件在
    持久盘上跨重启存活。重启后 monotonic 归零，旧值（上次开机时长，可达数天）
    会让冷却判据 `now - last_remind_ts < 30` 恒真 → 该 session 的提醒静默停发；
    且新用户请求只重置 request_ts/remind_count，救不回。冷却本就是同进程内的
    节流概念，跨重启无须继承 —— 丢掉它等于允许重启后立即提醒一次，无害。
    """
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, _SessionState] = {}
        for k, v in raw.items():
            # 非 dict 脏值同样按 cap 保守处理（不可 continue 丢弃）：丢弃会让该
            # 会话在内存中不存在 → 下轮 setdefault 新建 remind_count=0 → 重领满额，
            # 与落盘防重复提醒的初衷相反（fail-open）。
            if not isinstance(v, dict):
                out[k] = _SessionState(remind_count=_MAX_REMINDERS_CAP)
                print(f"[ccls_daemon] 预算状态 {k} 非 dict，按已耗尽处理",
                      file=sys.stderr, flush=True)
                continue
            # 逐条 try：单条脏数据（remind_count=null/dict、request_ts 非数）只丢该条。
            # 若让它抛到外层 except，整表预算会被清空 —— 所有角色重新获得满额预算，
            # 正是本文件反复防的"预算空烧"。
            # 解析失败的那条**按已耗尽处理**而非直接丢弃：丢弃会让该会话在内存里
            # 不存在，下轮 setdefault 新建 _SessionState → remind_count=0 → 满额预算，
            # 与"防重复提醒"的落盘初衷正好相反（fail-open）。写 cap 是保守方向：
            # 最坏少提醒一次，不会多提醒。
            try:
                _rc = int(v.get("remind_count", 0))
                out[k] = _SessionState(
                    request_ts=float(v.get("request_ts", 0.0)),
                    last_remind_ts=0.0,  # 见 docstring：monotonic 不可跨重启继承
                    # 墙钟时刻，可跨重启继承：今日源码热重载 13 次，每次重启丢掉
                    # epoch 都会让已回复的角色被再催一次（I1）。
                    last_remind_epoch=float(v.get("last_remind_epoch", 0.0) or 0.0),
                    remind_count=max(0, min(_rc, _MAX_REMINDERS_CAP)),
                    intent=None,  # 意图每轮从痕迹刷新，无需持久化
                )
            except (TypeError, ValueError):
                # 该条脏：remind_count 不可信 → 写 cap。
                # 同时**尽量保留 request_ts**：否则 _tick 的「新一轮用户请求」分支
                # 会因 request_ts=0.0 与真实 ts 不等而重置预算，脏条目被抵消，
                # 角色再次重领满额 —— 与首轮 P1「防 fail-open」的初衷正好相反。
                # request_ts 本身也脏时 float() 再抛，落回仅写 cap。
                try:
                    out[k] = _SessionState(
                        request_ts=float(v.get("request_ts", 0.0)),
                        remind_count=_MAX_REMINDERS_CAP,
                        intent=None,
                    )
                except (TypeError, ValueError):
                    out[k] = _SessionState(remind_count=_MAX_REMINDERS_CAP)
        if raw and not out:
            # 整表全脏但没触到外层 except → 静默空表会让预算追踪无声消失。
            # 文件格式漂移时没有这条日志就无迹可查。
            print(f"[ccls_daemon] 预算状态 {len(raw)} 条全部解析失败（文件格式漂移？）",
                  file=sys.stderr, flush=True)
        return out
    except Exception as e:
        if STATE_FILE.exists():  # 存在却读不出 = 真损坏，留日志；不存在是首启，正常
            print(f"[ccls_daemon] 预算状态读取失败（降级为内存态）: {e}",
                  file=sys.stderr, flush=True)
        return {}


def _save_states(states: dict[str, _SessionState]) -> None:
    """原子写回预算状态。失败只留日志 —— 落盘是优化，不该让 daemon 崩。

    不写 last_remind_ts（monotonic，跨重启无意义），只持久化能跨重启继承的
    请求时刻、已用预算与上次提醒的墙钟时刻。
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 唯一 tmp 名：两个进程（或 systemd 重拉窗口内新旧实例重叠）同写同一
        # `.tmp` 路径会互相覆盖，最终 replace 落盘的是其中一次的完整快照——
        # 不撕裂，但丢失另一次更新，表现为随机回退预算。mkstemp 保证独占。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(STATE_FILE.parent), prefix=STATE_FILE.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {k: {"request_ts": s.request_ts,
                         "remind_count": s.remind_count,
                         "last_remind_epoch": s.last_remind_epoch}
                     for k, s in states.items()},
                    ensure_ascii=False))
                # fsync 才算真正落盘：无它时 os.replace 返回后内容可能仍在
                # page cache，掉电/硬重启会让 STATE_FILE 指向空或半写文件
                # —— 本修复的核心价值就是跨重启续算（实测 04:43 重启场景），
                # 少了这步等于在最需要的场景失效。
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, STATE_FILE)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[ccls_daemon] 预算状态写入失败: {e}", file=sys.stderr, flush=True)


def _watched_sources(*modules: str) -> tuple[str, ...]:
    """需要盯 mtime 的源文件：自身 + 已导入的依赖模块。

    写成模块级函数而非类体内生成式：类体里的名字在生成式作用域里不可见
    （实测 NameError: _SRC_FILE）。
    """
    me = os.path.abspath(__file__)
    return tuple(os.path.abspath(getattr(sys.modules.get(m), "__file__", "") or me)
                 for m in modules)


class IntentReminderDaemon:
    """后台轮询：用户向 ccs 提出请求后，重复提醒其自检是否真解决了用户问题。

    触发源 = 该角色存在**未对齐的用户请求**，取自 bus 里 core.send 写下的注入
    痕迹（title 前缀 `[user_intent_reminder] 已注入意图提醒 → <role>`，其
    timestamp 即「最近一次用户请求」）。复用 core.send 既有写入，无需新状态；
    bus 的 ON CONFLICT(t) 保证每角色恒只留最新一条，天然免去去重维护。

    曾用「pane 快照无变化」判停滞 —— 该判据对 infinite 常驻角色等价于「角色空闲」，
    而空闲是其设计上的正常态：被判停滞 → 每 30s 注入 → 角色回一句"是/是/待机" →
    pane 变化 → 预算重置 → 再注入，形成自激循环（实测 pm 250 轮、coordinator 193 轮、
    engineer 175 轮；coordinator 应答"告警同前。待机。"逐字重复 98 次，每次消耗一个
    完整 LLM 回合）。改用请求驱动后，无用户请求即零注入，空转从根上消失，
    同时回到用户原始要求：「当用户输入信息给 ccs 时候…重复提醒」。

    启动: python3 ccls_daemon_intent_classifier.py --daemon
    """
    _INTERVAL = 10.0        # 轮询间隔
    _REMIND_COOLDOWN = 30.0  # 同 session 冷却（纯冷却门控，修复 P0#2）
    # 单次用户请求的重复提醒预算。冷却只限频率不限总量；同一请求超过此数仍未
    # 对齐（应答恒为同一句"是/是/待命"）说明角色是**选择空闲**而非卡住，转静默
    # 避免空转消耗。3 次跨 90s 已足够让真跑偏的角色察觉。
    # 新一轮用户请求到达 → 预算重置 → 可再次提醒。
    _MAX_REMINDERS = 3
    # 用户请求的提醒有效期：超过则视为已过期，不再重复提醒（旧请求不该无限期
    # 挂着消耗回合；也不该在新请求到来时被复活）。
    _REQUEST_TTL = 1800.0
    # 连续空枚举多少轮才认定"会话真的都没了"（此前不回收状态）。对齐架构红线
    # 「3 次连续失败才标 down」：单次 tmux 抖动不算，连续则是真退出。
    _EMPTY_POLL_THRESHOLD = 3
    # 本文件路径，供 run() 检测源码更新（见 run 内注释）。
    _SRC_FILE = os.path.abspath(__file__)
    # 依赖模块也必须一起盯：只盯自身时，tmux_ops 的修复（如"卡死 CLI 不再算生成中"）
    # 要等本文件下次改动才被加载 —— 实测 2026-10-21 19:24 的 tmux_ops 修复因此
    # 一直没生效，qa 仍被旧判据挡住。同理 user_intent_reminder 里就是注入给角色的
    # 提醒正文、core 里是痕迹签名格式：改了措辞却要等别人碰本文件才生效（实测
    # 22:18 改的提醒措辞到 22:25 仍未上线，运行实例还是旧文案）。故按名字全量盯。
    _WATCHED = _watched_sources("tmux_ops", "user_intent_reminder")
    # 由 supervisor 托管（--daemon）时置 True：源码更新后退出，交由
    # systemd Restart=always 以新代码拉起。线程模式下无人重拉，故不退出。
    _supervised = False
    # 待确认的源码 mtime：非 0 表示上一轮见到过变化，本轮再相同才认定稳定（防抖）。
    _pending_mtime: tuple | float = 0.0

    def __init__(self, interval: float = _INTERVAL):
        self._interval = interval
        # 从磁盘续算预算：本 daemon 按设计会因源码更新退出重拉（见 run()），
        # 若不续算，每次编辑源文件都会让同一条痕迹重新领满额提醒。
        self._states: dict[str, _SessionState] = _load_states()
        self._empty_polls = 0
        self._stop = threading.Event()

    def _push_reminder(self, session: str, round_no: int = 1,
                       intent: "Intent | None" = None,
                       request: str = "") -> bool:
        """向 tmux pane 注入「重复自检」提醒。返回 True 表示已推送。

        无请求正文时不注入：见 repeat_reminder 的 WHY（无正文提醒会量产伪造动作）。
        """
        st = self._states.setdefault(session, _SessionState())
        now = time.monotonic()
        if now - st.last_remind_ts < self._REMIND_COOLDOWN:  # 纯冷却门控 (P0#2)
            return False
        # 角色生成中时**不注入、也不计预算**。Claude Code 在生成期间收到 send-keys
        # 会排进 queued messages（pane 底部 "Press up to edit queued messages"），
        # 角色只在当前回合结束后才读——实测 qa 忙碌 12 分钟时 3 条提醒全卡在队列，
        # 而预算已耗尽转静默。结果是：提醒全部在角色忙时发出、全部在角色闲时失效，
        # 恰与"提醒它自检"的时机需求相反。返回 False 让调用方下轮重试。
        try:
            if _role_is_busy(session):
                # 只在"由闲转忙"时留痕一次：每 tick(10s) 每忙角色各打一行，
                # 50 角色忙 12 分钟即 ~3600 行噪音，与本文件空枚举分支
                # 「只在状态变化时留痕」的既定风格一致。
                if not st.deferred_logged:
                    st.deferred_logged = True
                    print(f"[ccls_daemon] {session} 生成中 → 延后提醒（不计预算）",
                          file=sys.stderr, flush=True)
                return False
            st.deferred_logged = False  # 已转闲，下次变忙时再报
        except Exception:
            pass  # 忙检测不可用则照常投递，宁可误投也不静默停发
        # 触发前提已成立（该角色确有未对齐的用户请求），故用用户要求的原始措辞。
        # 这与 core.send 的首轮注入同源（_REMINDER_HEADER / repeat_reminder），
        # 不再需要 v2 的「停滞自检」中性框架——那是为了在「不知有无用户输入」时
        # 避免虚假前提而设的妥协，现已有 bus 痕迹作为确证。
        # 意图取自痕迹（疑问/任务措辞不同：疑问强调"先回答用户问题"）；取不到
        # （旧痕迹/字段缺失）降级 TASK，即本 daemon 修复前的行为。
        if request:
            st.no_digest_logged = False  # 有正文了：下次缺失时重新报一次
        reminder = repeat_reminder(intent or Intent.TASK, round_no, request)
        if not reminder:
            # 旧痕迹无 req= / 验签不过 → 宁可静默也不发无正文提醒。
            # 用独立的一次性位：与"角色忙"共用会被忙/闲切换每 10s 清回 False，
            # 旧痕迹角色就变成每 tick 一行（审查 P1-3 实测 3 tick → 3 行）。
            if not st.no_digest_logged:
                st.no_digest_logged = True
                print(f"[ccls_daemon] {session} 痕迹无请求正文 → 不注入无正文提醒",
                      file=sys.stderr, flush=True)
            return False
        try:
            # 必须看返回值：_tmux_send 重试耗尽时 **return False 而不抛异常**
            # （其 docstring 明写"调用方必须检查返回值"；不按行号引用——行号会漂）。
            # 旧写法丢弃返回值 → 投递全失败也计成功，三重后果：用户请求一次没送到
            # 预算却已耗尽（此后永久静默）、bus 留下"已提醒"的假痕迹（把"没送到"
            # 伪装成"已对齐"）、daemon 侧无任何失败信号。
            # 判据用 `is not True` 而非 `is False`：只认明确的成功，任何其他返回
            # （None/0/""/异常值）都算失败。_tmux_send 的实现历史上有过"超限后静默
            # 返回 None"的版本（见其 docstring），`is False` 会在那种实现下重新
            # 静默失效——这是防御性判据，不是对当前实现的断言。
            delivered = _inject_text(session, reminder)
            if delivered is not True:
                print(f"[ccls_daemon] 重复自检投递失败 {session}（返回 {delivered!r}）",
                      file=sys.stderr, flush=True)
                return False
            st.last_remind_ts = now
            st.last_remind_epoch = time.time()
            st.remind_count += 1
            # 留一行可观测记录：提醒覆盖率此前只能靠翻 pane/转录才知道，而"角色
            # 对话质量"的第一前提就是提醒本身真的到达了、带着正文。
            print(f"[ccls_daemon] {session} 已注入提醒 第{st.remind_count}次 "
                  f"含正文={'原始请求：' in reminder}", file=sys.stderr, flush=True)
            return True
        except Exception as e:
            # 不静默：注入失败会让角色永久停在未对齐态而无人知晓。
            print(f"[ccls_daemon] 重复自检注入失败 {session}: {e}",
                  file=sys.stderr, flush=True)
            return False

    @staticmethod
    def _role_of(session: str) -> str:
        """tmux 会话名 → 角色名。复用 tmux_ops.parse_tmux_name 的权威解析。

        不可用 `role.rsplit('-', 1)[0]`：那只对 `ccs-<role>-<N>` 成立，
        对角色名**本身含连字符**的会话会把真名砍掉（`ccs-ccs-monitor` → `ccs`、
        `ccs-codex-dev` → `codex`），与痕迹标题永不相等 → 这些角色静默零注入。
        parse_tmux_name 只在后缀为纯数字时才剥离实例号。
        """
        try:
            return parse_tmux_name(session)[0]
        except Exception:
            return session[len("ccs-"):] if session.startswith("ccs-") else session

    @staticmethod
    def _read_request_traces() -> dict[str, tuple[float, "Intent | None", str]]:
        """一次性读回全部用户请求痕迹：{角色名: (最近请求时刻, 意图, 请求正文)}。

        意图取自 core.send 写下的 evidence `intent=` 字段（classify_intent 的
        产物）。缺失时为 None —— 旧痕迹（本字段上线前写入）与证据格式变动都会
        命中，调用方按 TASK 降级（保持本 daemon 修复前的行为）。

        每轮只读一次 bus，而非每个 session 各读一次——查询与 session 无关
        （只有标题里的角色名不同）。逐 session 读会形成 N+1 子进程：实测单次
        read 79ms，10 个会话 × 每 10s = 每分钟 ~4.7s CPU，且 bus_client 每次
        构造 Blackboard 都会触发全表 FTS 回填。整轮读一次的代价降到 ~0.5s/分。

        用 `search` 而非 `read --cat code_fix`：痕迹是各角色各自的最新一条，而 read
        按分类取整批返回，与全部 code_fix 写入（含 contract_updater 等高频源）共享
        同一个 limit 窗口。实测该窗口约 53 天（50 条 / 近 30 分钟新增 9 条），
        当前速率下离饥饿还很远，但代价为零而语义更精确——search 按标题关键词命中，
        实测只返回痕迹本身，无窗口竞争、不受其他写入速率影响。
        任何异常返回空 dict（宁可不提醒，也不凭推测注入），但留日志不静默。
        """
        try:
            out = subprocess.run(
                # 搜共用前缀：一条查询同时命中新标题「用户请求痕迹」与旧标题
                # 「已注入意图提醒」（bus 里的旧事实不会自动改名），省掉每 tick 两次子进程。
                [BUS_CLIENT, "search", "user_intent_reminder", "--limit", "50", "--json"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            import json
            data = json.loads(out)
            facts = data.get("facts", data) if isinstance(data, dict) else data
            # 新标题诚实陈述"这是用户请求痕迹"，是否注入由本 daemon 决定（旧标题
            # 「已注入意图提醒」只在"只写注入"时代成立，会虚增注入计数）。
            prefixes = ("[user_intent_reminder] 用户请求痕迹 → ",
                        "[user_intent_reminder] 已注入意图提醒 → ")
            traces: dict[str, tuple[float, "Intent | None", str]] = {}
            for f in facts:
              if not isinstance(f, dict):
                  continue  # 形状漂移（bus 返回别的东西）不逐条打日志：20 键=20 行/tick
              try:  # 单条坏事实只丢一条：整体 try 会让一条脏数据清空所有角色的提醒
                title = f.get("title") or ""
                _hit = next((p for p in prefixes if title.startswith(p)), "")
                if not _hit:
                    continue
                role = title[len(_hit):].strip()
                ev = f.get("evidence") or ""
                # 边界是**最后一个** " sig="：正文自身可能带 " sig=" 字样（用户贴签名/日志），
                # 按首处切会把正文截断 → 真 sig 验不过 → 该请求静默零提醒
                # （独立审查 BLOCKER，已复现：msg 含 " sig=deadbeef"）。
                # 先切边界再解析：sig 之后的字节不在签名范围内，任何字段都只能取自 _body
                # （未签名的 intent=other 能把 ack 正文判成静默，实测 pushes 1→0）。
                _body, _sep, _tail = ev.rpartition(" sig=")
                if not _sep:
                    _body, _tail = ev, ""       # 旧痕迹无签名
                # 请求时刻取 evidence 的 req_ts：痕迹标题按角色固定，bus UPSERT 保留
                # 首次 ts，故 timestamp 只反映**第一次**注入；ut 又会被 search 命中
                # 刷成当前时刻。两者都不能判「最近一次请求」，读它们会让 request_ts
                # 冻结在首条痕迹 → TTL 过后该角色永久零提醒。
                # 旧痕迹无 req_ts（字段上线前写入）→ 退回 timestamp，行为同修复前。
                _rt = re.search(r"req_ts=([\d.]+)", _body)
                try:
                    ts = float(_rt.group(1)) if _rt else float(f.get("timestamp") or 0.0)
                    # 未签名的未来 req_ts 会赢下 max-ts 竞争并让该角色永久静默
                    # （TTL 永不生效）。夹到当前时间+60s：允许时钟小幅漂移，不允许穿越
                    # （审查 P2-2，实测 no-sig 远未来事实可永久压住合法痕迹）。
                    ts = min(ts, time.time() + 60)
                except (TypeError, ValueError):
                    # 脏 req_ts（1.2.3 / 空值）不得整条丢弃：丢一条 = 该角色静默零
                    # 提醒。退回 facts 时间戳，再失败才跳过（独立审查 T7）。
                    try:
                        ts = float(f.get("timestamp") or 0.0)
                    except (TypeError, ValueError):
                        continue
                # 请求正文（core.send 写入，见 core._req_digest）：无此字段的
                # 旧痕迹 → ""，调用方据此跳过注入。
                # 前缀匹配足够：所有字段都取自 _body，sig 之后的字节不参与解析。
                # **已接受的风险**：任何写入方在合法痕迹尾部追加 " sig=任意值" 都能
                # 让验签失败 → 该角色本轮零提醒（实测 B1/C1，无需密钥）。这是 fail-
                # closed：宁可不注入也不发未经验签的正文。想进一步收窄得在验签失败时
                # 回退试第一个 " sig=" 边界，当前提醒价值不值那份复杂度。
                # 改成 fullmatch 会让尾随换行（旧痕迹带夹注）把正文整条丢掉（实测回归）。
                _sg = re.match(r"([0-9a-f]{32})", _tail)
                _rq = re.search(r"\breq=(.*)$", _body)
                _req = (_rq.group(1).strip() if _rq else "")
                # bus 无认证、写入方任意，而 req= 会被贴进角色 pane：只接受带有效签名的
                # 痕迹。签名不符/缺失一律按"无正文"处理（不注入），既挡住手滑误写，
                # 也不误杀没有正文的旧痕迹。
                # **天花板**：密钥与角色同 uid，持有者（任何角色 shell）仍可签出合法
                # 痕迹 —— 这是完整性证据，不是同 uid 认证（审查 P1-1）。
                # 验签对象是**整条正文**（末尾 sig 之前），不只是 role|req_ts|req：
                # 否则截获合法痕迹后单改 intent= 就能让该角色静默（审查 P2-2）。
                # 不在此处打日志：解析每 tick 跑一次，旧痕迹会每 10s 刷一行；
                # "无正文"由 _push_reminder 每 session 只报一次。
                if _req and not (_sg and verify_trace(_body, _sg.group(1))):
                    _req = ""
                # 标题角色必须与**签名正文里的** role= 一致：否则任何 bus 写入方都能
                # 把一条合法签名痕迹"改投"到另一个角色的 pane（审查 P2-1，实测可把
                # qa 的请求重放到 ops/pm）。签名只绑内容，不绑投递目标，需在此校验。
                # 代价（已接受）：source= 必须是首字段。实测 role 先于 source 的旧格式会被
                # 整条丢弃（审查 G1）——唯一构造点 core._trace_evidence 恒以 source= 开头，
                # 将来若在前面插字段，必须同步改这里，否则所有正文静默丢失。
                # 必须是紧跟在 source= 之后的 role=：正文任意位置出现 " role=ops"
                # 也会命中，实测可把 qa 的请求投进 ops pane。
                if _req and re.match(r"source=[^ ]* role=" + re.escape(role) + r"(?: |$)", _body) is None:
                    _req = ""
                # 边界再洗一遍：签名证明"谁写的"，不证明"内容是单行" —— 同 uid 的
                # 签名者能塞多行，把伪造的「第N次重复自检：…」写进 pane（审查 P2-4）。
                _req = sanitize_request(_req) if _req else ""
                # 只在 req= 之前的字段段里取意图：_body 含 req= 用户正文，
                # 正文里贴一句 "看下 intent=other 这个字段" 就能翻掉意图
                # （实测 A1：写入侧 intent 缺失时，该请求被判 ack 静默）。
                _m = re.search(r"intent=(\w+)", _body.split(" req=", 1)[0])
                try:
                    intent = Intent(_m.group(1)) if _m else None
                except ValueError:
                    intent = None  # 未知取值：降级 TASK，不让脏数据中断整轮
                if role and ts > traces.get(role, (0.0, None, ""))[0]:
                    traces[role] = (ts, intent, _req)
              except Exception as _e:  # noqa: BLE001 - 单条脏数据不得清空整张表
                print(f"[ccls_daemon] 单条痕迹解析失败（已跳过）: {_e!r}",
                      file=sys.stderr, flush=True)
            return traces
        except Exception as e:
            # 不静默：读不到痕迹 = 所有角色的提醒静默停发，必须留下痕迹。
            print(f"[ccls_daemon] 读取用户请求痕迹失败: {e}",
                  file=sys.stderr, flush=True)
            return {}

    def _tick(self) -> int:
        """One poll cycle. Returns # reminders pushed."""
        pushed = 0
        _sessions = _tmux_sessions()
        # _sessions=None 表示 tmux 探测失败（超时/ socket 抖动），**不**计入
        # _empty_polls、不回收 _states：否则一次抖动就清空预算，同一条用户请求
        # 立刻重领满额（实测每 ~3 分钟复现）。None 与 [] 语义分离，见函数 docstring。
        if _sessions is None:
            self._empty_polls = 0
            return pushed
        live = set(_sessions)
        # 空枚举几乎总是 tmux 抖动（`tmux ls` 3s timeout / socket 重建），不是
        # "所有 session 都消失了"。若照常回收 _states，下一轮 setdefault 会把
        # remind_count 归零 → 同一用户请求再拿满额预算（实测单次抖动使注入翻倍 3→6）。
        # 故按架构红线「3 次连续失败才标 down」的同一判据：连续为空才认定真退出，
        # 那时照常回收状态（否则全部 session 真退出时 _states 永久泄漏）。
        # 每轮空都留日志：否则"抖动"与"daemon 卡死"在观感上无法区分。
        if not live:
            self._empty_polls += 1
            print(f"[ccls_daemon] 未枚举到任何 ccs-* 会话（连续第 {self._empty_polls} 轮）",
                  file=sys.stderr, flush=True)
            if self._empty_polls < self._EMPTY_POLL_THRESHOLD:
                return pushed
        else:
            self._empty_polls = 0
        # P1 修复：剔除已不存在的 session
        for dead in set(self._states) - live:
            del self._states[dead]
        # 每轮读一次请求痕迹，供所有 session 复用（避免 N+1 子进程）
        traces = self._read_request_traces()
        for sess in live:
            st = self._states.setdefault(sess, _SessionState())
            role_key = self._role_of(sess)
            if not traces:
                continue  # 无任何用户请求 → 零注入（infinite 角色的空闲是正常态）
            # 有未对齐用户请求时**不做 lifecycle 过滤**：该过滤的意图是"不主动唤醒
            # 被动角色"，而此刻用户明确找过它、它也确实在 tmux 里跑着——提醒是响应
            # 用户请求，不是主动唤醒。过滤掉会让"用户问了 qa，qa 永远收不到重复提醒"。
            # 实证：qa 的 persona 声明 ondemand 但运行时哨兵是 infinite，被静默跳过。
            trace = traces.get(role_key)
            if not trace:
                continue
            req, req_intent, req_text = trace
            if req_intent is Intent.OTHER and is_ack(req_text):
                # 只跳"整条就是一句确认"（好的/收到/ok）。不能按 OTHER 整个枚举跳过：
                # 分类器是关键词启发式，实测把「回滚到上一个版本」「看一下日志」
                # 判成 OTHER —— 那会让真请求彻底静默（审查 D4）。
                continue
            if time.time() - req > self._REQUEST_TTL:
                continue  # 请求已过期，不再重复提醒
            if req != st.request_ts:
                # 新一轮用户请求 → 预算重置，可重新提醒
                st.request_ts = req
                st.remind_count = 0
                st.last_remind_epoch = 0.0
            st.intent = req_intent  # 每轮刷新：措辞跟随最近一次请求的意图
            if st.remind_count >= self._MAX_REMINDERS:
                continue  # 同请求预算耗尽：角色选择空闲而非卡住，转静默
            if st.last_remind_epoch and _transcript_mtime(sess) > st.last_remind_epoch:
                # 提醒后角色已经回过一轮 → 不再催。第 1 次没让它动手时，
                # 第 2、3 次只会催出复述（实测 qa 2h 34 条零动作回复里 33 条
                # 由此产生）。转录 mtime 一次 stat 即可，不解析内容。
                continue
            if self._push_reminder(sess, st.remind_count + 1, st.intent, req_text):
                pushed += 1
                self._bus_trace(sess)
                if st.remind_count == self._MAX_REMINDERS:
                    # 预算耗尽转静默时留痕一次：否则"角色已对齐"与"daemon 挂了"
                    # 在观感上无法区分（journald 侧可查）。
                    print(f"[ccls_daemon] {sess} 重复自检预算耗尽 "
                          f"({self._MAX_REMINDERS}次) → 转静默，等下一轮用户请求",
                          file=sys.stderr, flush=True)
        # 落盘：仅在有提醒产出时写（每轮无条件写会以 10s 频率刷盘）。
        # 代价是"重载前的最后一次 tick"若没推提醒就不留痕——可接受：
        # remind_count 只在推送成功后自增，未推送时磁盘值即最新值。
        if pushed:
            _save_states(self._states)
        return pushed

    def _bus_trace(self, session: str) -> None:
        # 重复提醒属监控/健康事件，写入 monitor_audit（语义正确），
        # 不再污染 code_fix（角色据该 cat 找真实修复方案，噪声会稀释信号）。
        try:
            subprocess.run([
                BUS_CLIENT, "write", "monitor_audit",
                f"[ccls_daemon] 重复自检提醒 {session} (存在未对齐用户请求)",
                "--evidence", f"round=R70 session={session}",
                "--src", "ccls_daemon",
            ], capture_output=True, timeout=5)
        except Exception:
            pass

    def run(self) -> None:
        """轮询主循环。

        WHY 每轮查源码 mtime：本 daemon 由 systemd(ccs-intent-guardian)常驻托管，
        进程启动后永不再读源文件——main 分支上的修复对运行中的实例完全不生效
        （实测 2026-10-16：18:23 与 20:37 两次修复提交后，运行实例仍在执行修复前
        的判据，继续以旧逻辑每 30s 轰击角色，coordinator 累计 193+ 轮）。
        supervised 时主动退出，靠单元既有的 Restart=always 以新代码重启——
        比自建 exec/reload 更省，且天然复用单实例 PID 锁。
        线程模式（watchdog 内启动）无 supervisor，退出等于永久停摆，故不查。

        WHY 要求"映入后稳定一轮"：源文件正被编辑（多人/多 agent 同时改 main）时，
        每次重拉都会立刻读到又变了的 mtime → 立即再退出 → systemd 连续重拉，
        RestartSteps=5 的退避触顶后服务永久停摆。实测 2026-10-16 21:03-21:04：
        21:03:41/21:04:03/21:04:28 三次「源码已更新→退出」，restart counter 累到 3，
        随后 ccs-intent-guardian 停摆。故 mtime 需连续两轮相同才算稳定，
        只有稳定后才退出，避免边写边拉的自激循环。
        """
        def _mtimes():
            out = []
            for _p in self._WATCHED:
                try:
                    out.append(os.path.getmtime(_p))
                except OSError:
                    out.append(0.0)
            return tuple(out)

        src_mtime = _mtimes()
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._tick()
            if self._supervised:
                try:
                    cur = _mtimes()
                    if cur != src_mtime:
                        if cur == self._pending_mtime:
                            # 连续两轮 mtime 一致 → 编辑已停止，可安全退出重拉
                            print("[ccls_daemon] 源码已更新（稳定）→ 退出，"
                                  "等 supervisor 以新代码拉起", file=sys.stderr, flush=True)
                            return
                        self._pending_mtime = cur  # 首次见到变化：待下一轮确认
                    else:
                        self._pending_mtime = 0.0
                except OSError:
                    pass  # 源文件暂不可读（原子替换窗口）：下一轮再判
            # 扣掉本轮耗时：否则一次慢 tick（tmux ls 吃满超时）会把周期整体拉长，
            # 全角色提醒节奏减半（审查 P2#13）。
            self._stop.wait(max(0.0, self._interval - (time.monotonic() - t0)))

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="IntentReminderDaemon")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def _acquire_pid_lock() -> int | None:
    """单实例锁（架构红线 3）。返回 fd 或 None（已被占用）。"""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(PID_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except OSError:
        os.close(fd)
        return None


# ── CLI self-check ─────────────────────────────────────────────────────────
def _selfcheck() -> int:
    """Assert-based self-check for classification rules."""
    assert classify_intent("请帮我写一个排序函数") == Intent.TASK
    assert classify_intent("什么是 YAGNI 原则？") == Intent.QUESTION
    assert classify_intent("能否解释下 tmux socket 隔离") == Intent.QUESTION
    assert classify_intent("分析下三项目架构") == Intent.TASK
    assert classify_intent("hello") == Intent.OTHER
    assert classify_intent("") == Intent.OTHER
    assert classify_intent("修改 core.py 中的注入逻辑") == Intent.TASK
    # C1 修复：长上下文前的任务动词
    assert classify_intent("blah " * 50 + "分析下架构") == Intent.TASK
    # C2 修复：英文任务
    assert classify_intent("please fix this bug") == Intent.TASK
    assert classify_intent("please implement the feature") == Intent.TASK
    # 其他
    assert classify_intent("今天天气真好") == Intent.OTHER
    print("selfcheck: 11/11 PASS")
    return 0

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true", help="Run as background daemon")
    ap.add_argument("--selfcheck", action="store_true", help="Run assert self-check")
    args = ap.parse_args()
    if args.selfcheck:
        raise SystemExit(_selfcheck())
    if args.daemon:
        lock = _acquire_pid_lock()
        if lock is None:
            print("ccls_daemon already running (pid lock held). Exiting.")
            raise SystemExit(1)
        d = IntentReminderDaemon()
        d._supervised = True  # 由 systemd 托管：源码更新时退出重拉（见 run）
        d.run()
    else:
        print("Run with --daemon or --selfcheck")
