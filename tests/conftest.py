"""conftest.py — 测试路径初始化，确保 src/、src/ops/ 和 session-pipeline/src 在 sys.path 中。"""
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
_OPS = str(Path(__file__).resolve().parent.parent / "src" / "ops")
_PIPELINE_SRC = str(Path.home() / "session-pipeline" / "src")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _OPS not in sys.path:
    sys.path.insert(1, _OPS)
if _PIPELINE_SRC not in sys.path:
    sys.path.insert(2, _PIPELINE_SRC)
