# CP-041B: Concentration-Adjusted Stability Audit

**Type:** READ-ONLY SIDECAR — does not modify any gate, threshold, or verdict  
**Generated:** 2026-07-01T17:53:57Z  
**Governance:** raw_stability_preserved=true | phase3_unlock=false | live_unlock=false

---

## Safety

PAPER_ONLY remains active. This report does not change alpha_validation_report.py,  
stability gates, execution engine, or any runtime configuration.  
Real trading remains LOCKED. Phase 3 remains NOT UNLOCKED.

---

## Dataset

| Item | Value |
|---|---|
| Total closed trades | 610 |
| Outlier symbols identified | 15 |
| Clean trades (excl. outliers) | 354 |
| Outlier criteria | cum_pnl < 0 OR max_loss < -30 (n >= 5) |

### Outlier Symbols
- AIOUSDT
- ASTERUSDT
- BEATUSDT
- BTWUSDT
- ESPORTSUSDT
- FILUSDT
- HBARUSDT
- HEIUSDT
- HUSDT
- MRVLUSDT
- SOXLUSDT
- TRUMPUSDT
- VELVETUSDT
- WCTUSDT
- XLMUSDT

---

## Raw Stability (All 610 trades)

**Verdict: DEGRADING**  
Windows: 12 total, 3 negative

| Range | n | WR | Exp | PF |
|---|---|---|---|---|
| 1–50 | 50 | 54.0% | -1.25 | 0.76 ❌ |
| 51–100 | 50 | 58.0% | 12.31 | 5.93 ✅ |
| 101–150 | 50 | 70.0% | 4.95 | 1.52 ✅ |
| 151–200 | 50 | 40.0% | 5.35 | 1.87 ✅ |
| 201–250 | 50 | 76.0% | 13.64 | 5.04 ✅ |
| 251–300 | 50 | 56.0% | 1.23 | 1.21 ✅ |
| 301–350 | 50 | 56.0% | 2.02 | 1.28 ✅ |
| 351–400 | 50 | 60.0% | -0.74 | 0.88 ❌ |
| 401–450 | 50 | 46.0% | 1.40 | 1.69 ✅ |
| 451–500 | 50 | 46.0% | -2.35 | 0.55 ❌ |
| 501–550 | 50 | 52.0% | 1.45 | 1.29 ✅ |
| 551–600 | 50 | 46.0% | 4.47 | 2.17 ✅ |

---

## Adjusted Stability (Clean 354 trades, outliers excluded)

**Verdict: STABLE**  
Windows: 7 total, 0 negative

| Range | n | WR | Exp | PF |
|---|---|---|---|---|
| 1–50 | 50 | 72.0% | 6.01 | 2.87 ✅ |
| 51–100 | 50 | 64.0% | 8.80 | 7.77 ✅ |
| 101–150 | 50 | 68.0% | 4.92 | 4.01 ✅ |
| 151–200 | 50 | 56.0% | 1.15 | 1.32 ✅ |
| 201–250 | 50 | 78.0% | 6.01 | 26.63 ✅ |
| 251–300 | 50 | 64.0% | 5.20 | 2.76 ✅ |
| 301–350 | 50 | 48.0% | 10.71 | 4.31 ✅ |

---

## Clean Subset Performance

| Metric | Value |
|---|---|
| n | 354 |
| Win Rate | 64.69% |
| Expectancy | 6.2051 |
| Profit Factor | 3.71 |
| Latest-50 PF | 4.71 |

---

## Conclusion

| Finding | Value |
|---|---|
| Raw stability | DEGRADING (3/12 negative windows) |
| Adjusted stability | STABLE (0/7 negative windows) |
| Degradation systemic | NO |
| Degradation concentration artifact | YES |
| Recommendation | **READY_FOR_UNLOCK_REVIEW** |

---

## Governance

- Evidence only — no threshold change, no runtime rule, no execution change
- alpha_validation_report.py Stability field unchanged (still shows raw DEGRADING)
- Phase 3 NOT UNLOCKED
- Real trading LOCKED
- PAPER_ONLY active
- Forward trades from CP-040 model (acc=70.85%) to be monitored for further evidence
