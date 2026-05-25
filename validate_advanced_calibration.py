#!/usr/bin/env python3
"""
Validate advanced score calibration methods using date-based holdout.

Safety:
- Reads data/ml_calibration_matched_20260520.csv only.
- Does not touch SQLite DB.
- Writes logs/advanced_calibration_validation_report.json only.

Methods:
- raw score proxy
- global train winrate
- score bucket mapping
- Platt-style logistic calibration on score
- regime-aware Platt-style logistic calibration
- isotonic-style monotonic calibration on score
"""

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/advanced_calibration_validation_report.json"

TRAIN_START = "2026-05-20"
TRAIN_END = "2026-05-23"      # exclusive
VALID_START = "2026-05-23"
TARGET_BRIER = 0.24


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


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


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


def profit_factor(rows):
    pos = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    neg = sum(r["pnl"] for r in rows if r["pnl"] < 0)
    return (pos / abs(neg)) if neg else None


def summarize(rows, prob_keys):
    out = {
        "rows": len(rows),
        "wins": sum(r["y"] for r in rows),
        "losses": sum(1 - r["y"] for r in rows),
        "winrate_pct": round(winrate(rows) * 100, 4) if rows else 0.0,
        "avg_pnl_pct": round(sum(r["pnl"] for r in rows) / len(rows), 6) if rows else 0.0,
        "profit_factor": round(profit_factor(rows), 6) if profit_factor(rows) is not None else None,
    }
    for key in prob_keys:
        value = brier(rows, key)
        out[f"brier_{key}"] = round(value, 6) if value is not None else None
    return out


def load_rows():
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
                "signal_timestamp": row.get("signal_timestamp"),
                "signal_dt": to_dt(row.get("signal_timestamp")),
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "score_norm": (score - 50.0) / 50.0,
                "bucket": bucket_name(score),
                "raw_prob": clamp(score / 100.0),
                "y": 1 if win_loss == "WIN" else 0,
                "pnl": to_float(row.get("pnl_pct")),
            })
    return rows


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


def fit_logistic(features, labels, lr=0.08, epochs=2500, l2=0.02):
    n = len(labels)
    if n == 0:
        return []

    m = len(features[0])
    weights = [0.0] * m

    for _ in range(epochs):
        grad = [0.0] * m

        for x, y in zip(features, labels):
            z = sum(w * xi for w, xi in zip(weights, x))
            p = sigmoid(z)
            err = p - y

            for j in range(m):
                grad[j] += err * x[j]

        for j in range(m):
            reg = 0.0 if j == 0 else l2 * weights[j]
            weights[j] -= lr * ((grad[j] / n) + reg)

    return weights


def predict_logistic(weights, x):
    return clamp(sigmoid(sum(w * xi for w, xi in zip(weights, x))))


def fit_isotonic(xs, ys):
    pairs = sorted(zip(xs, ys), key=lambda t: t[0])
    blocks = []

    for x, y in pairs:
        blocks.append({
            "x_min": x,
            "x_max": x,
            "sum_y": float(y),
            "weight": 1.0,
            "avg": float(y),
        })

        while len(blocks) >= 2 and blocks[-2]["avg"] > blocks[-1]["avg"]:
            b2 = blocks.pop()
            b1 = blocks.pop()
            merged = {
                "x_min": b1["x_min"],
                "x_max": b2["x_max"],
                "sum_y": b1["sum_y"] + b2["sum_y"],
                "weight": b1["weight"] + b2["weight"],
                "avg": (b1["sum_y"] + b2["sum_y"]) / (b1["weight"] + b2["weight"]),
            }
            blocks.append(merged)

    return blocks


def predict_isotonic(blocks, x):
    if not blocks:
        return 0.5

    if x <= blocks[0]["x_max"]:
        return clamp(blocks[0]["avg"])

    for b in blocks:
        if b["x_min"] <= x <= b["x_max"]:
            return clamp(b["avg"])

    return clamp(blocks[-1]["avg"])


