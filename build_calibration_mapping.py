#!/usr/bin/env python3
"""
Build empirical calibration mapping from clean calibration dataset.

Safety:
- Reads data/ml_calibration_matched_20260520.csv only.
- Does not touch SQLite DB.
- Writes logs/calibration_mapping_report.json only.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/calibration_mapping_report.json"


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


def calc_brier(rows, prob_key):
    if not rows:
        return None
    return sum((r[prob_key] - r["y"]) ** 2 for r in rows) / len(rows)


def group_stats(rows):
    total = len(rows)
    wins = sum(1 for r in rows if r["y"] == 1)
    losses = sum(1 for r in rows if r["y"] == 0)
    winrate = wins / total if total else 0.0
    pos_pnl = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    neg_pnl = sum(r["pnl"] for r in rows if r["pnl"] < 0)
    return {
        "rows": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate * 100, 4),
        "calibrated_prob": round(winrate, 6),
        "avg_pnl_pct": round(sum(r["pnl"] for r in rows) / total, 6) if total else 0.0,
        "profit_factor": round(pos_pnl / abs(neg_pnl), 6) if neg_pnl else None,
    }


def main():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    rows = []

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            win_loss = row.get("win_loss") or ""
            if win_loss not in ("WIN", "LOSS"):
                continue

            score = to_float(row.get("score"))
            rows.append({
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "bucket": bucket_name(score),
                "raw_prob": clamp(score / 100.0),
                "y": 1 if win_loss == "WIN" else 0,
                "pnl": to_float(row.get("pnl_pct")),
            })

    overall = group_stats(rows)

    by_bucket = defaultdict(list)
    by_regime_bucket = defaultdict(list)

    for r in rows:
        by_bucket[r["bucket"]].append(r)
        by_regime_bucket[(r["regime"], r["bucket"])].append(r)

    bucket_mapping = {bucket: group_stats(items) for bucket, items in by_bucket.items()}
    regime_bucket_mapping = {
        f"{regime}|{bucket}": group_stats(items)
        for (regime, bucket), items in by_regime_bucket.items()
    }

    for r in rows:
        bucket_prob = bucket_mapping[r["bucket"]]["calibrated_prob"]
        regime_key = f"{r['regime']}|{r['bucket']}"
        regime_group = regime_bucket_mapping.get(regime_key, {})
        if regime_group.get("rows", 0) >= 100:
            regime_prob = regime_group["calibrated_prob"]
        else:
            regime_prob = bucket_prob

        r["bucket_calibrated_prob"] = bucket_prob
        r["regime_bucket_calibrated_prob"] = regime_prob

    raw_brier = calc_brier(rows, "raw_prob")
    bucket_brier = calc_brier(rows, "bucket_calibrated_prob")
    regime_bucket_brier = calc_brier(rows, "regime_bucket_calibrated_prob")

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_CALIBRATION_MAPPING",
        "source_csv": str(CSV_PATH),
        "rows": len(rows),
        "overall": overall,
        "brier": {
            "raw_score_proxy": round(raw_brier, 6),
            "bucket_calibrated": round(bucket_brier, 6),
            "regime_bucket_calibrated": round(regime_bucket_brier, 6),
            "target": 0.24,
        },
        "score_bucket_mapping": dict(sorted(bucket_mapping.items())),
        "regime_bucket_mapping": dict(sorted(regime_bucket_mapping.items())),
        "recommendation": {
            "use_for_trading": False,
            "paper_only": True,
            "next_step": "If calibrated Brier is still above 0.24, continue with Platt/isotonic calibration or add regime/feature-aware calibration.",
        },
        "verdict": "READ_ONLY_CALIBRATION_MAPPING_COMPLETE",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
