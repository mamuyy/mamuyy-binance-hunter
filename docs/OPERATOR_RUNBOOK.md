# MAMUYY Hunter Operator Runbook

## Safety Envelope

MAMUYY Hunter remains **PAPER_ONLY**. Operator commands in this runbook are limited to read-only analytics/report generation unless explicitly documented otherwise.

Never use these commands to:

- route broker orders,
- mutate execution state,
- enable live trading,
- deploy strategies,
- retrain models,
- tune thresholds automatically,
- unlock or auto-promote Phase 3.

## Phase 3 Readiness Stabilization

When governance audit shows stale governance reports, refresh only the report artifacts and immediately rerun the readiness chain:

```bash
python main.py --refresh-governance-reports
```

This command refreshes/regenerates the governance evidence reports when supported by existing generators:

- `reports/drift_detection_report.json`
- `reports/emergency_brake_simulation.json`
- `reports/transition_prediction_report.json`

If a generator is missing or cannot run, the command writes a safe fallback report with `generated_at`, `source = READ_ONLY_REFRESH`, and explicit PAPER_ONLY/read-only safety metadata. The fallback does not change the database, models, thresholds, labels, execution, routing, or live-trading settings.

After report refresh, the command automatically runs the follow-up readiness chain in this order:

1. portfolio risk budget,
2. promotion scorecard,
3. governance audit,
4. Phase 3 readiness.

## Verify Phase 3 Readiness

After any governance refresh or remediation work, verify the Phase 3 lock/readiness status:

```bash
python main.py --phase3-readiness
```

Expected safety posture: Phase 3 may become more accurately diagnosed, but it must remain governed by manual review and must not auto-unlock.

## Why Orchestrator Does Not Auto-Refresh Governance Reports

The orchestrator currently schedules only the configured engine callbacks (`scanner`, `regime`, `flow`, `ML`, `walkforward`, `portfolio`, `execution`, and `shadow`). The governance report generators for drift detection, emergency-brake simulation, and transition prediction are research/report scripts, not registered orchestrator engines. Therefore, governance audit can detect stale report artifacts, but the orchestrator does not automatically regenerate them.

Use `python main.py --refresh-governance-reports` when governance audit reports stale governance artifacts.
