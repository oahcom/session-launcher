"""workflow_logs 落库脱敏：审批密钥不得以明文进库（bus #429005）。

修复前 gatekeeper 直接 `message[:200]` 落 workflow_logs，生产库累计 107 行
明文审批密钥（98 条 cross_role_send + 9 条 source_verification_denied，
按 actor: workflow_engine 82 / product_architect 8 / ccs-monitor 6 / pipeline 6 / qa 3）。
审批 token 是 secrets.token_urlsafe(32)，能读 workflows.db 的角色即可在 TTL 内
直接 wf confirm —— 落库等于把审批权旁路给全体角色。

契约：
  1. 5 种标签形态全部抹除（生产实测：密钥: / 密钥：/ 审批密钥: / approval_token: / token=）
  2. 无凭证的消息原样保留（不盲抹长串，否则 wf_id/路径/sha 全废）
  3. 放行路径 cross_role_send 落库无 token
  4. 拒绝路径 source_verification_denied 的 message_preview **与** detail 均无 token
  5. 真实入口 intercept 端到端不落明文

运行: cd ~/session-launcher && python3 -m pytest tests/unit/test_gatekeeper_redaction.py -q -p no:cacheprovider
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "routing"))

import gatekeeper as gk  # noqa: E402

# 生产样例（#429005 引用的 id 131436 实际 token）
_TOKEN = "r7sUvvbo9c1Z6Tzmnt8chr76hFPDX50p_yiCmKvTiyg"


def _detail(tmp_path, action):
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))
    conn = r._get_conn()
    row = conn.execute("SELECT detail FROM workflow_logs WHERE action=?",
                       (action,)).fetchone()
    conn.close()
    assert row, f"未写入 {action} 日志"
    return json.loads(row["detail"])


def test_redact_covers_all_label_variants():
    """按泄漏面枚举标签形态，不按实现枚举（#429388：旧用例 5 过但 3 式仍漏）。"""
    for label in ("密钥: ", "密钥：", "审批密钥: ", "approval_token: ",
                  "approval_token=", "token=", "**密钥**: ", "`密钥`: ",
                  '{"approval_token": "', "--token ", "token: ", "Token: "):
        out = gk._redact_secrets(f"{label}{_TOKEN}")
        assert _TOKEN not in out, f"{label!r} 未脱敏: {out!r}"
        assert "***" in out, f"{label!r} 抹除后丢了占位符: {out!r}"


def test_redact_preserves_non_secret_context():
    """不得误伤：wf_id / 路径 / 普通文本原样保留，否则排障记录全废。"""
    msg = "工作流 wf_sub_f93f81010342/s4 产出 ~/.hermes/scripts/x.py （无需审批）"
    assert gk._redact_secrets(msg) == msg


def test_cross_role_send_log_has_no_token(tmp_path):
    """放行路径落库不得含明文 token。"""
    gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))._log_cross_role_send(
        "workflow_engine", "engineer", f"审批密钥: {_TOKEN}")
    d = _detail(tmp_path, "cross_role_send")
    assert _TOKEN not in d["message_preview"], f"落库仍含明文: {d}"
    assert "***" in d["message_preview"], "抹除位置丢了占位符"


def test_violation_log_has_no_token(tmp_path):
    """拒绝路径同样落 message_preview；detail 也可能回显原文，两处都要抹。"""
    gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))._log_violation(
        "ccs-monitor", "engineer", f"密钥: {_TOKEN}", f"回显密钥: {_TOKEN}")
    d = _detail(tmp_path, "source_verification_denied")
    assert _TOKEN not in d["message_preview"], f"违规日志 preview 泄漏: {d}"
    assert _TOKEN not in d["detail"], f"违规日志 detail 泄漏: {d}"


def test_intercept_end_to_end_does_not_persist_token(tmp_path):
    """真实入口复跑：同样的消息进 intercept 后库里查不到该串。"""
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))
    r.intercept("workflow_engine", "engineer", f"[approval] 密钥: {_TOKEN}")
    conn = r._get_conn()
    raw = "".join(row["detail"] for row in conn.execute("SELECT detail FROM workflow_logs"))
    conn.close()
    assert _TOKEN not in raw, "intercept 路径仍把 token 写进了 workflow_logs"


def test_cross_role_send_dedupes_non_red_within_window(tmp_path):
    """非红消息 300s 窗口内相同内容只落 1 行（bus #432065：单条 /goal 曾 3494 行）。"""
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))
    for _ in range(3):
        r._log_cross_role_send("workflow_engine", "pg", "same goal text", "")
    conn = r._get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM workflow_logs "
                     "WHERE action='cross_role_send'").fetchone()["c"]
    conn.close()
    assert n == 1, f"非红重复消息应只落 1 行，实得 {n}"


def test_cross_role_send_red_never_dedupes(tmp_path):
    """红消息逐条落库：速率限制 COUNT 依赖精确行数，去重会削弱门禁。"""
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))
    for _ in range(3):
        r._log_cross_role_send("engineer", "qa", "red msg", "red")
    conn = r._get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM workflow_logs "
                     "WHERE action='cross_role_send'").fetchone()["c"]
    conn.close()
    assert n == 3, f"红消息不得去重，应 3 行实得 {n}"
