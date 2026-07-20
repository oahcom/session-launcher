#!/bin/bash
# rollback_skill_migration.sh — 完整回滚技能迁移
set -e
TAG_BEFORE="${1:-v2-pre-skill-migration}"

echo "[0/6] 验证 TAG_BEFORE..."
cd ~/shared-skills/hermes-origin
git rev-parse --verify "$TAG_BEFORE" >/dev/null 2>&1 || { echo "TAG $TAG_BEFORE not found"; exit 1; }

echo "[1/6] 恢复全局 symlink..."
ln -sfn ~/shared-skills/hermes-origin ~/.claude/skills

echo "[2/6] 删除 workspace 项目层 skill..."
for ws in ~/ccs-workspaces/*/; do
  rm -rf "$ws.claude/skills" 2>/dev/null || true
done

echo "[3/6] shared-skills: 恢复原 .md 文件..."
# 先移除 SKILL.md 目录（避免 git checkout 文件/目录冲突）
find ~/shared-skills/hermes-origin/skills -name "SKILL.md" -exec dirname {} \; | sort -u | while read d; do
  rm -rf "$d"
done
cd ~/shared-skills/hermes-origin
git checkout "$TAG_BEFORE" -- skills/

echo "[4/6] hermes-session-roles + session-launcher: 恢复代码..."
cd ~/hermes-session-roles && git checkout "$TAG_BEFORE" -- src/
cd ~/session-launcher && git checkout "$TAG_BEFORE" -- src/routing/roles.py

echo "[5/6] 重新注入完整 KNOWLEDGE 块..."
cd ~/session-launcher
python3 -c "
from src.routing.roles import inject_role_knowledge_into_workspace, load_roles
for role in load_roles():
    name = role.get('name', '')
    if name:
        inject_role_knowledge_into_workspace(role)
        print(f'  restored KNOWLEDGE for {name}')
"

echo "[6/6] 重启所有 CCS session..."
python3 -c "
from src.core import stop as _stop, start as _start
roles = ['pg','qa','coordinator','product_architect','lr','pm','devops',
         'reviewer','security_auditor','writer','maintainer','scout','engineer','closer']
for r in roles:
    try: _stop(r); _start(r); print(f'  restarted {r}')
    except Exception as e: print(f'  FAIL {r}: {e}')
"

echo "回滚完成。运行 redline_probe.py 确认..."
python3 ~/session-launcher/scripts/redline_probe.py
