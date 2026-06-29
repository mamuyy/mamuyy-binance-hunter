# CP-044B Internal Paper Recovery Audit Evidence Pack

## Safety / Governance State
* Internal paper bridge recovery: **OPERATIONALLY RESOLVED**
* Score95 forward evidence: **INSUFFICIENT**
* CP-045: **NOT APPROVED**
* Phase 3: **LOCKED**
* Live execution: **OFF**
* PAPER_ONLY: **TRUE**
* Classifier: **FROZEN**
* Model promotion: **HOLD**

## Audit Parameters
* Baseline timestamp: `2026-06-22T18:05:35.736930+00:00`
* Database: `mamuyy_hunter.db` opened in SQLite read-only URI mode
* Active cap: `20`

## Internal Paper Trade Evidence
* Status counts: `{"CLOSED": 588, "OPEN": 16, "TP1 HIT": 4}`
* Active count (`OPEN` + `TP1 HIT`): `20`
* Active cap comparison: `20/20`
* Expired orphaned count (`exit_reason='EXPIRED_ORPHANED'`): `136`
* Latest internal paper `source_signal_timestamp`: `2026-06-27T14:25:44.807487+00:00`
* Closed internal paper rows after baseline: `2`
* Closed score>=95 rows after baseline: `0`

## Signal Freshness / Candidate Score Buckets
* signal_candidates score buckets after baseline: `{"gte_85": 0, "gte_90": 0, "gte_95": 0, "score_column": "score", "timestamp_column": "timestamp"}`
* Latest `signals` timestamp (`timestamp`): `2026-06-29T14:20:05.945062+00:00`
* Latest `signal_candidates` timestamp (`timestamp`): `2026-06-29T14:20:44.113610+00:00`
* Latest `shadow_trades` timestamp (`timestamp`): `2026-06-29T15:15:07.102564+00:00`

## Verdicts
* Labels: `RECOVERY_CONFIRMED, SCORE95_EVIDENCE_INSUFFICIENT, ACTIVE_CAP_RISK, PHASE3_LOCKED`
* RECOVERY_CONFIRMED: `True`
* SCORE95_EVIDENCE_INSUFFICIENT: `True`
* ACTIVE_CAP_RISK: `True`
* PHASE3_LOCKED: `True`

## Artifacts
* JSON: `reports/cp044b_internal_paper_recovery_audit.json`
* Latest 30 closed rows after baseline CSV: `reports/cp044b_closed_forward_rows.csv`

This audit is read-only with respect to `mamuyy_hunter.db`; it writes only the evidence artifacts listed above.
