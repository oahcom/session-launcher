"""conftest — 将 src/ 添加到 sys.path 以便测试导入。"""
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
