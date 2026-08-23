"""轻量自检：断言意图判定与提醒语正确（无框架，python3 selfcheck_intent_guardian.py 即可跑）。

WHY: 守护逻辑是用户要求的核心护栏，需可运行自检证明判定与提醒两件事正确。
路径指向 session-launcher 主仓库 src（编辑在主仓库，非 worktree）。
"""
import sys
from pathlib import Path

# 唯一真相源：session-launcher 主仓库 src。
_REPO = Path("/home/administrator/session-launcher/src")
sys.path.insert(0, str(_REPO))
from user_intent_reminder import (  # noqa: E402
    classify_intent, inject_reminder, repeat_reminder, Intent, _CORE_QUESTIONS,
)
from ccls_daemon_intent_classifier import IntentReminderDaemon  # noqa: E402


def test_classify():
    # 疑问：以问号结尾 / 含疑问词
    assert classify_intent("这个 bug 怎么修？") == Intent.QUESTION
    assert classify_intent("你能帮我看看吗") == Intent.QUESTION
    assert classify_intent("能否解释下 tmux socket 隔离") == Intent.QUESTION
    # 任务：含动作动词 / 交付物要求（含长上下文前的任务动词、英文任务）
    assert classify_intent("请帮我写一个日志解析脚本") == Intent.TASK
    assert classify_intent("生成一份周报") == Intent.TASK
    assert classify_intent("blah " * 50 + "分析下架构") == Intent.TASK
    assert classify_intent("please fix this bug") == Intent.TASK
    # 其他：无明确意图
    assert classify_intent("你好") == Intent.OTHER
    assert classify_intent("今天天气真好") == Intent.OTHER
    assert classify_intent("") == Intent.OTHER


def test_reminder_text():
    # repeat_reminder 必须覆盖用户要求的两个自检问题
    r = repeat_reminder(Intent.TASK, round_no=2)
    assert _CORE_QUESTIONS[0] in r          # 你真的解决了用户的问题吗？
    assert _CORE_QUESTIONS[1] in r          # 你真的有严格按用户的提示去完成任务吗？
    assert "第2次" in r
    # daemon _reminder_text 同样覆盖
    d = IntentReminderDaemon()._reminder_text
    assert _CORE_QUESTIONS[0] in d and _CORE_QUESTIONS[1] in d


def test_inject_filter_and_idempotent():
    # 非疑问/任务（闲聊）被 _should_inject 过滤，不注入
    assert inject_reminder("你好", source="user") == "你好"
    # 疑问/任务类注入，且幂等
    m = "帮我部署服务"
    once = inject_reminder(m, source="user")
    assert once != m and _CORE_QUESTIONS[0] in once
    twice = inject_reminder(once, source="user")  # 已实现幂等，不重复包裹
    assert once == twice
    # 非人类来源不注入
    assert inject_reminder(m, source="pipeline") == m


def test_daemon_reuses_single_source():
    # daemon 复用同一 Intent 枚举与 reminder 文案（single source of truth）
    d = IntentReminderDaemon()
    assert d._reminder_text and _CORE_QUESTIONS[0] in d._reminder_text


if __name__ == "__main__":
    test_classify()
    test_reminder_text()
    test_inject_filter_and_idempotent()
    test_daemon_reuses_single_source()
    print("PASS: 意图判定 + 重复提醒 + 幂等注入 + daemon 复用单一真相源 全部断言通过")
