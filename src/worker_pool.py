#!/usr/bin/env python3
"""
Worker Pool — parallel codex exec for each CCS dispatcher.

Architecture:
  Dispatcher (CCS, 1 API call) 
    ├── Worker 1 (codex exec, parallel)
    ├── Worker 2 (codex exec, parallel)  
    ├── Worker N (codex exec, parallel)
    └── Aggregator (collects → decides → bus)
    
With 11 dispatchers × 15 parallel workers × 60s:
  = 11 × 15 × 60 = 9,900 calls/hr ✓
"""
import asyncio
import json
import time
import sys
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import io
from paths import ROUTER_API_ENDPOINT

POOL_DIR = Path("/tmp/cdx-pools")
MAX_CONCURRENT = 100  # per dispatcher

def worker_sync(task_id: str, prompt: str, timeout: int = 60) -> dict:
    """Single HTTP call to 9Router — 1 API call, parallel-safe, ~50MB less RAM than codex exec."""
    start = time.time()
    try:
        payload = {
            "model": "9router_hermes",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 5000,
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ROUTER_API_ENDPOINT,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        text = resp.read().decode("utf-8")
        # Strip SSE suffix if present (9Router sometimes appends data: [DONE])
        if "data:" in text:
            text = text[:text.index("data:")].rstrip()
        data = json.loads(text)
        if data.get("status") == "error":
            return {"task_id": task_id, "success": False, "error": data.get("errorMessage","9Router error"), "api_calls": 1, "elapsed": round(time.time()-start,2)}
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        elapsed = time.time() - start
        return {
            "task_id": task_id,
            "success": True,
            "output": data.get("choices", [{}])[0].get("message", {}).get("content", ""),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "elapsed": round(elapsed, 2),
            "api_calls": 1,
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "task_id": task_id,
            "success": False,
            "output": "",
            "error": str(e),
            "elapsed": round(elapsed, 2),
            "api_calls": 1,
        }

def dispatch_sync(role: str, tasks: list[dict]) -> list[dict]:
    """Dispatch N parallel HTTP workers, return all results."""
    n_workers = min(len(tasks), MAX_CONCURRENT)
    with ThreadPoolExecutor(max_workers=n_workers) as exe:
        results = list(exe.map(
            lambda t: worker_sync(t["id"], t["prompt"]),
            tasks
        ))
    return results

def run_parallel(role: str, tasks: list[dict]) -> dict:
    """Synchronous entry point for CCS sessions."""
    start = time.time()
    results = dispatch_sync(role, tasks)
    elapsed = time.time() - start
    
    success_count = sum(1 for r in results if r["success"])
    total_calls = sum(r["api_calls"] for r in results)
    total_tokens = sum(r.get("completion_tokens", 0) for r in results)
    
    summary = {
        "role": role,
        "total_tasks": len(tasks),
        "success": success_count,
        "failed": len(tasks) - success_count,
        "total_api_calls": total_calls,
        "total_tokens": total_tokens,
        "elapsed_sec": round(elapsed, 2),
        "calls_per_hour": round(total_calls / (elapsed / 3600)) if elapsed > 0 else 0,
        "results": results,
    }
    
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    (POOL_DIR / f"{role}.json").write_text(json.dumps(summary, indent=2))
    
    return summary

if __name__ == "__main__":
    """CLI: python3 worker_pool.py <role> <json_tasks>
    
    json_tasks format: [{"id": "...", "prompt": "..."}, ...]
    """
    if len(sys.argv) < 3:
        print("Usage: worker_pool.py <role> '<json_tasks>'")
        sys.exit(1)
    
    role = sys.argv[1]
    tasks = json.loads(sys.argv[2])
    result = run_parallel(role, tasks)
    print(json.dumps(result, ensure_ascii=False, indent=2))
