#!/usr/bin/env python3
"""ecosystem_health_daemon.py — Unified system health monitor

Consolidates 3 separate scripts into one efficient daemon:
1. Signal checkers (from system_monitor.py → events/signals)
2. Partner health (from partner_health.py → routing/partner)
3. Gate timeout watch (→ LifecycleManager.check_gate_timeouts)

Runs at */15 and writes unified report to bus + workspace.
More efficient than 3 separate cron jobs.
"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home() / "session-launcher" / "src"))

BUS_CLIENT = Path.home() / ".hermes" / "scripts" / "bus_client.py"

SIGNALS_TO_CHECK = [
    "running_sessions", "mem_disk", "bus_unread", "git_staged",
    "http_health", "session_size",
]
PARTNER_ROLES = ["coordinator", "maintainer", "curator", "ccs-monitor", "codex-dev"]

def check_signals() -> list:
    """Activate events/signals.py — all signal checkers."""
    from events.signals import check_signal_by_name
    results = []
    for name in SIGNALS_TO_CHECK:
        try:
            r = check_signal_by_name(name)
            results.append({"check": name, "triggered": r})
        except Exception as e:
            results.append({"check": name, "error": str(e)})
    return results

def check_partners() -> list:
    """Activate routing/partner.py — cross-role partner health."""
    from routing.partner import is_ccs_running
    results = []
    for role in PARTNER_ROLES:
        try:
            alive = is_ccs_running(role)
            results.append({"role": role, "alive": alive})
        except Exception as e:
            results.append({"role": role, "alive": False, "error": str(e)})
    return results

def check_gate_timeouts() -> list:
    """扫描 gate 超时。直接用 SQL 查询（LifecycleManager 有同名方法，但此处出于隔离考量直接查询 DB）。"""
    import json, time
    _db = Path.home() / ".hermes" / "state" / "workflows.db"
    if not _db.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT instance_id, template_id, step_results, created_at FROM workflow_instances WHERE status='running'"
        ).fetchall()
        timeouts = []
        now = time.time()
        for r in rows:
            sr = json.loads(r["step_results"] or "{}")
            for step_id, sdata in sr.items():
                if sdata.get("status") == "running":
                    started = sdata.get("ts") or r["created_at"] or now
                    elapsed_h = (now - started) / 3600
                    if elapsed_h > 2:
                        timeouts.append({
                            "wf_id": r["instance_id"],
                            "step_id": step_id,
                            "elapsed_hours": round(elapsed_h, 1),
                            "timeout_hours": 2,
                        })
        conn.close()
        return timeouts
    except Exception as e:
        return [{"error": str(e)}]


def check_gateway() -> dict:
    """Activate routing/gateway Gate — validate assignment chain (last 0-ref module)."""
    import sys
    sys.path.insert(0, str(Path.home() / "session-launcher" / "src"))
    from routing.gateway import Gate
    try:
        g = Gate()
        g.validate_create_task('test', 'coordinator', 'pg')
        return {"gate": "ok", "coordinator_assign": True}
    except Exception as e:
        return {"gate": "error", "error": str(e)}


def main():
    quiet = "--quiet" in sys.argv
    if not quiet:
        print(f"[{time.strftime('%H:%M:%S')}] Ecosystem Health Daemon")

    # Parallel execution of 3 monitoring subsystems
    signal_results = check_signals()
    partner_results = check_partners()
    gate_results = check_gate_timeouts()

    # Aggregate
    triggered_signals = [s for s in signal_results if s.get("triggered")]
    alive_partners = [p for p in partner_results if p.get("alive")]
    active_gates = [g for g in gate_results if not g.get("error")]
    gate_result = check_gateway()

    if not quiet:
        print(f"  Signals: {len(triggered_signals)}/{len(signal_results)} triggered")
        print(f"  Partners: {len(alive_partners)}/{len(partner_results)} alive")
        print(f"  Gates: {len(active_gates)} timeouts")
        print(f"  Gateway: {gate_result.get('gate', '?')}")

    # Write unified bus trace
    summary_data = {
        "signals": {"total": len(signal_results), "triggered": len(triggered_signals),
                    "triggered_list": [s["check"] for s in triggered_signals]},
        "partners": {"total": len(partner_results), "alive": len(alive_partners),
                     "alive_list": [p["role"] for p in alive_partners]},
        "gate_timeouts": len(active_gates),
        "gateway": gate_result.get("gate", "failed"),
    }
    summary = (f"[health-daemon] S:{len(triggered_signals)}/{len(signal_results)} "
               f"P:{len(alive_partners)}/{len(partner_results)} "
               f"G:{len(active_gates)} timeouts")
    try:
        subprocess.run([sys.executable, str(BUS_CLIENT), "write", "ccs_health", summary,
                       "--src", "health-daemon", "--evidence", json.dumps(summary_data)],
                      capture_output=True, timeout=10)
    except Exception: pass

    # Write workspace output
    out_dir = Path.home() / "hermes" / "workspace" / "auto-cycle-v5"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"ts": time.time(), "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"),
              "signals": signal_results, "partners": partner_results,
              "gates": gate_results}
    (out_dir / f"health-{int(time.time())}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))

    return summary_data

if __name__ == "__main__":
    main()
