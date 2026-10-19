"""ops/runner.py — 运行循环 + 健康仪表板（从 core.py 提取）"""

import time

from ops.sentinel import list_sentinels
from tmux_ops import _is_alive

from paths import ensure_paths as _ensure_paths
_ensure_paths()


def dashboard() -> str:
    """聚合健康仪表板 + 路由拓扑。"""
    lines = ["╔═════════════════════════════════════════════╗",
             "║     CCS 生态健康仪表板                       ║",
             "╚══════════════════════════════════════════════╝", ""]

    # ── 1. CCS 存活概览 ──
    sents = list_sentinels()
    alive_count = sum(1 for s in sents if _is_alive(s.tmux_session))
    lines.append(f"CCS 哨兵: {len(sents)}  存活: {alive_count}  死亡: {len(sents)-alive_count}")
    lines.append("")

    # ── 2. 每个 CCS 详情 ──
    lines.append(f"{'角色':16} {'状态':6} {'运行':8} {'PID':8} {'伙伴':14} {'bus':14} {'看门狗':6} {'重启':4}")
    lines.append("-" * 88)
    for s in sorted(sents, key=lambda x: x.role):
        alive = _is_alive(s.tmux_session)
        age = int(time.time() - s.started_at)
        partner = s.partners[0] if s.partners else "-"
        box = "✅" if alive else "❌"
        # 未测量（last_watchdog_check=0，角色无 partners）显示 —，不要把默认值当健康
        if not s.health.last_watchdog_check:
            wd = "—"
        else:
            wd = "✅" if s.health.watchdog_ok else "❌"
        lines.append(f"{s.role:16} {box:6} {age//60:3}分{age%60:02d}s "
                     f"{s.pid or 0:<8} {partner:14} {s.bus_track or '-':14} {wd:6} {s.health.restart_count:<4}")

    # ── 3. 路由拓扑摘要 ──
    lines.append("")
    lines.append("── 路由拓扑 ──")
    lines.append("  路由拓扑由 routing.gatekeeper 动态计算，未在此静态汇总")

    return "\n".join(lines)

