# CP-043 — Score95 Paper-only Watchlist Gate Draft

## Governance header
- Verdict: `POLICY_DRAFT_REVIEW`
- Phase 3 status: `LOCKED`
- Classifier gate: `FROZEN`
- Model promotion: `HOLD`
- PAPER_ONLY: `true`
- Runtime/execution/Telegram/candidate queue/registry changed: `false`

## Evidence summary
- CP-041 verdict: `REVIEW`
- CP-041 score>=95: rows=missing, win_rate=missing, loss_rate=missing, loss_avoidance_delta=missing
- CP-041 IPT-only score 95-100: `{'rows': None, 'win_rate': None, 'loss_rate': None}`
- CP-042 verdict: `REVIEW`
- CP-042 aggregate score95 status: `missing`
- CP-042 IPT forward counts: `{'PASS': 7, 'FAIL': 2, 'LOW_SAMPLE': 2, 'total': 11}`
- CP-042 rolling window counts: `missing`
- CP-042 major forward contradiction: `missing`

## Proposed paper-only watchlist policy
- A candidate may be marked score95_watchlist_candidate=true only if score >= 95.
- This marker is informational and PAPER_ONLY.
- It must not trigger execution.
- It must not bypass portfolio risk.
- It must not bypass market regime filters.
- It must not bypass freshness guard.
- It must not bypass daily order caps.
- It must not bypass safety supervisor.
- It must not unlock Phase 3.
- It must not promote classifier.
- It must not create live Binance orders.
- It must require ongoing source-aware forward evidence.

## Required future evidence before any gate proposal
- Minimum additional IPT/live-like sample collection.
- Minimum rolling windows.
- No major forward contradiction.
- Stable loss avoidance.
- Positive avg profit.
- Drawdown / adverse outcome review.
- Symbol concentration review.
- Regime-specific review.
- Manual approval chain before any runtime proposal.

## Explicit non-goals
- No execution.
- No runtime integration.
- No Telegram alert change.
- No dashboard change unless future separate CP.
- No live trading.
- No model promotion.
- No Phase 3 unlock.

## Final recommendation
- Keep Phase 3 LOCKED.
- Keep classifier gate FROZEN.
- Continue PAPER_ONLY score95 observation.
- CP-044 optional: score95 shadow monitoring dashboard/reporting audit after more forward data exists.
- Do not open live execution.

## Missing evidence flags
- cp041_report_missing: `False`
- cp042_report_missing: `False`
- cp041_report_unreadable: `False`
- cp042_report_unreadable: `False`
