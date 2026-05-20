# Resource Monitor Plan

## Purpose

`resource_monitor.py` is a standalone, lightweight monitor for local runtime resource visibility. It records host and project artifact health without changing trading logic, ML logic, paper execution, orchestrator behavior, or broker/exchange execution paths.

This is monitor-only. It does not throttle, schedule, pause, place orders, change risk limits, or trigger deployment.

## Current Outputs

Run manually from the repository root:

```bash
python3 resource_monitor.py
```

The script creates `logs/` if missing and writes:

- `logs/resource_monitor_latest.json`: the latest resource snapshot.
- `logs/resource_monitor.csv`: append-only history for trend review.

Each snapshot includes:

- CPU percent.
- RAM percent.
- Disk percent for the repository filesystem.
- Load average when supported by the operating system.
- SQLite database size for `mamuyy_hunter.db`.
- Logs directory size when `logs/` exists.

If `psutil` is installed, the monitor uses it for CPU and RAM readings. If `psutil` is unavailable, the monitor falls back to standard-library-safe readings where possible, including load-average-based CPU estimation, `shutil.disk_usage`, `/proc/meminfo` on Linux, and graceful `null` values when a metric cannot be collected.

## Safety Boundaries

- No trading logic is imported or modified.
- No ML logic is imported or modified.
- No paper execution logic is imported or modified.
- No orchestrator behavior is imported or modified.
- No broker or exchange execution is added.
- No GitHub Actions or deployment workflow is added.
- Missing files are handled gracefully. A missing `mamuyy_hunter.db` is reported as `database_exists=false` with size `0`.

## Future Integration Ideas

CLI integration can add a read-only command such as `python3 main.py --resource-status` or a dedicated CLI subcommand that prints the latest JSON snapshot without changing runtime behavior.

Telegram integration can send a concise resource summary on demand or during manual health reports. Alerts should remain informational until explicit safety policies are designed and reviewed.

The anomaly detector can consume `logs/resource_monitor.csv` as an additional read-only signal to correlate high CPU, high RAM, disk pressure, database growth, or log growth with runtime incidents.

An adaptive scheduler can eventually use the resource history to recommend safer scan cadence or maintenance windows. That future step should be designed separately because the current monitor does not throttle, pause, or reschedule anything.
