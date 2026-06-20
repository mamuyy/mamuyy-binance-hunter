# Phase 9D.1B-B — ML Metric Reconciliation

This phase adds a **read-only** audit for MAMUYY Hunter model-quality metrics. It does not retrain models, change labels, tune thresholds, change scoring, update Telegram output, alter schedulers, promote models, or unlock execution.

## Why multiple accuracies can all be technically correct

The project has historically exposed several values that can be valid under different contracts:

- **Current Model Accuracy** is produced by `ml_engine.run_ml_research` from a random holdout split over the current ML dataset.
- **Walk-Forward OOS Accuracy** is produced by `walkforward.run_walkforward_validation` as an aggregate over chronological rolling folds.
- **Historical snapshots** such as 66.40% or approximately 70% may come from older reports, smaller datasets, or superseded models.
- **Candidate evidence accuracy** and **paper-trade winrate** are separate populations and must not be averaged with classifier accuracy.

The audit intentionally keeps unlike contracts separate. A random holdout accuracy, walk-forward fold average, heuristic confidence score, and trade winrate are not interchangeable.

## Accuracy is not trading profitability

Classification accuracy measures whether a predicted class matches a label. Trading profitability depends on position sizing, return magnitude, fees, slippage, exposure, overlap, drawdown, and capital allocation. A model can have high accuracy but poor profitability if wins are small and losses are large, or lower accuracy but profitable asymmetry.

## Why balanced accuracy and baselines matter

Raw accuracy can be misleading when one class dominates. The audit reports majority-class and random-prior baselines, balanced accuracy, precision, recall, F1, and confusion matrices when the target contract allows. Model Readiness cannot pass from accuracy alone.

## Metric contracts

Recommended future terminology:

- Current Holdout Accuracy
- Walk-Forward OOS Accuracy
- Latest-Fold Accuracy
- Balanced Accuracy
- Candidate Evidence Accuracy
- Paper Trade Winrate
- AI Confidence Heuristic
- Model Readiness

The current generic Telegram label `Current Model Accuracy` is not changed by this phase.

## Leakage methodology

The audit checks or documents:

- chronological fold ordering;
- train/test timestamp overlap;
- duplicate symbol/timestamp contamination;
- target-like feature columns;
- future timestamps;
- stale source artifacts;
- unverifiable label horizons and maturity requirements.

Undocumented label contracts are marked `BLOCKED_LABEL_CONTRACT` instead of being inferred.

## Minimum-sample policy

Segment-level metrics use configurable code-level sample minimums. Sparse segments are `REVIEW`, never `PASS`. Baseline superiority is blocked when the sample is insufficient.

## Model Readiness

Model Readiness is advisory only and includes Metric Integrity, Data Lineage, Label Integrity, Leakage Safety, Baseline Superiority, Out-of-Sample Adequacy, Walk-Forward Stability, Regime Stability, Calibration Quality, and Candidate-Evidence Support. Any blocking component produces a blocking overall status; otherwise REVIEW precedes PASS.

## Relationship to Data, Engineering, and Economic Readiness

This audit is a model-quality reconciliation layer. It does not replace data continuity audits, engineering readiness gates, or economic reconciliation. Economic readiness remains the authority for capital-normalized profitability and PAPER_ONLY trade evidence.

## PAPER_ONLY restrictions

The JSON governance block always reports:

- `paper_only = true`
- `execution_allowed = false`
- `automatic_promotion_allowed = false`
- `model_promotion_allowed = false`
- `readiness_advisory_only = true`

## Rollback and compatibility

Rollback is safe: remove `ml_metric_reconciliation.py`, the generated `reports/ml_*` artifacts, the documentation page, tests, and the optional `main.py ml-metric-audit` command. Existing ML, walk-forward, dashboard, Telegram, model, and trading behavior are preserved.
