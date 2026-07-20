#!/usr/bin/env python3
"""convert_skills_to_skillmd.py — 批量将 .md skill 转换为 SKILL.md 目录格式

分类规则：
  行为约束: 技能名含 redline、enforce、禁止、不允许 等 → KNOWLEDGE 块
  流程参考: 其他 → SKILL.md（按需加载）

用法:
  python3 convert_skills_to_skillmd.py               # 全部转换（到 staging）
  python3 convert_skills_to_skillmd.py --role pg      # 只转 PG
  python3 convert_skills_to_skillmd.py --apply        # 从 staging 应用到 skills/
"""
import argparse, json, shutil, sys
from pathlib import Path

SKILLS_DIR = Path.home() / "shared-skills" / "hermes-origin" / "skills"
STAGING_DIR = Path.home() / "shared-skills" / "hermes-origin" / "skills_staging"

# 行为约束 skill 清单（精确路径）
BEHAVIORAL_SKILLS = {
    "pg/redline_enforce",
    "pg/decision_ladder",
    "coordination/cluster_monitor",
    "lr/redline_check",
}

DESCRIPTION_TEMPLATES = {
    "impl_plan": "编码前将任务拆分为可独立验证的步骤（E1-E5）。用户说'写代码'或'实现'时触发。这是流程工具，非行为约束。",
    "risk_assess": "编码前对每个步骤做风险矩阵评估（概率×影响）。用户说'评估风险'或'有什么风险'时触发。这是流程工具，非行为约束。",
    "write_code": "编写、修改或审查代码。用户需要编码实现时触发。",
    "write_tests": "为代码编写测试用例。用户说'加测试'或'测试覆盖'时触发。",
    "refactor": "重构代码，改善结构不改变行为。用户说'重构'或'优化代码'时触发。",
    "debug": "调试和修复代码缺陷。用户说'修bug'或'出错了'时触发。",
    "git_commit": "提交代码变更到 git。用户说'提交'或'commit'时触发。",
    "health_check": "检查服务健康状态。定时巡检时触发。",
    "diagnose": "诊断故障根因。用户说'诊断'或'出问题了'时触发。",
    "auto_fix": "自动修复已知问题。检测到故障时触发。",
    "verify_fix": "验证修复是否生效。修复完成后触发。",
    "test_plan": "制定测试计划。用户说'测试计划'时触发。",
    "test_case_write": "编写测试用例。用户说'写用例'时触发。",
    "test_execute": "执行测试并记录结果。用户说'跑测试'时触发。",
    "test_report": "生成测试报告。测试完成后触发。",
    "deploy_plan": "制定部署计划。用户说'部署计划'时触发。",
    "infra_provision": "配置基础设施资源。用户说'配环境'或'搭建'时触发。",
    "monitor_setup": "配置监控告警。用户说'加监控'时触发。",
    "incident_response": "响应和处理生产事件。用户说'告警'或'线上问题'时触发。",
    "rollback": "执行回滚操作。用户说'回滚'时触发。",
    "cluster_monitor": "监控 CCS/Codex 集群状态。定时巡检时触发。",
    "sentinel_audit": "审计哨兵文件一致性。定时巡检时触发。",
    "cross_role_dispatch": "跨角色调度任务。用户说'调度'或'分配任务'时触发。",
    "escalation": "升级问题到更高级别。问题持续未解决时触发。",
    "brief_write": "编写状态简报。用户说'简报'或'状态报告'时触发。",
    "intake_analysis": "分析需求输入。收到新需求时触发。",
    "prd_write": "编写产品需求文档。用户说'PRD'或'需求文档'时触发。",
    "system_design": "进行系统设计。用户说'系统设计'或'架构设计'时触发。",
    "task_spec": "将需求拆解为可执行任务。用户说'任务拆解'时触发。",
    "adr_write": "记录架构决策。做出架构决策时触发。",
    "doc_structure": "规划文档结构。用户说'文档结构'时触发。",
    "api_doc_write": "编写 API 文档。用户说'API 文档'时触发。",
    "changelog_write": "编写变更日志。用户说'changelog'时触发。",
    "readme_write": "编写 README 文件。用户说'README'时触发。",
    "version_bump": "更新版本号。用户说'版本号'或'release'时触发。",
    "requirement_analysis": "分析用户需求。收到需求时触发。",
    "user_story_write": "编写用户故事。用户说'用户故事'时触发。",
    "priority_rank": "对任务进行优先级排序。用户说'优先级'时触发。",
    "acceptance_verify": "验证验收标准。用户说'验收'时触发。",
    "owasp_scan": "执行 OWASP Top 10 安全检查。用户说'安全扫描'时触发。",
    "threat_model": "进行威胁建模。用户说'威胁建模'时触发。",
    "vuln_assess": "评估漏洞严重程度。发现漏洞时触发。",
    "compliance_check": "检查合规性要求。用户说'合规'时触发。",
    "d1_correctness": "审查代码正确性。Code review 时触发。",
    "d2_security": "审查代码安全性。Code review 时触发。",
    "d3_maintainability": "审查代码可维护性。Code review 时触发。",
    "d4_performance": "审查代码性能。Code review 时触发。",
    "d5_consistency": "审查代码一致性。Code review 时触发。",
    "d6_testability": "审查代码可测试性。Code review 时触发。",
    "tech_selection": "进行技术选型评估。用户说'技术选型'时触发。",
    "decision_record": "记录技术决策。做出技术决策时触发。",
    "tradeoff_analysis": "进行权衡分析。用户说'权衡'或'对比'时触发。",
    "evidence_chain": "分析证据链。用户说'排查'或'追踪'时触发。",
    "stack_trace_analysis": "分析堆栈跟踪。出现崩溃时触发。",
    "root_cause_identify": "定位根因。诊断问题时触发。",
    "fix_propose": "提出修复方案。定位根因后触发。",
    "cross_stack_correlation": "跨栈关联分析。用户说'跨栈'时触发。",
    "systemic_root_cause": "分析系统性根因。同类问题重复出现时触发。",
    "triage_decision": "进行分诊决策。收到新问题报告时触发。",
    "log_analysis": "分析日志。用户说'查日志'时触发。",
    "escalation_rule": "应用升级规则。问题处理超时时触发。",
    "backlog_scan": "扫描积压任务。用户说'积压'或'清理'时触发。",
    "close_loop": "执行闭环操作。用户说'闭环'时触发。",
    "close_report": "生成闭环报告。闭环完成后触发。",
    "profile_analysis": "分析性能剖析数据。用户说'性能分析'或'profile'时触发。",
    "bottleneck_identify": "识别性能瓶颈。性能分析时触发。",
    "optimization_apply": "应用优化方案。识别瓶颈后触发。",
    "benchmark": "执行基准测试。用户说'基准测试'或'benchmark'时触发。",
    "goal_manage": "管理 GOAL 目标。用户说'goal'或'目标'时触发。",
    "bus_poll": "轮询总线消息。定时巡检时触发。",
    "code_write": "编写代码（Codex 模式）。用户说'写代码'时触发。",
    "verify_commit": "验证提交。代码编写完成后触发。",
    "budget_wrap": "预算收尾。预算接近上限时触发。",
    "argument_parse": "解析论点。用户说'论证'或'辩论'时触发。",
    "evidence_check": "核验证据。用户说'证据'时触发。",
    "verdict_write": "编写裁决。辩论完成时触发。",
    "escalation": "升级问题到更高级别。双方僵持时触发。",
    "knowledge_extract": "抽取知识。用户说'知识提取'时触发。",
    "knowledge_organize": "组织知识。知识提取后触发。",
    "knowledge_verify": "验证知识。用户说'知识验证'时触发。",
    "knowledge_publish": "发布知识。知识验证通过后触发。",
    "skill_audit": "审计技能库。用户说'技能审计'时触发。",
    "skill_sync": "同步技能库。技能变更后触发。",
    "category_map": "维护分类映射。用户说'分类'时触发。",
    "quality_gate": "执行质量门禁。用户说'质量门禁'时触发。",
    "github_scan": "扫描 GitHub 项目。用户说'扫描'或'调研'时触发。",
    "trend_analysis": "分析技术趋势。扫描完成后触发。",
    "opportunity_eval": "评估机会价值。用户说'机会评估'时触发。",
    "report_write": "编写研究报告。调研完成后触发。",
    "ccs_health_check": "检查 CCS 健康状态。定时巡检时触发。",
    "log_anomaly_detect": "检测日志异常。用户说'异常检测'时触发。",
    "alert_write": "编写告警消息。检测到异常时触发。",
    "consume_validate": "消费并验证数据。收到新数据时触发。",
    "quality_check": "执行质量检查。数据验证通过后触发。",
    "lesson_distill": "提炼经验教训。完成回顾后触发。",
}

