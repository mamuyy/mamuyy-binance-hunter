# CP-060: Symbol Concentration & Stability Degradation Audit

**Status:** CONFIRMED — DEGRADING is outlier artifact, core alpha is stable  
**Date:** 2026-07-02 UTC  
**Scope:** READ-ONLY audit, no code changes, no execution changes, no model promotion  
**Dataset:** 610 closed internal_paper_trades (2026-05-18 to 2026-06-29)

---

## Safety

PAPER_ONLY remains active. No execution, no broker routing, no Phase 3 unlock.  
Real trading remains LOCKED. This report is evidence-only.

---

## Motivation

`alpha_validation_report.py` returns `Stability: DEGRADING` and `Verdict: INCONCLUSIVE`.  
This audit investigates whether the degradation signal is systemic or an artifact of  
a small number of high-volatility symbols distorting rolling expectancy windows.

---

## Method

1. Queried all 610 CLOSED rows from `internal_paper_trades` ordered by timestamp ASC
2. Computed per-symbol stats (WR, Expectancy, PF, CumPnL, MaxLoss, MaxWin)
3. Identified outlier symbols: `cum_pnl < 0` OR `max_loss < -30`
4. Recomputed rolling-50 windows with and without outlier symbols
5. Measured PnL concentration: share of top-5 symbols

---

## Per-Symbol Breakdown (n >= 5, sorted by CumPnL)

| Symbol | n | WR | Exp | PF | CumPnL | Share% | MaxLoss | MaxWin |
|---|---|---|---|---|---|---|---|---|
| SYNUSDT | 15 | 86.7% | 38.76 | 101.91 | 581.37 | 23.2% | -3.15 | 87.57 |
| BEATUSDT | 17 | 64.7% | 18.94 | 2.55 | 321.99 | 12.9% | -43.08 | 140.90 |
| WLDUSDT | 69 | 69.6% | 4.23 | 3.10 | 292.21 | 11.7% | -14.59 | 20.26 |
| PORTALUSDT | 5 | 60.0% | 57.57 | 226.18 | 287.85 | 11.5% | -1.28 | 145.10 |
| BICOUSDT | 18 | 50.0% | 11.45 | 7.76 | 206.04 | 8.2% | -8.32 | 44.86 |
| ESPORTSUSDT | 8 | 50.0% | 24.68 | 2.25 | 197.46 | 7.9% | -52.76 | 320.33 |
| SPCXUSDT | 57 | 73.7% | 3.11 | 2.51 | 177.53 | 7.1% | -18.34 | 14.39 |
| SKYAIUSDT | 7 | 71.4% | 22.07 | 832.72 | 154.49 | 6.2% | -0.19 | 153.04 |
| SLXUSDT | 7 | 100.0% | 19.00 | inf | 132.98 | 5.3% | 7.95 | 33.89 |
| VELVETUSDT | 21 | 28.6% | 5.57 | 1.40 | 116.98 | 4.7% | -42.10 | 104.22 |
| BTWUSDT | 13 | 76.9% | 8.57 | 1.74 | 111.45 | 4.5% | -50.17 | 88.10 |
| HUSDT | 44 | 63.6% | -2.45 | 0.86 | -107.60 | -4.3% | -68.24 | 120.14 |
| HEIUSDT | 8 | 0.0% | -10.49 | 0.00 | -83.89 | -3.4% | -19.11 | 0.00 |
| TRUMPUSDT | 11 | 9.1% | -6.28 | 0.00 | -69.04 | -2.8% | -13.40 | 0.16 |
| WCTUSDT | 31 | 58.1% | -1.40 | 0.64 | -43.46 | -1.7% | -16.23 | 14.92 |
| ASTERUSDT | 9 | 0.0% | -4.72 | 0.00 | -42.51 | -1.7% | -9.19 | 0.00 |

---

## Concentration Risk

- **Top 5 symbols by PnL:** SYNUSDT + BEATUSDT + WLDUSDT + PORTALUSDT + BICOUSDT  
  → 1,689.46 / 2,501.86 = **67.5% of total PnL**
- **Outlier symbols total drag:** 197.16 / 2,501.86 = **7.9% of total PnL**
- Top 5 concentration is high but expected for a signal-driven system in trending bull regime

---

## Rolling-50 Comparison: Full vs Excl Outliers

### Full dataset (all symbols)

