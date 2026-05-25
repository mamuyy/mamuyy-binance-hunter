#!/usr/bin/env python3
"""
Diagnose why Phase 2C Brier remains above gate despite sufficient data.

Safety:
- READ-ONLY only.
- No DB writes.
- No runtime/execution/broker/scanner/orchestrator/model behavior changes.
- Writes logs/phase2c_brier_failure_diagnosis.json only.
"""

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
PROJECT_DIR = DEFAULT_PROJECT_DIR if DEFAULT_PROJECT_DIR.exists() else Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
HOLDOUT_PATH = PROJECT_DIR / "logs/calibration_holdout_report.json"
ADV_PATH = PROJECT_DIR / "logs/advanced_calibration_validation_report.json"
FEATURE_PATH = PROJECT_DIR / "logs/feature_level_calibration_report.json"
SUFF_PATH = PROJECT_DIR / "logs/phase2c_data_sufficiency_report.json"
OUT_PATH = PROJECT_DIR / "logs/phase2c_brier_failure_diagnosis.json"

TRAIN_START = "2026-05-20"
TRAIN_END = "2026-05-23"
VALID_START = "2026-05-23"
GATE = 0.24

REGIMES_FOCUS = ["RISK OFF", "SIDEWAYS / CHOPPY", "TRENDING BULL"]
BUCKETS = ["00-19", "20-39", "40-59", "60-79", "80-100"]


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


def clamp(v, low=0.01, high=0.99):
    return max(low, min(high, v))


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


def brier(rows, key):
    if not rows:
        return None
    return sum((r.get(key, 0.5) - r["y"]) ** 2 for r in rows) / len(rows)


def maybe_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def fit_logistic(features, labels, lr=0.04, epochs=2200, l2=0.06):
    n = len(labels)
    m = len(features[0])
    w = [0.0] * m
    for _ in range(epochs):
        grad = [0.0] * m
        for x, y in zip(features, labels):
            z = sum(wi * xi for wi, xi in zip(w, x))
            p = sigmoid(z)
            err = p - y
            for j in range(m):
                grad[j] += err * x[j]
        for j in range(m):
            reg = 0.0 if j == 0 else l2 * w[j]
            w[j] -= lr * ((grad[j] / n) + reg)
    return w


def predict(w, x):
    return clamp(sigmoid(sum(wi * xi for wi, xi in zip(w, x))))


def brier_decompose(rows, key, bins=10):
    if not rows:
        return None
    n = len(rows)
    ybar = sum(r["y"] for r in rows) / n
    uncertainty = ybar * (1 - ybar)
    groups = defaultdict(list)
    for r in rows:
        p = r.get(key, 0.5)
        idx = min(bins - 1, max(0, int(p * bins)))
        groups[idx].append(r)
    reliability = 0.0
    resolution = 0.0
    for g in groups.values():
        ng = len(g)
        pbar = sum(x.get(key, 0.5) for x in g) / ng
        yb = sum(x["y"] for x in g) / ng
        weight = ng / n
        reliability += weight * ((pbar - yb) ** 2)
        resolution += weight * ((yb - ybar) ** 2)
    approx = uncertainty + reliability - resolution
    return {
        "uncertainty": round(uncertainty, 6),
        "reliability": round(reliability, 6),
        "resolution": round(resolution, 6),
        "brier_approx": round(approx, 6),
        "brier_actual": round(brier(rows, key), 6),
    }


def psi_like(train_counts, valid_counts, labels):
    t_total = sum(train_counts.get(k, 0) for k in labels)
    v_total = sum(valid_counts.get(k, 0) for k in labels)
    if t_total == 0 or v_total == 0:
        return None
    val = 0.0
    for k in labels:
        t = max(train_counts.get(k, 0) / t_total, 1e-6)
        v = max(valid_counts.get(k, 0) / v_total, 1e-6)
        val += (v - t) * math.log(v / t)
    return round(val, 6)


