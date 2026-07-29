#!/bin/bash
# deploy_role_skills.sh v2 — 带角色名映射的部署脚本
set -e
SKILL_SRC=~/shared-skills/hermes-origin/skills
WS_DIR=~/ccs-workspaces

# 全量角色→技能分类映射（来自 persona JSON 的 skill_refs）
declare -A ROLE_SKILL_DIR=(
  [maintainer]=maintenance
  [coordinator]=coordination
  [ccs-coordinator]=coordination
  [product_architect]=arch
  [security_auditor]=security
  [codex-dev]=codex
  [knowledge_curator]=knowledge
  [debate_verifier]=debate
  [investigator_python]=investigator
  [investigator_senior]=investigator
  [investigator_general]=investigator
  [curator]=curation
  [consumer]=curation
  [scout]=research
  [ccs-monitor]=monitor
  [closer]=closing
  [optimizer]=optimization
  [engineer]=code
  [reviewer]=review
  [archivist]=curation
  [pm]=pm
  [qa]=qa
  [devops]=devops
  [writer]=writer
  [lr]=lr
  [pg]=pg
  [public]=coordination
  [rebutter]=debate
  [regression_test_return]=qa
  [research]=research
  [test]=maintenance
  # 以下角色无 persona JSON，使用默认分类
  [architect]=coordination
  [architecture]=coordination
  [e2e_test]=qa
  [data-analyst]=research
  [verifier]=debate
  [workflow_engine]=maintenance
  [ccs-product_architect]=arch
)

# 跨目录 skill_refs 补充映射
declare -A ROLE_EXTRA_DIRS=(
  [consumer]="knowledge"
  [debate_verifier]="coordination"
  [public]="coordination"
  [ccs-coordinator]="monitor coordination"
  [archivist]="maintenance coordination"
  [rebutter]="coordination"
)

deploy_role() {
  local role=$1
  local ws=${2:-$WS_DIR/$role}
  local sd=${ROLE_SKILL_DIR[$role]:-$role}
  local tgt=$ws/.claude/skills
  [ ! -d "$ws" ] && return 0
  rm -rf "$tgt" 2>/dev/null || true
  mkdir -p "$tgt"
  local count=0
  for skill_dir in "$SKILL_SRC/$sd"/*/; do
    [ -d "$skill_dir" ] || continue
    local name=$(basename "$skill_dir")
    [ -f "$skill_dir/SKILL.md" ] || continue
    case "$name" in redline_enforce|decision_ladder|cluster_monitor|redline_check) continue ;; esac
    ln -sfn "$skill_dir" "$tgt/$name" 2>/dev/null && count=$((count+1))
  done
  IFS=' ' read -ra extra <<< "${ROLE_EXTRA_DIRS[$role]:-}"
  for extra_dir in "${extra[@]}"; do
    for skill_dir in "$SKILL_SRC/$extra_dir"/*/; do
      [ -d "$skill_dir" ] || continue
      local name=$(basename "$skill_dir")
      [ -f "$skill_dir/SKILL.md" ] || continue
      [ -L "$tgt/$name" ] && continue
      ln -sfn "$skill_dir" "$tgt/$name" 2>/dev/null && count=$((count+1))
    done
  done
  [ "$count" -gt 0 ] && echo "  $role ($sd): $count skills"
}

# ── 单角色部署入口（被 core.py start() 调用）──
deploy_single_role() {
  deploy_role "$1" "${2:-}"
  echo "=== role $1 deploy complete ==="
}

# ── 全量部署（手动运行）──
if [ "${1:-}" = "--all" ]; then
  for ws in "$WS_DIR"/*/; do
    deploy_role "$(basename "$ws")"
  done
  echo "=== full deploy complete ==="
elif [ -n "${1:-}" ] && [ "${1#--}" = "$1" ]; then
  deploy_single_role "$1" "${2:-}"
fi
