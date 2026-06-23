# SQLite Housekeeping Audit

## Current Problem

### journal_mode = delete (default)

SQLite's default `journal_mode=delete` writes a rollback journal file before each transaction and deletes it after commit. Under this mode:

- **Only one writer is allowed at a time.** Any concurrent write attempt immediately returns `SQLITE_BUSY` ("database is locked").
- With `busy_timeout=0`, there is **zero retry window** — any lock contention causes an instant failure rather than waiting.
- The orchestrator and cron jobs (health guardian, candidate queue, resource monitor) all write to the same DB concurrently. This combination is the root cause of observed `database is locked` errors.

---

## Why WAL May Help

WAL (Write-Ahead Log) mode (`PRAGMA journal_mode=WAL`) separates reads from writes:

| | DELETE mode | WAL mode |
|---|---|---|
| Concurrent readers | Blocked during writes | Never blocked |
| Concurrent writers | Only 1 allowed | Only 1 allowed (but readers unaffected) |
| Crash recovery | Rollback journal | WAL replay |
| `busy_timeout=0` risk | Immediate failure | Still immediate, but less contention |

With WAL enabled **and** `busy_timeout` set to ~10,000–30,000 ms, the orchestrator's read-heavy cycles will no longer compete with cron write jobs.

**WAL is persistent** — once set, it survives DB restarts. It is not reset by `sqlite3.connect()`.

---

## WAL + Backup: Critical Warning

After enabling WAL, the DB consists of **three files**:

```
mamuyy_hunter.db       ← main data file
mamuyy_hunter.db-wal   ← uncommitted write-ahead log
mamuyy_hunter.db-shm   ← shared memory index
```

**Any backup script (`backup_hunter.sh`, `cp` one-liners) that copies only `mamuyy_hunter.db` will produce a corrupt or incomplete backup if a WAL file exists.**

Safe backup approaches after WAL:
1. Use `sqlite3 mamuyy_hunter.db ".backup /path/to/backup.db"` — this performs a hot backup that includes WAL frames.
2. Or checkpoint first: `PRAGMA wal_checkpoint(FULL)` then copy all three files atomically.
3. Or use the `VACUUM INTO '/path/backup.db'` command (creates a fresh defragmented copy).

The existing `backup_hunter.sh` must be reviewed and updated before WAL is enabled.

---

## Why VACUUM Is Not Run During Live Runtime

`VACUUM` rewrites the entire database into a new file. During this process:

- The DB is exclusively locked for the full duration (~minutes for a 2.5 GB DB).
- All concurrent reads and writes fail with `database is locked`.
- The orchestrator cycle, cron jobs, and health guardian will all error out.

**Never run VACUUM while the `hunter` tmux session is active.**

Safe VACUUM window: only during a planned maintenance window with the orchestrator stopped.

---

## Safe Next Steps (After Audit Review)

In recommended order:

### Step 1 — Set busy_timeout immediately (zero-risk, no downtime)
```python
# In database.py or wherever init_db() is defined, add:
conn.execute("PRAGMA busy_timeout = 15000")  # 15 seconds
```
This is safe to do while the orchestrator is running.

### Step 2 — Review backup_hunter.sh
Confirm whether it copies only the `.db` file. If so, update it to use `.backup` before enabling WAL.

### Step 3 — Enable WAL (during a quiet period, not mid-cycle)
```bash
# Use retrain_safe.sh's idle detection as a guide for timing
sqlite3 mamuyy_hunter.db "PRAGMA journal_mode=WAL;"
sqlite3 mamuyy_hunter.db "PRAGMA wal_autocheckpoint=1000;"
```
Verify the `.db-wal` and `.db-shm` files appear after the first write.

### Step 4 — VACUUM (optional, planned maintenance window only)
```bash
# Stop orchestrator first
tmux send-keys -t hunter C-c
sleep 5
sqlite3 mamuyy_hunter.db "VACUUM;"
# Restart orchestrator
```

### Step 5 — Run monthly retrain
Only after WAL and busy_timeout are confirmed stable. Use `retrain_safe.sh`.

---

## Diagnostic Script

```bash
cd /home/ubuntu/mamuyy-binance-hunter
.venv/bin/python scripts/sqlite_housekeeping_audit.py
```

Output: `reports/sqlite_housekeeping_audit.json`
