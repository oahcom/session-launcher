"""parallel_worker.py — 线程池并行工作执行器

为 auto_cycle 提供 parallel_execute()，用最多8个线程并行处理独立工作项。
stdlib only: concurrent.futures.ThreadPoolExecutor.
"""
import concurrent.futures
import sys
import time
from pathlib import Path
from typing import Callable, Any

MAX_WORKERS = 16

def parallel_execute(
    work_items: list[dict],
    worker_fn: Callable[[dict], dict],
    max_workers: int = MAX_WORKERS,
    timeout_per_item: int = 120,
) -> list[dict]:
    """并行执行一批独立工作项，返回结果列表。
    
    Args:
        work_items: 工作项列表（每个是 dict）
        worker_fn: 处理单个工作项的函数 → dict
        max_workers: 最大并行数（默认8）
        timeout_per_item: 单个超时秒数（默认120）
    
    Returns:
        results: 与 work_items 顺序对应的结果列表
    """
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_to_idx = {pool.submit(worker_fn, item): i for i, item in enumerate(work_items)}
        for future in concurrent.futures.as_completed(fut_to_idx, timeout=sum(timeout_per_item for _ in work_items)):
            idx = fut_to_idx[future]
            try:
                result = future.result(timeout=timeout_per_item)
            except concurrent.futures.TimeoutError:
                result = {"success": False, "item": work_items[idx], "error": "timeout"}
            except Exception as e:
                result = {"success": False, "item": work_items[idx], "error": str(e)}
            results.append((idx, result))
    # Restore original order
    results.sort(key=lambda x: x[0])
    return [r for _, r in results]


def parallel_ccs_tasks(tasks: list[dict], db_path: str) -> list[dict]:
    """并行处理多个CCS任务（WorkflowClient.create_task_v2 调用）。
    
    Args:
        tasks: [{"title":..., "assignee":..., "template_id":..., "initiator_role":...}, ...]
        db_path: workflow DB 路径
    Returns:
        每个任务的 (task_id, wf_id) 或 None
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workflow.client import WorkflowClient
    
    def _create_one(t: dict) -> dict:
        role = t.get("initiator_role", "coordinator")
        wc = WorkflowClient(role, db_path=db_path)
        try:
            tid, wid = wc.create_task_v2(
                t["title"], t["assignee"],
                t.get("template_id", "WL-01"),
                role
            )
            return {"success": True, "task_id": tid, "wf_id": wid, "title": t["title"]}
        except Exception as e:
            return {"success": False, "title": t["title"], "error": str(e)}
        finally:
            wc.close()
    
    return parallel_execute(tasks, _create_one)

if __name__ == "__main__":
    import json, sys
    print("parallel_worker.py — 并行工作执行器")
    print(f"  MAX_WORKERS = {MAX_WORKERS}")
    # Self-test
    def dummy(item: dict) -> dict:
        time.sleep(0.1)
        return {"result": item["id"] * 2}
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    rs = parallel_execute(items, dummy)
    assert [r["result"] for r in rs] == [2, 4, 6], f"self-test failed: {rs}"
    print("  self-test: ✅")
