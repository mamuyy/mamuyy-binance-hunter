# Phase 3 Readiness Action Plan

MAMUYY Hunter remains **PAPER_ONLY**. This action plan is for readiness evidence collection and operational stabilization only. It must not enable execution, broker routing, live trading, model retraining, threshold tuning, or automatic Phase 3 unlock.

## Immediate Governance Freshness Action

1. Refresh governance evidence reports:

   ```bash
   python main.py --refresh-governance-reports
   ```

2. Re-check Phase 3 readiness:

   ```bash
   python main.py --phase3-readiness
   ```

## Blocker Remediation Plan

### 1. Label Quality Audit

- Run or repair the label quality audit workflow so Phase 3 readiness has a current, passing label-quality artifact.
- Confirm the audit covers historical outcome labels, FLAT/UNKNOWN handling, timestamp integrity, and label leakage risk.
- Keep the workflow read-only with respect to model weights and thresholds.
- Acceptance evidence: a current label quality report with pass/warning/fail status, generated timestamp, sample counts, and remediation notes for any warnings.

### 2. Verified SQLite Backup

- Create a fresh SQLite backup using the existing database maintenance path.
- Verify that the backup file exists, is non-empty, and can be opened by SQLite.
- Record the backup path, timestamp, size, and verification result.
- Acceptance evidence: Phase 3 readiness can find a recent verified backup artifact or database backup entry.

### 3. Stress Test Report

- Produce a stress test report covering portfolio concentration, drawdown pressure, stale heartbeat behavior, adverse regime conditions, and emergency-brake review behavior.
- Keep stress testing analytics-only; do not mutate execution routing or thresholds.
- Acceptance evidence: a current stress test report with generated timestamp, scenarios, pass/fail/warning outcomes, and operator recommendations.

### 4. Operator Runbook

- Maintain `docs/OPERATOR_RUNBOOK.md` as the canonical operator procedure for governance freshness and Phase 3 readiness checks.
- Include the exact commands:

  ```bash
  python main.py --refresh-governance-reports
  python main.py --phase3-readiness
  ```

- Acceptance evidence: runbook exists, documents safety boundaries, explains the stale-report workflow, and confirms no auto Phase 3 unlock.

### 5. PAPER Trades >= 100

- Continue PAPER_ONLY data collection until there are at least 100 closed PAPER trades available for readiness evidence.
- Do not switch to live trading, broker routing, or execution mutation to accelerate evidence collection.
- Acceptance evidence: readiness report counts at least 100 closed PAPER trades and paper-trade evidence remains internally consistent.

### 6. System Health / Heartbeat Stability

- Stabilize runtime heartbeat generation and monitoring so the latest heartbeat stays within the configured stale threshold.
- Investigate stale/missing heartbeat causes in orchestrator diagnostics, Health Guardian output, and runtime heartbeat tables.
- Acceptance evidence: Phase 3 readiness reports healthy database access, fresh heartbeat age, sufficient latest health score, and no critical diagnostics issue.

## Governance Guardrails

- Keep Phase 3 status manually controlled and locked unless all criteria pass and human governance approves.
- Treat governance refresh as evidence maintenance only.
- Do not retrain models, tune thresholds, change labels, alter broker routing, or mutate execution state as part of this plan.
