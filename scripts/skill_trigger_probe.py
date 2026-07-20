#!/usr/bin/env python3
"""skill_trigger_probe.py — 自动化隐式触发率测量

通过分析 Claude 响应文本判断 skill 是否被加载。
"""
import subprocess, sys, json, time
from pathlib import Path

SKILL_MARKERS = {
    "impl_plan": ["E1:", "E2:", "依赖:", "验证:", "回滚:", "E1-E5"],
    "risk_assess": ["风险矩阵", "概率", "影响", "缓解", "P0", "P1", "P2", "风险"],
}

TEST_PROMPTS = {
    "impl_plan": [
        "帮我拆一下这个编码任务的步骤",
        "这个需求要怎么实现？列一下步骤",
        "我需要写一个排序算法，拆一下任务",
        "实现一个用户登录功能，分解步骤",
        "帮我规划一下这个功能的实现路径",
    ],
    "risk_assess": [
        "评估一下这个方案的风险",
        "有什么潜在问题？帮我分析",
        "这个改动有没有风险？",
        "帮我看看这个设计有什么风险",
        "这个变更的风险评估",
    ],
}

def run_claude(prompt: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout + result.stderr

def measure_trigger_rate(skill: str, trials: int = 10) -> dict:
    prompts = TEST_PROMPTS.get(skill, [])
    markers = SKILL_MARKERS.get(skill, [])
    if not prompts or not markers:
        return {"skill": skill, "error": "no prompts or markers"}
    
    loaded = 0
    results = []
    for i in range(trials):
        prompt = prompts[i % len(prompts)]
        try:
            response = run_claude(prompt)
            triggered = any(m in response for m in markers)
            if triggered:
                loaded += 1
            results.append({"trial": i+1, "prompt": prompt[:50], "triggered": triggered})
        except subprocess.TimeoutExpired:
            results.append({"trial": i+1, "prompt": prompt[:50], "triggered": False, "error": "timeout"})
        time.sleep(1)
    
    rate = loaded / trials if trials > 0 else 0
    return {
        "skill": skill,
        "trials": trials,
        "loaded": loaded,
        "rate": rate,
        "pass": rate >= 0.8,
        "results": results,
    }

if __name__ == "__main__":
    skills = sys.argv[1:] if len(sys.argv) > 1 else ["impl_plan", "risk_assess"]
    for skill in skills:
        result = measure_trigger_rate(skill, trials=10)
        status = "✅" if result["pass"] else "❌"
        print(f"{status} {skill}: {result['loaded']}/{result['trials']} ({result['rate']:.0%})")
        for r in result["results"]:
            mark = "✅" if r["triggered"] else "❌"
            print(f"  {mark} #{r['trial']}: {r['prompt']}{r.get('error', '')}")
        print()
