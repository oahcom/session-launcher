"""paths 公开常量契约：仓外消费者按**名字** import，删掉就是运行时 ImportError。

2026-10-22 实测两次：死代码扫描按"仓库内 0 引用"删掉 CCS_CLI —— 而
`~/.hermes/scripts/ccs_monitor.py`（ccs@*.service 的 ExecStart）正是
`from paths import CCS_CLI`，删后 coordinator 连退 5 次、任何 ccs@ 角色单元
重启即死，仓库内 460 条测试全绿。仓库内引用数不是消费者全集。
"""
from pathlib import Path

import paths


# 仓外/跨仓按名取用的常量。新增仓外读者时同步加一行（说明写注释，别只加名字）。
_EXTERNAL = {
    "CCS_CLI": "~/.hermes/scripts/ccs_monitor.py:37（ccs@*.service ExecStart）",
    "BUS_CLIENT": "全部 bus 写入方按此路径起子进程",
    "SESSION_PIPELINE_SRC": "launcher 侧 import pipeline 模块的唯一入口",
}


def test_external_consumers_can_import():
    for name, why in _EXTERNAL.items():
        target = getattr(paths, name, None)
        # 真值 + 目标存在两件事分开判：常量被删 → AttributeError 前先拿到 None；
        # 常量还在但指向被挪走的文件 → 同样让 ccs@ 单元启动即崩（codex 审查 P3 补）。
        # Path() 包一层：conftest 把 BUS_CLIENT 换成 str 桩。
        assert target and Path(target).exists(), \
            f"paths.{name} 缺失或指向不存在路径（仓外消费者会 ImportError）：{why}"


def test_all_declares_only_existing_names():
    missing = [n for n in paths.__all__ if not hasattr(paths, n)]
    assert not missing, f"__all__ 声明了不存在的名字: {missing}"
