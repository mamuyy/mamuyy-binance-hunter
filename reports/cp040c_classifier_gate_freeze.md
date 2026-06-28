# CP-040C — Classifier Gate Freeze & Ranking/EV Pivot Governance Evidence

## Executive verdict

**Verdict: CLASSIFIER_GATE_FROZEN**

CP-040C freezes the classifier promotion gate based on the accumulated failed classifier evidence from CP-038D, CP-040A, CP-040B0, and CP-040B1.

- Classifier promotion gate is **frozen**.
- Phase 3 remains **LOCKED**.
- Model promotion remains **HOLD**.
- Production registry remains **unchanged**.
- Runtime remains **unchanged**.
- Execution remains **unchanged**.
- PAPER_ONLY governance remains **enforced**.

This report is a governance evidence artifact only. It does not modify databases, runtime code, execution behavior, `ml_engine.py`, `portfolio_engine.py`, model registry entries, or model weights.

## Evidence summary

| Evidence item | Metric / finding | Verdict |
| --- | ---: | --- |
| CP-038D multiclass walk-forward | avg accuracy = 0.474 | FAIL |
| CP-040A binary walk-forward | avg accuracy = 0.530 | FAIL |
| CP-040B0 source drift | source drift confirmed | FAIL |
| CP-040B1 source-aware sensitivity | no robust classifier edge | FAIL |

## Technical findings

- The CP-039D additive production-universe dataset is valid for coverage evidence, but it is not valid as a single clean training distribution.
- `historical_outcomes` and `internal_paper_trades` exhibit source drift.
- Historical-to-IPT generalization remains weak, with CP-040B1 historical-to-IPT accuracy at 0.439 versus a 0.572 majority baseline.
- IPT-only validation shows a small episodic signal, but the available folds and row counts are insufficient for model promotion.
- The classifier does not consistently beat the majority baseline across source-aware validation regimes.
- CP-040B1 best overall result was IPT first70-to-last30 accuracy of 0.567 versus baseline 0.530, but that result had only one valid split and is not robust promotion evidence.

## Freeze rule

Classifier promotion is blocked until all of the following are true:

1. Source-aware weighted accuracy is at least **0.60**.
2. Weighted `model_vs_baseline_delta` is at least **+0.03**.
3. `folds_beating_baseline_rate` is at least **0.60**.
4. Cross-source contradiction does not block interpretation.
5. IPT/live-like validation has enough rows and folds.
6. There is no PAPER_ONLY or governance violation.

Until these criteria are met and separately reviewed, classifier promotion remains frozen, model promotion remains HOLD, and Phase 3 remains LOCKED.

## Pivot recommendation

Next research should pivot away from a classification accuracy promotion gate and toward source-aware decision quality evidence, including:

- Ranking quality.
- EV/profit-weighted scoring.
- Lifecycle intelligence.
- Loss avoidance.
- Candidate evidence ledger.
- Source-aware IPT/live-like validation.

## Next CP recommendation

Recommended next change proposal:

**CP-041 — Ranking / EV / Lifecycle Pivot Audit**

CP-041 should evaluate:

- Candidate rank versus realized outcome.
- EV score versus `pnl_percent`.
- Loss avoidance by score bucket.
- Lifecycle outcomes by `source_artifact`.
- Profit-weighted accuracy.
- Top-k candidate quality.
- Calibration by IPT-only rows.
- Evidence ledger consistency.
