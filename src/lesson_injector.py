#!/usr/bin/env python3
"""
lesson_injector.py — 从 bus 读取 reflexion_lesson，归类并注入角色 CLAUDE.md。

接口:
  LessonInjector()

链路:
  reflexion_lesson (bus) → load_lessons() → group_by_role() → deduplicate()
  → format_for_injection() → inject_to_role() → CLAUDE.md KNOWLEDGE 块
"""

import os
import sys
from pathlib import Path
from typing import Optional

# bus_protocol 路径
from paths import HERMES_SCRIPTS as _SCRIPTS_DIR
_BUS_PROTOCOL = _SCRIPTS_DIR / "bus_protocol.py"
if _BUS_PROTOCOL.exists():
    _BP_DIR = str(_SCRIPTS_DIR)
    if _BP_DIR not in sys.path:
        sys.path.insert(0, _BP_DIR)

from bus_protocol import Blackboard, Fact


class LessonInjector:
    """从 refexion_lesson 到 CLAUDE.md 经验教训块的注入器。"""

    def __init__(self):
        self.bb = Blackboard()

    def load_lessons(self, limit: int = 500) -> list[dict]:
        """读取 reflexion_lesson 分类消息，返回 dict 列表。"""
        facts = self.bb.read(cat="reflexion_lesson", limit=limit)
        return [
            {
                "id": f.id,
                "src": f.src,
                "title": f.t or f.title or "",
                "evidence": f.e or f.evidence or "",
                "ts": getattr(f, "ts", 0),
            }
            for f in facts
        ]

    def group_by_role(self, lessons: list[dict]) -> dict[str, list[dict]]:
        """按 src 字段归类到对应角色。"""
        grouped: dict[str, list[dict]] = {}
        for l in lessons:
            src = l.get("src", "unknown")
            grouped.setdefault(src, []).append(l)
        return grouped

    def deduplicate(self, grouped: dict) -> dict:
        """同类 lesson 聚类去重（标题关键词匹配）。

        当前 reflexion_lesson 数据 >99% 为看门狗 STALE_HEARTBEAT 心跳告警。
        聚类去重后同类 lessons 高度集中。
        """
        result: dict[str, list[dict]] = {}
        for src, lessons in grouped.items():
            seen_titles: set = set()
            unique: list[dict] = []
            for l in lessons:
                title = l.get("title", "")
                # STALE_HEARTBEAT 按关键词聚类
                if "STALE_HEARTBEAT" in title:
                    title_key = title.split(":")[0]
                else:
                    title_key = title
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    unique.append(l)
            result[src] = unique
        return result

    def format_for_injection(self, role: str, lessons: list[dict]) -> str:
        """格式化为 markdown 方法论规则块。"""
        if not lessons:
            return ""
        blocks = [f"## 经验教训（{role}）\n"]
        for l in lessons:
            title = l.get("title", "")[:60]
            evidence = l.get("evidence", "")[:200]
            lid = l.get("id", "")
            blocks.append(f"### 教训: {title}")
            if evidence:
                blocks.append(f"问题: {evidence}")
            blocks.append(f"来源: bus #{lid}")
            blocks.append(f"参考来源: #reflexion\n")
        return "\n".join(blocks)

    def inject_to_role(self, role: str, formatted: str) -> bool:
        """写入角色 workspace 的 CLAUDE.md 的 KNOWLEDGE 块内。

        v4 未上线时直接写入文件，v4 上线后走 _get_role_knowledge() 路径。
        """
        # 角色名到工作空间目录的映射
        _ROLE_WS_DIR: dict[str, str] = {
            "coordinator": "ccs-coordinator",
        }
        dirname = _ROLE_WS_DIR.get(role, role)
        path = Path.home() / "ccs-workspaces" / dirname / "CLAUDE.md"
        if not path.exists():
            return False

        content = path.read_text(encoding="utf-8")

        # 构建注入块
        inject_block = f"\n{formatted}\n"

        if "<!-- KNOWLEDGE:START -->" in content:
            # 已有 KNOWLEDGE 块，在块末尾追加
            end_marker = "<!-- KNOWLEDGE:END -->"
            idx = content.rindex(end_marker)
            new_content = content[:idx] + inject_block + content[idx:]
        else:
            # 无 KNOWLEDGE 块，追加到末尾
            new_content = (
                content.rstrip()
                + "\n\n<!-- KNOWLEDGE:START -->\n"
                + formatted
                + "\n<!-- KNOWLEDGE:END -->\n"
            )

        path.write_text(new_content, encoding="utf-8")
        return True

    def inject_lessons_to_role(self, role: str, lesson_ids: Optional[list[int]] = None) -> int:
        """从 bus 读取 reflexion_lesson，按角色归类并注入（完整的单步调用）。

        返回注入的 lesson 数量。
        """
        all_lessons = self.load_lessons(limit=500)
        # 如果指定了 lesson_ids，只注入这些
        if lesson_ids is not None:
            id_set = set(lesson_ids)
            all_lessons = [l for l in all_lessons if l.get("id") in id_set]

        grouped = self.group_by_role(all_lessons)
        deduped = self.deduplicate(grouped)
        role_lessons = list(deduped.get(role, []))

        if not role_lessons:
            return 0

        formatted = self.format_for_injection(role, role_lessons)
        if not formatted:
            return 0

        self.inject_to_role(role, formatted)
        return len(role_lessons)
