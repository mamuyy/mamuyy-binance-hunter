# Phase 9D.1A Data Continuity Core

Phase 9D.1A adds governance controls that keep market data fresh and prevent research backfills from contaminating live candidate review. It is PAPER_ONLY, read-only toward brokers, and does not change strategy scoring, regimes, thresholds, sizing, cron, Telegram, or dashboard UI.

## Architecture

Core components are `data_source` lineage in SQLite, the live-only candidate queue, immutable candidate batch archives, lightweight kline synchronization, freshness gating, reason-aware validation, and infrastructure capacity reporting.

## Data lineage

`signals` and `flow_logs` now carry `data_source` values: `LIVE_SCANNER`, `HISTORICAL_BACKFILL`, or `LEGACY_UNKNOWN`. New and migrated databases default existing rows to `LEGACY_UNKNOWN` without mass updates. Live scanner inserts explicitly write `LIVE_SCANNER`; historical backfill writes `HISTORICAL_BACKFILL`.

## Live vs historical separation

The queue reads only `LIVE_SCANNER` rows. Historical and legacy rows are counted in diagnostics and excluded. If no live rows qualify, the latest queue is still valid and records `NO_LIVE_SCANNER_CANDIDATES`.

## Lightweight sync

`python main.py data-sync` runs kline-only synchronization against Binance Futures public market data. It uses configured core symbols plus OPEN candidate symbols, validates symbols conservatively, applies an overlap window, uses `INSERT OR IGNORE`, and writes `reports/market_data_sync_report.json` atomically.

## Freshness statuses

`python main.py data-freshness` writes `reports/data_freshness_report.json`. Statuses are `GREEN`, `WARNING`, `BLOCKED_STALE_DATA`, `BLOCKED_MISSING_SYMBOL`, and `BLOCKED_CAPACITY`. Validation and analytics are blocked on stale, missing, future, or capacity-blocked data.

## Candidate batches

Every queue generation writes `reports/candidate_batches/<batch_id>.json` and updates `reports/binance_candidate_queue.json` as the latest pointer. Archived batches are not deleted or overwritten.

## Symbol validation

Validation requires a Binance Futures symbol to be TRADING, USDT-quoted, and a supported PERPETUAL contract. Policy-denied symbols include the prior exclusions and `SKHYNIXUSDT`. Rejection reasons are explicit.

## Capacity gates

`python main.py capacity-check` writes filesystem and project-size metrics. Heavy backfill refuses to start at `BLOCK_HEAVY_JOBS` (>=90% usage). No destructive cleanup is performed.

## Report schemas

Reports include timestamps, source paths, status/reason fields, PAPER_ONLY governance, and `execution_allowed=false` / `automatic_promotion_allowed=false`.

## Deployment notes

Run migration with normal startup or `python main.py health`. Then run capacity check, data sync, freshness guard, candidate queue, and validator. Do not install cron until Phase 9D.1B.

## Rollback

Revert code and keep database backups. The added columns are backward compatible and default to legacy lineage; no mass rewrite is required.

## Governance restrictions

No broker routing, API keys, testnet orders, real orders, withdrawals, position opening, automatic promotion, configuration auto-tuning, cron, Telegram delivery, or dashboard UI changes are introduced.

## Phase 9D.1B remainder

Operational scheduling, Telegram delivery, dashboard presentation, and deployment automation remain out of scope.

## Audit hardening updates

Freshness now fails closed: stale candidate-symbol data is `BLOCKED_STALE_DATA`, missing candidate symbols remain `BLOCKED_MISSING_SYMBOL`, capacity remains `BLOCKED_CAPACITY`, and validation honors `validation_allowed=false` for all blocking freshness outcomes.

Validation observations are bounded by `CANDIDATE_VALIDATION_MAX_OBSERVATION_LAG_MINUTES` or the configured candle interval plus a small tolerance. A candle outside that target window is treated as missing data, and READY horizons include `observed_lag_minutes`.

Symbol validation is fail-closed. Queue generation uses Binance Futures exchange metadata or a validated atomic cache; unavailable metadata yields `EXCHANGE_INFO_UNAVAILABLE`, and accepted candidates persist symbol-validation evidence in the immutable batch.

Lightweight sync now paginates bounded kline requests, validates HTTP and Binance API errors with retry/backoff, computes candidate-aware earliest timestamps, reports `INCOMPLETE_SYNC` when request caps are reached before coverage is current, and checks lightweight free-space safety before writes.

## Final audit round 3 controls

Legacy candidate batches without embedded symbol-validation evidence are validated with fresh Binance Futures exchange metadata or a TTL-bounded metadata cache. Cache files include `cached_at`, `source`, and schema metadata; stale, malformed, or missing caches fail closed with explicit reasons.

Candidate batches now have lifecycle sidecars (`<batch_id>.state.json`) and an atomic `reports/candidate_batches/registry.json` registry so archived batch status can be tracked without rewriting immutable batch payloads.

Freshness checks and horizon observations filter strictly on the configured candle interval. Data from another interval cannot satisfy global freshness, candidate-symbol coverage, or validation horizon observations.
