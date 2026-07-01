#!/usr/bin/env python3
"""CP-041B: Concentration-Adjusted Stability Audit — READ-ONLY sidecar.

Governance:
  raw_stability_preserved = True
  alpha_verdict_forced     = False
  phase3_unlock            = False
  live_unlock              = False
  paper_only               = True

Does NOT modify alpha_validation_report.py, gate logic, thresholds,
model registry, execution engine, or any runtime configuration.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_PATH = "mamuyy_hunter.db"
REPORT_DIR = "reports"
WINDOW = 50
OUTLIER_CRITERIA_CUM_PNL_LT = 0.0
OUTLIER_CRITERIA_MAX_LOSS_LT = -30.0
MIN_TRADES_FOR_OUTLIER_CHECK = 5


def safe_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def profit_factor(pnls: list[float]) -> float:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls) * 100


def expectancy(pnls: list[float]) -> float:
    return sum(pnls) / len(pnls) if pnls else 0.0


def rolling_windows(pnls: list[float], window: int = WINDOW) -> list[dict[str, Any]]:
    results = []
    i = 0
    while i + window <= len(pnls):
        w = pnls[i: i + window]
        pf = profit_factor(w)
        results.append({
            "window_start": i + 1,
            "window_end": i + window,
            "n": window,
            "win_rate_pct": round(win_rate(w), 2),
            "expectancy": round(expectancy(w), 4),
            "profit_factor": round(pf, 4) if math.isfinite(pf) else None,
            "negative": pf < 1.0,
        })
        i += window
    # partial last window if >= 20 trades left
    if i < len(pnls) and len(pnls) - i >= 20:
        w = pnls[i:]
        pf = profit_factor(w)
        results.append({
            "window_start": i + 1,
            "window_end": len(pnls),
            "n": len(w),
            "win_rate_pct": round(win_rate(w), 2),
            "expectancy": round(expectancy(w), 4),
            "profit_factor": round(pf, 4) if math.isfinite(pf) else None,
            "negative": pf < 1.0,
        })
    return results


def main() -> None:
    con = sqlite3.connect(f"file:{Path(DB_PATH).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT symbol, pnl, timestamp FROM internal_paper_trades "
        "WHERE status='CLOSED' AND pnl IS NOT NULL "
        "ORDER BY timestamp ASC"
    ).fetchall()
    con.close()

    all_trades = [{"symbol": r["symbol"], "pnl": float(r["pnl"]), "ts": r["timestamp"]} for r in rows]
    n_total = len(all_trades)

    # Per-symbol stats
    sym_pnls: dict[str, list[float]] = {}
    for t in all_trades:
        sym_pnls.setdefault(t["symbol"], []).append(t["pnl"])

    sym_stats: dict[str, dict[str, Any]] = {}
    for sym, pnls in sym_pnls.items():
        cum = sum(pnls)
        ml = min(pnls)
        n = len(pnls)
        sym_stats[sym] = {
            "n": n,
            "win_rate_pct": round(win_rate(pnls), 2),
            "expectancy": round(expectancy(pnls), 4),
            "profit_factor": round(profit_factor(pnls), 4) if math.isfinite(profit_factor(pnls)) else None,
            "cum_pnl": round(cum, 4),
            "max_loss": round(ml, 4),
        }

    # Identify outlier symbols (same criteria as CP-060)
    outlier_symbols: set[str] = set()
    for sym, s in sym_stats.items():
        if s["n"] >= MIN_TRADES_FOR_OUTLIER_CHECK:
            if s["cum_pnl"] < OUTLIER_CRITERIA_CUM_PNL_LT or s["max_loss"] < OUTLIER_CRITERIA_MAX_LOSS_LT:
                outlier_symbols.add(sym)

    # Split trades
    raw_pnls = [t["pnl"] for t in all_trades]
    clean_trades = [t for t in all_trades if t["symbol"] not in outlier_symbols]
    clean_pnls = [t["pnl"] for t in clean_trades]
    n_clean = len(clean_pnls)

    # Rolling windows
    raw_windows = rolling_windows(raw_pnls)
    clean_windows = rolling_windows(clean_pnls)

    raw_negative = sum(1 for w in raw_windows if w["negative"])
    clean_negative = sum(1 for w in clean_windows if w["negative"])

    # Stability scores
    raw_stability = "DEGRADING" if raw_negative >= 3 else ("STABLE" if raw_negative == 0 else "WEAK")
    adj_stability = "STABLE" if clean_negative == 0 else ("WEAK" if clean_negative <= 1 else "DEGRADING")

    # Latest 50 window on clean
    latest_clean_pnls = clean_pnls[-50:] if len(clean_pnls) >= 50 else clean_pnls
    latest_clean_pf = profit_factor(latest_clean_pnls)

    # Recommendation
    if adj_stability == "STABLE" and clean_negative == 0 and latest_clean_pf >= 2.0:
        recommendation = "READY_FOR_UNLOCK_REVIEW"
    elif adj_stability in ("STABLE", "WEAK") and clean_negative <= 1:
        recommendation = "WAIT_FOR_FORWARD_EVIDENCE"
    else:
        recommendation = "BLOCKED"

    report: dict[str, Any] = {
        "change_id": "CP-041B",
        "type": "READ_ONLY_SIDECAR_AUDIT",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "governance": {
            "raw_stability_preserved": True,
            "alpha_verdict_forced": False,
            "phase3_unlock": False,
            "live_unlock": False,
            "paper_only": True,
            "execution_change": False,
        },
        "dataset": {
            "total_closed_trades": n_total,
            "outlier_symbols": sorted(outlier_symbols),
            "outlier_symbol_count": len(outlier_symbols),
            "clean_trade_count": n_clean,
            "outlier_criteria": {
                "cum_pnl_lt": OUTLIER_CRITERIA_CUM_PNL_LT,
                "max_loss_lt": OUTLIER_CRITERIA_MAX_LOSS_LT,
                "min_trades_to_qualify": MIN_TRADES_FOR_OUTLIER_CHECK,
            },
        },
        "raw_stability": {
            "verdict": raw_stability,
            "total_windows": len(raw_windows),
            "negative_windows": raw_negative,
            "windows": raw_windows,
        },
        "adjusted_stability": {
            "verdict": adj_stability,
            "total_windows": len(clean_windows),
            "negative_windows": clean_negative,
            "windows": clean_windows,
        },
        "clean_subset_metrics": {
            "n": n_clean,
            "win_rate_pct": round(win_rate(clean_pnls), 4),
            "expectancy": round(expectancy(clean_pnls), 4),
            "profit_factor": round(profit_factor(clean_pnls), 4) if math.isfinite(profit_factor(clean_pnls)) else None,
            "latest_50_profit_factor": round(latest_clean_pf, 4) if math.isfinite(latest_clean_pf) else None,
        },
        "per_symbol_stats": sym_stats,
        "conclusion": {
            "degradation_is_systemic": raw_negative > 0 and clean_negative > 0,
            "degradation_is_concentration_artifact": raw_negative > 0 and clean_negative == 0,
            "recommendation": recommendation,
        },
    }

    # Markdown report
    def pf_str(v: float | None) -> str:
        if v is None: return "inf"
        return f"{v:.2f}"

    neg_mark = lambda w: "❌" if w["negative"] else "✅"

    raw_table = "\n".join(
        f"| {w['window_start']}–{w['window_end']} | {w['n']} | {w['win_rate_pct']}% | {w['expectancy']:.2f} | {pf_str(w['profit_factor'])} {neg_mark(w)} |"
        for w in raw_windows
    )
    clean_table = "\n".join(
        f"| {w['window_start']}–{w['window_end']} | {w['n']} | {w['win_rate_pct']}% | {w['expectancy']:.2f} | {pf_str(w['profit_factor'])} {neg_mark(w)} |"
        for w in clean_windows
    )
    outlier_list = "\n".join(f"- {s}" for s in sorted(outlier_symbols))

    md = f"""# CP-041B: Concentration-Adjusted Stability Audit