def main():
    rows = load_rows()

    train_start = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    train_end = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    valid_start = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)

    train_rows = [r for r in rows if r["signal_dt"] and train_start <= r["signal_dt"] < train_end]
    valid_rows = [r for r in rows if r["signal_dt"] and r["signal_dt"] >= valid_start]

    global_prob = winrate(train_rows)

    bucket_map = group_mapping(train_rows, lambda r: r["bucket"])
    for r in valid_rows:
        item = bucket_map.get(r["bucket"])
        r["global_prob"] = global_prob
        r["bucket_prob"] = item["prob"] if item else global_prob

    score_features = [[1.0, r["score_norm"]] for r in train_rows]
    labels = [r["y"] for r in train_rows]
    score_weights = fit_logistic(score_features, labels)

    regimes = sorted({r["regime"] for r in train_rows})
    regime_index = {regime: idx for idx, regime in enumerate(regimes)}

    def regime_features(r):
        vec = [1.0, r["score_norm"]]
        one_hot = [0.0] * len(regimes)
        if r["regime"] in regime_index:
            one_hot[regime_index[r["regime"]]] = 1.0
        return vec + one_hot

    regime_weights = fit_logistic([regime_features(r) for r in train_rows], labels, lr=0.05, epochs=2000, l2=0.05)

    isotonic_blocks = fit_isotonic([r["score"] for r in train_rows], labels)

    for r in valid_rows:
        r["platt_score_prob"] = predict_logistic(score_weights, [1.0, r["score_norm"]])
        r["platt_regime_prob"] = predict_logistic(regime_weights, regime_features(r))
        r["isotonic_score_prob"] = predict_isotonic(isotonic_blocks, r["score"])

    prob_keys = [
        "raw_prob",
        "global_prob",
        "bucket_prob",
        "platt_score_prob",
        "platt_regime_prob",
        "isotonic_score_prob",
    ]

    validation_summary = summarize(valid_rows, prob_keys)

    method_results = []
    for key in prob_keys:
        method_results.append({
            "method": key,
            "validation_brier": validation_summary.get(f"brier_{key}"),
            "passes_target": (validation_summary.get(f"brier_{key}") is not None and validation_summary.get(f"brier_{key}") <= TARGET_BRIER),
        })

    method_results.sort(key=lambda x: x["validation_brier"] if x["validation_brier"] is not None else 999)

    best = method_results[0] if method_results else {}

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_ADVANCED_CALIBRATION_VALIDATION",
        "source_csv": str(CSV_PATH),
        "split": {
            "train_start": TRAIN_START,
            "train_end_exclusive": TRAIN_END,
            "valid_start": VALID_START,
        },
        "train_summary": summarize(train_rows, ["raw_prob"]),
        "validation_summary": validation_summary,
        "method_ranking": method_results,
        "best_method": best,
        "models": {
            "platt_score_weights": [round(x, 8) for x in score_weights],
            "platt_regime_weights": [round(x, 8) for x in regime_weights],
            "regime_order": regimes,
            "isotonic_blocks": [
                {
                    "x_min": round(b["x_min"], 6),
                    "x_max": round(b["x_max"], 6),
                    "avg": round(b["avg"], 6),
                    "weight": int(b["weight"]),
                }
                for b in isotonic_blocks
            ],
        },
        "gate_reference": {
            "phase_2c_target_brier": TARGET_BRIER,
            "status": "PASS if best validation brier <= target",
        },
        "recommendation": {
            "paper_only": True,
            "use_for_trading": False,
            "next_step": "If no method passes, do not deploy confidence as probability; proceed to feature-level model calibration or threshold retuning with caution.",
        },
        "verdict": "PASS" if best and best.get("passes_target") else "REVIEW_NEEDS_FEATURE_LEVEL_CALIBRATION",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    compact = {
        "verdict": report["verdict"],
        "split": report["split"],
        "train_rows": len(train_rows),
        "validation_rows": len(valid_rows),
        "method_ranking": method_results,
        "best_method": best,
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
