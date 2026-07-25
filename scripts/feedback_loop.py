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
    """检查 gate 超时（lifecycle 迁移后 LifecycleManager 无 check_gate_timeouts，直接 SQL 查询）。"""
    import json, time, sqlite3
    _db = Path.home() / ".hermes" / "state" / "workflows.db"
    if not _db.exists():
        return ""
    try:
        conn = sqlite3.connect(str(_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT instance_id, step_results, created_at FROM workflow_instances WHERE status='running'"
        ).fetchall()
        now = time.time()
        lines = []
        for r in rows:
            sr = json.loads(r["step_results"] or "{}")
            for step_id, sdata in sr.items():
                if sdata.get("status") == "running":
                    started = sdata.get("ts") or r["created_at"] or now
                    elapsed_h = (now - started) / 3600
                    if elapsed_h > 2:
                        lines.append(f'GATE_TIMEOUT: wf={r["instance_id"]} step={step_id} elapsed={elapsed_h:.1f}h timeout=2h')
        conn.close()
        return "\n".join(lines) if lines else "No gate timeouts"
    except Exception as e:
        return f"gate timeout check failed: {e}"
if __name__ == "__main__":
    import sys
    if "--step-engine" in sys.argv:
        print(activate_step_engine())
    else:
        main()
