# CP-044C Lifecycle Health Guard

## Governance
* Guard type: **READ-ONLY observation only**
* Database: `mamuyy_hunter.db` opened with SQLite URI `mode=ro`
* Baseline timestamp: `2026-06-22T18:05:35.736930+00:00`
* CP-045: **NOT APPROVED**
* Phase 3: **LOCKED**
* Live execution: **OFF**
* PAPER_ONLY: **TRUE**
* Classifier: **FROZEN**
* Model promotion: **HOLD**

## Lifecycle Summary
* Total internal paper rows: `None`
* Status counts: `{}`
* Active statuses: ``
* Active by symbol: `{}`
* Latest `source_signal_timestamp`: `None`
* Latest `updated_at`: `None`
* Inserted rows last 24h: `None`
* Closed rows last 24h: `None`
* Closed rows after baseline: `None`

## Active Cap Status
* Active count (`OPEN` + `TP1 HIT`): `None`
* Active cap comparison: `None`
* ACTIVE_CAP_OK: `None`
* ACTIVE_CAP_FULL: `None`
* ACTIVE_CAP_OVERFLOW: `None`

## Stale Active Status
* Active rows older than 24h by `source_signal_timestamp`: `None`
* Active rows older than 7d by `source_signal_timestamp`: `None`
* STALE_ACTIVE_WARNING: `None`
* STALE_ACTIVE_CRITICAL: `None`

## Freshness Summary
* `signals` rows: `None`; latest `None`: `None`
* `signal_candidates` rows: `None`; latest `None`: `None`
* `shadow_trades` rows: `None`; latest `None`: `None`

## Signal Candidate Score Buckets After Baseline
* Buckets: `{}`

## Score95 Evidence Status
* Closed score>=95 rows after baseline: `None`
* Required minimum rows: `30`
* SCORE95_EVIDENCE_INSUFFICIENT: `True`

## Latest 20 Active Rows
```json
[]
```

## Latest 20 Closed Rows After Baseline
```json
[]
```

## Verdicts
* Overall status: **DB_UNREADABLE**
* Verdict map: `{"CP045_APPROVED": false, "DB_READABLE": false, "INTERNAL_PAPER_AVAILABLE": false, "PHASE3_LOCKED": true, "SCORE95_EVIDENCE_INSUFFICIENT": true}`

## Final Decision
* CP-045 **NOT APPROVED**
* Phase 3 **LOCKED**
* Live execution **OFF**
* PAPER_ONLY **TRUE**

This guard is read-only and writes only this Markdown report plus the paired JSON artifact.
