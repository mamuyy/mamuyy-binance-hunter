# CP-035 — Shadow PnL / Sizing Sanity Audit

Date: 2026-06-26 UTC  
Phase: 2.5F — MAMUYY Hunter Governance

## Status

FAIL as outcome metric source.  
PASS as observability/simulation log source.

## Finding

`shadow_trades` must not be used as official winrate, edge, or outcome-tracking source.

## Evidence

- shadow_trades rows: 56,807
- lifecycle_status coverage: all rows are `execution simulated`
- pnl_percent records: 53,760
- negative pnl_percent count: 0
- pnl_percent is always positive, minimum observed approximately 0.7461%
- exposure range: 0.01–0.1
- temporal coverage: 43 active days

## Interpretation

Shadow PnL / shadow winrate is a metric-definition artifact, not a real strategy outcome.

`shadow_trades` represents expected fill / observability simulation, not TP/SL lifecycle outcome.

## Governance Decision

Official paper performance source must be:

- `internal_paper_trades`

Not:

- `shadow_trades`

## Required Follow-Up

Any dashboard/report wording that presents shadow PnL or shadow WR as official edge must be relabeled or blocked.

Recommended terminology:

- Shadow execution simulation
- Expected fill simulation
- Observability log

Forbidden terminology:

- Shadow winrate
- Shadow PnL as realized PnL
- Shadow edge as official strategy edge
