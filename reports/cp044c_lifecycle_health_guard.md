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
* Total internal paper rows: `608`
* Status counts: `{"CLOSED": 588, "OPEN": 16, "TP1 HIT": 4}`
* Active statuses: `OPEN, TP1 HIT`
* Active by symbol: `{"BEATUSDT": 2, "BTWUSDT": 1, "MAGMAUSDT": 3, "MRVLUSDT": 3, "MSTRUSDT": 1, "RESOLVUSDT": 3, "REUSDT": 3, "SPCXUSDT": 3, "SYNUSDT": 1}`
* Latest `source_signal_timestamp`: `2026-06-27T14:25:44.807487+00:00`
* Latest `updated_at`: `2026-06-29T15:45:17.354944+00:00`
* Inserted rows last 24h: `17`
* Closed rows last 24h: `43`
* Closed rows after baseline: `2`

## Active Cap Status
* Active count (`OPEN` + `TP1 HIT`): `20`
* Active cap comparison: `20/20`
* ACTIVE_CAP_OK: `True`
* ACTIVE_CAP_FULL: `True`
* ACTIVE_CAP_OVERFLOW: `False`

## Stale Active Status
* Active rows older than 24h by `source_signal_timestamp`: `20`
* Active rows older than 7d by `source_signal_timestamp`: `5`
* STALE_ACTIVE_WARNING: `True`
* STALE_ACTIVE_CRITICAL: `True`

## Freshness Summary
* `signals` rows: `321726`; latest `timestamp`: `2026-06-29T14:20:05.945062+00:00`
* `signal_candidates` rows: `514`; latest `timestamp`: `2026-06-29T15:50:46.535999+00:00`
* `shadow_trades` rows: `61427`; latest `timestamp`: `2026-06-29T15:45:14.957646+00:00`

## Signal Candidate Score Buckets After Baseline
* Buckets: `{"gte_85": 0, "gte_90": 0, "gte_95": 0, "score_column": "score", "timestamp_column": "timestamp"}`

## Score95 Evidence Status
* Closed score>=95 rows after baseline: `0`
* Required minimum rows: `30`
* SCORE95_EVIDENCE_INSUFFICIENT: `True`

