#!/usr/bin/env python3
"""
Analyze confidence calibration metrics from clean calibration dataset.

Safety:
- Reads data/ml_calibration_matched_20260520.csv only.
- Does not touch SQLite DB.
- Writes logs/calibration_metrics_report.json only.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/calibration_metrics_report.json"


def to_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, low=0.01, high=0.99):
    return max(low, min(high, value))


def bucket_name(score):
    if score < 20:
        return "00-19"
    if score < 40:
        return "20-39"
    if score < 60:
        return "40-59"
    if score < 80:
        return "60-79"
    return "80-100"


def summarize_group(rows):
    total = len(rows)
    if total == 0:
        return {}

    wins = sum(1 for r in rows if r["y"] == 1)
    losses = sum(1 for r in rows if r["y"] == 0)
    avg_score = sum(r["score"] for r in rows) / total
    avg_prob = sum(r["prob"] for r in rows) / total
    winrate = wins / total
    brier = sum((r["prob"] - r["y"]) ** 2 for r in rows) / total
    calibration_gap = avg_prob - winrate

    pos_pnl = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    neg_pnl = sum(r["pnl"] for r in rows if r["pnl"] < 0)
    profit_factor = (pos_pnl / abs(neg_pnl)) if neg_pnl else None
    avg_pnl = sum(r["pnl"] for r in rows) / total

    return {
        "rows": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate * 100, 4),
        "avg_score": round(avg_score, 4),
        "avg_prob_proxy": round(avg_prob, 6),
        "brier_proxy": round(brier, 6),
        "calibration_gap": round(calibration_gap, 6),
        "avg_pnl_pct": round(avg_pnl, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
    }


def main():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Calibration CSV not found: {CSV_PATH}")

    rows = []

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            win_loss = row.get("win_loss") or ""
            if win_loss not in ("WIN", "LOSS"):
                continue

            score = to_float(row.get("score"))
            prob = clamp(score / 100.0)
            y = 1 if win_loss == "WIN" else 0

            rows.append({
                "symbol": row.get("symbol") or "UNKNOWN",
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "prob": prob,
                "bucket": bucket_name(score),
                "y": y,
                "pnl": to_float(row.get("pnl_pct")),
            })

    by_regime = defaultdict(list)
    by_bucket = defaultdict(list)
    by_regime_bucket = defaultdict(list)

    for r in rows:
        by_regime[r["regime"]].append(r)
        by_bucket[r["bucket"]].append(r)
        by_regime_bucket[(r["regime"], r["bucket"])].append(r)

    regime_report = []
    for regime, items in by_regime.items():
        item = summarize_group(items)
        item["regime"] = regime
        regime_report.append(item)

    bucket_report = []
    for bucket, items in by_bucket.items():
        item = summarize_group(items)
        item["score_bucket"] = bucket
        bucket_report.append(item)

    regime_bucket_report = []
    for (regime, bucket), items in by_regime_bucket.items():
        item = summarize_group(items)
        item["regime"] = regime
        item["score_bucket"] = bucket
        regime_bucket_report.append(item)

    regime_report.sort(key=lambda x: x["brier_proxy"])
    bucket_report.sort(key=lambda x: x["score_bucket"])
    regime_bucket_report.sort(key=lambda x: (x["regime"], x["score_bucket"]))

    overall = summarize_group(rows)

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_CALIBRATION_METRICS",
        "source_csv": str(CSV_PATH),
        "note": "score/100 is treated as probability proxy; this is audit metric, not final isotonic/Platt calibration.",
        "overall": overall,
        "by_regime": regime_report,
        "by_score_bucket": bucket_report,
        "by_regime_score_bucket": regime_bucket_report,
        "gate_reference": {
            "phase_2c_target_brier_score": 0.24,
            "current_metric_type": "proxy_brier_from_score_field",
        },
        "verdict": "READ_ONLY_CALIBRATION_METRICS_COMPLETE",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
