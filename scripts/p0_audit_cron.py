#!/usr/bin/env python3
"""P0 Audit Scan — 30min cron: escalates 1h+ drafts, downgrades 4h+ drafts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / 'session-pipeline' / 'src'))
sys.path.insert(0, str(Path.home() / 'session-launcher' / 'src'))
from p0_exemption import P0Exemption
p = P0Exemption('system')
try:
    results = p.p0_audit_scan()
    if results:
        import json
        print(f"P0 audit: {json.dumps(results)}")
finally:
    p.close()
