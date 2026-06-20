# Phase 9D.1B-A Paper Economic Metric Reconciliation

This phase adds a read-only reconciliation layer for CLOSED `internal_paper_trades` rows. It preserves legacy paper outcome reports and does not alter strategy, scoring, lifecycle, scheduler, broker routing, Telegram output, thresholds, or historical records.

## Why 372 closed trades are sufficient

A sample of 372+ CLOSED paper trades is sufficient for a targeted economic audit because it can expose arithmetic-labeling issues, duplicate exposure, overlap dependence, outlier dependence, and data-contract gaps. It is not, by itself, proof of live account ROI or production readiness.

## Event-return sum vs account ROI

`legacy_event_return_sum_pct` is the arithmetic sum of individual closed-trade percentage returns. It is not capital-normalized account ROI, portfolio return, capital growth, or an equity return. A winrate can be valid for CLOSED rows while cumulative PnL labeling is misleading when the cumulative number is built from summed trade percentages rather than capital-weighted cash flows.

## Capital-normalized scenario assumptions and accounting

The deterministic `equal_allocation_capital_scenario` starts from `ECON_AUDIT_INITIAL_CAPITAL` (default 10000), allocates `ECON_AUDIT_ALLOCATION_PCT_PER_TRADE` (default 1%) of current realized capital per accepted trade, respects `ECON_AUDIT_MAX_GROSS_EXPOSURE_PCT` (default 100%), applies `ECON_AUDIT_ROUND_TRIP_FEE_BPS` (default 8) and `ECON_AUDIT_SLIPPAGE_BPS` (default 15), uses no leverage, and never fabricates fill prices.

The curve is explicitly a `realized_close_to_close_equity_curve`: opening a trade does not increase equity, open gross exposure, reserved notional, and available unallocated capacity are reported separately, and realized capital/realized account equity changes only on close when realized PnL, fee, and slippage are applied. Because historical rows do not provide reliable intratrade marks, maximum drawdown is calculated from realized close-to-close equity only. If the database, table, CLOSED rows, valid reconciled rows, timestamps, or return quality are insufficient, the scenario is `BLOCKED_DATA_QUALITY` and normalized ROI fields are `null`.

## Readiness thresholds

Economic readiness is advisory only and is controlled by configurable thresholds: `ECON_AUDIT_MIN_VALID_CLOSED_TRADES` (default 100), `ECON_AUDIT_MIN_COST_ADJUSTED_NORMALIZED_RETURN_PCT` (default 0), `ECON_AUDIT_MIN_PROFIT_FACTOR` (default 1.0), `ECON_AUDIT_MAX_REALIZED_DRAWDOWN_PCT` (default 20), `ECON_AUDIT_MAX_TOP_SYMBOL_CONCENTRATION_PCT` (default 50), `ECON_AUDIT_MAX_OUTLIER_CONTRIBUTION_PCT` (default 25), and `ECON_AUDIT_MAX_OVERLAP_DEPENDENCE_PCT` (default 25). A single source-of-truth data-quality predicate blocks readiness for unavailable sources, zero CLOSED rows, zero valid reconciled rows, any excluded required CLOSED row, material mismatches, recomputation failures, missing required fields, invalid timestamps, or unavailable returns. A completed scenario with negative net return cannot pass, and unacceptable drawdown, concentration, material data mismatch, outlier dependence, or overlap dependence blocks or reviews economic readiness. Readiness never unlocks execution.

## Methodology

Overlap is detected from `opened_at`/`closed_at` equivalents (`timestamp`/`updated_at`) by interval intersection, with both same-symbol and all-symbol concurrency measured. The one-symbol counterfactual keeps the earliest valid trade per symbol and rejects later overlapping entries until the earlier trade closes.

Concentration gates use top 1/3/5/10 gross absolute symbol contribution shares and a Herfindahl-style measure. Signed return contribution percentages are retained separately as informational attribution metrics; they are `null` when the signed denominator is zero. HHI and concentration gates use absolute symbol contribution shares so negative and positive contributors both count toward concentration.

Outliers are flagged using configurable absolute return thresholds and reconciliation mismatches. Outlier dependence gates use absolute outlier contribution divided by total absolute valid return contribution; when valid trades exist but total absolute return is zero, the gate is REVIEW rather than PASS. Signed outlier contribution is retained separately as informational attribution. Outliers remain in the raw authoritative report; with/without-outlier metrics are reported separately.

Material stored-versus-recomputed return mismatches are conservatively excluded from authoritative statistics because the stored `pnl` unit is not assumed proven. Status counts are reported for MATCH, SMALL_DIFFERENCE, MATERIAL_DIFFERENCE, CANNOT_RECOMPUTE, and INVALID_CONTRACT.

