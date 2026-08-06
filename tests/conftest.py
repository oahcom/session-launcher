"""conftest.py — 测试路径初始化，确保 src/ 和 src/ops/ 在 sys.path 中。

不注入 session-pipeline/src：pipeline 与 launcher 都有同名 workflow 包，
pytest rootdir 收集时 pipeline/src 排在 launcher/src 前 → test_wf_cleanup
import workflow.client 会解析到 pipeline 版（无 find_zombies）→ AttributeError。
没有任何测试依赖 pipeline/src 的全局注入（test_wl_selfcheck/test_layer_pollution
均使用硬编码路径，test_gateway 从独立 worktree 导入）。
"""
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
_OPS = str(Path(__file__).resolve().parent.parent / "src" / "ops")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _OPS not in sys.path:
    sys.path.insert(1, _OPS)
