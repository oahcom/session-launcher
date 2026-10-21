"""conftest.py — 测试路径初始化，确保 src/ 和 src/ops/ 在 sys.path 中。

不注入 session-pipeline/src：pipeline 与 launcher 都有同名 workflow 包，
pytest rootdir 收集时 pipeline/src 排在 launcher/src 前 → test_wf_cleanup
import workflow.client 会解析到 pipeline 版（无 find_zombies）→ AttributeError。
没有任何测试依赖 pipeline/src 的全局注入（test_wl_selfcheck/test_layer_pollution
均使用硬编码路径，test_gateway 从独立 worktree 导入）。
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parent.parent / "src")
_OPS = str(Path(__file__).resolve().parent.parent / "src" / "ops")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _OPS not in sys.path:
    sys.path.insert(1, _OPS)

PROD_WF_DB = Path.home() / ".hermes" / "state" / "workflows.db"


def assert_workflows_db_isolated() -> None:
    """测试可见的工作流库路径必须全部指向临时库，一个都不许落在生产。

    2026-10-21 实测：只设 HERMES_WORKFLOWS_DB 时，缺省建流会委托给 pipeline
    子进程，子进程读的是 SESSION_PIPELINE_WORKFLOWS_DB → 生产库仍新增
    tasks/workflow_instances 各 1 行（1108/1070 → 1109/1071）。
    """
    import paths
    import workflow.db as _wdb

    prod = str(PROD_WF_DB)
    seen = {
        "paths.WORKFLOWS_DB": str(paths.WORKFLOWS_DB),
        "workflow.db.DB_PATH": str(_wdb.DB_PATH),
        "env HERMES_WORKFLOWS_DB": os.environ.get("HERMES_WORKFLOWS_DB", prod),
        "env SESSION_PIPELINE_WORKFLOWS_DB":
            os.environ.get("SESSION_PIPELINE_WORKFLOWS_DB", prod),
    }
    leaked = [k for k, v in seen.items() if v == prod]
    assert not leaked, f"以下入口仍指向生产 workflows.db: {leaked}"
    # 生产总线同理：DB 隔离但 bus notify 写生产黑板，会向角色派发真任务
    _prod_bus = str(Path.home() / ".hermes" / "sister_bus" / "blackboard.db")
    assert os.environ.get("HERMES_BLACKBOARD_DB", _prod_bus) != _prod_bus, \
        "测试总线仍指向生产 blackboard.db"


@pytest.fixture(scope="session", autouse=True)
def _isolate_workflows_db(tmp_path_factory):
    """把整个测试会话的工作流库重定向到生产库的一次性副本。

    两个环境变量都要钉：子进程只认环境变量，in-process 的模块常量在 import
    时已固化，须显式改写。副本保留全部模板，否则缺省建流的自动推荐分支取
    不到模板，test_default_path_creates_workflow_and_cleans_up 会假红。
    """
    if not PROD_WF_DB.exists():
        yield
        return
    import paths

    tmp = str(tmp_path_factory.mktemp("wfdb") / "workflows.db")
    _src = sqlite3.connect(f"file:{PROD_WF_DB}?mode=ro", uri=True)
    _dst = sqlite3.connect(tmp)
    _src.backup(_dst)
    _dst.close()
    _src.close()

    # SESSION_PIPELINE_TEST：委托子进程走 pipeline 的 create_task_v2，它只认该
    # 环境变量（不看 db_path）—— 不设则 DB 隔到 tmp、bus notify 仍写生产黑板
    # （实测 2026-10-22 00:37 跑 test_wf_create_entry，生产总线新增 #437515
    # "创建任务: 做一个高质量网页探针1792600679"，行本身落在 tmp 库）。
    _wf_env_keys = ("HERMES_WORKFLOWS_DB", "SESSION_PIPELINE_WORKFLOWS_DB")
    _env_keys = _wf_env_keys + ("SESSION_PIPELINE_TEST", "HERMES_BLACKBOARD_DB")
    _saved_env = {k: os.environ.get(k) for k in _env_keys}
    for k in _wf_env_keys:
        os.environ[k] = tmp
    os.environ["SESSION_PIPELINE_TEST"] = "1"
    # 委托子进程的 bus_client 默认写生产黑板，hermes_bus 只认这个环境变量
    os.environ["HERMES_BLACKBOARD_DB"] = str(Path(tmp).parent / "blackboard.db")
    _saved_paths_db = paths.WORKFLOWS_DB
    paths.WORKFLOWS_DB = tmp
    try:
        import workflow.db as _wdb
    except ImportError:
        _wdb = None
    _saved_db_path = getattr(_wdb, "DB_PATH", None)
    if _wdb is not None:
        _wdb.DB_PATH = tmp
    try:
        yield
    finally:
        paths.WORKFLOWS_DB = _saved_paths_db
        if _wdb is not None:
            _wdb.DB_PATH = _saved_db_path
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 模块级自检脚本（非 pytest 用例）在 import 时 raise SystemExit → 收集阶段
# INTERNALERROR，整套测试无法运行。它们自述用途是 `python3 <file>` 手工执行。
collect_ignore = ["test_wl_selfcheck.py"]