def load_rows():
    if not CSV_PATH.exists():
        return []
    rows = []
    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wl = row.get("win_loss") or ""
            if wl not in ("WIN", "LOSS"):
                continue
            score = to_float(row.get("score"))
            entry = to_float(row.get("entry"))
            sl = to_float(row.get("sl"))
            tp1 = to_float(row.get("tp1"))
            tp2 = to_float(row.get("tp2"))
            regime_score = to_float(row.get("matched_regime_score"))
            delta = to_float(row.get("regime_match_delta_seconds"))
            holding = to_float(row.get("holding_candles"))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            rr1 = tp1_dist / sl_dist if sl_dist else 0.0
            rr2 = tp2_dist / sl_dist if sl_dist else 0.0
            rows.append({
                "signal_dt": to_dt(row.get("signal_timestamp")),
                "regime": row.get("matched_regime") or "UNKNOWN",
                "score": score,
                "bucket": bucket_name(score),
                "score_norm": (score - 50.0) / 50.0,
                "regime_score_norm": (regime_score - 50.0) / 50.0,
                "delta_norm": min(delta, 1800.0) / 1800.0,
                "holding_norm": holding / 20.0 if holding else 1.0,
                "sl_dist": sl_dist,
                "tp1_dist": tp1_dist,
                "tp2_dist": tp2_dist,
                "rr1": rr1,
                "rr2": rr2,
                "raw_prob": clamp(score / 100.0),
                "y": 1 if wl == "WIN" else 0,
            })
    return rows


def assign_methods(train, valid):
    global_prob = sum(r["y"] for r in train) / len(train)
    for r in valid:
        r["global_prob"] = global_prob

    by_bucket = defaultdict(list)
    by_regime_bucket = defaultdict(list)
    for r in train:
        by_bucket[r["bucket"]].append(r)
        by_regime_bucket[f"{r['regime']}|{r['bucket']}"] .append(r)

    bucket_prob = {k: sum(x["y"] for x in v) / len(v) for k, v in by_bucket.items()}
    regime_bucket_prob = {}
    for k, v in by_regime_bucket.items():
        if len(v) >= 100:
            regime_bucket_prob[k] = sum(x["y"] for x in v) / len(v)

    for r in valid:
        bp = bucket_prob.get(r["bucket"], global_prob)
        rbk = f"{r['regime']}|{r['bucket']}"
        r["bucket_prob"] = bp
        r["regime_bucket_prob"] = regime_bucket_prob.get(rbk, bp)

    labels = [r["y"] for r in train]
    w_platt = fit_logistic([[1.0, r["score_norm"]] for r in train], labels, lr=0.08, epochs=2500, l2=0.02)
    for r in valid:
        r["platt_score_prob"] = predict(w_platt, [1.0, r["score_norm"]])

    regimes = sorted({r["regime"] for r in train})
    rix = {k: i for i, k in enumerate(regimes)}

    def core_feat(r):
        return [1.0, r["score_norm"], r["regime_score_norm"], r["delta_norm"], r["holding_norm"], r["sl_dist"] * 100.0, r["tp1_dist"] * 100.0, r["tp2_dist"] * 100.0, r["rr1"], r["rr2"]]

    def full_feat(r):
        onehot = [0.0] * len(regimes)
        if r["regime"] in rix:
            onehot[rix[r["regime"]]] = 1.0
        return core_feat(r) + onehot

    w_core = fit_logistic([core_feat(r) for r in train], labels, lr=0.04, epochs=2200, l2=0.06)
    w_full = fit_logistic([full_feat(r) for r in train], labels, lr=0.04, epochs=2200, l2=0.06)
    for r in valid:
        r["core_features_prob"] = predict(w_core, core_feat(r))
        r["full_features_prob"] = predict(w_full, full_feat(r))


def mean_std(vals):
    if not vals:
        return {"mean": None, "std": None}
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / len(vals)
    return {"mean": round(m, 6), "std": round(math.sqrt(var), 6)}


