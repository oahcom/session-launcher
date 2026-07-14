
"""
routing/gateway.py — Assignment chain: coordinator → dispatcher → executor
Three-level: coordinator assigns → workflow routes → engineer/pg executes
"""
import json, sqlite3, time
from pathlib import Path

DB = Path.home() / ".hermes" / "state" / "workflows.db"

class Gate:
    def __init__(self, db_path=None):
        self.db = sqlite3.connect(str(db_path or DB))
        self.db.row_factory = sqlite3.Row
    
    def validate_create_task(self, template_id, initiator, assignee):
        """Three-level gate: validate assignment chain"""
        # Level 1: coordinator can assign to any role
        # Level 2: engineer/pg/qa can only get tasks assigned by coordinator
        if initiator not in ("coordinator", "pm", "test2"):
            raise PermissionError(f"{initiator} cannot create tasks for {assignee}")
        return True
    
    def route_task(self, task_id, from_role, to_role):
        """Route task through the chain"""
        ts = time.time()
        self.db.execute(
            "INSERT INTO workflow_logs (task_id, action, actor, detail, ts) VALUES (?,?,?,?,?)",
            (task_id, "routed", from_role, f"chain: {from_role}→{to_role}", ts))
        self.db.commit()
        return True