def generate_description(skill_path: str) -> str:
    """从文件名和路径生成 auto-detection description。"""
    skill_name = Path(skill_path).stem
    if skill_name in DESCRIPTION_TEMPLATES:
        return DESCRIPTION_TEMPLATES[skill_name]
    return f"角色 {Path(skill_path).parent.name} 的 {skill_name} 技能。按需使用。"

def convert_skill_file(md_path: Path, staging: bool = True) -> bool:
    """将单个 .md 文件转换为 SKILL.md 目录格式。"""
    base_dir = STAGING_DIR if staging else Path.home() / "shared-skills" / "hermes-origin" / "skills"
    rel = md_path.relative_to(Path.home() / "shared-skills" / "hermes-origin" / "skills")
    skill_name = rel.stem
    role_dir = rel.parent.name
    skill_path = f"{role_dir}/{skill_name}"
    
    # 行为约束 → 不转换（留在 KNOWLEDGE 块）
    if skill_path in BEHAVIORAL_SKILLS:
        return False
    
    content = md_path.read_text()
    desc = generate_description(skill_path)
    
    target_dir = base_dir / role_dir / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_md = target_dir / "SKILL.md"
    
    yaml_frontmatter = f"""---
description: >-
  {desc}
models: []
visibility: auto
---
"""
    if not skill_md.exists():
        skill_md.write_text(yaml_frontmatter + content)
        return True
    return False