**Type:** READ-ONLY SIDECAR — does not modify any gate, threshold, or verdict  
**Generated:** {report['generated_at']}  
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
| Total closed trades | {n_total} |
| Outlier symbols identified | {len(outlier_symbols)} |
| Clean trades (excl. outliers) | {n_clean} |
| Outlier criteria | cum_pnl < 0 OR max_loss < -30 (n >= {MIN_TRADES_FOR_OUTLIER_CHECK}) |

### Outlier Symbols
{outlier_list}

---

## Raw Stability (All {n_total} trades)

**Verdict: {raw_stability}**  
Windows: {len(raw_windows)} total, {raw_negative} negative

| Range | n | WR | Exp | PF |
|---|---|---|---|---|
{raw_table}

---

## Adjusted Stability (Clean {n_clean} trades, outliers excluded)

**Verdict: {adj_stability}**  
Windows: {len(clean_windows)} total, {clean_negative} negative

| Range | n | WR | Exp | PF |
|---|---|---|---|---|
{clean_table}

---

## Clean Subset Performance

| Metric | Value |
|---|---|
| n | {n_clean} |
| Win Rate | {win_rate(clean_pnls):.2f}% |
| Expectancy | {expectancy(clean_pnls):.4f} |
| Profit Factor | {pf_str(profit_factor(clean_pnls) if math.isfinite(profit_factor(clean_pnls)) else None)} |
| Latest-50 PF | {pf_str(latest_clean_pf if math.isfinite(latest_clean_pf) else None)} |

