"""wake_ccs 必须透传 source —— 防角色空转回归（R88）。

背景（实测）：`ccs send` 的 CLI 默认 `--from cli`，而 `cli` 在
`user_intent_reminder._USER_SOURCES` 人类白名单里。`wake_ccs` 不传 source 时
source 落为 ""，配合上游 `--from` 默认值使**角色间唤醒/系统唤醒被当成人类请求**，
每次注入写一条 `已注入意图提醒` 痕迹；bus 对同内容 UPSERT 刷新 ts，故
`_REQUEST_TTL=1800` 永不生效 → qa 被反复唤醒空转。单条 fact 实测被重写 264 次
（#330525，05:38–11:06），角色输出退化为「qa 无待办（核验第 5 次，状态未变）」。

契约：source 必须区分人类与自动化——人类才触发提醒注入。
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


def _wake_ccs_source_arg() -> str:
    """静态解析 wake_ccs 内 send(...) 的 source 实参文本。"""
    tree = ast.parse((SRC / "core.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "wake_ccs":
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "send":
                    for kw in call.keywords:
                        if kw.arg == "source":
                            return ast.unparse(kw.value)
    raise AssertionError("wake_ccs 内未找到 send(source=...)：source 会落为空串")


def test_wake_ccs_passes_source():
    """wake_ccs 调 send 必须显式传 source（不传即 '' → 语义落灰区）。"""
    arg = _wake_ccs_source_arg()
    assert arg, "source 实参为空"


def test_source_prefers_role_then_automation():
    """有 by_role 时归因到角色；无 by_role 时归因到自动化常量（非人类白名单）。"""
    arg = _wake_ccs_source_arg()
    assert "by_role" in arg, f"应优先用 by_role 归因，实际: {arg}"
    assert "_AUTOMATION_SOURCE" in arg, f"无 by_role 时应回退自动化常量，实际: {arg}"


def test_automation_source_is_not_human():
    """_AUTOMATION_SOURCE 绝不能落在人类来源白名单里，否则提醒又被误注入。"""
    import core
    from user_intent_reminder import is_user_source, _USER_SOURCES

    assert not is_user_source(core._AUTOMATION_SOURCE), \
        f"_AUTOMATION_SOURCE={core._AUTOMATION_SOURCE!r} 被判定为人类来源 → 提醒会再次误注入"
    assert core._AUTOMATION_SOURCE not in _USER_SOURCES


def test_automation_source_is_trusted_gatekeeper_source():
    """_AUTOMATION_SOURCE 必须在 gatekeeper 白名单里，否则唤醒消息被三源验证拒绝。"""
    from routing.gatekeeper import _AUTOMATED_SOURCES

    import core
    assert core._AUTOMATION_SOURCE in _AUTOMATED_SOURCES, \
        f"_AUTOMATION_SOURCE={core._AUTOMATION_SOURCE!r} 不在 _AUTOMATED_SOURCES → 唤醒被拒"


# ── ccs send --from 默认值（R88 空转根因的最后一环） ──

def _send_from_default_expr() -> str:
    """AST 提取 ccs.py 中 `--from` 的 default 表达式源码。"""
    tree = ast.parse((SRC / "ccs.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") != "add_argument":
            continue
        if not any(isinstance(a, ast.Constant) and a.value == "--from" for a in node.args):
            continue
        for kw in node.keywords:
            if kw.arg == "default":
                return ast.unparse(kw.value)
    raise AssertionError("ccs.py 未找到 send 的 --from default")


def test_send_from_default_uses_ccs_role():
    """角色内调用 ccs send 时来源自动归因为该角色（非人类）。

    角色 prompt 教的用法是不带 --from 的裸 `ccs send <角色> 消息`，旧默认 "cli"
    会让角色消息被当人类请求 → 注入提醒 + 写永不过期痕迹 → 对端空转。
    """
    expr = _send_from_default_expr()
    assert "CCS_ROLE" in expr, \
        f"--from 默认值必须优先取 $CCS_ROLE（launcher 已注入），实际: {expr}"
    assert "cli" in expr, f"未设 CCS_ROLE 时应回退 cli（人类手敲），实际: {expr}"


def test_ccs_role_is_not_human_source_when_agent():
    """角色名不得被 user_intent_reminder 判为人类来源，否则空转复发。"""
    from user_intent_reminder import is_user_source
    for role in ("engineer", "qa", "reviewer", "coordinator"):
        assert not is_user_source(role), f"{role} 被误判为人类来源 → 提醒会误注入"
