# MAMUYY Hunter Operator Runbook

_Last updated: 2026-06-23 | Mode: PAPER_ONLY | VPS: Oracle Cloud Ubuntu 24.04_

---

## Safety Envelope / PAPER_ONLY Boundary

MAMUYY Hunter remains **PAPER_ONLY**. Every procedure in this runbook is scoped to monitoring, diagnostics, and safe maintenance only.

Never use these commands to:
- route broker orders or enable live trading
- mutate execution state or trade tables directly
- deploy strategies or auto-promote Phase 3
- retrain models outside `retrain_safe.sh`
- tune thresholds automatically

The Phase 3 readiness pipeline is evidence maintenance only. It must not be interpreted as permission for live execution.

---

## 1. System Overview

| Item | Value |
|------|-------|
| VPS | Oracle Cloud Ubuntu 24.04 |
| IP | 168.110.200.231 |
| App dir | `/home/ubuntu/mamuyy-binance-hunter` |
| tmux sessions | `hunter` (orchestrator), `dashboard` (Streamlit UI) |
| Database | `mamuyy_hunter.db` (SQLite, WAL mode enabled) |
| Mode | **PAPER_ONLY** |
| Python env | `.venv/bin/activate` |
| SSH key | `~/Downloads/ssh-key-2026-05-15.key` |

---

## 2. Daily Health Checks

Run these every day to confirm the system is healthy.

### Check tmux sessions
```bash
tmux ls
```
Expected: both `hunter` and `dashboard` sessions present.

### Check heartbeat
```bash
tail -20 logs/hunter.log | grep -E "heartbeat|ERROR|WARN"
```

### Check disk usage
```bash
df -h /
```
Alert threshold: **>85%**. Action required: **>90%**.

### Check recent logs (last 50 lines)
```bash
tail -50 logs/hunter.log
tail -20 logs/guardian_cron.log
```

### Check open trades count
```bash
sqlite3 -readonly mamuyy_hunter.db "SELECT status, COUNT(*) FROM internal_paper_trades GROUP BY status ORDER BY COUNT(*) DESC;"
```

---

## 3. Weekly Checks

### Review guardian_cron.log
```bash
tail -100 logs/guardian_cron.log
```
Look for: ERROR, WARN, missed heartbeats.

### Review backup_cron.log
```bash
tail -50 logs/backup_cron.log
```
Confirm backups completed without errors. Verify `quick_check: ok`.

### Review daily_ops_report.log
```bash
tail -100 logs/daily_ops_report.log
```
Look for anomalies in daily PnL, trade counts, or signal quality.

### Check disk trend
```bash
df -h .
du -sh logs/ manual_audit_backups/ hunter_backups/ 2>/dev/null
```
If `manual_audit_backups/` grows beyond 2 entries, prune oldest (keep 2 newest).

---

## 4. Emergency Procedures

### Hunter session missing
If `tmux ls` does not show `hunter`:
```bash
cd /home/ubuntu/mamuyy-binance-hunter
git status --short
tmux new -d -s hunter 'cd ~/mamuyy-binance-hunter && source .venv/bin/activate && python main.py --orchestrator'
tmux ls
tail -20 logs/hunter.log
```
Wait 30 seconds, then verify heartbeat in logs before assuming stable.

### DB locked (SQLITE_BUSY errors)
1. Identify lock holder:
```bash
lsof mamuyy_hunter.db 2>/dev/null
fuser mamuyy_hunter.db 2>/dev/null
```
2. Check WAL files:
```bash
ls -lh mamuyy_hunter.db-wal mamuyy_hunter.db-shm 2>/dev/null
```
3. If a stale process is holding the lock and is safe to kill, kill it by PID.
4. Do NOT run VACUUM. Do NOT manually modify the DB.
5. WAL mode (already enabled) reduces lock contention — most busy errors resolve within `busy_timeout` (5000ms).

### Disk >90%
1. Check what is consuming space:
```bash
df -h .
du -sh * 2>/dev/null | sort -rh | head -20
```
2. Safe cleanup options (in order):
   - Prune `manual_audit_backups/` — keep only 2 newest files
   - Remove old compressed backups older than 7 days (already automated in backup script)
   - Check for orphan `.tmp` files: `find . -name "*.tmp" -ls`
3. Do NOT touch `logs/`, `reports/`, or the live DB.
4. Do NOT run VACUUM.

### Retrain failed — rollback procedure
If `retrain_safe.sh` exits non-zero or the log shows `Accepted: False`:
```bash
tail -50 logs/monthly_retrain.log
```
Check `Rollback Available: True` in the log, then:
```bash
source .venv/bin/activate
python main.py --rollback-model
python main.py --phase3-readiness
```
If rollback is unavailable, the previous production model remains active — no action needed.

