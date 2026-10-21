"""用户意图提醒：在 user→CCS 直接 send 路径上，于消息前注入"两个核心问题"。

对齐 session-pipeline/src/pipeflow/integrity.py:23-37 同源提问文本（CORE_QUESTIONS），
避免两套措辞分裂。本模块是意图判定的唯一真相源（single source of truth）：
classify_intent / Intent / repeat_reminder / _CORE_QUESTIONS 均在此定义，
daemon (ccls_daemon_intent_classifier.py) 仅导入复用，不再各自维护副本。

契约（ponytail: 用户来源标记依赖上游网关/控制台显式 --from user；本仓无该约定，
故放宽到 "user"/"cli" 两种人类来源，pipeline/daemon 自动化消息排除以防递归自提醒）：
- source 为 "user" 或 "cli" → 人类原始输入 → 注入提醒。
- source 为 "pipeline"（daemon 自动路由/步骤提醒） → 不注入。
- 消息已含提醒标记前缀 → 幂等跳过，防止重复注入。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
from enum import Enum

# 本地签名密钥：痕迹经 bus（无认证、写入方任意）进入角色 pane，故 evidence 带 MAC。
# **天花板（2026-10-21 对抗审查 P1-1，实测）**：所有 ccs 角色、daemon 与 core.send
# 同属一个 uid，任何角色 shell 都能读这把 0600 密钥并签出合法痕迹。所以 MAC 是
# "防误写 / 防跨用户 / 防手滑改字段"的完整性证据，**不是对同 uid 内部的认证**。
# 真认证要把密钥移出该 uid（systemd LoadCredential + 守护方 socket + SO_PEERCRED）。
_SIG_KEY = os.path.join(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "run", "reminder.sig.key")


def _load_key(create: bool = True) -> bytes:
    """读密钥；缺失/非完整（<16 字节）且允许创建时原子落盘（临时文件 + os.replace）。

    必须校验长度：0 字节或半截密钥会让"空密钥 HMAC"通过验签（审查 P2-3 实测），
    且 create→write 之间崩溃会留下这种文件。读路径（create=False）绝不重建 ——
    否则密钥被删后 daemon 会自造新键，把所有既有痕迹判成伪造。
    """
    try:
        with open(_SIG_KEY, "rb") as fh:
            key = fh.read()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    if not create:
        raise OSError(f"签名密钥不可用: {_SIG_KEY}")
    key = __import__("secrets").token_bytes(32)
    try:
        os.makedirs(os.path.dirname(_SIG_KEY), exist_ok=True)
        tmp = f"{_SIG_KEY}.{os.getpid()}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _SIG_KEY)  # 原子：读者看到旧键或完整新键，永不是半截
    except OSError:
        with open(_SIG_KEY, "rb") as fh:  # 并发创建/只读 HOME：回读一次
            key = fh.read()
    if len(key) < 16:
        # 回读到半截密钥时必须报错：拿它签名会造出 verify(create=False) 永远不认的
        # 追踪（写方与读方密钥不一致 → 静默零提醒），比直接失败更难排查。
        raise OSError(f"签名密钥不可用（长度 {len(key)} < 16）: {_SIG_KEY}")
    return key


def sign_trace(payload: str) -> str:
    """HMAC-SHA256(整条 evidence 正文) 截 32 位十六进制。"""
    key = _load_key()
    return hmac.new(key, payload.encode("utf-8", "replace"),
                    hashlib.sha256).hexdigest()[:32]


def verify_trace(payload: str, sig: str) -> bool:
    """验签。任何异常一律 False：读不到密钥时宁可不注入，也不放行未验证正文。

    **不重建密钥**（create=False）：密钥被删时既有痕迹应被判无效，而不是换新键
    把所有旧痕迹当伪造。
    """
    try:
        if not sig:
            return False
        key = _load_key(create=False)
        want = hmac.new(key, payload.encode("utf-8", "replace"),
                        hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(want, sig)
    except Exception:  # noqa: BLE001 - 验签失败必须是"不注入"，不是"崩整轮"
        return False

# 复用 integrity.py 的标准提问主干，确保与 workflow /goal 步骤措辞一致。
# 跨项目依赖不可靠时回退到同源硬编码文本（realpath 规范化防路径遍历）。
try:
    _PIPE_SRC = os.path.realpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "session-pipeline", "src"))
    if _PIPE_SRC not in sys.path:
        sys.path.insert(0, _PIPE_SRC)
    from pipeflow.integrity import CORE_QUESTIONS as _CORE_QUESTIONS  # type: ignore
except Exception:  # pragma: no cover - 仅当跨项目路径不可用时
    _CORE_QUESTIONS = [
        "你真的解决了用户的问题吗？",
        "你真的有严格按用户的提示去完成任务吗？",
    ]

# 用户意图分类：守护逻辑需先判定输入是疑问/任务/其他，再决定提醒措辞与强度。
class Intent(str, Enum):
    """用户输入的意图分类（str 子类，便于序列化进 bus 证据与日志）。"""
    QUESTION = "question"   # 疑问：请求澄清 / 求解
    TASK = "task"           # 任务：含明确动作或交付物要求
    OTHER = "other"         # 陈述 / 闲聊：无明确意图

# 人类来源标记：用户原始输入触发提醒；自动化来源排除。
_USER_SOURCES = ("user", "cli")
# 提醒包裹标记，用于幂等检测（已含则不重复注入）。
_REMINDER_MARKER = "【用户意图提醒 · 每轮必对齐】"

_REMINDER_HEADER = (
    f"{_REMINDER_MARKER}\n"
    "你收到的是用户直接指令。开始产出前，先自问并诚实回答两点：\n"
    f"1. {_CORE_QUESTIONS[0]}\n"
    f"2. {_CORE_QUESTIONS[1]}\n"
    "若任一答案为否，先补齐，再结束本轮。\n"
    "────────────────"
)

# 意图判定（简单启发式，不引新依赖）。
# 疑问词覆盖中文常见问法；任务动词覆盖"做/写/修/生成/实现"等动作指令及英文常见动词。
_QUESTION_TAIL = "？?！!"
_QUESTION_WORDS = ("吗", "呢", "怎么", "如何", "为什么", "为何", "是否",
                   "可以", "能不能", "能否", "可否", "什么", "哪个", "谁", "哪里")
_TASK_CN = ("请", "帮我", "做", "写", "修", "修复", "生成", "实现",
            "创建", "添加", "加", "改", "更新", "检查", "分析", "整理",
            "翻译", "总结", "设计", "构建", "部署", "运行", "执行", "对比", "输出",
            # 2026-10-21 对抗审查 D4 实测漏判：「回滚到上一个版本」「再跑一遍测试」
            # 「看一下日志」都被判 OTHER → 重复提醒静默。补常见动作词。
            "回滚", "回退", "删除", "重跑", "再跑", "跑一遍", "看一下", "看看",
            "提交", "恢复", "排查", "日志", "删")
_TASK_EN = ("please", "fix", "write", "implement", "create", "add", "update",
            "deploy", "run", "generate", "build", "refactor", "analyze",
            "review", "test", "debug", "help me",
            "can you", "could you", "would you", "let's")  # 审查 D4：「can you fix…」曾判 other
# 延续指令：无动作词、无问号，但明确要求继续推进既有工作。
# 实测本项目用户两次只回「继续」，被判 OTHER → core.send 不写意图痕迹 →
# daemon 失去触发源 → 整个「你有真的解决用户问题吗」提醒链对该输入完全静默。
# 这类输入恰恰最需要提醒：角色容易把「继续」理解成「随便再做点事」。
_CONTINUATION = ("继续", "接着", "往下", "go on", "continue", "carry on")
# 否定式：「不要继续」「别继续」语义相反，不能被"短消息含继续即 TASK"吞掉。
_NEGATED_CONTINUE = ("不要继续", "别继续", "不用继续", "停止", "先停")


def classify_intent(message: str) -> Intent:
    """判定用户输入是疑问 / 任务 / 其他。

    WHY: 守护需先分类再决定提醒强度。优先级 疑问 > 任务 > 其他：
    同时含问号与动作词时，先澄清疑问更符合用户真实诉求。
    中文任务动词全串扫描（任务常出现在上下文后部）；英文动词仅看开头，
    避免正文提及动词被误判。
    启发式天花板(ponytail): 纯关键词匹配，长任务含"吗"可能被误判为疑问；
    升级路径改为意图分类器或显式 --intent 入参。
    """
    text = (message or "").strip()
    if not text:
        return Intent.OTHER
    # 词级判据只对短消息生效：长任务正文里出现"吗/如何"（常见于被回填的自检问句
    # 「…完成任务吗？」）会把整条任务判成疑问，提醒措辞跟着翻转成"先回答问题"
    # （实测 maintainer bus #374459：/goal 修 hook 被判成 question）。
    # 句末问号仍对任意长度生效——那是用户真的在问。
    if text[-1] in _QUESTION_TAIL or (
        len(text) <= 40 and any(w in text for w in _QUESTION_WORDS)
    ):
        return Intent.QUESTION
    # slash command 带实质参数才是任务（`/goal 修复X` → TASK）；
    # 裸命令（`/goal`、`/compact`）无参数 → OTHER，不注入提醒。
    # WHY: 引擎下发的 `/goal <任务>` 此前被判 OTHER → 零提醒。
    if text.startswith("/"):
        _parts = text.split(None, 1)
        if len(_parts) > 1 and len(_parts[1].strip()) >= 4:
            return Intent.TASK
    if any(v in text for v in _TASK_CN) or text.lower().startswith(_TASK_EN):
        return Intent.TASK
    # 短延续指令：短消息里**出现**即算（不必整条就是）。
    # 「好的，继续」此前因不以「继续」开头被判 OTHER → 提醒静默，而它恰恰是最该
    # 提醒的输入（审查 D4 实测）。长消息仍交给上面的动词判据，避免误伤正文。
    if (len(text) <= 12 and any(c in text.lower() for c in _CONTINUATION)
            and not any(n in text for n in _NEGATED_CONTINUE)):
        return Intent.TASK
    return Intent.OTHER


def repeat_reminder(intent: Intent, round_no: int = 1, request: str = "") -> str:
    """守护每轮重复注入两条自检提醒。request 为原始用户请求摘要，缺失返回 ""。

    WHY: 用户要求 daemon 在处理期间"重复提醒" ccs 自检两个问题，
    防止答非所问或偷懒。round_no 标记轮次，让 ccs 可见这是第几次重复。
    intent 用于措辞微调（疑问强调"先回答用户问题"，任务强调"严格按指令"）。

    WHY 必须带 request: 无正文的自检只剩轮次号，三次升级文本逐字相同，角色
    一旦丢失上下文就无法从中恢复"该对齐哪条请求"，只能回一句"已闭环/待机"
    （2026-10-21 审计：222 次注入，N 封顶 3 且第 N 次与前次逐字一致；qa 独占
    198 次，多为复述）。取不到正文时返回空串，调用方跳过投递——宁可不提醒，
    也不发一条只能被复述的提醒。

    调用方仅可在**确知该角色存在未对齐用户请求**时调用（触发源为 bus 里的
    core.send 注入痕迹）。前提不成立时不调用——不得凭"角色空闲"推断用户在等。

    WHY 结尾要求"证据或下一步"，而不是"回一句结论即可"：旧措辞直接授权了
    「已完成，无新动作。」这类零工具回复。2026-10-21 计数：qa 当天 18 轮，
    全部逐字是这七个字，且都没有任何工具调用（request 是测试残留的
    "test message"，触发器已由 conftest 隔离修掉，但措辞仍在邀请复述）。
    """
    request = (request or "").strip()
    if not request or len(_CORE_QUESTIONS) < 2:
        return ""
    q, t = _CORE_QUESTIONS[0], _CORE_QUESTIONS[1]
    focus = ("（你收到的是疑问，先确保回答了用户的问题）" if intent == Intent.QUESTION
             else "（你收到的是任务，先确保严格按用户指令交付）" if intent == Intent.TASK
             else "")
    return (
        f"{_REMINDER_MARKER} 第{round_no}次重复自检：\n"
        f"原始请求：{request}\n"
        f"1. {q}\n2. {t}\n{focus}\n"
        "已完成就回一行可验证据（命令或文件:行）；未完成就回下一步动作并直接做。"
        "不要只回「已完成，无新动作」，也不要为自检新建任务或子代理。"
    ).rstrip()


# 纯确认语：对它们重复提醒是噪音，对「看一下日志」不是。
_ACK_ONLY = ("好的", "好", "收到", "嗯", "行", "可以", "明白", "知道了", "谢谢",
             "ok", "okay", "thanks", "thx", "sure", "roger")


def is_ack(message: str) -> bool:
    """是否纯确认语（无新任务）。

    WHY 不用 Intent.OTHER 当判据：classify_intent 是关键词启发式，实测把
    「回滚到上一个版本」「再跑一遍测试」「看一下日志」判成 OTHER —— 按 OTHER
    跳过提醒会让真请求彻底静默（审查 D4）。这里只认"整条就是一句确认"。
    """
    text = (message or "").strip().strip("。.!！~～,， ")
    return len(text) <= 6 and text.lower() in _ACK_ONLY


def sanitize_request(text: str, limit: int = 140) -> str:
    """把任意文本压成单行摘要：剥控制字符、折叠空白、超长截断并显形。

    放在本模块而不是 core：daemon 在**自己的边界**也要再洗一遍 —— 签名只证明
    "谁写的"，不证明"内容是单行的"；同 uid 的签名者可以塞多行正文，把
    「第9次重复自检：…」伪造进 pane（审查 P2-4）。
    天花板：脱敏（审批密钥/裸 key）在 core._req_digest，本函数只做结构清洗。
    """
    text = (text or "").encode("utf-8", "replace").decode("utf-8")
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
    return text[:limit - 1].rstrip() + "…" if len(text) > limit else text


def is_user_source(source: str) -> bool:
    """仅人类来源（user/cli）触发提醒；pipeline/daemon 自动化排除。"""
    return source in _USER_SOURCES


def _should_inject(message: str) -> bool:
    """仅疑问或任务类输入才注入提醒（避免陈述/闲聊噪音）。"""
    return classify_intent(message) in (Intent.QUESTION, Intent.TASK)


def inject_reminder(message: str, source: str = "") -> str:
    """若消息来自用户且为疑问/任务且未含提醒标记，则前置注入意图提醒。

    行为 (R70): 仅对 classify_intent 判定为 QUESTION/TASK 的输入注入；
    陈述类输入跳过（减少噪音）。daemon 负责 repeated reminder。

    防御：CORE_QUESTIONS 缺一或问句为空时返回原消息（不抛错、不丢失）。
    """
    # 分类结果必须能被调用方取出：daemon 的重复提醒曾硬编码 Intent.TASK，
    # 于是疑问被按任务措辞提醒。此处挂到函数属性上（inject_reminder.intent），
    # 调用方复用同一份判定而不是各自重算 —— 重算会在 message 已被包裹后
    # 拿到不同结果（含提醒前缀的文本尾部是「再结束本轮。」，判定必然漂移）。
    inject_reminder.intent = classify_intent(message) if is_user_source(source) else None
    if not is_user_source(source):
        return message
    if _REMINDER_MARKER in message:
        return message  # 幂等：已注入过，不重复
    if len(_CORE_QUESTIONS) < 2 or not all(_CORE_QUESTIONS[:2]):
        return message  # 回退数据缺失 → 原样投递
    if not _should_inject(message):
        return message  # R70: 非疑问/任务 → 跳过
    # 提醒放消息之后，不在之前：slash command（/goal、/compact）必须保持首行，
    # 前置提醒会让 Claude CLI 把 `/goal ...` 当普通文本而非命令解析。
    return f"{message}\n\n{_REMINDER_HEADER}"
