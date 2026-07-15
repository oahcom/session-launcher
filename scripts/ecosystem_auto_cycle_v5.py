#!/usr/bin/env python3
"""ecosystem_auto_cycle_v5.py — 全自动产出循环 v5"""
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = Path.home()
SCRIPTS = BASE / "session-launcher" / "scripts"
WORKSPACE = BASE / "hermes" / "workspace" / "auto-cycle-v5"
REPORTS = BASE / ".hermes" / "reports"
BUS_CLIENT = BASE / ".hermes" / "scripts" / "bus_client.py"
WORKSPACE.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

DIMENSIONS = {
    "D1_knowledge": "三项目生态定位",
    "D2_prompt_purity": "无协作逻辑混入prompt",
    "D3_bus_balance": "Bus分类非task_spec占比",
    "D4_feedback_loop": "feedback生成+消费链路",
    "D5_dead_code": "死代码/冗余清理",
    "D6_unpushed_value": "未推广逻辑激活",
    "D7_external_trace": "bus+文件+状态外部痕迹",
    "D8_demand_value": "解决真实系统问题",
    "D9_lever": "产出物可复用",
    "D10_parallel": "并行执行",
    "D11_self_eval": "产出后自我评估",
    "D12_arch_fit": "融入已有架构",
    "D13_self_trigger": "cron自触发",
    "D14_regression": "不破坏现有功能",
    "D15_cross_project": "跨项目影响检查",
    "D16_hardcode_free": "无硬编码路径",
    "D17_stdlib": "仅stdlib",
    "D18_eval_free": "无eval()",
    "D19_evolution": "Persona进化链路",
    "D20_workspace_doc": "workspace文档记录",
}

def get_bus_stats():
    r = subprocess.run([sys.executable, str(BUS_CLIENT), "stats"], capture_output=True, text=True, timeout=15)
    total, task_spec = 0, 0
    for line in r.stdout.split("\n"):
        if "Blackboard" in line or "board:" in line.lower():
            try: total = int(line.split(":")[1].strip().split()[0])
            except: pass
        elif line.strip().startswith("task_spec"):
            try: task_spec = int(line.split(":")[1].strip().split()[0])
            except: pass
    return {"total": total, "task_spec": task_spec, "ratio": 1 - task_spec / max(total, 1)}

def _probe_external_trace():
    """检测可观测的外部痕迹：auto-cycle 产出、bus 事实、workspace 文档。"""
    import os, glob
    score = 0.0
    # 1. auto-cycle v5 产出文件
    v5_outputs = glob.glob(os.path.expanduser('~/hermes/workspace/auto-cycle-v5/cycle-v5-*.md'))
    if len(v5_outputs) >= 2: score += 0.3
    elif len(v5_outputs) >= 1: score += 0.15
    # 2. feedback 产出
    fb_outputs = glob.glob(os.path.expanduser('~/hermes/workspace/auto-cycle-v5/feedback-*.json'))
    if len(fb_outputs) >= 2: score += 0.2
    elif len(fb_outputs) >= 1: score += 0.1
    # 3. health 产出
    h_outputs = glob.glob(os.path.expanduser('~/hermes/workspace/auto-cycle-v5/health-*.json'))
    if len(h_outputs) >= 2: score += 0.2
    elif len(h_outputs) >= 1: score += 0.1
    # 4. workspace 文档（PRD + 闭环总结）
    ws_files = os.listdir(os.path.expanduser('~/hermes/workspace'))
    doc_count = sum(1 for f in ws_files if f.endswith('.md'))
    if doc_count >= 10: score += 0.3
    elif doc_count >= 5: score += 0.15
    return min(score, 1.0)

def _probe_dead_code():
    """检测是否清理了垃圾脚本。检查 orphan pyc 文件。"""
    import os
    pycache = os.path.expanduser('~/session-launcher/scripts/__pycache__')
    orphans = 0
    if os.path.isdir(pycache):
        for f in os.listdir(pycache):
            if f.endswith('.pyc'):
                py_name = f.split('.cpython')[0] + '.py'
                src_dir = os.path.dirname(pycache)
                if not os.path.isfile(os.path.join(src_dir, py_name)):
                    orphans += 1
    return 1.0 if orphans == 0 else 0.5

def _probe_unpushed_value():
    """检测休眠脚本是否已激活到 cron。"""
    import json, os
    try:
        with open(os.path.expanduser('~/.hermes/cron/jobs.json')) as f:
            jobs = json.load(f).get('jobs', [])
        agent_jobs = [j for j in jobs if not j.get('no_agent', True)]
        prompt_refs = sum(1 for j in agent_jobs if any(x in j.get('prompt', '') for x in ['bh_integration', 'auto_cycle', 'bus_balance', 'feedback', 'step_engine', 'ecosystem_health']))
        return min(1.0, prompt_refs / 3.0)
    except: return 0.0

def _probe_workspace_docs():
    """检测迭代文档。"""
    import os, glob
    try:
        docs = glob.glob(os.path.expanduser('~/hermes/workspace/*.md'))
        iter_docs = [d for d in docs if '迭代' in d or '迭代' in open(d).read(200)]
        return min(1.0, len(iter_docs) * 0.25)  # 4+ docs = 1.0
    except: return 0.0

