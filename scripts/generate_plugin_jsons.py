#!/usr/bin/env python3
"""为共享源 skill 目录批量生成 plugin.json。"""
import json, re
from pathlib import Path

SKILLS_ROOT = Path.home() / "shared-skills" / "hermes-origin" / "skills"

def extract_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n(?:---|\.\.\.)', text, re.DOTALL)
    if not m:
        return {"name": skill_md.parent.name, "description": ""}
    fm = m.group(1)
    name = ""
    description = ""
    desc_line_started = False
    desc_indent = 0
    for line in fm.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            val = line.split(":", 1)[1]
            start_stripped = val.strip()
            if start_stripped == ">-":
                desc_line_started = True
                desc_indent = len(val) - len(val.lstrip())
                description = ""
            else:
                description = start_stripped.strip('"').strip("'")
        elif desc_line_started:
            stripped = line
            if not stripped.strip():
                desc_line_started = False
            elif stripped.startswith("  ") and len(stripped) - len(stripped.lstrip()) > desc_indent:
                description += " " + stripped.strip()
            else:
                desc_line_started = False
    if not name:
        name = skill_md.parent.name
    return {"name": name, "description": description.strip()}

def main():
    total = len(list(SKILLS_ROOT.rglob("SKILL.md")))
    count = 0
    for skill_md in sorted(SKILLS_ROOT.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        plugin_path = skill_dir / "plugin.json"
        fm = extract_frontmatter(skill_md)
        category = skill_dir.parent.name
        plugin = {
            "name": fm["name"] or skill_dir.name,
            "version": "1.0.0",
            "description": fm.get("description", ""),
            "category": category,
            "dependencies": [],
            "compatible": ">=2.1.0",
            "compatible_clients": ["claude"]
        }
        plugin_path.write_text(json.dumps(plugin, ensure_ascii=False, indent=2) + "\n")
        count += 1
    print(f"生成完成: {count}/{total} 个 plugin.json")

if __name__ == "__main__":
    main()
