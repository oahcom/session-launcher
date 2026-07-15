#!/usr/bin/env python3
"""feedback_loop.py — 从 Bus 信号生成反馈闭环"""
import json, os, subprocess, sys, time
from pathlib import Path; from collections import Counter
BASE = Path.home()
BUS_CLIENT = Path(os.environ.get("BUS_CLIENT", str(BASE / ".hermes" / "scripts" / "bus_client.py")))
STATE_FILE = BASE / ".hermes" / "state" / "feedback_cursor.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
SOURCES = ["reflexion_lesson", "blocker", "code_fix", "architecture", "notice", "performance", "security"]; MAX_PER_SOURCE = 30
def get_cursor() -> int:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text()).get("cursor", 0)
        except: return 0
    return 0
def set_cursor(c: int):
    STATE_FILE.write_text(json.dumps({"cursor": c, "updated": time.time()}))
def read_facts(cat: str, limit: int = 30) -> list:
    r = subprocess.run([sys.executable, str(BUS_CLIENT), "read", "--cat", cat, "--limit", str(limit), "--json"], capture_output=True, text=True, timeout=15)
    try:
        data = json.loads(r.stdout)
        return [{"id": f["id"], "title": f["title"], "cat": f["category"]} for f in data.get("facts", [])]
    except: return []
def main():
    dry_run = "--dry-run" in sys.argv; quiet = "--quiet" in sys.argv
    if not quiet: print(f"[{time.strftime("%H:%M:%S")}] Feedback Loop")
    cursor = get_cursor()
    if not quiet: print(f"  Cursor: {cursor}")
    all_facts = []
    for src in SOURCES:
        facts = read_facts(src, MAX_PER_SOURCE)
        new_facts = [f for f in facts if f.get("id", 0) > cursor]
        all_facts.extend(new_facts)
        if not quiet: print(f"  {src}: {len(facts)} total, {len(new_facts)} new")
    if not all_facts:
        if not quiet: print("  No new signals, skipping")
        return
    by_cat = Counter(f["cat"] for f in all_facts)
    themes = []
    for cat, count in by_cat.most_common(5):
        sample = [f["title"] for f in all_facts if f["cat"] == cat][:3]
        themes.append(f"{cat}({count}): {"; ".join(sample)}")
    # Add actionable recommendations
    recs = []
    if 'blocker' in by_cat: recs.append(f"{by_cat['blocker']}个blocker需coordinator关注")
    if 'performance' in by_cat: recs.append(f"{by_cat['performance']}个性能信号需optimizer关注")
    if 'security' in by_cat: recs.append(f"{by_cat['security']}个安全信号需security_auditor关注")
    if recs: themes.append(f"建议: {"; ".join(recs)}")
    feedback = f"[feedback-loop] {len(all_facts)} signals: {" | ".join(themes)}"
    if not quiet: print(f"  Feedback: {feedback}")
    if not dry_run:
        try:
            evidence = json.dumps([f["id"] for f in all_facts])
            subprocess.run([sys.executable, str(BUS_CLIENT), "write", "feedback", feedback, "--src", "feedback-loop", "--evidence", evidence], capture_output=True, timeout=10)
        except Exception as e: print(f"  [ERR] write failed: {e}")
        max_id = max(f["id"] for f in all_facts)
        if max_id > cursor: set_cursor(max_id)
    output_dir = BASE / "hermes" / "workspace" / "auto-cycle-v5"; output_dir.mkdir(parents=True, exist_ok=True)
    report = {"ts": time.time(), "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"), "signals": len(all_facts), "by_category": dict(by_cat), "feedback": feedback}
    (output_dir / f"feedback-{int(time.time())}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
if __name__ == "__main__": main()

def activate_step_engine():
    """Activate StepEngine gate timeout checking (previously unused logic)."""
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, "-c", """
import sys; sys.path.insert(0, '/home/administrator/session-launcher/src')
from lifecycle.engine import StepEngine
engine = StepEngine('coordinator')
timeouts = engine.check_gate_timeouts()
if timeouts:
    for t in timeouts:
        print(f'GATE_TIMEOUT: wf={t[\"wf_id\"]} step={t[\"step_id\"]} elapsed={t[\"elapsed_hours\"]}h timeout={t[\"timeout_hours\"]}h')
else:
    print('No gate timeouts')
engine.close()
"""],
            capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        return f"StepEngine check failed: {e}"

if __name__ == "__main__":
    import sys
    if "--step-engine" in sys.argv:
        print(activate_step_engine())
    else:
        main()
