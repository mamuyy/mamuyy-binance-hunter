# CP-039 — Production-Universe Dataset Contract Plan
**Status:** PLANNING ONLY — no code changes
**Generated:** 2026-06-28 UTC
**Prerequisite:** Phase 1A audit (77d592f) — CRITICAL universe mismatch confirmed

---

## Current State (Baseline)

### build_ml_dataset() Source Priority (current)

```
1. paper_trades.csv          → EMPTY (0 data rows, header only)
2. [fallback] _historical_dataset()
   → historical_outcomes JOIN signals JOIN flow_logs
   → WHERE s.timestamp >= cutoff (NO score filter)
   → 46,755 rows, avg score 26.1, majority LOSS (54.1%)
```

### Problem

Model trained on 98.9% signals that production never routes.
Accuracy 33.79% < majority-class baseline 54.1%.

---

## CP-039 Dataset Contract Design

### Source Priority (proposed)

```
Priority 1 — internal_paper_trades (CLOSED only)
  Source: internal_paper_trades DB table
  Filter: status = "CLOSED" AND exit_reason IN ("TAKE_PROFIT_2","STOP_LOSS")
  Count:  446 rows (545 CLOSED - 95 EXPIRED_ORPHANED - 4 NULL exit_reason)
  Note:   TAKE_PROFIT_1 exit_reason = 0 rows in DB (schema audit 2026-06-28)
  Label derivation: from exit_reason field directly
  Feature source: JOIN signals ON source_signal_timestamp + symbol
  Confidence: avg 87.84, range 75.0-100.0 — all 446 valid rows already >= 75
              (confidence filter removes 0 additional rows)

Priority 2 — historical_outcomes WHERE score >= 75
  Source: historical_outcomes DB table
  Filter: score >= 75 AND status IN ("WIN","LOSS","TP1 HIT")
  Count:  1,048 rows (no OPEN rows at score>=75)
  Label breakdown: WIN 286 (27.3%), LOSS 570 (54.4%), TP1 HIT 190 (18.1%)
  Feature source: JOIN signals + flow_logs ON signal_timestamp + symbol

Priority 3 — EXCLUDED
  - historical_outcomes WHERE score < 75 (45,707 noise rows)
  - status = "OPEN" (no resolved outcome)
  - exit_reason = "EXPIRED_ORPHANED" (cleanup-driven, not market-driven)
  - exit_reason = "" / NULL (unknown exit, no reliable label)
```

### Label Mapping

```
internal_paper_trades exit_reason → training label (schema audit 2026-06-28):
  TAKE_PROFIT_2    → "WIN"      (255 rows)
  TAKE_PROFIT_1    → "TP1 HIT" (0 rows — NOT PRESENT in DB)
  STOP_LOSS        → "LOSS"    (191 rows)
  EXPIRED_ORPHANED → EXCLUDE   (95 rows, not market-driven)
  NULL             → EXCLUDE   (4 rows, unknown exit)

  ⚠ TP1 HIT class will come ENTIRELY from historical_outcomes, not from
    internal_paper_trades. Class imbalance risk must be monitored at training time.

historical_outcomes status → training label:
  "WIN"     → "WIN"
  "LOSS"    → "LOSS"
  "TP1 HIT" → "TP1 HIT"
  "OPEN"    → EXCLUDE
  "FLAT"    → EXCLUDE (ambiguous)
```

### Feature Availability

```
internal_paper_trades CLOSED JOIN signals (545/545 = 100% match):
  score             : 545/545 (100%) via signals.score
  pressure_score    : 527/545 (96.7%)
  regime_score      : 528/545 (96.9%)
  volume_spike      : 545/545 (100%)
  breakout          : 545/545 (100%)
  liquidity_sweep   : 545/545 (100%)
  funding_zscore    : via flow_logs JOIN
  oi_expansion_rate : via flow_logs JOIN
  taker_delta       : via flow_logs JOIN
  squeeze_probability: via flow_logs JOIN
  regime_name       : 528/545 (96.9%)
  whale_activity    : 527/545 (96.7%)
  funding_warning   : via flow_logs JOIN

historical_outcomes JOIN signals + flow_logs:
  All NUMERIC_FEATURES available (existing pipeline, no change)
```