## Breakdowns and limitations

When timestamps are valid, the report populates symbol, side, exit reason, holding-period, calendar-day, and calendar-week breakdowns. Score and regime remain `UNAVAILABLE` unless they can be reliably joined or are directly present with a proven contract.

Historical paper rows may not include authoritative quantity or notional. Therefore the report provides a deterministic scenario, not actual account ROI.

## Readiness separation

Engineering readiness verifies data readability, audit execution, PAPER_ONLY governance, and artifact generation. Economic readiness evaluates sample adequacy, data quality, expectancy, profit factor, normalized scenario behavior, drawdown, concentration, overlap dependence, outlier dependence, and cost-adjusted results. Economic readiness is advisory only and cannot unlock execution.

## Safety and rollback

The audit opens SQLite in read-only mode, writes only JSON/CSV/doc artifacts, and never mutates trade rows. It makes no broker API calls, places no orders, promotes no strategy, changes no thresholds, changes no scoring, changes no lifecycle behavior, changes no scheduler, and changes no Telegram output. Rollback is removal of the new script, docs, generated artifacts, tests, and CLI command.

## Phase 9D.1B-A.1 Economic Robustness and Legacy Cohort Reconciliation

Phase 9D.1B-A.1 separates core economic validity from temporal simulation validity without mutating historical `internal_paper_trades` rows. CLOSED rows are classified as `CORE_AND_TEMPORAL_VALID`, `LEGACY_TEMPORAL_INCOMPLETE`, or `BLOCKING_INVALID`. Legacy temporal-incomplete rows must have valid symbol, side, positive entry, valid exit, matching or small-difference recomputed return, and an opened timestamp; only the close/updated timestamp may be missing.

Core non-temporal statistics may include both `CORE_AND_TEMPORAL_VALID` and `LEGACY_TEMPORAL_INCOMPLETE` rows. Time-dependent calculations (overlap, concurrency, holding periods, realized equity simulation, drawdown, and chronological capital allocation) use only the `CORE_AND_TEMPORAL_VALID` cohort.

The audit now emits four capital-normalized robustness scenarios using the same allocation, fee, slippage, and exposure assumptions as the original capital scenario: original temporal-valid, one-active-trade-per-symbol, no-outlier, and combined one-symbol/no-outlier. Each scenario reports accepted/rejected trades, initial and ending capital, gross and cost-adjusted net return, maximum drawdown, profit factor, winrate, fees, slippage, and maximum gross exposure.

Maximum concurrency is computed with a sweep-line algorithm that processes close events before open events at identical timestamps. The report retains pairwise overlap diagnostics separately and reports maximum total concurrency, maximum same-symbol concurrency, and the timestamp(s) where sweep-line maxima occurred.

Readiness remains advisory and paper-only. Legacy temporal incompleteness is reported transparently and does not automatically become a PASS; blocking-invalid rows still block data quality, and execution plus automatic promotion remain disabled.

### Readiness semantics after HOLD review

The strict global data-quality block is driven by unavailable sources, empty CLOSED input, or `BLOCKING_INVALID` rows. `LEGACY_TEMPORAL_INCOMPLETE` rows are included in the core economic cohort, excluded from temporal simulation, disclosed as a legacy warning, and make temporal simulation readiness `REVIEW` rather than globally blocking data quality.

Core economic readiness evaluates blocking-invalid status, sample adequacy, expectancy, profit factor, concentration, and outlier dependence on the core economic cohort. Temporal simulation readiness evaluates temporal-valid coverage, capital scenario completion, normalized net return, realized drawdown, sample adequacy, and legacy temporal incompleteness. Robustness readiness evaluates every required scenario for completion, accepted funded sample, cost-adjusted net return, profit factor, and maximum drawdown; completed scenarios with negative returns or excessive drawdown cannot pass.

### Aggregate overall readiness after audit round 2

Authoritative `overall_economic_readiness_status` is now a deterministic aggregate of `core_economic_readiness.status`, `temporal_simulation_readiness.status`, and `robustness_readiness.status`: any `BLOCKED`/`BLOCKED_*` component makes overall `BLOCKED`; otherwise any `REVIEW` component makes overall `REVIEW`; only three `PASS` components make overall `PASS`. The previous detailed status is retained only as `legacy_compatibility_economic_status` and cannot override the aggregate.

Robustness accepted-sample adequacy uses `ECON_AUDIT_MIN_VALID_CLOSED_TRADES`: zero accepted funded trades is `BLOCKED_DATA_QUALITY`, a positive accepted count below the configured minimum is `REVIEW`, and accepted count at or above the configured minimum is `PASS`.
