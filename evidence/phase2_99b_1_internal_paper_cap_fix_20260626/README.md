# Phase 2.99B-1 — Internal Paper Hard-Cap Fix

Date: 2026-06-26 UTC

## Status

COMPLETE.

## Commit

028e2d8 fix: enforce internal paper active position caps

## Problem

internal_paper_trades had active paper positions above displayed cap:

- OPEN: 53
- TP1 HIT: 9
- Total active: 62
- Displayed global cap: 20

The API cap existed as status reporting, but the internal paper trade writer did not enforce a hard gate before INSERT.

## Fix

Added hard cap enforcement before `INSERT OR IGNORE INTO internal_paper_trades` in `internal_paper_engine.py`.

Active statuses:

- OPEN
- TP1 HIT

Caps:

- RISK_MAX_OPEN_TRADES=20
- RISK_MAX_OPEN_TRADES_PER_SYMBOL=3

Behavior:

- If global active count >= cap, reject new internal paper trade.
- If per-symbol active count >= cap, reject new internal paper trade.
- Existing paper trades are not deleted or mutated.
- Lifecycle updates remain allowed.

## Verification

Synthetic insert test:

- insert_result: False
- before_count: 591
- after_count: 591
- blocked_ok: True

Runtime monitor after restart:

- CLOSED: 529
- OPEN: 53
- TP1 HIT: 9
- global_count: 62
- global_cap: 20
- mode: PAPER_ONLY
- heartbeat: RUNNING

Result: active count did not increase after restart.