---

## 5. Monthly Procedures

### Verify retrain ran on the 18th
```bash
grep "monthly retrain start\|monthly retrain end\|DONE\|ERROR" logs/monthly_retrain.log | tail -10
```
Confirm a `[DONE] Retrain completed successfully` entry exists for the 18th.

### Review monthly_retrain.log
```bash
tail -60 logs/monthly_retrain.log
```
Key fields to review:
- `Rows:` — should grow month over month
- `Candidate PF:` — target >= 1.0
- `Candidate Walkforward:` — target >= 75
- `Accepted:` — True/False and reason

### Check model acceptance/rejection
```bash
sqlite3 -readonly mamuyy_hunter.db "SELECT id, timestamp, accuracy, setup_ranking FROM ml_results ORDER BY timestamp DESC LIMIT 5;"
```
If `Accepted: False`, check whether rollback was triggered automatically or needs manual intervention.

---

## 6. Governance Gates Before Real Execution

All gates below must be satisfied before PAPER_ONLY mode can be lifted. Current status: **LOCKED**.

| Gate | Requirement | Status |
|------|-------------|--------|
| Rolling accuracy | >= 65% | Pending |
| Closed trades | >= 500 | Pending |
| Stress test | Passed | See `docs/STRESS_TEST_REPORT.md` |
| Operator Runbook | Approved (this doc) | In review |
| Max concurrent policy | Enforced in engine | Done (2026-06-23) |
| WAL mode | Enabled | Done (2026-06-23) |
| Backup verified | `quick_check: ok` after WAL patch | Done (2026-06-23) |

Do not change `PAPER_ONLY` mode without explicit operator sign-off on all gates above.

---

## 7. Key Commands Reference

### Dashboard Access
The Streamlit dashboard is accessible via browser at http://168.110.200.231:8501
Dashboard access requires the  tmux session to be running.
Verify dashboard session: 


### Dashboard Access

The Streamlit dashboard is accessible via browser at http://168.110.200.231:8501
Dashboard access requires the `dashboard` tmux session to be active on the VPS.
Verify dashboard session is running before attempting access:
```bash
tmux ls | grep dashboard
```
If session is missing, see Section 8 (Restart Dashboard) to restore dashboard access.

### SSH into VPS
```bash
ssh -i ~/Downloads/ssh-key-2026-05-15.key ubuntu@168.110.200.231
```

### Check tmux sessions
```bash
tmux ls
```

### Check disk
```bash
df -h .
```

### Check DB health
```bash
sqlite3 -readonly mamuyy_hunter.db "PRAGMA integrity_check;"
sqlite3 -readonly mamuyy_hunter.db "PRAGMA journal_mode;"
sqlite3 -readonly mamuyy_hunter.db "PRAGMA quick_check;"
```

### Run health guardian manually
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
python main.py --health
```

### Run retrain manually (safe wrapper only)
```bash
cd /home/ubuntu/mamuyy-binance-hunter
./retrain_safe.sh
```
Never run `python main.py --retrain-model` directly — always use `retrain_safe.sh` to enforce safety checks.

### Run daily ops report manually
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
python scripts/daily_ops_report.py
```

### Expire orphaned trades (dry run — read only)
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
python scripts/expire_orphaned_trades.py
```
Add `--confirm` only after reviewing dry-run output and taking a manual backup.

### Run SQLite housekeeping audit
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
python scripts/sqlite_housekeeping_audit.py
```

### Run concurrent positions audit
```bash
cd /home/ubuntu/mamuyy-binance-hunter
source .venv/bin/activate
python scripts/audit_concurrent_positions.py
```

---

## 8. Restart Procedures

### Restart Orchestrator
```bash
tmux ls
tmux kill-session -t hunter
cd ~/mamuyy-binance-hunter
git status --short
tmux new -d -s hunter 'cd ~/mamuyy-binance-hunter && source .venv/bin/activate && python main.py --orchestrator'
```
After restart, verify heartbeat:
```bash
python main.py --health
python main.py --phase3-readiness
```

### Restart Dashboard
```bash
tmux kill-session -t dashboard
tmux new -d -s dashboard 'cd ~/mamuyy-binance-hunter && source .venv/bin/activate && streamlit run dashboard.py --server.address 0.0.0.0'
```

---

## 9. Governance Incident Rule

