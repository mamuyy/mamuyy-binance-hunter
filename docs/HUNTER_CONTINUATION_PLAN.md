# HUNTER Continuation Plan (2 Weeks)

Last updated: 2026-05-27
Mode: `PAPER_ONLY` (enforced)

## Objective

Formalize governance-first continuation without adding new models and without enabling real execution. The focus is to harden Research → Shadow → Paper workflow, define promotion gates, and keep Live mode locked.

## Non-Negotiables

- No real execution changes.
- No broker/live connector activation.
- No new model family introduction in this continuation window.
- All outputs remain research/paper artifacts until governance gates are explicitly passed.

## Lifecycle Formalization

### 1) Research (Experiment Zone)

Allowed:
- Feature/threshold/risk-policy experiments
- Validation scripts and diagnostics
- Offline analysis reports

Required artifacts:
- Experiment note (purpose, metric, verdict)
- Link to scripts + output files
- Explicit status: `research_only`

Exit gate to Shadow candidate:
- Reproducible run command documented
- Metrics compared to baseline (same window/split policy)
- No unresolved data integrity issue

### 2) Shadow (Non-Execution Decision Replay)

Allowed:
- Signal replay + reason logging
- Regime-specific performance and calibration monitoring
- Promotion scoring only

Required artifacts:
- Shadow summary (daily)
- Reason-based rejection/acceptance distribution
- Regime summary (winrate, calibration proxy, instability flags)

Exit gate to Paper candidate:
- Stability window complete (e.g., 7 consecutive days)
- No severe safety flag (data lag, unstable bucket crisis, guardian hard halt)
- Governance checklist marked pass

### 3) Paper (Execution Simulation)

Allowed:
- Portfolio-level risk budget enforcement
- Adaptive threshold tuning within bounded config
- Daily ops review + alert triage

Required artifacts:
- Daily ops summary
- Portfolio exposure/risk budget report
- Incident log (if halts/guardians triggered)

Exit gate to Live Locked review:
- Paper KPIs pass minimum window (e.g., 14 days)
- Drawdown and concentration limits respected
- Alert fatigue controlled (actionable, low-noise)

### 4) Live Locked (Governance Hold)

- This stage is intentionally locked.
- No auto-promotion to live execution.
- Any future unlock requires separate governance approval doc and explicit sign-off.

## Week 1 — Safety, Monitoring, Governance

### A. Safety & Governance Controls

1. Add/standardize promotion checklist file for each candidate experiment:
   - candidate_id
   - owner/date
   - baseline reference commit
   - reproducibility command
   - gate results (pass/fail)
2. Enforce explicit labels in reports:
   - `research_only`, `shadow_candidate`, `paper_candidate`, `rejected`
3. Add anti-repeat check:
   - same config hash + same dataset window should not be re-promoted as new evidence.

### B. Actionable Monitoring

1. Reason-based alert taxonomy:
   - `DATA_LAG`, `REGIME_UNSTABLE`, `RISK_BUDGET_EXCEEDED`, `GUARDIAN_HALT`, `MODEL_CONFIDENCE_DROP`
2. Regime summary block per day:
   - regime distribution
   - win/loss proxy by regime
   - confidence drift indicator
3. Daily ops summary template:
   - what changed
   - what halted
   - what is blocked
   - next concrete action

### Week 1 measurable gates

- Gate W1-G1: 100% of new experiments have checklist + label.
- Gate W1-G2: Daily reason-based alert report generated for at least 5 consecutive paper days.
- Gate W1-G3: Regime summary appears in daily ops report for same window.
- Gate W1-G4: Zero live execution touchpoints modified.

## Week 2 — Promotion Pipeline, Portfolio Risk Budget, Adaptive Threshold

### A. Promotion Pipeline

1. Create explicit transition criteria table:
   - Research → Shadow
   - Shadow → Paper
   - Paper → Live Locked review
2. Add promotion scorecard fields:
   - stability score
   - calibration score
   - risk compliance score
   - ops reliability score
3. Require regression check against latest accepted paper baseline before promotion.

### B. Portfolio-Level Risk Budget (Not Trade-Level Only)

1. Define per-regime and aggregate exposure ceilings.
2. Define concentration rules:
   - max symbols concurrently exposed
   - max correlated bucket exposure
3. Add daily budget breach summary and automatic paper-only downscaling rule.

### C. Adaptive Threshold (Bounded)

1. Allow threshold adjustment only within pre-defined safe band.
2. Record reason + before/after metrics for every threshold move.
3. Freeze threshold changes during instability incidents.

### Week 2 measurable gates

- Gate W2-G1: Promotion scorecard present for all active candidates.
- Gate W2-G2: Portfolio risk budget report generated daily for at least 7 days.
- Gate W2-G3: Adaptive threshold changes logged with reasons and bounded constraints.
- Gate W2-G4: `PAPER_ONLY` confirmed across docs/report outputs; no live execution path enabled.

## Suggested Minimal Patch Scope (No Engine Change)

- Docs + report template updates only.
- Optional lightweight metadata fields in existing reporting pipeline outputs.
- No changes to real order routing/execution modules.

## Definition of Success for This Continuation

At the end of two weeks, we can clearly answer:

1. Which candidates are truly promotion-ready vs still research-only?
2. Why a candidate was promoted/rejected (reason-based, auditable)?
3. Whether portfolio-level risk remained controlled under paper conditions?
4. Whether governance and monitoring quality improved without touching live execution?

If all answers are “yes”, continuation is successful while maintaining strict `PAPER_ONLY` policy.