def main():
    rows = load_rows()
    ts = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    te = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    vs = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)
    train = [r for r in rows if r["signal_dt"] and ts <= r["signal_dt"] < te]
    valid = [r for r in rows if r["signal_dt"] and r["signal_dt"] >= vs]
    if train and valid:
        assign_methods(train, valid)

    if not train or not valid:
        report = {
            "build_time_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "READ_ONLY_PHASE_2C_BRIER_FAILURE_DIAGNOSIS",
            "paper_only": True,
            "inputs": {
                "csv": str(CSV_PATH),
                "holdout_report_present": HOLDOUT_PATH.exists(),
                "advanced_report_present": ADV_PATH.exists(),
                "feature_report_present": FEATURE_PATH.exists(),
                "sufficiency_report_present": SUFF_PATH.exists(),
            },
            "sample_counts": {"train_rows": len(train), "validation_rows": len(valid)},
            "error": "insufficient_or_missing_calibration_csv_data",
            "next_read_only_experiment": "B) rolling/expanding time split instead of fixed split",
            "safety": {
                "db_write": False,
                "execution_change": False,
                "production_scoring_change": False,
                "phase_3": False,
                "real_execution": "blocked",
            },
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("===== PHASE 2C BRIER FAILURE DIAGNOSIS (READ-ONLY) =====")
        print(f"Train rows                      : {len(train)}")
        print(f"Validation rows                 : {len(valid)}")
        print("Status                          : insufficient_or_missing_calibration_csv_data")
        print(f"Report                          : {OUT_PATH}")
        return

    methods = ["raw_prob", "global_prob", "bucket_prob", "platt_score_prob", "regime_bucket_prob", "core_features_prob", "full_features_prob"]
    bri = {m: round(brier(valid, m), 6) for m in methods}
    base_g = bri["global_prob"]
    base_r = bri["raw_prob"]

    compare = {}
    for m in methods:
        compare[m] = {
            "brier": bri[m],
            "improvement_vs_global_prob": round(base_g - bri[m], 6),
            "improvement_vs_raw_prob": round(base_r - bri[m], 6),
            "gap_to_target_0_24": round(bri[m] - GATE, 6),
        }

    decomp = {m: brier_decompose(valid, m) for m in methods if m != "regime_bucket_prob"}

    regime_brier = {}
    for reg in REGIMES_FOCUS:
        sub = [r for r in valid if r["regime"] == reg]
        regime_brier[reg] = {"rows": len(sub), **{m: (round(brier(sub, m), 6) if sub else None) for m in methods}}

    bucket_brier = {}
    for b in BUCKETS:
        sub = [r for r in valid if r["bucket"] == b]
        bucket_brier[b] = {"rows": len(sub), **{m: (round(brier(sub, m), 6) if sub else None) for m in methods}}

    regime_bucket = {}
    rg = defaultdict(list)
    for r in valid:
        rg[f"{r['regime']}|{r['bucket']}"] .append(r)
    for k, sub in sorted(rg.items()):
        regime_bucket[k] = {"rows": len(sub), "raw_prob": round(brier(sub, "raw_prob"), 6), "core_features_prob": round(brier(sub, "core_features_prob"), 6)}

    train_win = sum(r["y"] for r in train) / len(train) if train else 0.0
    valid_win = sum(r["y"] for r in valid) / len(valid) if valid else 0.0
    train_bucket_dist = Counter(r["bucket"] for r in train)
    valid_bucket_dist = Counter(r["bucket"] for r in valid)
    train_reg_dist = Counter(r["regime"] for r in train)
    valid_reg_dist = Counter(r["regime"] for r in valid)

    features = ["score_norm", "regime_score_norm", "delta_norm", "holding_norm", "sl_dist", "tp1_dist", "tp2_dist", "rr1", "rr2"]
    sep = []
    wins = [r for r in valid if r["y"] == 1]
    losses = [r for r in valid if r["y"] == 0]
    for f in features:
        wv = [r[f] for r in wins]
        lv = [r[f] for r in losses]
        wm = sum(wv) / len(wv) if wv else 0.0
        lm = sum(lv) / len(lv) if lv else 0.0
        ws = math.sqrt(sum((x - wm) ** 2 for x in wv) / len(wv)) if wv else 0.0
        ls = math.sqrt(sum((x - lm) ** 2 for x in lv) / len(lv)) if lv else 0.0
        pooled = math.sqrt((ws ** 2 + ls ** 2) / 2.0) if (ws or ls) else 0.0
        effect = abs(wm - lm) / pooled if pooled else 0.0
        sep.append({"feature": f, "win": mean_std(wv), "loss": mean_std(lv), "effect_size": round(effect, 6)})
    sep.sort(key=lambda x: x["effect_size"], reverse=True)

    findings = []
    if compare["core_features_prob"]["gap_to_target_0_24"] > 0.007:
        findings.append("weak_score_separation")
    if decomp["core_features_prob"]["reliability"] > decomp["core_features_prob"]["resolution"]:
        findings.append("poor_calibration_reliability")
    if decomp["core_features_prob"]["resolution"] < 0.01:
        findings.append("low_resolution")
    if abs(valid_win - train_win) > 0.03:
        findings.append("regime_or_time_drift")
    sparse_high = [k for k, v in regime_bucket.items() if k.endswith("|80-100") and v["rows"] < 20]
    if sparse_high:
        findings.append("sparse_high_score_regime_buckets")
    if sep and sep[0]["effect_size"] < 0.15:
        findings.append("noisy_or_non_predictive_features")

    primary = "B) rolling/expanding time split instead of fixed split"
    if "noisy_or_non_predictive_features" in findings:
        primary = "C) feature engineering audit"
    elif "sparse_high_score_regime_buckets" in findings:
        primary = "D) exclude sparse unstable buckets"

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PHASE_2C_BRIER_FAILURE_DIAGNOSIS",
        "paper_only": True,
        "inputs": {
            "csv": str(CSV_PATH),
            "holdout_report_present": HOLDOUT_PATH.exists(),
            "advanced_report_present": ADV_PATH.exists(),
            "feature_report_present": FEATURE_PATH.exists(),
            "sufficiency_report_present": SUFF_PATH.exists(),
        },
        "sample_counts": {"train_rows": len(train), "validation_rows": len(valid)},
        "brier_by_method": bri,
        "brier_decomposition": decomp,
        "method_comparison": compare,
        "per_regime_brier": regime_brier,
        "per_score_bucket_brier": bucket_brier,
        "per_regime_score_bucket_brier": regime_bucket,
        "distribution_drift": {
            "train_winrate": round(train_win, 6),
            "validation_winrate": round(valid_win, 6),
            "winrate_delta": round(valid_win - train_win, 6),
            "train_bucket_distribution": dict(sorted(train_bucket_dist.items())),
            "validation_bucket_distribution": dict(sorted(valid_bucket_dist.items())),
            "train_regime_distribution": dict(sorted(train_reg_dist.items())),
            "validation_regime_distribution": dict(sorted(valid_reg_dist.items())),
            "bucket_psi_like": psi_like(train_bucket_dist, valid_bucket_dist, BUCKETS),
            "regime_psi_like": psi_like(train_reg_dist, valid_reg_dist, sorted(set(train_reg_dist) | set(valid_reg_dist))),
        },
        "feature_separation": sep,
        "diagnosed_primary_causes": findings,
        "next_read_only_experiment": primary,
        "safety": {
            "db_write": False,
            "execution_change": False,
            "production_scoring_change": False,
            "phase_3": False,
            "real_execution": "blocked",
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    best_m = min(compare.items(), key=lambda kv: kv[1]["brier"])
    print("===== PHASE 2C BRIER FAILURE DIAGNOSIS (READ-ONLY) =====")
    print(f"Train rows                      : {len(train)}")
    print(f"Validation rows                 : {len(valid)}")
    print(f"Best method                     : {best_m[0]} ({best_m[1]['brier']})")
    print(f"Gap to 0.24                     : {best_m[1]['gap_to_target_0_24']}")
    print(f"Primary diagnosed causes        : {', '.join(findings) if findings else 'none'}")
    print(f"Recommended read-only experiment: {primary}")
    print(f"Report                          : {OUT_PATH}")


if __name__ == "__main__":
    main()