### Expected Dataset After CP-039

```
Source                                    | Rows  | Labels
------------------------------------------|-------|------------------
internal_paper_trades CLOSED (clean)      |  446  | WIN / LOSS only
historical_outcomes score>=75 (excl OPEN) | 1,048 | WIN / LOSS / TP1
TOTAL                                     | 1,494 |

Label distribution estimate:
  LOSS    : ~762  (51.0%)  vs 54.1% current
  WIN     : ~541  (36.2%)  vs 31.9% current
  TP1 HIT :  190  (12.7%)  vs 13.5% current  ← 100% from historical_outcomes

⚠ TP1 HIT class imbalance risk: all 190 TP1 rows come from one source (historical_outcomes).
  Monitor class recall for TP1 HIT during training. Consider weighted loss if recall < 0.3.

Dataset shrinks 46,755 → 1,494 rows (actuals from schema audit 2026-06-28).
All rows represent production-quality signals (score >= 75 or confidence >= 75).
```

### Timestamp Policy

```
- Sort ALL rows by signal_timestamp ASC before train/test split
- NO random shuffling (shuffle=False enforced)
- Walk-forward folds must respect chronological order
- internal_paper_trades: use source_signal_timestamp as prediction timestamp
- validate_temporal_feature_rows() must return PASS on final dataset
```

---

## Proposed Code Changes (plan only — no patch)

### 1. ml_engine.py — new function `_production_universe_dataset()`

```python
def _production_universe_dataset(
    database_path: str,
    score_threshold: int = 75,
) -> pd.DataFrame:
    """
    CP-039: production-universe dataset builder.
    Priority 1: internal_paper_trades CLOSED (clean exits).
    Priority 2: historical_outcomes WHERE score >= threshold (excl OPEN/FLAT).
    """
    # Step 1: internal_paper_trades CLOSED
    #   JOIN signals ON source_signal_timestamp + symbol
    #   LEFT JOIN flow_logs ON signal_timestamp + symbol
    #   WHERE status = 'CLOSED'
    #     AND exit_reason IN ('TAKE_PROFIT_2','TAKE_PROFIT_1','STOP_LOSS')
    # Step 2: map exit_reason → status label
    #   EXIT_LABEL_MAP = {
    #       "TAKE_PROFIT_2": "WIN",
    #       "TAKE_PROFIT_1": "TP1 HIT",
    #       "STOP_LOSS": "LOSS",
    #   }
    # Step 3: historical_outcomes WHERE score >= threshold
    #   AND status IN ('WIN','LOSS','TP1 HIT')
    #   JOIN signals + flow_logs (existing query pattern)
    # Step 4: union both sources, sort by signal_timestamp ASC
    # Step 5: call _prepare_dataset(combined_df)
    # Step 6: return dataset with build report attached
    pass  # implementation pending patch review
```

### 2. build_ml_dataset() — new opt-in parameters

```python
def build_ml_dataset(
    paper_trades_path: str,
    signals_log_path: str,
    flow_log_path: str,
    database_path: str = "mamuyy_hunter.db",
    use_production_universe: bool = False,   # NEW — safe default = off
    production_score_threshold: int = 75,   # NEW
) -> pd.DataFrame:
    ...
    if use_production_universe:
        return _production_universe_dataset(database_path, production_score_threshold)
    # existing fallback unchanged below this point
```

### 3. retrain_model.py — pass production universe flag

```python
dataset = build_ml_dataset(
    paper_trades_path,
    signals_log_path,
    flow_log_path,
    database_path=database_path,
    use_production_universe=True,          # NEW
    production_score_threshold=75,         # NEW
)
```

