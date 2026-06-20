# Phase 9D Data Staleness Postmortem

## Incident summary

Production evidence showed `historical_klines` stopped on **25 May 2026**. The issue was discovered on **20 June 2026** during Phase 9D operations, after candidate validation displayed misleading `PENDING` statuses for horizons that had already matured.

## Impact

Backups were healthy, but they contained stale data. A full recovery backfill was required and inserted hundreds of thousands of candles, signals, and flow rows. The candidate queue also created a temporary full database snapshot that consumed approximately the size of the full database. Historical and live signal lineage was missing, so candidate selection could not distinguish research backfill rows from live scanner rows.

There was no real-money impact because the system remained PAPER_ONLY, with no broker writes, no execution, and no automatic promotion.

## Lesson learned

Backup health is not data freshness.

## Data Protection vs Data Continuity

Data Protection means files and backups exist and can be restored. Data Continuity means required market data is current, complete for candidate horizons, and safe to use for validation.

## Prevention and detection controls

Phase 9D.1A adds lineage, live-only candidate selection, immutable candidate batches, lightweight kline sync, freshness reports, explicit blocked statuses, symbol validation, and capacity gates. These controls make stale, missing, invalid, and capacity-blocked data visible before analytics continue.
