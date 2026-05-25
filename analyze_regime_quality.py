#!/usr/bin/env python3
"""
Analyze regime-enriched ML dataset.

Safety:
- Reads data/ml_dataset_regime_enriched.csv only.
- Writes logs/regime_quality_report.json only.
- Does not touch SQLite database.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_dataset_regime_enriched.csv"
OUT_JSON = PROJECT_DIR / "logs/regime_quality_report.json"


def to_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def main():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Dataset not found: {CSV_PATH}")

    groups = defaultdict(lambda: {
        "rows": 0,
        "wins": 0,
        "losses": 0,
        "flats": 0,
        "tp1_hits": 0,
        "pnl_sum": 0.0,
        "positive_pnl_sum": 0.0,
        "negative_pnl_sum": 0.0,
        "exact_5m": 0,
        "near_15m": 0,
        "far_30m": 0,
        "unmatched": 0,
    })

    total_rows = 0

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            total_rows += 1

            regime = row.get("matched_regime") or "UNMATCHED"
            status = row.get("status") or ""
            win_loss = row.get("win_loss") or ""
            match_quality = row.get("match_quality") or "UNMATCHED"
            pnl = to_float(row.get("pnl_pct"))

            g = groups[regime]
            g["rows"] += 1
            g["pnl_sum"] += pnl

            if pnl > 0:
                g["positive_pnl_sum"] += pnl
            elif pnl < 0:
                g["negative_pnl_sum"] += pnl

            if win_loss == "WIN":
                g["wins"] += 1
            elif win_loss == "LOSS":
                g["losses"] += 1
            else:
                g["flats"] += 1

            if status == "TP1 HIT":
                g["tp1_hits"] += 1

            if match_quality == "EXACT_5M":
                g["exact_5m"] += 1
            elif match_quality == "NEAR_15M":
                g["near_15m"] += 1
            elif match_quality == "FAR_30M":
                g["far_30m"] += 1
            else:
                g["unmatched"] += 1

    report_rows = []

    for regime, g in groups.items():
        rows = g["rows"]
        wins = g["wins"]
        losses = g["losses"]
        trades = wins + losses

        winrate = round((wins / trades) * 100, 4) if trades else 0.0
        avg_pnl = round(g["pnl_sum"] / rows, 6) if rows else 0.0
        profit_factor = (
            round(g["positive_pnl_sum"] / abs(g["negative_pnl_sum"]), 6)
            if g["negative_pnl_sum"]
            else None
        )

        matched_rows = g["exact_5m"] + g["near_15m"] + g["far_30m"]
        matched_pct = round((matched_rows / rows) * 100, 4) if rows else 0.0

        report_rows.append({
            "regime": regime,
            "rows": rows,
            "wins": wins,
            "losses": losses,
            "flats": g["flats"],
            "tp1_hits": g["tp1_hits"],
            "winrate_pct": winrate,
            "avg_pnl_pct": avg_pnl,
            "profit_factor": profit_factor,
            "matched_rows": matched_rows,
            "matched_pct": matched_pct,
            "exact_5m": g["exact_5m"],
            "near_15m": g["near_15m"],
            "far_30m": g["far_30m"],
            "unmatched": g["unmatched"],
        })

    ranked = sorted(
        [r for r in report_rows if r["regime"] != "UNMATCHED"],
        key=lambda x: (x["profit_factor"] or 0, x["rows"]),
        reverse=True,
    )

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_ANALYSIS",
        "source_csv": str(CSV_PATH),
        "total_rows": total_rows,
        "regime_count": len(report_rows),
        "top_by_profit_factor": ranked[:10],
        "all_regimes": sorted(report_rows, key=lambda x: x["rows"], reverse=True),
        "verdict": "READ_ONLY_REGIME_ANALYSIS_COMPLETE",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
