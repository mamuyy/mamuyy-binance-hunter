# CP-044B Internal Paper Recovery Audit Evidence

Generated: `2026-06-29T15:40:05.424417+00:00`
Baseline CP-042: `2026-06-22T18:05:35.736930+00:00`

## Governance

- audit_mode: `READ_ONLY`
- db_writes: `False`
- runtime_changes: `False`
- model_changes: `False`
- threshold_changes: `False`
- execution_changes: `False`
- paper_only: `True`
- classifier: `FROZEN`
- model_promotion: `HOLD`
- phase3: `LOCKED`
- live_execution: `OFF`

## Internal Paper Trades

- Active count: `20` / `20`
- Active cap full: `True`
- Expired orphaned count: `136`
- Latest source_signal_timestamp: `2026-06-27T14:25:44.807487+00:00`
- Closed rows after baseline: `2`
- Closed score>=95 rows after baseline: `0`

### Status Counts

| Status | Count |
|---|---:|
| CLOSED | 588 |
| OPEN | 16 |
| TP1 HIT | 4 |

### Latest Closed Forward Rows

| ID | Source Signal Timestamp | Symbol | Confidence | Status | Exit Reason | PnL |
|---:|---|---|---:|---|---|---:|
| 38952 | 2026-06-27T13:05:49.272807+00:00 | SKHYNIXUSDT | 93.63 | CLOSED | STOP_LOSS | -2.084731 |
| 38949 | 2026-06-27T13:05:29.561188+00:00 | SKHYNIXUSDT | 93.38 | CLOSED | STOP_LOSS | -2.109013 |

## Score Buckets

- signal_candidates: `{'available': True, 'table': 'signal_candidates', 'timestamp_column': 'timestamp', 'score_column': 'score', 'latest_timestamp': '2026-06-29T14:20:44.113610+00:00', 'after_baseline_total': 512, 'buckets_after_baseline': {'gte_85': 0, 'gte_90': 0, 'gte_95': 0}}`

## Freshness

- signals: `{'available': True, 'table': 'signals', 'row_count': 321726, 'timestamp_column': 'timestamp', 'latest_timestamp': '2026-06-29T14:20:05.945062+00:00'}`
- shadow_trades: `{'available': True, 'table': 'shadow_trades', 'row_count': 61407, 'timestamp_column': 'timestamp', 'latest_timestamp': '2026-06-29T15:15:07.102564+00:00'}`

## Final Decision

- Internal paper bridge recovery: **OPERATIONALLY RESOLVED**
- Score95 forward evidence: **INSUFFICIENT**
- CP-045: **NOT APPROVED**
- Phase 3: **LOCKED**
- Live execution: **OFF**
- PAPER_ONLY: **TRUE**
