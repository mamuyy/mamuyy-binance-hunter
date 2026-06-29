# CP-044 Score95 Shadow Monitoring Audit

## Governance Header

* Phase 3 status: LOCKED
* Classifier gate: FROZEN
* Model promotion: HOLD
* PAPER_ONLY: true
* Runtime/execution/Telegram/candidate queue/dashboard/registry changes: false

## Baseline Source

* Baseline timestamp: 2026-06-22T18:05:35.736930+00:00
* Baseline source: CP-042 dataset timestamp_max
* Baseline estimated: False

## Dataset Overview

* Total rows: 1045
* Timestamp range: 2026-05-09T07:29:59.999000+00:00 -> 2026-06-27T13:05:49.272807+00:00
* Source distribution: {'historical_outcomes': 597, 'internal_paper_trades': 448}
* Target distribution: {'LOSS': 556, 'WIN': 299, 'TP1 HIT': 190}

## New Score95 Monitoring Summary

* New forward rows: 2
* New score>=95 rows: 0
* Win rate: None
* Loss rate: None
* Average profit: None
* Top symbol concentration: None
* Rolling pass/fail/low-sample: 0/0/1

## Pass/Fail Checks

* minimum_new_score95_rows: False
* ideal_new_score95_rows: False
* loss_rate_within_limit: False
* avg_profit_positive: False
* rolling_pass_rate_within_limit: False
* recent_consecutive_fails_within_limit: True
* top_symbol_concentration_within_limit: False
* top_regime_concentration_within_limit: None
* no_major_forward_contradiction: True

## Risk Notes

* Insufficient fresh score>=95 rows for CP-045 readiness confidence.

## Final Recommendation

* Verdict: INSUFFICIENT_NEW_FORWARD_DATA
* CP-044 can only recommend CP-045 readiness review; it never approves Phase 3 unlock.
