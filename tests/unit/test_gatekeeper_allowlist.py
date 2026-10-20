"""门禁放行面回归：模板边必须被信任，两源查询不得用窗口/文本解析。

三条契约，每条对应一次实测假阴性（R110）：
  1. 主链 8 条模板边（engineer->qa 等）不在 persona 自述里 → 必须仍放行
  2. DB assigner 源按「活跃实例」定向查，不按最近 N 行窗口
  3. bus 源：base64/方括号不炸 FTS5；从 JSON 顶层 src 读，不解析 stdout 文本

运行: cd ~/session-launcher && python3 -m pytest tests/unit/test_gatekeeper_allowlist.py -q -p no:cacheprovider
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "routing"))

import gatekeeper as gk  # noqa: E402


MAIN_CHAIN_EDGES = [
    ("engineer", "qa"), ("engineer", "scout"), ("pm", "engineer"),
    ("product_architect", "engineer"), ("qa", "closer"),
    ("reviewer", "engineer"), ("reviewer", "writer"), ("writer", "closer"),
]


@pytest.mark.parametrize("src,dst", MAIN_CHAIN_EDGES)
def test_template_edge_is_allowed(src, dst):
    """模板声明的执行面边必须放行——否则引擎自己派发的交接被自己拦住。"""
    assert dst in gk.WORKGROUP_MATRIX.get(src, set()), \
        f"模板边 {src}->{dst} 不在允许面：主链会在这一步断掉"


def test_template_edges_never_shrink_persona_matrix():
    """并入是并集，不能覆盖 persona 自述（曾因赋值搞掉 coordinator 的 *）。"""
    assert "*" in gk.WORKGROUP_MATRIX["coordinator"]


def _router_with_db(tmp_path):
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "wf.db"))
    conn = r._get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS workflow_instances(
        instance_id TEXT PRIMARY KEY, assigner TEXT, status TEXT,
        created_at REAL)""")
    return r, conn


def test_db_assigner_hits_when_active_instance_is_old(tmp_path):
    """活跃实例排在第 11 行之后也必须命中（窗口法在此恒假阴性）。"""
    r, conn = _router_with_db(tmp_path)
    now = 1e9
    # 10 条更新的、已终结的实例，把活跃实例挤出任何固定窗口
    conn.executemany("INSERT INTO workflow_instances VALUES(?,?,?,?)",
                     [(f"new{i}", "qa", "completed", now + i) for i in range(10)])
    conn.execute("INSERT INTO workflow_instances VALUES(?,?,?,?)",
                 ("old", "engineer", "running", now))
    conn.commit()
    assert r._check_db_assigner("engineer") == "matched"
    conn.close()


def test_db_assigner_ignores_settled_instances(tmp_path):
    """已终结实例不得让角色永久免检。"""
    r, conn = _router_with_db(tmp_path)
    conn.execute("INSERT INTO workflow_instances VALUES(?,?,?,?)",
                 ("done", "writer", "completed", 1.0))
    conn.execute("INSERT INTO workflow_instances VALUES(?,?,?,?)",
                 ("blk", "closer", "delivery_blocked", 1.0))
    conn.commit()
    assert r._check_db_assigner("writer") == ""
    assert r._check_db_assigner("closer") == "matched", "暂停投递的实例仍算活着"
    conn.close()


def test_db_assigner_fails_closed_on_bad_db(tmp_path):
    """库不可读 → 空串（不计分），不得抛穿门禁。"""
    r = gk.CrossRoleRouter(db_path=str(tmp_path / "nope" / "x.db"))
    assert r._check_db_assigner("engineer") == ""


@pytest.mark.parametrize("message,token", [
    ("[workflow] 审批请求 wf_sub_f93f81010342/s4 请确认", "wf_sub_f93f81010342"),
    ("needs_implementation @product_architect task_abc123", "task_abc123"),
])
def test_bus_source_extracts_id_token(message, token):
    """优先取 wf_/task_ 前缀 ID：它在 bus 里几乎唯一，普通词会命中大批无关事实。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        class R:
            returncode = 0
            stdout = json.dumps({"facts": [{"src": "engineer", "id": 1}]})
        return R()

    with patch("gatekeeper.subprocess.run", fake_run):
        assert gk.CrossRoleRouter()._check_bus_source("engineer", message) == "matched"
    assert token in seen["cmd"], f"未用 ID token 检索: {seen['cmd']}"


def test_bus_source_reads_json_src_not_stdout_text():
    """旧实现找 `--src`/`src=` 文本，而 search 输出是 `[id] (cat) title` → 恒不匹配。"""
    class R:
        returncode = 0
        stdout = json.dumps({"facts": [{"src": "qa", "id": 7}]})
    with patch("gatekeeper.subprocess.run", return_value=R()):
        assert gk.CrossRoleRouter()._check_bus_source("engineer", "wf_x1234") == ""
        assert gk.CrossRoleRouter()._check_bus_source("qa", "wf_x1234") == "matched"


def test_bus_source_fails_closed_on_garbage():
    """子进程输出不是可信输入：非 JSON / 非零退出都不得放行。"""
    class Bad:
        returncode = 0
        stdout = "FTS5: syntax error near ["
    with patch("gatekeeper.subprocess.run", return_value=Bad()):
        assert gk.CrossRoleRouter()._check_bus_source("engineer", "wf_x1234") == ""
    with patch("gatekeeper.subprocess.run", side_effect=TimeoutError):
        assert gk.CrossRoleRouter()._check_bus_source("engineer", "wf_x1234") == ""


def test_short_message_skips_bus_lookup():
    """无可用 token 时不做子进程（每次红消息 15s 超时的代价）。"""
    with patch("gatekeeper.subprocess.run") as sp:
        assert gk.CrossRoleRouter()._check_bus_source("engineer", "ok") == ""
    assert not sp.called
