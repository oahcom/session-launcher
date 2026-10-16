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

import os
import sys
from enum import Enum

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
            "翻译", "总结", "设计", "构建", "部署", "运行", "执行", "对比", "输出")
_TASK_EN = ("please", "fix", "write", "implement", "create", "add", "update",
            "deploy", "run", "generate", "build", "refactor", "analyze",
            "review", "test", "debug", "help me")


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
    if text[-1] in _QUESTION_TAIL or any(w in text for w in _QUESTION_WORDS):
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
    return Intent.OTHER


def repeat_reminder(intent: Intent, round_no: int = 1) -> str:
    """守护每轮重复注入两条自检提醒。

    WHY: 用户要求 daemon 在处理期间"重复提醒" ccs 自检两个问题，
    防止答非所问或偷懒。round_no 标记轮次，让 ccs 可见这是第几次重复。
    intent 用于措辞微调（疑问强调"先回答用户问题"，任务强调"严格按指令"）。

    调用方仅可在**确知该角色存在未对齐用户请求**时调用（触发源为 bus 里的
    core.send 注入痕迹）。前提不成立时不调用——不得凭"角色空闲"推断用户在等。
    """
    q, t = _CORE_QUESTIONS[0], _CORE_QUESTIONS[1]
    focus = ("（你收到的是疑问，先确保回答了用户的问题）" if intent == Intent.QUESTION
             else "（你收到的是任务，先确保严格按用户指令交付）" if intent == Intent.TASK
             else "")
    return (
        f"{_REMINDER_MARKER} 第{round_no}次重复自检：\n"
        f"1. {q}\n2. {t}\n{focus}"
    ).rstrip()


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