---

## Conclusion

| Finding | Value |
|---|---|
| Raw stability | {raw_stability} ({raw_negative}/{len(raw_windows)} negative windows) |
| Adjusted stability | {adj_stability} ({clean_negative}/{len(clean_windows)} negative windows) |
| Degradation systemic | {"YES" if report['conclusion']['degradation_is_systemic'] else "NO"} |
| Degradation concentration artifact | {"YES" if report['conclusion']['degradation_is_concentration_artifact'] else "NO"} |
| Recommendation | **{recommendation}** |

---

## Governance

- Evidence only — no threshold change, no runtime rule, no execution change
- alpha_validation_report.py Stability field unchanged (still shows raw DEGRADING)
- Phase 3 NOT UNLOCKED
- Real trading LOCKED
- PAPER_ONLY active
- Forward trades from CP-040 model (acc=70.85%) to be monitored for further evidence
"""

    import os
    os.makedirs(REPORT_DIR, exist_ok=True)
    json_path = f"{REPORT_DIR}/cp041b_concentration_adjusted_stability.json"
    md_path = f"{REPORT_DIR}/cp041b_concentration_adjusted_stability.md"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    with open(md_path, "w") as f:
        f.write(md)

    print("=" * 60)
    print("CP-041B: CONCENTRATION-ADJUSTED STABILITY AUDIT")
    print("=" * 60)
    print(f"Total trades     : {n_total}")
    print(f"Outlier symbols  : {len(outlier_symbols)}")
    print(f"Clean trades     : {n_clean}")
    print(f"Raw stability    : {raw_stability} ({raw_negative}/{len(raw_windows)} neg windows)")
    print(f"Adj stability    : {adj_stability} ({clean_negative}/{len(clean_windows)} neg windows)")
    print(f"Degradation artifact: {report['conclusion']['degradation_is_concentration_artifact']}")
    print(f"Recommendation   : {recommendation}")
    print(f"Created: {json_path}")
    print(f"Created: {md_path}")


if __name__ == "__main__":
    main()