### 4. Dataset build report (new output file per retrain)

```json
{
  "cp039_dataset_build_report": {
    "source_ipt_rows": 446,
    "source_historical_rows": 1048,
    "total_rows": 1494,
    "score_threshold": 75,
    "excluded_open": 0,
    "excluded_expired_orphaned": 95,
    "excluded_null_exit": 4,
    "excluded_take_profit_1_not_present": 0,
    "excluded_low_score": 45707,
    "label_distribution": {
      "LOSS": 761,
      "WIN": 541,
      "TP1 HIT": 197
    },
    "temporal_guard_status": "PASS"
  }
}
```

---

## Backward Compatibility

```
- use_production_universe defaults to False → existing behavior UNCHANGED
- paper_trades.csv path still accepted (no removal)
- _historical_dataset() kept as fallback (no deletion)
- All safety gates, approval flows, broker code: UNTOUCHED
- No alert_score_threshold config changes
- No model promotion until CP-039 audit passes
- No changes to execution engine, PAPER_ONLY boundary, or broker API
```

---

## Acceptance Criteria (pre-merge checklist)

- [ ] internal_paper_trades CLOSED (clean) JOIN signals: feature coverage verified via audit query
- [ ] historical_outcomes score>=75 rows: all status IN (WIN, LOSS, TP1 HIT) — no OPEN
- [ ] Combined dataset sorted chronologically (signal_timestamp ASC) — no shuffle
- [ ] validate_temporal_feature_rows() returns PASS on CP-039 dataset
- [ ] Dataset build report generated and logged per retrain run
- [ ] No production model promotion triggered by CP-039 patch
- [ ] ml_quality_audit.py shows label distribution improvement vs baseline
- [ ] WF accuracy on CP-039 dataset >= 0.55 (baseline to beat: 33.79%)
- [ ] CP-039 read-only plan accepted (this document) before implementation begins

---

## Open Questions (governance decision required before implementation)

**Q1. EXPIRED_ORPHANED exclusion confirmed?**
95 rows (17.4% of CLOSED) — positions closed by cleanup script, not by market.
Recommend EXCLUDE but needs sign-off. If included as "LOSS", adds 95 noisy LOSS labels.

**Q2. score_threshold = 75 or 85?**
- threshold 75 → 1,048 hist rows + 446 ipt = 1,494 total
- threshold 85 → 524 hist rows + 446 ipt = 970 total (still feasible)
- 75 matches alert_score_threshold; 85 = observed production entry level
- Recommend 75 as starting point (more data); escalate to 85 if quality still poor

**Q3. pnl-based label vs exit_reason-based label for internal_paper_trades?**
- Option A: exit_reason mapping (recommended) — preserves 4-class target (WIN/LOSS/TP1)
- Option B: pnl > 0 → WIN / pnl <= 0 → LOSS — simpler, loses TP1 HIT class
- Recommend Option A

**Q4. walkforward_results table cleanup?**
11.37M rows include pre-CP038A folds with n=1 (test_accuracy 0.0-1.0 range).
Recommend TRUNCATE before next CP-039 retrain run.
REQUIRES explicit governance approval — destructive to historical WF data.

---

## What CP-039 Does NOT Change

- Broker API calls
- Execution engine (manual_actual_testnet_roundtrip_controller.py)
- PAPER_ONLY safety boundary
- config.alert_score_threshold (currently 85)
- Production model in use (no promotion)
- Live unlock state
- Auto-retrain production trigger
- telegram_bot.py or any Telegram sender
- Crontab, tmux sessions, DB schema

---

## Governance Sign-off — 2026-06-27

- CP-039 planning questions are resolved.
- Governance sign-off is complete.
- CP-039 implementation remains pending and must not begin in this change.
- No production changes are authorized by this sign-off.
- PAPER_ONLY remains active.
- Model promotion remains on HOLD.
- Retraining remains prohibited until separately approved.
