#!/usr/bin/env python3
"""
Session Launcher — python3 -m session_launcher 入口。

所有逻辑委托给 launcher.main()，不重复实现。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import main

if __name__ == "__main__":
    main()
