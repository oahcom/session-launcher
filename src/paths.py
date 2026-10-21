"""paths.py — 集中管理路径常量

所有路径从 hermes_bus.config 统一导入，消除重复定义。
"""
import os
import sys
from pathlib import Path

_HOME = Path.home()

# ── hermes_bus 路径注册 ──
sys.path.insert(0, str(_HOME / ".hermes" / "scripts"))

# ── 从 hermes_bus.config 统一导入 ──
from hermes_bus.config import (
    BUS_CLIENT,
    SISTER_BUS_CCS_SOCK,
    SISTER_BUS_DKK_SOCK, SISTER_BUS_SSK_SOCK,
)

# re-exports：各模块从 paths 导入以上常量
__all__ = ["BUS_CLIENT",
           "SISTER_BUS_CCS_SOCK",
           "SISTER_BUS_DKK_SOCK", "SISTER_BUS_SSK_SOCK",
           "STARVE_STATE", "SESSION_PIPELINE_SRC"]
# ── 路径常量：由本项目自行管理，不依赖 bus config ──
_SESSION_PIPELINE_SRC = Path(_HOME / "session-pipeline" / "src")
SESSION_ROLES_ROOT = Path(_HOME / "hermes-session-roles")
CCS_WORKSPACES = Path(_HOME / "ccs-workspaces")
LIFECYCLE_SENTINEL_DIR = Path(_HOME / ".hermes" / "run" / "ccs-sentinels")

# ── 数据目录 ──
HERMES_STATE = _HOME / ".hermes" / "state"
# 环境变量口子：测试/演练必须能把工作流库重定向到临时路径，否则一跑就是生产。
WORKFLOWS_DB = Path(os.environ.get("HERMES_WORKFLOWS_DB") or HERMES_STATE / "workflows.db")
# 与 session-pipeline/src/paths.py 保持一致；daemon.py 从 paths 导入此常量
STARVE_STATE = _HOME / ".hermes" / "run" / "wf_starve.json"

# ── 项目根目录 ──
HERMES_SCRIPTS = Path(_HOME / ".hermes" / "scripts")
SESSION_LAUNCHER_SRC = Path(__file__).resolve().parent
# 公开别名：launcher 侧需要直接 import pipeline 模块时复用，避免调用点硬编码路径。
SESSION_PIPELINE_SRC = Path(_SESSION_PIPELINE_SRC)


# ── 角色目录 ──
SESSION_ROLES_PERSONAS = SESSION_ROLES_ROOT / "personas" / "session-roles"

# ── Hermes 工作流 ──
HERMES_WORKFLOWS = _HOME / ".hermes" / "workflows"



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
