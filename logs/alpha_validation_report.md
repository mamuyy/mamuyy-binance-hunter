# MAMUYY HUNTER — Alpha Validation Report

Safety: PAPER_ONLY / READ_ONLY / NO BROKER API / NO REAL CAPITAL / NO RUNTIME MODIFICATION.
Phase 3 remains **NOT UNLOCKED**. Real Trading remains **LOCKED**.

## Data Quality
- Total rows: 0
- Closed rows used for primary audit: 0
- Non-closed rows excluded: 0
- Invalid/null statuses: 0 null, 0 unknown
- Missing PnL rows among closed trades: 0
- Duplicate closed rows: 0
- Usable closed trades: 0
- Date range: [None, None]

## Core Performance
- Win rate: N/A%
- Expectancy per trade: N/A
- Profit factor: N/A
- Cumulative PnL: 0
- Maximum drawdown absolute: 0
- Maximum drawdown pct: N/A (missing_starting_equity_or_capital)

## Uncertainty
- Bootstrap seed/samples: 20260615 / 0
- Expectancy 95% CI: None
- Win-rate 95% CI: None
- Bootstrap expectancy > 0: N/A%

## Stability
- First half expectancy: N/A
- Second half expectancy: N/A
- Earliest 100 expectancy: N/A
- Latest 100 expectancy: N/A
- Earliest-100 to latest-100 delta: {}
- Latest rolling-50: None
- Latest 50: None
- Assessment: INCONCLUSIVE
- Recent degradation note: N/A

## Valid Regime Groups (sample_count >= 20)
- UNAVAILABLE

## Sample Limitations
- Groups with sample_count < 20 are retained in JSON as LOW_SAMPLE exploratory groups and excluded from headline best/worst rankings.
- Drawdown percentage and the 15% gate are NOT_EVALUABLE unless starting equity/capital is detected.
- shadow_trades, if present, are secondary reference only and are not mixed into primary forward-paper results.

## Readiness References (Report Only)
- 500 closed trades: False
- Rolling win rate >= 45%: {'window': 'latest_rolling_50', 'value': None, 'passed': False}
- Rolling PF >= 1.3: {'window': 'latest_rolling_50', 'value': None, 'passed': False}
- Max drawdown pct <= 15%: {'value': None, 'passed': 'UNKNOWN', 'reason': 'missing_starting_equity_or_capital'}

## Verdict and Locks
- Verdict: INCONCLUSIVE
- Phase 3: NOT UNLOCKED
- Real Trading: LOCKED
