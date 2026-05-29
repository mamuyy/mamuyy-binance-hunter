# Phase 3 Readiness Action Plan

MAMUYY Hunter remains **PAPER_ONLY**. This action plan is for readiness evidence collection and operational stabilization only. It must not enable execution, broker routing, live trading, model retraining, threshold tuning, strategy auto-promotion, or automatic Phase 3 unlock.

## Safe Remediation Pipeline

Run the full Phase 3 remediation pipeline:

```bash
python main.py --phase3-remediation
```

The command runs these read-only steps in order:

1. `reports/backup_verification.json` via backup verification,
2. `reports/label_quality_audit.json` via label quality audit,
3. `reports/stress_test_report.json` and `docs/STRESS_TEST_REPORT.md` via stress test simulator,
4. governance report refresh,
5. portfolio risk budget,
6. promotion scorecard,
7. governance audit,
8. `reports/phase3_readiness.json`.

Re-check readiness directly at any time:

```bash
python main.py --phase3-readiness
```

## Blocker Remediation Plan

### 1. Label Quality Audit

- Command: `python label_quality_audit.py` or `python main.py --phase3-remediation`.
- The audit is read-only and samples existing label/outcome tables.
- It checks label/outcome distribution, PnL/label mismatch when PnL columns exist, FLAT/UNKNOWN handling, timestamp integrity, and per-regime distribution when regime columns exist.
- Acceptance evidence: `reports/label_quality_audit.json` with verdict `PASS` or `REVIEW`.
- A `FAIL` verdict must remain a blocker; do not mutate labels or tune thresholds to force a pass.

### 2. Verified SQLite Backup

- Command: `python backup_verification.py` or `python main.py --phase3-remediation`.
- The verifier checks the primary SQLite DB with read-only `PRAGMA integrity_check` and separately checks the latest backup artifact in the configured backup directory.
- Acceptance evidence: `reports/backup_verification.json` with `valid = true` and `verdict = PASS`.
- If no backup artifact exists, backup readiness must remain failed. Do not claim PASS from primary DB integrity alone.

### 3. Stress Test Report

- Command: `python stress_test_simulator.py` or `python main.py --phase3-remediation`.
- The stress report assesses drawdown pressure, concentration pressure, emergency brake behavior, and risk budget freeze behavior from existing reports.
- Acceptance evidence: fresh `reports/stress_test_report.json` with verdict `PASS` or `REVIEW`.
- `REVIEW` is acceptable evidence that the stress test ran and preserved the lock; it is not a Phase 3 unlock.

### 4. Operator Runbook

- Canonical file: `docs/OPERATOR_RUNBOOK.md`.
- Required topics: dashboard access, restart orchestrator, restart dashboard, governance incident rule, git update safety, and PAPER_ONLY/no live execution boundary.
- Acceptance evidence: Phase 3 readiness detects the runbook and all required topics.

### 5. System Health / Heartbeat Stability

- Commands:

  ```bash
  python main.py --health
  python main.py --daily-ops-report
  python main.py --phase3-readiness
  ```

- Readiness checks DB readability, heartbeat freshness, latest heartbeat health score, orchestrator diagnostics, tmux status if detectable, and daily ops report freshness if available.
- If heartbeat/table/tmux/daily ops evidence is unavailable or stale, the health blocker must remain failed with a clear reason.

## Expected Blockers That Cannot Be Faked

These blockers must improve naturally through PAPER_ONLY operations and valid governance evidence:

1. **Closed PAPER trades >= 100**
   - Continue PAPER_ONLY data collection until at least 100 closed paper trades exist.
   - Do not insert fake trades or switch to live trading to accelerate evidence.

2. **Risk budget must improve from FREEZE/HOLD/HALT to DEFENSIVE/WATCH/NORMAL naturally**
   - High exposure or utilization must remain a blocker until real read-only evidence shows improvement.
   - Do not tune caps, thresholds, or strategy behavior to force a better recommendation.

3. **Promotion scorecard must improve from FREEZE naturally**
   - `FREEZE` must remain a blocker until underlying governance/risk/quality inputs improve.
   - Do not auto-promote strategies or bypass manual governance.

## Governance Guardrails

- Keep Phase 3 status manually controlled and locked unless all criteria pass and human governance approves.
- Treat governance refresh as evidence maintenance only.
- Do not retrain models, tune thresholds, change labels, alter broker routing, or mutate execution state as part of this plan.
