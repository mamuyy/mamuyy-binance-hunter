# OPS: Safe Retrain & Daily Ops Cron Setup

## Overview

This document explains two operational fixes applied to the MAMUYY Hunter bot:

1. `retrain_safe.sh` — a crash-safe monthly model retrain wrapper
2. `daily_ops_report.py` — confirmed to have `import csv` present (stdlib import was already correct)

---

## 1. retrain_safe.sh

### What It Does

`retrain_safe.sh` is a shell wrapper around `main.py --retrain-model` that adds three safety layers before allowing a retrain to proceed:

| Guard | Mechanism | Effect if triggered |
|---|---|---|
| Concurrency lock | `flock -n` on `/tmp/retrain_safe.lock` | Second invocation exits immediately |
| Orchestrator presence | `tmux has-session -t hunter` | Skips if orchestrator is down |
| Mid-cycle detection | Reads last 30 min of `logs/orchestrator_diagnostics.log` | Skips if `engine_start` seen but no `cycle_end` |

All decisions are logged (with UTC timestamps) to `logs/monthly_retrain.log`.

### What It Does NOT Touch

- Database files
- Tmux sessions (no attach, no kill, no send-keys)
- Crontab
- Any config files

### Mid-Cycle Detection Logic

The script reads the JSONL diagnostics log and filters entries from the last 30 minutes. If it finds at least one `engine_start` event with zero `cycle_end` events in that window, it concludes the orchestrator is mid-cycle and exits cleanly rather than launching a CPU/memory-intensive retrain that could corrupt an in-flight cycle.

---

## 2. daily_ops_report.py

`import csv` was confirmed already present at line 1. No code change was required; this is documented here for audit trail completeness.

---

## Recommended Cron Entries

**Do not apply these automatically.** Add them manually after verifying the script works in a dry run.

```cron
# Monthly retrain — runs at 03:15 UTC on the 1st of each month
# Uses flock + orchestrator-idle check; safe to leave enabled
15 3 1 * * /home/ubuntu/mamuyy-binance-hunter/retrain_safe.sh

# Daily ops report — runs at 08:00 UTC every day
0 8 * * * cd /home/ubuntu/mamuyy-binance-hunter && .venv/bin/python daily_ops_report.py >> logs/daily_ops_report.log 2>&1
```

To edit crontab for the ubuntu user:
```bash
crontab -e
```

---

## Verification Commands

### Syntax check retrain_safe.sh
```bash
bash -n /home/ubuntu/mamuyy-binance-hunter/retrain_safe.sh
```

### Syntax check daily_ops_report.py
```bash
cd /home/ubuntu/mamuyy-binance-hunter
.venv/bin/python -m py_compile daily_ops_report.py && echo OK
```

### Confirm retrain_safe.sh is executable
```bash
ls -la /home/ubuntu/mamuyy-binance-hunter/retrain_safe.sh
```

### Dry-run mid-cycle detection manually
```bash
cd /home/ubuntu/mamuyy-binance-hunter
CUTOFF=$(date -u -d '-30 minutes' '+%Y-%m-%dT%H:%M:%S')
awk -v cutoff="$CUTOFF" '/"timestamp":/ { match($0, /"timestamp": "([^"]+)"/, a); if (a[1] >= cutoff) print }' \
    logs/orchestrator_diagnostics.log | grep -c '"event_type": "engine_start"' || true
```

### Check monthly_retrain.log after a run
```bash
tail -30 /home/ubuntu/mamuyy-binance-hunter/logs/monthly_retrain.log
```

### Confirm lock file clears after script exits
```bash
ls -la /tmp/retrain_safe.lock
```
