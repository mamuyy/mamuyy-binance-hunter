#!/usr/bin/env python3
"""
Validate calibration mapping using date-based holdout.

Safety:
- Reads data/ml_calibration_matched_20260520.csv only.
- Does not touch SQLite DB.
- Writes logs/calibration_holdout_report.json only.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/calibration_holdout_report.json"

TRAIN_START = "2026-05-20"
TRAIN_END = "2026-05-23"      # exclusive
VALID_START = "2026-05-23"


def to_dt(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def brier(rows, prob_key):
    if not rows:
        return None
    return sum((r[prob_key] - r["y"]) ** 2 for r in rows) / len(rows)


def winrate(rows):
    if not rows:
        return 0.0
    return sum(r["y"] for r in rows) / len(rows)


def group_mapping(rows, key_fn, min_rows=1):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)

    mapping = {}
    for key, items in groups.items():
        if len(items) >= min_rows:
            mapping[key] = {
                "rows": len(items),
                "prob": winrate(items),
            }
    return mapping


def summarize(rows, prob_keys):
    out = {
        "rows": len(rows),
        "wins": sum(r["y"] for r in rows),
        "losses": sum(1 - r["y"] for r in rows),
        "winrate_pct": round(winrate(rows) * 100, 4) if rows else 0.0,
    }

    for key in prob_keys:
        value = brier(rows, key)
        out[f"brier_{key}"] = round(value, 6) if value is not None else None

    return out


def main():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    train_start = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    train_end = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    valid_start = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)

    rows = []

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            win_loss = row.get("win_loss") or ""
            if win_loss not in ("WIN", "LOSS"):
                continue

            signal_dt = to_dt(row.get("signal_timestamp"))
            score = to_float(row.get("score"))

            rows.append({
                "signal_timestamp": row.get("signal_timestamp"),
                "signal_dt": signal_dt,
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "bucket": bucket_name(score),
                "raw_prob": clamp(score / 100.0),
                "y": 1 if win_loss == "WIN" else 0,
                "pnl": to_float(row.get("pnl_pct")),
            })

    train_rows = [r for r in rows if r["signal_dt"] and train_start <= r["signal_dt"] < train_end]
    valid_rows = [r for r in rows if r["signal_dt"] and r["signal_dt"] >= valid_start]

    global_prob = winrate(train_rows)

    bucket_map = group_mapping(train_rows, lambda r: r["bucket"])
    regime_bucket_map = group_mapping(
        train_rows,
        lambda r: f"{r['regime']}|{r['bucket']}",
        min_rows=100,
    )

    for r in valid_rows:
        bucket_item = bucket_map.get(r["bucket"])
        bucket_prob = bucket_item["prob"] if bucket_item else global_prob

        rb_key = f"{r['regime']}|{r['bucket']}"
        rb_item = regime_bucket_map.get(rb_key)
        regime_bucket_prob = rb_item["prob"] if rb_item else bucket_prob

        r["bucket_prob"] = bucket_prob
        r["regime_bucket_prob"] = regime_bucket_prob
        r["global_prob"] = global_prob

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_HOLDOUT_CALIBRATION_VALIDATION",
        "source_csv": str(CSV_PATH),
        "split": {
            "train_start": TRAIN_START,
            "train_end_exclusive": TRAIN_END,
            "valid_start": VALID_START,
        },
        "train_summary": summarize(train_rows, ["raw_prob"]),
        "validation_summary": summarize(
            valid_rows,
            ["raw_prob", "global_prob", "bucket_prob", "regime_bucket_prob"],
        ),
        "train_global_prob": round(global_prob, 6),
        "bucket_mapping_from_train": {
            k: {"rows": v["rows"], "prob": round(v["prob"], 6)}
            for k, v in sorted(bucket_map.items())
        },
        "regime_bucket_mapping_from_train": {
            k: {"rows": v["rows"], "prob": round(v["prob"], 6)}
            for k, v in sorted(regime_bucket_map.items())
        },
        "gate_reference": {
            "phase_2c_target_brier": 0.24,
            "status": "PASS if validation brier <= 0.24",
        },
        "recommendation": {
            "paper_only": True,
            "use_for_trading": False,
        },
        "verdict": "READ_ONLY_HOLDOUT_CALIBRATION_VALIDATION_COMPLETE",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
