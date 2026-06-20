# Phase 9D.1B-A Paper Economic Metric Reconciliation

This phase adds a read-only reconciliation layer for CLOSED `internal_paper_trades` rows. It preserves legacy paper outcome reports and does not alter strategy, scoring, lifecycle, scheduler, broker routing, Telegram output, thresholds, or historical records.

## Why 372 closed trades are sufficient

A sample of 372+ CLOSED paper trades is sufficient for a targeted economic audit because it can expose arithmetic-labeling issues, duplicate exposure, overlap dependence, outlier dependence, and data-contract gaps. It is not, by itself, proof of live account ROI or production readiness.

## Event-return sum vs account ROI

`legacy_event_return_sum_pct` is the arithmetic sum of individual closed-trade percentage returns. It is not capital-normalized account ROI, portfolio return, capital growth, or an equity return. A winrate can be valid for CLOSED rows while cumulative PnL labeling is misleading when the cumulative number is built from summed trade percentages rather than capital-weighted cash flows.

## Capital-normalized scenario assumptions

The deterministic `equal_allocation_capital_scenario` starts from `ECON_AUDIT_INITIAL_CAPITAL` (default 10000), allocates `ECON_AUDIT_ALLOCATION_PCT_PER_TRADE` (default 1%) of current capital per accepted trade, respects `ECON_AUDIT_MAX_GROSS_EXPOSURE_PCT` (default 100%), applies `ECON_AUDIT_ROUND_TRIP_FEE_BPS` (default 8) and `ECON_AUDIT_SLIPPAGE_BPS` (default 15), uses no leverage, and never fabricates fill prices. If timestamps or return quality are insufficient, normalized return fields are blocked rather than fabricated.

## Methodology

Overlap is detected from `opened_at`/`closed_at` equivalents (`timestamp`/`updated_at`) by interval intersection, with both same-symbol and all-symbol concurrency measured. The one-symbol counterfactual keeps the earliest valid trade per symbol and rejects later overlapping entries until the earlier trade closes.

Outliers are flagged using configurable absolute return thresholds and reconciliation mismatches. Outliers remain in the raw authoritative report; with/without-outlier metrics are reported separately.

## Limitations

Historical paper rows may not include authoritative quantity or notional. Therefore the report provides a scenario, not actual account ROI. Score and regime enrichment are only used when directly available or safely joinable; otherwise enrichment status is `UNAVAILABLE`.

## Readiness separation

Engineering readiness verifies data readability, audit execution, PAPER_ONLY governance, and artifact generation. Economic readiness evaluates sample adequacy, data quality, expectancy, profit factor, normalized scenario behavior, drawdown, concentration, overlap dependence, outlier dependence, and cost-adjusted results. Economic readiness is advisory only and cannot unlock execution.

## Safety and rollback

The audit opens SQLite in read-only mode, writes only JSON/CSV/doc artifacts, and never mutates trade rows. It makes no broker API calls, places no orders, promotes no strategy, changes no thresholds, changes no lifecycle behavior, changes no scheduler, and changes no Telegram output. Rollback is removal of the new script, docs, generated artifacts, tests, and CLI command.