## Latest 20 Active Rows
```json
[
  {
    "confidence": 100.0,
    "exit_reason": "",
    "id": 38969,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T14:25:44.807487+00:00",
    "status": "OPEN",
    "symbol": "MAGMAUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:15:09.554831+00:00",
    "updated_at": "2026-06-29T15:45:17.354944+00:00"
  },
  {
    "confidence": 87.77,
    "exit_reason": "",
    "id": 38968,
    "pnl": 0.373578,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T14:25:27.392377+00:00",
    "status": "OPEN",
    "symbol": "MAGMAUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:15:09.549709+00:00",
    "updated_at": "2026-06-29T15:45:17.354937+00:00"
  },
  {
    "confidence": 90.57,
    "exit_reason": "",
    "id": 38961,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T14:25:28.112417+00:00",
    "status": "OPEN",
    "symbol": "MSTRUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.771553+00:00",
    "updated_at": "2026-06-29T15:45:17.354930+00:00"
  },
  {
    "confidence": 100.0,
    "exit_reason": "",
    "id": 38960,
    "pnl": -0.095071,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T14:25:10.415881+00:00",
    "status": "OPEN",
    "symbol": "MAGMAUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.761934+00:00",
    "updated_at": "2026-06-29T15:45:17.354924+00:00"
  },
  {
    "confidence": 84.1,
    "exit_reason": "TAKE_PROFIT_1",
    "id": 38959,
    "pnl": 3.855339,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:25:45.518433+00:00",
    "status": "TP1 HIT",
    "symbol": "REUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.750298+00:00",
    "updated_at": "2026-06-29T15:45:17.354917+00:00"
  },
  {
    "confidence": 87.18,
    "exit_reason": "",
    "id": 38958,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:25:46.229241+00:00",
    "status": "OPEN",
    "symbol": "BEATUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.740708+00:00",
    "updated_at": "2026-06-29T15:45:17.354910+00:00"
  },
  {
    "confidence": 84.06,
    "exit_reason": "TAKE_PROFIT_1",
    "id": 38957,
    "pnl": 3.890785,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:25:27.697127+00:00",
    "status": "TP1 HIT",
    "symbol": "REUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.725378+00:00",
    "updated_at": "2026-06-29T15:45:17.354903+00:00"
  },
  {
    "confidence": 87.13,
    "exit_reason": "",
    "id": 38956,
    "pnl": -0.113422,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:25:28.415737+00:00",
    "status": "OPEN",
    "symbol": "BEATUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.715023+00:00",
    "updated_at": "2026-06-29T15:45:17.354896+00:00"
  },
  {
    "confidence": 84.01,
    "exit_reason": "TAKE_PROFIT_1",
    "id": 38955,
    "pnl": 3.660821,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:25:10.993221+00:00",
    "status": "TP1 HIT",
    "symbol": "REUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.704716+00:00",
    "updated_at": "2026-06-29T15:45:17.354889+00:00"
  },
  {
    "confidence": 75.34,
    "exit_reason": "",
    "id": 38954,
    "pnl": 0.197872,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:50.435025+00:00",
    "status": "OPEN",
    "symbol": "MRVLUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.692057+00:00",
    "updated_at": "2026-06-29T15:45:17.354882+00:00"
  },
  {
    "confidence": 87.07,
    "exit_reason": "",
    "id": 38953,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:45.106297+00:00",
    "status": "OPEN",
    "symbol": "SPCXUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.675232+00:00",
    "updated_at": "2026-06-29T15:45:17.354876+00:00"
  },
  {
    "confidence": 75.34,
    "exit_reason": "",
    "id": 38951,
    "pnl": 0.194131,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:30.728668+00:00",
    "status": "OPEN",
    "symbol": "MRVLUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.652430+00:00",
    "updated_at": "2026-06-29T15:45:17.354869+00:00"
  },
  {
    "confidence": 87.21,
    "exit_reason": "",
    "id": 38950,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:25.356452+00:00",
    "status": "OPEN",
    "symbol": "SPCXUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.642341+00:00",
    "updated_at": "2026-06-29T15:45:17.354862+00:00"
  },
  {
    "confidence": 75.33,
    "exit_reason": "",
    "id": 38948,
    "pnl": 0.190391,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:11.320436+00:00",
    "status": "OPEN",
    "symbol": "MRVLUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.616544+00:00",
    "updated_at": "2026-06-29T15:45:17.354855+00:00"
  },
  {
    "confidence": 87.04,
    "exit_reason": "",
    "id": 38947,
    "pnl": 0.012953,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:05.894020+00:00",
    "status": "OPEN",
    "symbol": "SPCXUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.607911+00:00",
    "updated_at": "2026-06-29T15:45:17.354849+00:00"
  },
  {
    "confidence": 77.39,
    "exit_reason": "TAKE_PROFIT_1",
    "id": 38146,
    "pnl": 3.696899,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-22T08:45:54.120754+00:00",
    "status": "TP1 HIT",
    "symbol": "SYNUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-23T00:19:06.857193+00:00",
    "updated_at": "2026-06-29T15:45:17.354841+00:00"
  },
  {
    "confidence": 100.0,
    "exit_reason": "",
    "id": 38145,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-22T04:36:08.141944+00:00",
    "status": "OPEN",
    "symbol": "RESOLVUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-23T00:19:06.853557+00:00",
    "updated_at": "2026-06-29T15:45:17.354833+00:00"
  },
  {
    "confidence": 100.0,
    "exit_reason": "",
    "id": 38071,
    "pnl": 0.662837,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-22T04:35:42.719526+00:00",
    "status": "OPEN",
    "symbol": "RESOLVUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-22T23:04:13.796088+00:00",
    "updated_at": "2026-06-29T15:45:17.354825+00:00"
  },
  {
    "confidence": 100.0,
    "exit_reason": "",
    "id": 37796,
    "pnl": 1.019956,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-22T04:35:17.651330+00:00",
    "status": "OPEN",
    "symbol": "RESOLVUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-22T18:29:11.276803+00:00",
    "updated_at": "2026-06-29T15:45:17.354814+00:00"
  },
  {
    "confidence": 88.76,
    "exit_reason": "",
    "id": 37795,
    "pnl": 0.0,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-22T01:40:53.795281+00:00",
    "status": "OPEN",
    "symbol": "BTWUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-22T18:29:11.272154+00:00",
    "updated_at": "2026-06-29T15:45:17.354768+00:00"
  }
]
```

## Latest 20 Closed Rows After Baseline
```json
[
  {
    "confidence": 93.63,
    "exit_reason": "STOP_LOSS",
    "id": 38952,
    "pnl": -2.084731,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:49.272807+00:00",
    "status": "CLOSED",
    "symbol": "SKHYNIXUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.662607+00:00",
    "updated_at": "2026-06-29T15:15:09.505862+00:00"
  },
  {
    "confidence": 93.38,
    "exit_reason": "STOP_LOSS",
    "id": 38949,
    "pnl": -2.109013,
    "regime": "TRENDING BULL",
    "side": "LONG",
    "source_signal_timestamp": "2026-06-27T13:05:29.561188+00:00",
    "status": "CLOSED",
    "symbol": "SKHYNIXUSDT",
    "target_timestamp": null,
    "timestamp": "2026-06-29T15:12:59.629291+00:00",
    "updated_at": "2026-06-29T15:15:09.505839+00:00"
  }
]
```

## Verdicts
* Overall status: **BLOCKED_ACTIVE_OVERFLOW**
* Verdict map: `{"ACTIVE_CAP_FULL": true, "ACTIVE_CAP_OK": true, "ACTIVE_CAP_OVERFLOW": false, "CP045_APPROVED": false, "DB_READABLE": true, "FORWARD_CLOSED_ROWS_PRESENT": true, "INTERNAL_PAPER_AVAILABLE": true, "LEGACY_PAPER_TRADES_DEPRECATED": true, "PHASE3_LOCKED": true, "SCORE95_EVIDENCE_INSUFFICIENT": true, "STALE_ACTIVE_CRITICAL": true, "STALE_ACTIVE_WARNING": true}`

## Final Decision
* CP-045 **NOT APPROVED**
* Phase 3 **LOCKED**
* Live execution **OFF**
* PAPER_ONLY **TRUE**

This guard is read-only and writes only this Markdown report plus the paired JSON artifact.
