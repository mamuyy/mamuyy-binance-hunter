# Phase 3 Natural Gates

MAMUYY Hunter remains **PAPER_ONLY** and **LOCKED**. The remaining Phase 3 gates must be cleared by real runtime evidence from paper operations, not by code changes, hardcoded status, threshold tuning, or manual data fabrication.

Manual evidence refresh, when needed:

```bash
python main.py --phase3-remediation
```

## Current locked state

Latest VPS operator status:

- Phase 3 Readiness: `40%`
- Status: `LOCKED`
- Governance Audit: `100%` / `HEALTHY`
- Conflicts: `0`
- Stale Reports: `0`
- Violations: none
- Label Quality Audit: `REVIEW`
- Stress Test: `PASS`
- Backup Verification: `FAIL` because backup evidence is not yet available
- Risk Budget: `FREEZE NEW ALLOCATION`
- Promotion Scorecard: `FREEZE`
- Runtime mode: `PAPER_ONLY`

This state is safe and expected. Phase 3 must not auto-unlock from this state.

## Natural gates still remaining

1. **Paper closed trades must rise naturally to at least 100.**
   - Closed paper trades must come from normal `PAPER_ONLY` engine activity.
   - Do not insert fake trades, backfill synthetic closed trades, or switch to real execution to accelerate the count.

2. **Risk Budget must recover naturally from `FREEZE` toward `DEFENSIVE` or `NORMAL`.**
   - Recovery must come from actual paper/runtime evidence and improved portfolio risk conditions.
   - Do not tune caps, thresholds, allocations, or scoring rules to force a better risk-budget state.

3. **Promotion Scorecard must recover naturally from `FREEZE` toward `HOLD`, `WATCHLIST`, or `PASS`.**
   - Improvement must be caused by valid governance, risk, quality, and performance evidence.
   - Do not bypass manual review, auto-promote strategies, or weaken promotion criteria.

4. **Backup verification may pass only when valid backup evidence truly exists.**
   - Primary database health alone is not sufficient backup evidence.
   - `PASS` is allowed only after an actual backup artifact is present and verified.

5. **System health must remain evidence-based.**
   - Health must be inferred from heartbeat, runtime, diagnostics, and report freshness evidence.
   - Do not hardcode health, readiness, heartbeat freshness, or runtime stability.

6. **Brake context `HIGH` must stay under governance review until evidence improves.**
   - High brake context is not a trading command and must remain a review signal.
   - Do not suppress, downgrade, or relabel brake context just to improve readiness.

7. **Phase 3 must not auto-unlock.**
   - Unlock remains a manual governance decision after all gates are satisfied.
   - No script, remediation flow, dashboard, scorecard, or report may open Phase 3 automatically.

8. **Real execution and broker routing remain prohibited.**
   - The system must stay simulation, alerting, dashboard, and governance only.
   - Do not enable exchange order placement, live broker paths, or hidden routing hooks.
   - Do not mutate execution paths, execution state, order handlers, or order placement behavior while Phase 3 is locked.

9. **`PAPER_ONLY` remains active.**
   - Do not disable, bypass, or reinterpret `PAPER_ONLY` safeguards.
   - Any evidence refresh must preserve non-live operation and must not create synthetic trades or fake evidence.

## What operator should monitor daily

- Closed paper trade count and whether it is progressing naturally toward `>=100`.
- Risk Budget recommendation and evidence behind `FREEZE`, `DEFENSIVE`, or `NORMAL`.
- Promotion Scorecard recommendation and whether blockers are clearing naturally.
- Backup verification report and presence of a real verified backup artifact.
- Heartbeat freshness, runtime health, database readability, and daily ops report freshness.
- Governance audit health, conflicts, stale reports, violations, and label quality verdict.
- Brake context and any continued `HIGH` review conditions.

## What must not be forced

- Do not force closed paper trade counts.
- Do not force Risk Budget out of `FREEZE`.
- Do not force Promotion Scorecard out of `FREEZE`.
- Do not claim backup verification `PASS` without a valid backup artifact.
- Do not hardcode system health, heartbeat freshness, or runtime stability.
- Do not downgrade `HIGH` brake context without improved evidence.
- Do not auto-unlock Phase 3.
- Do not enable real execution, broker routing, order placement, execution mutation, threshold tuning, allocation tuning, or strategy auto-promotion.
- Do not disable `PAPER_ONLY` or fabricate fake evidence / synthetic trades.

## Next review condition

Review Phase 3 readiness only after the `PAPER_ONLY` engine has accumulated enough new empirical evidence for the natural gates to change state. At minimum, the next review should wait for materially updated closed paper trades, recovered Risk Budget and Promotion Scorecard recommendations, verified backup evidence, and stable heartbeat/runtime health evidence.

Until those conditions exist, the correct operator action is to keep the system **PAPER_ONLY**, **LOCKED**, and under observation.
