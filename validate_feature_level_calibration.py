#!/usr/bin/env python3
"""
Validate feature-level calibration using date-based holdout.

Safety:
- Reads data/ml_calibration_matched_20260520.csv only.
- Does not touch SQLite DB.
- Writes logs/feature_level_calibration_report.json only.

Goal:
- Check whether feature-level calibration can beat score-only calibration.
- Phase 2C target: validation Brier <= 0.24.
"""

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/feature_level_calibration_report.json"

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


def brier(rows, prob_key):
    if not rows:
        return None
    return sum((r[prob_key] - r["y"]) ** 2 for r in rows) / len(rows)


def winrate(rows):
    if not rows:
        return 0.0
    return sum(r["y"] for r in rows) / len(rows)


def profit_factor(rows):
    pos = sum(r["pnl_pct"] for r in rows if r["pnl_pct"] > 0)
    neg = sum(r["pnl_pct"] for r in rows if r["pnl_pct"] < 0)
    return (pos / abs(neg)) if neg else None


def safe_pct_distance(a, b):
    if a == 0:
        return 0.0
    return (b - a) / a


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

            entry = to_float(row.get("entry"))
            sl = to_float(row.get("sl"))
            tp1 = to_float(row.get("tp1"))
            tp2 = to_float(row.get("tp2"))
            score = to_float(row.get("score"))
            regime_score = to_float(row.get("matched_regime_score"))
            delta = to_float(row.get("regime_match_delta_seconds"))
            holding = to_float(row.get("holding_candles"))
            pnl = to_float(row.get("pnl_pct"))

            sl_dist = abs(safe_pct_distance(entry, sl))
            tp1_dist = abs(safe_pct_distance(entry, tp1))
            tp2_dist = abs(safe_pct_distance(entry, tp2))
            rr1 = tp1_dist / sl_dist if sl_dist else 0.0
            rr2 = tp2_dist / sl_dist if sl_dist else 0.0

            rows.append({
                "signal_timestamp": row.get("signal_timestamp"),
                "signal_dt": to_dt(row.get("signal_timestamp")),
                "symbol": row.get("symbol") or "UNKNOWN",
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "score_norm": (score - 50.0) / 50.0,
                "regime_score": regime_score,
                "regime_score_norm": (regime_score - 50.0) / 50.0,
                "delta_norm": min(delta, 1800.0) / 1800.0,
                "holding_norm": holding / 20.0 if holding else 1.0,
                "sl_dist": sl_dist,
                "tp1_dist": tp1_dist,
                "tp2_dist": tp2_dist,
                "rr1": rr1,
                "rr2": rr2,
                "raw_prob": clamp(score / 100.0),
                "y": 1 if win_loss == "WIN" else 0,
                "pnl_pct": pnl,
            })

    return rows


def fit_logistic(features, labels, lr=0.04, epochs=2200, l2=0.06):
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


def predict(weights, x):
    return clamp(sigmoid(sum(w * xi for w, xi in zip(weights, x))))


def summarize(rows, prob_keys):
    out = {
        "rows": len(rows),
        "wins": sum(r["y"] for r in rows),
        "losses": sum(1 - r["y"] for r in rows),
        "winrate_pct": round(winrate(rows) * 100, 4) if rows else 0.0,
        "avg_pnl_pct": round(sum(r["pnl_pct"] for r in rows) / len(rows), 6) if rows else 0.0,
        "profit_factor": round(profit_factor(rows), 6) if profit_factor(rows) is not None else None,
    }
    for key in prob_keys:
        value = brier(rows, key)
        out[f"brier_{key}"] = round(value, 6) if value is not None else None
    return out


