"""event_log.py — 统一事件溯源（Event Sourcing 模式）

所有 session 事件以追加式序列写入 SQLite，
支持跨项目审计、时序回放、异常检测。

用法:
    from event_log import EventLog
    log = EventLog()
    log.record("session-launcher", "ccs_started", "maintainer", {"tmux": "ccs-maintainer"})
    events = log.replay(event_type="ccs_started", limit=10)
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

EVENT_DB = Path.home() / ".hermes" / "state" / "event_log.db"

class EventLog:
    """追加式事件日志，支持跨项目统一审计。"""

    def __init__(self, db_path: str = None):
        self._db = Path(db_path) if db_path else EVENT_DB
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                role TEXT NOT NULL,
                payload TEXT DEFAULT '{}'
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_role ON events(role)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        self._conn.commit()

    def record(self, source: str, event_type: str, role: str, payload: dict = None) -> str:
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO events (event_id, ts, source, event_type, role, payload) VALUES (?,?,?,?,?,?)",
            (event_id, time.time(), source, event_type, role, json.dumps(payload or {}, ensure_ascii=False))
        )
        self._conn.commit()
        return event_id

    def replay(self, event_type: str = None, role: str = None, source: str = None,
               limit: int = 50, since: float = 0) -> list[dict]:
        clauses = []
        params = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if role:
            clauses.append("role=?")
            params.append(role)
        if source:
            clauses.append("source=?")
            params.append(source)
        if since:
            clauses.append("ts>?")
            params.append(since)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ts DESC LIMIT ?",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        by_type = {}
        for row in self._conn.execute("SELECT event_type, COUNT(*) as c FROM events GROUP BY event_type"):
            by_type[row[0]] = row[1]
        by_source = {}
        for row in self._conn.execute("SELECT source, COUNT(*) as c FROM events GROUP BY source"):
            by_source[row[0]] = row[1]
        return {"total": total, "by_type": by_type, "by_source": by_source}

    def close(self):
        self._conn.close()

# Self-check on import
if __name__ != "__main__":
    _log = EventLog()
    _log.record("event_log", "module_loaded", "system", {})
    _log.close()
