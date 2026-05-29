# MAMUYY Hunter Operator Runbook

## Safety Envelope / PAPER_ONLY Boundary

MAMUYY Hunter remains **PAPER_ONLY**. Operator procedures in this runbook are limited to read-only analytics/report generation unless explicitly documented otherwise.

Never use these commands to:

- route broker orders,
- mutate execution state,
- enable live trading,
- deploy strategies,
- retrain models,
- tune thresholds automatically,
- unlock or auto-promote Phase 3.

The Phase 3 readiness pipeline is evidence maintenance only. It must not be interpreted as permission for live execution.

## Dashboard Access

Use the dashboard only for monitoring and report review:

```bash
streamlit run dashboard.py --server.address 0.0.0.0
```

If the dashboard is already managed by tmux, inspect it without changing trading state:

```bash
tmux ls
tmux attach -t dashboard
```

Do not use the dashboard to enable live execution or route broker orders.

## Restart Orchestrator

The orchestrator is PAPER/simulation bounded by repository governance. Restart it only after confirming there is no live execution configuration change pending:

```bash
tmux ls
tmux kill-session -t hunter
cd ~/mamuyy-binance-hunter
git status --short
python main.py --phase3-readiness
tmux new -d -s hunter 'cd ~/mamuyy-binance-hunter && python main.py --orchestrator'
```

After restart, verify heartbeat stability:

```bash
python main.py --health
python main.py --phase3-readiness
```

## Restart Dashboard

Restart the dashboard separately from the orchestrator so UI recovery cannot affect scanner/execution state:

```bash
tmux ls
tmux kill-session -t dashboard
cd ~/mamuyy-binance-hunter
tmux new -d -s dashboard 'cd ~/mamuyy-binance-hunter && streamlit run dashboard.py --server.address 0.0.0.0'
```

Then confirm the dashboard is reachable and remains monitoring-only.

## Governance Incident Rule

Treat any of the following as a governance incident:

- governance audit conflicts greater than zero,
- stale or missing governance reports,
- PAPER_ONLY violation,
- risk budget `HALT`, `HOLD`, or `FREEZE`,
- promotion scorecard `FREEZE`,
- heartbeat instability or missing daily ops evidence.

Incident rule: keep Phase 3 locked, do not tune thresholds or retrain models, and run only read-only evidence refresh commands until the blocker is understood.

```bash
python main.py --refresh-governance-reports
python main.py --phase3-remediation
python main.py --phase3-readiness
```

## Git Update Safety

Before any git update, confirm the working tree and preserve local evidence artifacts intentionally:

```bash
cd ~/mamuyy-binance-hunter
git status --short
git fetch --all --prune
git log --oneline --decorate -5
```

Safety rules:

- do not pull over uncommitted operator changes,
- do not delete `reports/` evidence without explicit review,
- rerun readiness after code updates,
- keep PAPER_ONLY/no live execution boundaries unchanged.

## Phase 3 Readiness Remediation

Run the full read-only remediation chain:

```bash
python main.py --phase3-remediation
```

This runs, in order:

1. backup verification,
2. label quality audit,
3. stress test simulator,
4. governance report refresh,
5. portfolio risk budget,
6. promotion scorecard,
7. governance audit,
8. Phase 3 readiness.

Artifacts produced by valid read-only remediation:

- `reports/backup_verification.json`,
- `reports/label_quality_audit.json`,
- `reports/stress_test_report.json`,
- `docs/STRESS_TEST_REPORT.md`,
- `reports/phase3_readiness.json`.

## Verify Phase 3 Readiness

After any governance refresh or remediation work, verify the Phase 3 lock/readiness status:

```bash
python main.py --phase3-readiness
```

Expected safety posture: Phase 3 may become more accurately diagnosed, but it must remain governed by manual review and must not auto-unlock.

## Why Orchestrator Does Not Auto-Refresh Governance Reports

The orchestrator schedules only configured engine callbacks (`scanner`, `regime`, `flow`, `ML`, `walkforward`, `portfolio`, `execution`, and `shadow`). Governance report generators are research/report scripts, not registered orchestrator engines. Therefore, governance audit can detect stale report artifacts, but the orchestrator does not automatically regenerate them.

Use `python main.py --refresh-governance-reports` or `python main.py --phase3-remediation` when governance audit reports stale governance artifacts.
