#!/usr/bin/env python3
"""CLI wrapper for worker pool — callable from CCS sessions.
Usage: python3 pool_cli.py <role> '<json_tasks>' [--max-workers N]
"""
import sys, json, warnings, os
# Suppress all warnings
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

sys.path.insert(0, '/home/administrator/session-launcher/src')
from worker_pool import run_parallel

if __name__ == '__main__':
    role = sys.argv[1]
    tasks = json.loads(sys.argv[2])
    result = run_parallel(role, tasks)
    # Only print JSON to stdout
    print(json.dumps(result, ensure_ascii=False, indent=2))
