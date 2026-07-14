#!/usr/bin/env python3
"""Guard: ensure codex_ops.py goal mode doesn't have while true loop.
External processes may overwrite the file. This guard runs from cron to fix it."""
import re, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "codex_ops.py"
if not SRC.exists():
    sys.exit(0)

content = SRC.read_text()

# Fix: goal mode should not loop
old_goal = 'if drive == "goal":'
found = False
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if old_goal in line and 'while true' in lines[i+1] if i+1 < len(lines) else '':
        # Replace the goal mode block
        new_lines.append('    if drive == "goal":')
        new_lines.append('        # goal 模式：单次执行，不循环 (guarded)')
        # Find the return statement
        j = i + 1
        while j < len(lines) and 'return' not in lines[j]:
            j += 1
        if j < len(lines):
            # Replace the return line
            indent = len(lines[j]) - len(lines[j].lstrip())
            new_lines.append(f'{" " * indent}return f"codex --model {{CODEX_MODEL}} --dangerously-skip-permissions\\n"')
            # Skip the while/done/return block
            while j < len(lines) and j <= i + 5:
                j += 1
            i = j
        else:
            i += 1
    else:
        new_lines.append(line)
        i += 1

new_content = '\n'.join(new_lines)
if new_content != content:
    SRC.write_text(new_content)
    print(f"Fixed {SRC.name}: removed while true from goal mode")
else:
    print(f"{SRC.name}: goal mode already correct")

# ── Syntax check: restore from git if corrupted ──
import ast
try:
    ast.parse(SRC.read_text())
except SyntaxError as e:
    import subprocess
    subprocess.run(['git', 'checkout', '--', str(SRC)],
                   cwd=SRC.parent.parent, capture_output=True)
    print(f"Restored {SRC.name} from git: {e.msg}")
