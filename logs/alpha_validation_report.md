# MAMUYY HUNTER — Alpha Validation Report

Safety: PAPER_ONLY / READ_ONLY / NO BROKER API / NO REAL CAPITAL / NO RUNTIME MODIFICATION.
Phase 3 remains **NOT UNLOCKED**. Real Trading remains **LOCKED**.

## Data Quality
- Total rows: 384
- Closed rows used for primary audit: 249
- Non-closed rows excluded: 126
- Invalid/null statuses: 0 null, 9 unknown
- Missing PnL rows among closed trades: 0
- Duplicate closed rows: 0
- Usable closed trades: 249
- Date range: ['2026-05-18T13:46:59.526200+00:00', '2026-06-15T02:11:11.548770+00:00']

## Core Performance
- Win rate: 59.0361%
- Expectancy per trade: 6.18132
- Profit factor: 1.79638
- Cumulative PnL: 1539.15
- Maximum drawdown absolute: 442.22
- Maximum drawdown pct: N/A (missing_starting_equity_or_capital)

## Uncertainty
- Bootstrap seed/samples: 20260615 / 5000
- Expectancy 95% CI: [1.7178508554216865, 11.184108815261045]
- Win-rate 95% CI: [0.5301204819277109, 0.6506024096385542]
- Bootstrap expectancy > 0: 99.72%

## Stability
- First half expectancy: 6.84334
- Second half expectancy: 5.52458
- Earliest 100 expectancy: 7.78528
- Latest 100 expectancy: 4.64024
- Earliest-100 to latest-100 delta: {'expectancy_per_trade': -3.1450418000000004, 'profit_factor': -0.34814239845654527, 'win_rate': -0.12, 'cumulative_pnl': -314.5041799999999}
- Latest rolling-50: {'start_index': 200, 'end_index': 249, 'sample_count': 50, 'expectancy': 1.3434649199999997, 'profit_factor': 1.141977681645874, 'profit_factor_reason': None, 'win_rate': 0.54}
- Latest 50: {'sample_count': 50, 'wins': 27, 'losses': 23, 'breakeven': 0, 'win_rate': 0.54, 'average_win': 20.011060592592592, 'average_loss': -20.570669130434784, 'payoff_ratio': 0.9727958028835223, 'expectancy_per_trade': 1.3434649199999997, 'gross_profit': 540.298636, 'gross_loss': -473.12539, 'profit_factor': 1.141977681645874, 'profit_factor_reason': None, 'cumulative_pnl': 67.17324599999999, 'maximum_drawdown_absolute': 385.18930099999994, 'maximum_drawdown_pct': None, 'maximum_drawdown_pct_reason': 'missing_starting_equity_or_capital', 'longest_winning_streak': 17, 'longest_losing_streak': 6}
- Assessment: DEGRADING
- Recent degradation note: N/A

## Valid Regime Groups (sample_count >= 20)
- TRENDING BULL: n=228, win_rate=58.7719%, expectancy=5.82038, PF=1.71754

## Sample Limitations
- Groups with sample_count < 20 are retained in JSON as LOW_SAMPLE exploratory groups and excluded from headline best/worst rankings.
- Drawdown percentage and the 15% gate are NOT_EVALUABLE unless starting equity/capital is detected.
- shadow_trades, if present, are secondary reference only and are not mixed into primary forward-paper results.

## Readiness References (Report Only)
- 500 closed trades: False
- Rolling win rate >= 45%: {'window': 'latest_rolling_50', 'value': 0.54, 'passed': True}
- Rolling PF >= 1.3: {'window': 'latest_rolling_50', 'value': 1.141977681645874, 'passed': False}
- Max drawdown pct <= 15%: {'value': None, 'passed': 'UNKNOWN', 'reason': 'missing_starting_equity_or_capital'}

## Verdict and Locks
- Verdict: INCONCLUSIVE
- Phase 3: NOT UNLOCKED
- Real Trading: LOCKED