Treat any of the following as a governance incident:
- governance audit conflicts > 0
- stale or missing governance reports
- PAPER_ONLY violation
- risk budget `HALT`, `HOLD`, or `FREEZE`
- heartbeat instability or missing daily ops evidence

Response: keep Phase 3 locked, do not tune thresholds or retrain models, run only read-only evidence refresh:
```bash
python main.py --refresh-governance-reports
python main.py --phase3-remediation
python main.py --phase3-readiness
```

---

## 10. Git Update Safety

```bash
cd ~/mamuyy-binance-hunter
git status --short
git fetch --all --prune
git log --oneline --decorate -5
```
Rules: do not pull over uncommitted operator changes; rerun readiness after code updates; keep PAPER_ONLY boundaries unchanged.

---

_End of Operator Runbook. Review monthly or after any emergency procedure._

---

## 11. Known Behaviors & Design Notes (added 2026-06-24)

### Broadcast Router — CLI-only, not orchestrator-driven
`broadcast_router.py`'s `broadcast_test()` is invoked only via:
```
python main.py --broadcast_test
```
It is NOT registered as an orchestrator engine callback. In PAPER_ONLY mode
this is intentional — the broadcast system is a proposal layer only.
The 10 rows in `broadcast_events` are from a single manual test on 2026-05-19.
Zero automatic broadcasts is expected behavior, not a bug.

### Signal Volume Post-Retrain (2026-06-23)
After the June 23 retrain, signal volume dropped from ~9,600/day to ~15/day.
The new model is more selective (avg score 85–90 vs ~25 before), surfacing
only high-confidence signals. This is expected behavior from a larger, more
general dataset. Monitor weekly — if volume stays below 50/day for >30 days,
consider reviewing scanner score thresholds.

### MAX_SHADOW_EXPOSURE_CAP — Review Pending
The `MAX_SHADOW_EXPOSURE_CAP` (currently 20/10) was set when shadow cumulative
exposure was inflated (457,266%). After the 2026-06-24 shadow engine fix
(exposure now correctly shows ~8.80%), this cap may be overly conservative
and could be causing excessive AVOID tier assignments in the opportunity
allocator. Do NOT change yet — observe for 7 days post-fix before deciding.
Scheduled review: 2026-07-01.

### Two ML Accuracy Metrics — Expected Difference
Two accuracy figures exist and are both correct:
- Operational accuracy (0.3281): from ml_engine.py, updated every ~30 min,
  evaluates production model on live paper data. This is what Telegram shows.
- Retrain accuracy (0.3379): from retrain_model.py, written once per retrain,
  evaluates candidate model on held-out historical_outcomes split.
Gap of ~0.005 is normal (distribution shift between training and live data).

### Shadow Live Metrics — Windowed to Last 500 Rows (fixed 2026-06-24)
Prior to fix, cumulative shadow metrics were unbounded (87,539% PnL,
457,266% exposure, 94.42% winrate) due to accumulation over all 54,567 rows
since May 15 with no reset. Fixed in commit 85965bc — all metrics now
windowed to last 500 rows. Winrate remaining at 100% on last 500 rows is
expected due to signal filter survivorship bias (scanner only surfaces
high-score signals; synthetic pnl formula guarantees positive when score > 60).

### Market Regime Duplicate Fixed (2026-06-24)
Three identical Market Regime Telegram messages per cycle were caused by
three orchestrator engine lambdas (scanner, regime, flow) all pointing to
the same run_once() function which contained the regime send call.
Fixed in commit df64a46 — 60-second cooldown guard added. One message per
cycle now.

---

## 12. CP-044B Overlay Freshness Guard & Valid Signal Watch (added 2026-07-02)

The semi-auto testnet bridge refuses to evaluate stale ML overlay reports. The
freshness window is controlled by `TESTNET_OVERLAY_FRESHNESS_SECONDS` and
defaults to 6 hours. If the overlay/report timestamp is missing, invalid, in the
future, or older than the configured window, the bridge returns
`BLOCKED_STALE_OVERLAY` (or `BLOCKED` for unreadable input) and must not proceed
to approval prepare.

Read-only regeneration path for a stale overlay/report:
```
python ml_signal_overlay_v1.py
python semi_auto_testnet_bridge.py --telegram-preview
python cp044_valid_signal_watch.py --refresh-supervisor
```
These commands regenerate/read advisory reports only. Do not use
`--allow-need-review` to bypass freshness, do not run `--send`, and do not run
order-test send. CP-044 prepare is authorized only when
`reports/cp044_valid_signal_watch.json` reports `READY_FOR_PREPARE`; order send,
real trading, and auto testnet execution remain unauthorized.
