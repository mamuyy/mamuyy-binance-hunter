# VPS Restart Runbook — MAMUYY Hunter
**Created:** 2026-06-24  
**Applies to:** Oracle Cloud Ubuntu 24.04 VPS — `168.110.200.231`  
**System:** MAMUYY Binance Hunter — PAPER_ONLY mode

---

## IMPORTANT: PAPER_ONLY Boundary

Do NOT enable live broker execution during or after restart.
`PAPER_ONLY` is enforced in `config.py` and `main.py`. Do not change it.

---

## 1. Pre-Restart Checklist

Run all checks from inside the VPS before rebooting. Do not reboot if any check fails.

```bash
cd /home/ubuntu/mamuyy-binance-hunter

# 1. Git status — no uncommitted changes outstanding
git status

# 2. Latest backup — confirm recent backup exists
ls -lht ~/hunter_backups/ | head -5

# 3. Disk — must be below 90% before reboot
df -h /

# 4. Active tmux sessions
tmux ls

# 5. WAL mode confirmed
sqlite3 -readonly -cmd ".timeout 30000" mamuyy_hunter.db \
  "PRAGMA journal_mode; PRAGMA busy_timeout;"
# Expected: wal / 30000
```

Only proceed to reboot if:
- `git status` is clean (no staged or modified files)
- At least one backup file exists from the last 48 hours
- Disk usage < 90%
- WAL mode returns `wal`

---

## 2. Safe Reboot Command

```bash
sudo reboot
```

The SSH session will disconnect immediately. This is expected.

---

## 3. Reconnect After Reboot

Wait ~60 seconds for the VPS to come back online, then:

```bash
ssh -i ~/Downloads/ssh-key-2026-05-15.key ubuntu@168.110.200.231
```

If connection is refused, wait another 30 seconds and retry.

---

## 4. Post-Reboot Startup

The `@reboot` cron entry starts only the `api` session automatically.
Run the startup script to restore the full stack:

```bash
cd /home/ubuntu/mamuyy-binance-hunter
./scripts/start_hunter_stack.sh
```

This script is idempotent — safe to run even if some sessions are already up.
It will skip any session that is already running and start only missing ones.

---

## 5. Post-Reboot Verification

Run all checks in order. Do not proceed to the next step if any check fails.

### 5a. tmux sessions
```bash
tmux ls
# Expected: hunter, dashboard, api all present
```

### 5b. API health
```bash
curl -s http://127.0.0.1:8502/health
# Expected: {"ok":true,"ts":"..."}
```

### 5c. Database health
```bash
sqlite3 -readonly -cmd ".timeout 30000" mamuyy_hunter.db \
  "PRAGMA journal_mode; PRAGMA busy_timeout;"
# Expected: wal
#           30000
```

### 5d. Orchestrator log
```bash
tail -80 logs/hunter_tmux.log 2>/dev/null || \
  tmux capture-pane -t hunter -p | tail -30
# Expected: cycle running, no ERROR or Traceback lines
```

### 5e. Disk post-reboot
```bash
df -h /
# Expected: < 85% used
```

### 5f. API /status endpoint (full health)
```bash
curl -s http://127.0.0.1:8502/status | python3 -m json.tool | head -40
```

---

## 6. Incident Recovery

### Hunter session missing after reboot
```bash
cd /home/ubuntu/mamuyy-binance-hunter
./scripts/start_hunter_stack.sh
```

### Only dashboard missing
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
tmux new-session -d -s dashboard \
  "cd ~/mamuyy-binance-hunter && source .venv/bin/activate && \
   streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 \
   2>&1 | tee -a logs/dashboard_tmux.log"
```

### Only API missing
```bash
cd /home/ubuntu/mamuyy-binance-hunter
tmux new-session -d -s api \
  "cd ~/mamuyy-binance-hunter && source .venv/bin/activate && \
   uvicorn hunter_api:app --host 127.0.0.1 --port 8502 \
   2>&1 | tee -a logs/api_tmux.log"
```

### DB locked (busy_timeout error)
Wait 60 seconds, then retry the read-only query:
```bash
sqlite3 -readonly -cmd ".timeout 30000" mamuyy_hunter.db "PRAGMA journal_mode;"
```
If still locked after 2 minutes, check for stuck orchestrator process:
```bash
ps aux | grep "main.py --orchestrator" | grep -v grep
```
Do NOT run VACUUM or CHECKPOINT manually. Do NOT force-kill the process unless
the orchestrator has been unresponsive for >10 minutes.

### Orchestrator crashing on startup (Traceback in logs)
Check the log first:
```bash
tmux capture-pane -t hunter -p | tail -50
```
Common causes:
- DB schema mismatch after update — run `python main.py --health` to trigger migration
- Model file missing — check `model_registry.json` and restore from backup
- Config error — check `config.py` for syntax errors after any recent edit

---

## 7. What the @reboot Cron Entry Handles

The existing crontab `@reboot` entry starts only the `api` session:
```
@reboot sleep 30 && cd /home/ubuntu/mamuyy-binance-hunter && \
  tmux new -d -s api 'source .venv/bin/activate && \
  uvicorn hunter_api:app --host 127.0.0.1 --port 8502' 2>/dev/null || true
```

`hunter` and `dashboard` sessions are **not** auto-started on reboot.
Always run `./scripts/start_hunter_stack.sh` manually after reboot.

---

## 8. SSH Tunnel for Dashboard / API Access

The dashboard (8501) and API (8502) bind to `127.0.0.1` only (not public).
Access from your local machine via SSH tunnel:

```bash
# On local machine — forwards both ports:
ssh -i ~/Downloads/ssh-key-2026-05-15.key \
  -L 8501:127.0.0.1:8501 \
  -L 8502:127.0.0.1:8502 \
  ubuntu@168.110.200.231

# Then open in browser:
# Dashboard: http://localhost:8501
# API:       http://localhost:8502/health
```

---

_Runbook created 2026-06-24. Review after any emergency procedure or major code change._