def main():
    rows = load_rows()

    train_start = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    train_end = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    valid_start = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)

    train_rows = [r for r in rows if r["signal_dt"] and train_start <= r["signal_dt"] < train_end]
    valid_rows = [r for r in rows if r["signal_dt"] and r["signal_dt"] >= valid_start]

    symbol_counts = Counter(r["symbol"] for r in train_rows)
    top_symbols = [s for s, c in symbol_counts.most_common(12)]
    regimes = sorted({r["regime"] for r in train_rows})

    symbol_index = {s: i for i, s in enumerate(top_symbols)}
    regime_index = {r: i for i, r in enumerate(regimes)}

    def features_score_only(r):
        return [1.0, r["score_norm"]]

    def features_core(r):
        return [
            1.0,
            r["score_norm"],
            r["regime_score_norm"],
            r["delta_norm"],
            r["holding_norm"],
            r["sl_dist"] * 100.0,
            r["tp1_dist"] * 100.0,
            r["tp2_dist"] * 100.0,
            r["rr1"],
            r["rr2"],
        ]

    def features_full(r):
        base = features_core(r)

        reg = [0.0] * len(regimes)
        if r["regime"] in regime_index:
            reg[regime_index[r["regime"]]] = 1.0

        sym = [0.0] * len(top_symbols)
        if r["symbol"] in symbol_index:
            sym[symbol_index[r["symbol"]]] = 1.0

        return base + reg + sym

    labels = [r["y"] for r in train_rows]

    candidates = {
        "score_only": (features_score_only, 0.05, 1800, 0.04),
        "core_features": (features_core, 0.035, 2200, 0.08),
        "full_features": (features_full, 0.025, 2400, 0.12),
    }

    method_results = []

    global_prob = winrate(train_rows)
    for r in valid_rows:
        r["global_prob"] = global_prob

    for name, (fn, lr, epochs, l2) in candidates.items():
        weights = fit_logistic([fn(r) for r in train_rows], labels, lr=lr, epochs=epochs, l2=l2)

        for r in valid_rows:
            r[f"{name}_prob"] = predict(weights, fn(r))

        method_results.append({
            "method": name,
            "validation_brier": round(brier(valid_rows, f"{name}_prob"), 6),
            "passes_target": brier(valid_rows, f"{name}_prob") <= TARGET_BRIER,
            "weights_count": len(weights),
        })

    method_results.append({
        "method": "global_prob",
        "validation_brier": round(brier(valid_rows, "global_prob"), 6),
        "passes_target": brier(valid_rows, "global_prob") <= TARGET_BRIER,
        "weights_count": 0,
    })

    method_results.append({
        "method": "raw_prob",
        "validation_brier": round(brier(valid_rows, "raw_prob"), 6),
        "passes_target": brier(valid_rows, "raw_prob") <= TARGET_BRIER,
        "weights_count": 0,
    })

    method_results.sort(key=lambda x: x["validation_brier"])
    best = method_results[0]

    prob_keys = ["raw_prob", "global_prob"] + [f"{name}_prob" for name in candidates.keys()]

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_FEATURE_LEVEL_CALIBRATION_VALIDATION",
        "source_csv": str(CSV_PATH),
        "split": {
            "train_start": TRAIN_START,
            "train_end_exclusive": TRAIN_END,
            "valid_start": VALID_START,
        },
        "train_summary": summarize(train_rows, ["raw_prob"]),
        "validation_summary": summarize(valid_rows, prob_keys),
        "method_ranking": method_results,
        "best_method": best,
        "feature_sets": {
            "regimes": regimes,
            "top_symbols": top_symbols,
            "core_features": [
                "score_norm",
                "regime_score_norm",
                "regime_match_delta_norm",
                "holding_norm",
                "sl_dist_pct",
                "tp1_dist_pct",
                "tp2_dist_pct",
                "rr1",
                "rr2",
            ],
        },
        "gate_reference": {
            "phase_2c_target_brier": TARGET_BRIER,
            "status": "PASS if best validation brier <= target",
        },
        "recommendation": {
            "paper_only": True,
            "use_for_trading": False,
            "next_step": "If still above target, mark Phase 2C as REVIEW and proceed to Phase 2B/class imbalance or collect more paper-trade outcomes before attempting model-level recalibration.",
        },
        "verdict": "PASS" if best["passes_target"] else "REVIEW_PHASE_2C_NOT_PASSED",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "verdict": report["verdict"],
        "train_rows": len(train_rows),
        "validation_rows": len(valid_rows),
        "best_method": best,
        "method_ranking": method_results,
        "gate_reference": report["gate_reference"],
    }, indent=2))


if __name__ == "__main__":
    main()
