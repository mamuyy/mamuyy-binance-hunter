#!/usr/bin/env python3
"""
Build clean calibration dataset from regime-enriched ML dataset.

Safety:
- Reads data/ml_dataset_regime_enriched.csv only.
- Does not touch SQLite DB.
- Writes generated CSV/JSON only to data/ and logs/.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
DEFAULT_SOURCE = PROJECT_DIR / "data/ml_dataset_regime_enriched.csv"
DEFAULT_OUT_CSV = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
DEFAULT_OUT_JSON = PROJECT_DIR / "logs/calibration_dataset_report.json"


def to_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def to_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def summarize(rows):
    groups = defaultdict(lambda: {
        "rows": 0,
        "wins": 0,
        "losses": 0,
        "flats": 0,
        "tp1_hits": 0,
        "pnl_sum": 0.0,
        "positive_pnl_sum": 0.0,
        "negative_pnl_sum": 0.0,
    })

    for row in rows:
        regime = row.get("matched_regime") or "UNKNOWN"
        win_loss = row.get("win_loss") or ""
        status = row.get("status") or ""
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

    result = []
    for regime, g in groups.items():
        rows_count = g["rows"]
        trades = g["wins"] + g["losses"]
        winrate = round((g["wins"] / trades) * 100, 4) if trades else 0.0
        avg_pnl = round(g["pnl_sum"] / rows_count, 6) if rows_count else 0.0
        profit_factor = (
            round(g["positive_pnl_sum"] / abs(g["negative_pnl_sum"]), 6)
            if g["negative_pnl_sum"]
            else None
        )

        result.append({
            "regime": regime,
            "rows": rows_count,
            "wins": g["wins"],
            "losses": g["losses"],
            "flats": g["flats"],
            "tp1_hits": g["tp1_hits"],
            "winrate_pct": winrate,
            "avg_pnl_pct": avg_pnl,
            "profit_factor": profit_factor,
        })

    return sorted(result, key=lambda x: (x["profit_factor"] or 0, x["rows"]), reverse=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--start-date", default="2026-05-20")
    parser.add_argument("--out-csv", type=Path, default=DEFAULT_OUT_CSV)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    args = parser.parse_args()

    if not args.source.exists():
        raise FileNotFoundError(f"Source CSV not found: {args.source}")

    start_dt = datetime.fromisoformat(args.start_date).replace(tzinfo=timezone.utc)

    all_rows = []
    kept_rows = []
    dropped_unmatched = 0
    dropped_before_start = 0

    with args.source.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        for row in reader:
            all_rows.append(row)

            signal_dt = to_dt(row.get("signal_timestamp"))
            if signal_dt is None or signal_dt < start_dt:
                dropped_before_start += 1
                continue

            if (row.get("match_quality") or "UNMATCHED") == "UNMATCHED":
                dropped_unmatched += 1
                continue

            kept_rows.append(row)

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)

    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)

    total = len(all_rows)
    kept = len(kept_rows)
    kept_pct = round((kept / total) * 100, 4) if total else 0.0

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_CALIBRATION_DATASET_BUILD",
        "source_csv": str(args.source),
        "start_date": args.start_date,
        "total_source_rows": total,
        "kept_rows": kept,
        "kept_pct_of_source": kept_pct,
        "dropped_before_start": dropped_before_start,
        "dropped_unmatched_after_start": dropped_unmatched,
        "output_csv": str(args.out_csv),
        "output_json": str(args.out_json),
        "by_regime": summarize(kept_rows),
        "verdict": "READ_ONLY_CALIBRATION_DATASET_COMPLETE",
    }

    args.out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