def convert_all(staging: bool = True, roles: list[str] | None = None):
    converted = 0
    for md_file in sorted((Path.home() / "shared-skills" / "hermes-origin" / "skills").rglob("*.md")):
        if roles:
            role = md_file.relative_to(Path.home() / "shared-skills" / "hermes-origin" / "skills").parent.name
            if role not in roles:
                continue
        if convert_skill_file(md_path, staging=staging):
            converted += 1
            print(f"  {md_file.relative_to(Path.home() / 'shared-skills' / 'hermes-origin' / 'skills')}")
    print(f"转换完成: {converted} 个 skill")

def apply_staging():
    """从 staging 应用到 skills/ 目录。"""
    applied = 0
    for skill_dir in STAGING_DIR.rglob("SKILL.md"):
        rel = skill_dir.relative_to(STAGING_DIR)
        target = Path.home() / "shared-skills" / "hermes-origin" / "skills" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_dir, target)
        applied += 1
        print(f"  {rel}")
    print(f"应用完成: {applied} 个 SKILL.md")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert .md skills to SKILL.md format")
    parser.add_argument("--role", "-r", action="append", help="仅转换指定角色（可重复）")
    parser.add_argument("--apply", action="store_true", help="从 staging 应用到 skills/ 目录")
    args = parser.parse_args()
    
    if args.apply:
        apply_staging()
    else:
        convert_all(staging=True, roles=args.role)