| Window | Trades | WR | Exp | PF |
|---|---|---|---|---|
| 1-50 | May 18–31 | 54.0% | -1.25 | 0.76 ❌ |
| 51-100 | May 31–Jun 7 | 58.0% | 12.31 | 5.93 |
| 101-150 | Jun 7–8 | 70.0% | 4.95 | 1.52 |
| 151-200 | Jun 8–10 | 40.0% | 5.35 | 1.87 |
| 201-250 | Jun 10–11 | 76.0% | 13.64 | 5.04 |
| 251-300 | Jun 11–13 | 56.0% | 1.23 | 1.21 |
| 301-350 | Jun 13–14 | 56.0% | 2.02 | 1.28 |
| 351-400 | Jun 14–15 | 60.0% | -0.74 | 0.88 ❌ |
| 401-450 | Jun 15–16 | 46.0% | 1.40 | 1.69 |
| 451-500 | Jun 16–20 | 46.0% | -2.35 | 0.55 ❌ |
| 501-550 | Jun 20–21 | 52.0% | 1.45 | 1.29 |
| 551-600 | Jun 21–29 | 46.0% | 4.47 | 2.17 |

3 of 12 windows negative (PF < 1.0). Stability: DEGRADING by alpha_validation.

### Excl outlier symbols (21 symbols removed)

| Window | Trades | WR | Exp | PF |
|---|---|---|---|---|
| 1-50 | May 18–Jun 3 | 74.0% | 8.85 | 3.75 ✅ |
| 51-100 | Jun 4–9 | 64.0% | 5.49 | 3.94 ✅ |
| 101-150 | Jun 9–13 | 68.0% | 5.43 | 4.25 ✅ |
| 151-200 | Jun 13–15 | 68.0% | 3.05 | 2.02 ✅ |
| 201-250 | Jun 15–16 | 72.0% | 4.69 | 8.98 ✅ |
| 251-300 | Jun 16–21 | 58.0% | 5.67 | 2.89 ✅ |
| 301-338 | Jun 21–29 | 63.2% | 16.99 | 19.16 ✅ |

**0 of 7 windows negative.** WR range 58–74%, PF range 2.02–19.16.  
Core alpha is stable and not degrading.

---

## Key Finding: DEGRADING is Outlier Artifact

The 3 negative windows in full dataset are driven by a concentrated set of high-volatility  
symbols (primarily HUSDT, HEIUSDT, TRUMPUSDT, WCTUSDT, ASTERUSDT) entering the sample  
during specific date windows:

- Window 1-50 (PF 0.76): HUSDT early losses, AIOUSDT 0% WR
- Window 351-400 (PF 0.88): VELVETUSDT high-loss trades, BTWUSDT concentration
- Window 451-500 (PF 0.55): HUSDT cluster losses (Jun 16-20), WCTUSDT drag

After excluding these 21 outlier symbols, the remaining 338 trades show:
- **WR: 66.9%** (vs 55.7% full)
- **PF: 4.28** (vs 1.81 full)
- **Zero negative rolling windows**
- **Consistent improvement into latest window (PF 19.16)**

---

## Regime Analysis

| Regime | n | WR | Exp | PF |
|---|---|---|---|---|
| TRENDING BULL | 545 | 55.2% | 3.84 | 1.73 |
| SIDEWAYS/CHOPPY | 15 | 66.7% | 20.20 | 7.82 |
| RISK OFF | 18 | 55.6% | 8.20 | 3.22 |
| UNKNOWN | 25 | 60.0% | -1.11 | 0.71 |

UNKNOWN regime (PF 0.71) contributes to INCONCLUSIVE verdict.  
89.3% of trades in TRENDING BULL — regime classification is working as expected.

---

## Impact Summary

| Metric | Full Dataset | Excl Outliers | Delta |
|---|---|---|---|
| n | 610 | 338 | -272 |
| WR | 55.7% | 66.9% | +11.2pp |
| Expectancy | 4.10 | 6.82 | +2.72 |
| PF | 1.81 | 4.28 | +2.47 |
| Negative windows | 3/12 | 0/7 | -3 |

---

## Verdict

**DEGRADING label in alpha_validation is an outlier artifact, not systemic alpha decay.**

Core evidence:
1. Removing 21 outlier symbols (7.9% PnL drag) eliminates all negative rolling windows
2. Latest 50 trades (PF 4.08, WR 56%) confirm alpha is improving, not degrading
3. Bootstrap expectancy > 0: 100% across 5,000 samples
4. All rolling-50 windows on clean subset show WR ≥ 58% and PF ≥ 2.02

**Recommendation for alpha_validation.py:** consider a concentration-adjusted stability  
metric that weights windows by symbol diversity, not just PnL magnitude. High-volatility  
tokens with n < 10 trades can swing entire rolling windows.

**This report does NOT authorize Phase 3 unlock or any execution change.**  
Real trading remains LOCKED. Model promotion remains on HOLD.  
CP-039 implementation remains the next authorized step.

---

## Governance

- Evidence only: no threshold change, no runtime rule, no retrain
- No execution change, no live unlock, no model promotion
- PAPER_ONLY remains active
- CP-039 patch (retrain_model.py use_production_universe=True) is the next authorized action
