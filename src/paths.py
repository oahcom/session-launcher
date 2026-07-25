"""paths.py — 集中管理路径常量

所有路径从 hermes_bus.config 统一导入，消除重复定义。
"""
import os
import sys
from pathlib import Path

_HOME = Path.home()

# ── 从 hermes_bus.config 统一导入 ──
from hermes_bus.config import (
    BUS_CLIENT, BUS_PROTOCOL,
    SISTER_BUS_CCS_SOCK, SISTER_BUS_FEED_SOCK,
    SISTER_BUS_DKK_SOCK, SISTER_BUS_SSK_SOCK,
    SESSION_PIPELINE_SRC as _SESSION_PIPELINE_SRC,
    SESSION_ROLES_ROOT,
    CCS_WORKSPACES,
    LIFECYCLE_SENTINEL_DIR,
)

# ── 数据目录 ──
HERMES_STATE = _HOME / ".hermes" / "state"
WORKFLOWS_DB = HERMES_STATE / "workflows.db"
CURSOR_DB = HERMES_STATE / "pipeline_cursor.db"
ROUTING_DB = HERMES_STATE / "routing.db"
ACK_TRACKER_DB = HERMES_STATE / "ack_tracker.db"
# ── 模板与配置 ──
HERMES_TEMPLATES = _HOME / ".hermes" / "templates"
HERMES_BACKUPS = _HOME / ".hermes" / "backups"
WORKFLOW_GUIDE = HERMES_TEMPLATES / "WORKFLOW_GUIDE.md"

# ── 项目根目录 ──
HERMES_SCRIPTS = Path(_HOME / ".hermes" / "scripts")
SESSION_LAUNCHER_SRC = Path(__file__).resolve().parent
SESSION_PIPELINE_SRC = Path(_SESSION_PIPELINE_SRC)

# ── CCS CLI（供跨项目引用）──
CCS_CLI = SESSION_LAUNCHER_SRC / "ccs.py"

# ── API Endpoints ──
ROUTER_API_ENDPOINT = os.environ.get('ROUTER_API_ENDPOINT', 'http://localhost:20128/v1/chat/completions')
HEALTH_CHECK_ENDPOINTS = os.environ.get('HEALTH_CHECK_ENDPOINTS', 'http://localhost:8890').split(',')

# ── 角色目录 ──
SESSION_ROLES_PERSONAS = SESSION_ROLES_ROOT / "personas" / "session-roles"

# ── Hermes 工作流 ──
HERMES_WORKFLOWS = _HOME / ".hermes" / "workflows"
HERMES_WORKFLOW_CHAINS = HERMES_WORKFLOWS / "chains"


def ensure_paths() -> None:
    """统一注册跨项目 sys.path。launcher 自身模块优先于 pipeline，防遮蔽。"""
    _entries = [
        SESSION_PIPELINE_SRC.resolve(),
        HERMES_SCRIPTS.resolve(),
    ]
    for p in reversed(_entries):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
    # launcher 自身路径最后插入（最优先）
    _this = str(Path(__file__).resolve().parent)
    if _this not in sys.path:
        sys.path.insert(0, _this)
