# Investigation: Blackboard shows 1193 unconsumed facts, route_all returns 0/0

## Root Cause

Semantic mismatch between bus_client.py and poll_unconsumed():

- **bus_client.py unconsumed**: Calls `bb.unconsumed(consumer="claude")` — checks "not consumed by THIS consumer"
- **poll_unconsumed.py line 44**: Calls `bb.unconsumed()` with NO consumer — checks "not consumed by ANYONE"

Since all 1,196 facts have at least one consume log entry (35,247 total), `bb.unconsumed()` (no filter) returns 0.
The "1193 unconsumed" from bus_client refers to facts not consumed by a specific role (e.g. "claude" or "pipeline").

## Blackboard Schema

```sql
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t TEXT NOT NULL, cat TEXT NOT NULL,
    tags TEXT DEFAULT '', tr REAL DEFAULT 0.5,
    e TEXT DEFAULT '', src TEXT DEFAULT '',
    rc INTEGER DEFAULT 0, hc INTEGER DEFAULT 0,
    ts REAL NOT NULL, ut REAL NOT NULL
);
CREATE TABLE log (
    ts REAL NOT NULL, typ TEXT NOT NULL,
    cat TEXT NOT NULL, fid INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_log_consume_unique
    ON log(fid, cat) WHERE typ = 'consume';
```

The `log.cat` column stores the consumer name.

## Facts by Status (SQL-verified)

| Metric | Count |
|---|---|
| Total facts | 1,196 |
| Facts with rc=0 | 993 |
| Facts with rc>0 | 203 |
| Facts with ANY consume log | 35,247 distinct fids |
| Facts never consumed by ANYONE | 0 |
| Facts where rc != actual consume count | 1,079 |

## Schema Drift

None detected. `log` table, `idx_log_consume_unique` index, and all triggers are consistent.

## Proposed Fix (not applied)

`polling.py` line 44: change `bb.unconsumed()` to `bb.unconsumed(consumer=consumer)`

## Secondary Issue: Stale `rc` Column

`IdempotentConsume.safe_consume()` (used by route_all) writes to `log` but does NOT increment `facts.rc`. Only `Blackboard.mark_consumed()` updates `rc`. This causes 1,079 facts to have a stale `rc` value.