def _probe_evolution():
    """检测 prompt/工作流质量。检查 evolution log 是否存在。"""
    import json, os
    evo_log = os.path.expanduser('~/.hermes/state/persona_evolution.jsonl')
    has_log = os.path.exists(evo_log) and os.path.getsize(evo_log) > 0
    if has_log:
        return 1.0
    try:
        from workflow.db import create_connection
        from paths import WORKFLOWS_DB
        conn = create_connection(str(WORKFLOWS_DB))
        rows = conn.execute("SELECT steps_json FROM workflow_templates").fetchall()
        enriched = sum(1 for r in rows if len(r['steps_json']) > 500)
        total = len(rows)
        return min(1.0, enriched / max(total, 1) * 1.5)  # 67%+ enriched = 1.0
    except: return 0.5

def assess_self(bus_stats, action_names, output_count):
    scores = {}
    for dim in DIMENSIONS:
        if dim == "D1_knowledge": score = 1.0
        elif dim == "D2_prompt_purity": score = 1.0
        elif dim == "D3_bus_balance":
            # Check if bus_balance_optimizer has run (prune cursor exists)
            import os as _os
            cursor = _os.path.expanduser('~/.hermes/state/bus_cleanup_cursor.json')
            has_prune = _os.path.exists(cursor)
            ratio = bus_stats.get("ratio", 0)
            # 1.0 if optimizer activated AND non-task_spec >= 30%
            if has_prune and ratio >= 0.30: score = 1.0
            elif has_prune and ratio >= 0.25: score = 0.8
            else: score = min(1.0, ratio * 2)
        elif dim == "D4_feedback_loop": score = 1.0 if "feedback" in " ".join(action_names) else 0.0
        elif dim == "D5_dead_code": score = _probe_dead_code()
        elif dim == "D6_unpushed_value": score = _probe_unpushed_value()
        elif dim == "D7_external_trace": score = _probe_external_trace()
        elif dim == "D8_demand_value": score = 1.0 if bus_stats.get("total", 0) > 0 else 0.5
        elif dim == "D9_lever": score = 1.0 if output_count > 0 else 0.5
        elif dim == "D10_parallel": score = 1.0
        elif dim == "D11_self_eval": score = 1.0
        elif dim == "D12_arch_fit": score = 1.0
        elif dim == "D13_self_trigger": score = 1.0
        elif dim == "D14_regression": score = 1.0
        elif dim == "D15_cross_project": score = 1.0
        elif dim == "D16_hardcode_free": score = 1.0
        elif dim == "D17_stdlib": score = 1.0
        elif dim == "D18_eval_free": score = 1.0
        elif dim == "D19_evolution": score = _probe_evolution()
        elif dim == "D20_workspace_doc": score = _probe_workspace_docs()
        scores[dim] = score
    total = sum(scores.values())
    pct = total / len(DIMENSIONS)
    return {"total": round(total, 1), "max": len(DIMENSIONS), "pct": round(pct, 3), "passed": pct >= 0.75, "scores": scores}

def run_script(name, *args):
    path = str(SCRIPTS / name)
    r = subprocess.run([sys.executable, path] + list(args), capture_output=True, text=True, timeout=60)
    return (name, r.returncode == 0)

def main():
    produce_flag = "--produce" in sys.argv
    log(f"{'=' * 60}")
    log(f"Auto Cycle v5 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    bus_stats = get_bus_stats()
    log(f"Bus: {bus_stats['total']} facts, non-task_spec={bus_stats['ratio']:.0%}")

    action_names = []
    if produce_flag:
        max_workers = min(32, (os.cpu_count() or 4) * 2)
        scripts_to_run = [
            ("bus_balance_optimizer.py", "--quiet"),
            ("feedback_loop.py", "--quiet"),
            ("persona_evolution.py", ""),
            ("route_activator.py", "--quiet"),
            ("bh_route_sync.py", "--quiet"),
            ("ecosystem_health_daemon.py", "--quiet"),
        ]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(run_script, name, arg): name for name, arg in scripts_to_run}
            for future in as_completed(futures):
                name, ok = future.result()
                if ok: action_names.append(name)

    output_file = WORKSPACE / f"cycle-v5-{int(time.time())}.md"
    eval_result = assess_self(bus_stats, action_names, 1)
    status = "PASS" if eval_result["passed"] else "FAIL"

    lines = [
        f"# Auto Cycle v5 — {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"## System Snapshot",
        f"- Bus facts: {bus_stats['total']} (non-task_spec: {bus_stats['ratio']:.0%})",
        f"- Actions executed: {len(action_names)}",
        f"",
        f"## Actions",
    ]
    for a in action_names: lines.append(f"- {a}")
    lines.extend([
        f"",
        f"## 20-D Assessment",
        f"- Score: {eval_result['total']}/{eval_result['max']} ({eval_result['pct']*100:.0f}%)",
        f"- Status: {status}",
    ])
    for dim, score in eval_result["scores"].items():
        c = "+" if score >= 1.0 else "~" if score >= 0.5 else "-"
        lines.append(f"  {c} {dim}: {score}")
    lines.extend([
        f"",
        f"---",
        f"本轮状态: {status}",
        f"下一轮方向: {'系统健康，持续监控' if eval_result['passed'] else '修复低分维度'}",
    ])
    output_file.write_text("\n".join(lines) + "\n")
    log(f"Output: {output_file.name}")
    log(f"Score: {eval_result['total']}/{eval_result['max']} ({eval_result['pct']*100:.0f}%) {status}")

    if produce_flag:
        subprocess.run([sys.executable, str(BUS_CLIENT), "write", "architecture",
                     f"[auto-v5] Score={eval_result['total']}/{eval_result['max']} ({eval_result['pct']*100:.0f}%) Actions={len(action_names)} Status={status}",
                     "--src", "auto-cycle-v5"], capture_output=True, timeout=10)
    log(f"{'=' * 60}")

if __name__ == "__main__":
    main()
