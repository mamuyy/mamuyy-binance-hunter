#!/usr/bin/env python3
"""Read-only Phase 4 nonlinear model architecture exploration on fixed Phase 2C split."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data/ml_calibration_matched_20260520.csv"
OUT_PATH = ROOT / "logs/phase4_nonlinear_model_full_report.json"
PHASE2C_EXPLORATION_LOG = ROOT / "logs/phase2c_nonlinear_model_exploration_report.json"
PHASE2C_EVIDENCE_LOG = ROOT / "logs/phase2c_evidence_synthesis_report.json"

TRAIN_START = datetime.fromisoformat("2026-05-20").replace(tzinfo=timezone.utc)
TRAIN_END = datetime.fromisoformat("2026-05-23").replace(tzinfo=timezone.utc)
VALID_START = datetime.fromisoformat("2026-05-23").replace(tzinfo=timezone.utc)
PHASE2C_REFERENCE_BEST = 0.245729
TARGET_BRIER = 0.24


def to_dt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(v):
    try:
        return None if v in (None, "") else float(v)
    except Exception:
        return None


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def brier(preds, labels):
    return sum((p - y) ** 2 for p, y in zip(preds, labels)) / len(labels) if labels else None


def auc_score(preds, labels):
    if not labels or len(set(labels)) < 2:
        return None
    pairs = sorted(zip(preds, labels), key=lambda x: x[0])
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum_pos = 0.0
    i = 0
    rank = 1
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (rank + (rank + (j - i) - 1)) / 2.0
        pos_count = sum(y for _, y in pairs[i:j])
        rank_sum_pos += avg_rank * pos_count
        rank += j - i
        i = j
    auc = (rank_sum_pos - (n_pos * (n_pos + 1) / 2.0)) / (n_pos * n_neg)
    return max(0.0, min(1.0, auc))


def ece_score(preds, labels, bins=10):
    if not labels:
        return None
    n = len(labels)
    total = 0.0
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        bucket = [k for k, p in enumerate(preds) if (lo <= p < hi) or (i == bins - 1 and p == 1.0)]
        if not bucket:
            continue
        conf = sum(preds[k] for k in bucket) / len(bucket)
        acc = sum(labels[k] for k in bucket) / len(bucket)
        total += (len(bucket) / n) * abs(acc - conf)
    return total


def pred_stats(values):
    if not values:
        return {"min": None, "max": None, "mean": None, "std": None}
    m = sum(values) / len(values)
    v = sum((x - m) ** 2 for x in values) / len(values)
    return {"min": round(min(values), 6), "max": round(max(values), 6), "mean": round(m, 6), "std": round(math.sqrt(v), 6)}


def read_optional_json(path):
    if not path.exists():
        return {"exists": False}
    try:
        return {"exists": True, "payload": json.loads(path.read_text(encoding="utf-8"))}
    except Exception as exc:
        return {"exists": True, "parse_error": str(exc)}


def load_rows():
    if not CSV_PATH.exists():
        return [], []
    rows, prev = [], None
    with CSV_PATH.open("r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        fields = list(rd.fieldnames or [])
        for raw in rd:
            wl = (raw.get("win_loss") or "").upper()
            if wl not in ("WIN", "LOSS"):
                continue
            dt = to_dt(raw.get("signal_timestamp"))
            if not dt:
                continue
            r = {"dt": dt, "y": 1 if wl == "WIN" else 0}
            for k in fields:
                v = to_float(raw.get(k))
                if v is not None:
                    r[k] = v

            entry = r.get("entry", 0.0)
            sl = r.get("sl", 0.0)
            tp1 = r.get("tp1", 0.0)
            tp2 = r.get("tp2", 0.0)
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0

            r["score_norm"] = (r.get("score", 50.0) - 50.0) / 50.0
            r["regime_score_norm"] = (r.get("matched_regime_score", 50.0) - 50.0) / 50.0
            r["delta_norm"] = min(r.get("regime_match_delta_seconds", 0.0), 1800.0) / 1800.0
            r["holding_norm"] = r.get("holding_candles", 0.0) / 20.0
            r["sl_dist"] = sl_dist
            r["tp1_dist"] = tp1_dist
            r["tp2_dist"] = tp2_dist
            r["rr1"] = tp1_dist / sl_dist if sl_dist else 0.0
            r["rr2"] = tp2_dist / sl_dist if sl_dist else 0.0
            r["sl_tp_ratio"] = sl_dist / (tp1_dist + 1e-9)
            r["tp_skew"] = tp2_dist - tp1_dist

            if prev is None:
                r["cand_score_mom"] = 0.0
                r["cand_regime_score_mom"] = 0.0
                r["cand_rolling_return"] = 0.0
                r["cand_trend_slope"] = 0.0
            else:
                r["cand_score_mom"] = r.get("score", 0.0) - prev.get("score", 0.0)
                r["cand_regime_score_mom"] = r.get("matched_regime_score", 0.0) - prev.get("matched_regime_score", 0.0)
                pe = prev.get("entry", 0.0)
                r["cand_rolling_return"] = ((entry - pe) / pe) if pe else 0.0
                r["cand_trend_slope"] = r["cand_rolling_return"]
            r["cand_atr_like"] = (sl_dist + tp1_dist + tp2_dist) / 3.0

            rows.append(r)
            prev = r
    rows = sorted(rows, key=lambda x: x["dt"])
    return rows, fields


def leakage_safe_numeric_fields(fields):
    bad = (
        "win", "loss", "outcome", "label", "target", "pnl", "profit", "return", "drawdown",
        "exit", "close", "filled", "realized", "post", "future", "result"
    )
    allow = {"entry", "sl", "tp1", "tp2", "score", "matched_regime_score", "regime_match_delta_seconds", "holding_candles"}
    out = []
    for f in fields:
        fl = f.lower()
        if f in ("signal_timestamp", "win_loss"):
            continue
        if any(b in fl for b in bad) and f not in allow:
            continue
        out.append(f)
    return sorted(set(out))


def build_xy(rows, features):
    x = [[float(r.get(k, 0.0)) for k in features] for r in rows]
    y = [r["y"] for r in rows]
    return x, y


def fit_logistic(x, y, lr=0.05, epochs=1200, l2=0.05):
    if not x:
        return []
    m = len(x[0])
    w = [0.0] * (m + 1)
    n = len(y)
    for _ in range(epochs):
        g = [0.0] * (m + 1)
        for row, yy in zip(x, y):
            z = w[0] + sum(w[j + 1] * row[j] for j in range(m))
            p = sigmoid(z)
            e = p - yy
            g[0] += e
            for j in range(m):
                g[j + 1] += e * row[j]
        w[0] -= lr * (g[0] / n)
        for j in range(1, m + 1):
            w[j] -= lr * ((g[j] / n) + l2 * w[j])
    return w


def predict_logistic(w, x):
    if not x:
        return []
    m = len(x[0])
    out = []
    for row in x:
        z = w[0] + sum(w[j + 1] * row[j] for j in range(m))
        out.append(min(0.99, max(0.01, sigmoid(z))))
    return out


def fit_stump(x, y):
    if not x or not x[0]:
        return None
    best = None
    for j in range(len(x[0])):
        vals = sorted(set(row[j] for row in x))
        thresholds = vals if len(vals) == 1 else [(vals[i] + vals[i + 1]) / 2.0 for i in range(len(vals) - 1)]
        for t in thresholds[:64]:
            left = [i for i, row in enumerate(x) if row[j] <= t]
            right = [i for i, row in enumerate(x) if row[j] > t]
            lp = (sum(y[i] for i in left) / len(left)) if left else 0.5
            rp = (sum(y[i] for i in right) / len(right)) if right else 0.5
            preds = [lp if row[j] <= t else rp for row in x]
            b = brier(preds, y)
            if best is None or b < best["brier"]:
                best = {"feature_idx": j, "threshold": t, "left_p": min(0.99, max(0.01, lp)), "right_p": min(0.99, max(0.01, rp)), "brier": b}
    return best


def predict_stump(stump, x):
    if not stump:
        return [0.5 for _ in x]
    j = stump["feature_idx"]
    t = stump["threshold"]
    return [stump["left_p"] if row[j] <= t else stump["right_p"] for row in x]


def summarize(model_name, feature_set, train_rows, valid_rows, y_tr, p_tr, y_va, p_va, feature_importance=None):
    b_tr = brier(p_tr, y_tr)
    b_va = brier(p_va, y_va)
    auc = auc_score(p_va, y_va)
    ece = ece_score(p_va, y_va, bins=10)
    st = pred_stats(p_va)
    return {
        "model_name": model_name,
        "feature_set": feature_set,
        "train_rows": len(train_rows),
        "validation_rows": len(valid_rows),
        "validation_brier": round(b_va, 6),
        "train_brier": round(b_tr, 6),
        "AUC": None if auc is None else round(auc, 6),
        "ECE": None if ece is None else round(ece, 6),
        "improvement_vs_best_phase2c_0_245729": round(PHASE2C_REFERENCE_BEST - b_va, 6),
        "gap_to_0_24": round(b_va - TARGET_BRIER, 6),
        "passes_target": b_va <= TARGET_BRIER,
        "overfit_flag": (b_va - b_tr) > 0.015,
        "saturation_flag": bool((st["max"] is not None and st["max"] >= 0.99) or (st["min"] is not None and st["min"] <= 0.01) or (st["std"] is not None and st["std"] < 0.03)),
        "prediction_min": st["min"],
        "prediction_max": st["max"],
        "prediction_mean": st["mean"],
        "prediction_std": st["std"],
        "feature_importance": feature_importance,
    }


def main():
    rows, fields = load_rows()
    train = [r for r in rows if TRAIN_START <= r["dt"] < TRAIN_END]
    valid = [r for r in rows if r["dt"] >= VALID_START]

    sklearn_ok = False
    xgboost_ok = False
    sklearn_models = []
    xgb_ctor = None

    try:
        from sklearn.ensemble import GradientBoostingClassifier, HistGradientBoostingClassifier, RandomForestClassifier
        sklearn_ok = True
        sklearn_models = [
            ("RandomForestClassifier", lambda: RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=8, random_state=42)),
            ("GradientBoostingClassifier", lambda: GradientBoostingClassifier(random_state=42)),
            ("HistGradientBoostingClassifier", lambda: HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, random_state=42)),
        ]
    except Exception:
        pass

    try:
        import xgboost as xgb
        xgboost_ok = True
        xgb_ctor = lambda: xgb.XGBClassifier(
            n_estimators=250,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.9,
            colsample_bytree=0.9,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
        )
    except Exception:
        pass

    report = {
        "mode": "READ_ONLY_PHASE4_NONLINEAR_MODEL_FULL_EXPLORATION",
        "phase2c_reference_best_brier": PHASE2C_REFERENCE_BEST,
        "target_brier": TARGET_BRIER,
        "dependency_status": {"sklearn_available": sklearn_ok, "xgboost_available": xgboost_ok},
        "source_context": {
            "phase2c_nonlinear_model_exploration_report": read_optional_json(PHASE2C_EXPLORATION_LOG),
            "phase2c_evidence_synthesis_report": read_optional_json(PHASE2C_EVIDENCE_LOG),
        },
        "model_results": [],
        "best_model": None,
        "passes_target": False,
        "gap_to_target_0_24": None,
        "interpretation": {
            "nonlinear_signal_confirmed": False,
            "model_architecture_helped": False,
            "richer_features_still_needed": True,
        },
        "recommendation": "B) collect more outcomes and richer feature sources",
        "phase2c_status": "REVIEW_NOT_PASSED",
        "phase4_status": "RESEARCH_ONLY",
        "phase3_status": "LOCKED",
        "real_execution_status": "BLOCKED",
        "safety": {
            "db_write": False,
            "execution_change": False,
            "runtime_change": False,
            "production_scoring_change": False,
            "phase_3": False,
            "real_execution": "blocked",
        },
    }

    if not train or not valid:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(str(OUT_PATH))
        return

    core = ["score_norm", "regime_score_norm", "delta_norm", "holding_norm", "sl_dist", "tp1_dist", "tp2_dist", "rr1", "rr2"]
    proxy = ["cand_score_mom", "cand_regime_score_mom", "cand_rolling_return", "cand_atr_like", "cand_trend_slope", "tp_skew", "sl_tp_ratio"]
    safe = leakage_safe_numeric_fields(fields)
    regime_focus = ["regime_score_norm", "matched_regime_score", "delta_norm", "cand_regime_score_mom", "score_norm", "holding_norm"]
    feature_sets = [
        ("A_core", sorted(set(core))),
        ("B_core_plus_proxy", sorted(set(core + proxy))),
        ("C_leakage_safe_numeric_pre_signal", sorted(set(core + proxy + safe))),
        ("D_regime_score_focused", sorted(set(regime_focus))),
    ]

    results = []
    for fs_name, feats in feature_sets:
        xtr, ytr = build_xy(train, feats)
        xva, yva = build_xy(valid, feats)

        w = fit_logistic(xtr, ytr)
        results.append(summarize("LogisticBaselineReproduction", fs_name, train, valid, ytr, predict_logistic(w, xtr), yva, predict_logistic(w, xva)))

        stump = fit_stump(xtr, ytr)
        fi = None
        if stump:
            fi = [{"feature": feats[stump["feature_idx"]], "importance": 1.0}]
        results.append(summarize("DecisionStumpThreshold", fs_name, train, valid, ytr, predict_stump(stump, xtr), yva, predict_stump(stump, xva), fi))

        if sklearn_ok:
            for model_name, ctor in sklearn_models:
                model = ctor()
                model.fit(xtr, ytr)
                p_tr = [float(p[1]) for p in model.predict_proba(xtr)]
                p_va = [float(p[1]) for p in model.predict_proba(xva)]
                fim = None
                if hasattr(model, "feature_importances_"):
                    fim = sorted([
                        {"feature": f, "importance": round(float(i), 6)} for f, i in zip(feats, model.feature_importances_)
                    ], key=lambda z: z["importance"], reverse=True)[:12]
                results.append(summarize(model_name, fs_name, train, valid, ytr, p_tr, yva, p_va, fim))

        if xgboost_ok and xgb_ctor:
            model = xgb_ctor()
            model.fit(xtr, ytr)
            p_tr = [float(p) for p in model.predict_proba(xtr)[:, 1]]
            p_va = [float(p) for p in model.predict_proba(xva)[:, 1]]
            fim = None
            if hasattr(model, "feature_importances_"):
                fim = sorted([
                    {"feature": f, "importance": round(float(i), 6)} for f, i in zip(feats, model.feature_importances_)
                ], key=lambda z: z["importance"], reverse=True)[:12]
            results.append(summarize("XGBoostClassifier", fs_name, train, valid, ytr, p_tr, yva, p_va, fim))

    best = min(results, key=lambda r: r["validation_brier"]) if results else None
    report["model_results"] = results
    report["best_model"] = best
    report["passes_target"] = bool(best and best["passes_target"])
    report["gap_to_target_0_24"] = None if best is None else round(best["validation_brier"] - TARGET_BRIER, 6)

    if best:
        report["interpretation"] = {
            "nonlinear_signal_confirmed": best["model_name"] not in ("LogisticBaselineReproduction", "DecisionStumpThreshold") and best["improvement_vs_best_phase2c_0_245729"] > 0.001,
            "model_architecture_helped": best["improvement_vs_best_phase2c_0_245729"] > 0.001,
            "richer_features_still_needed": best["validation_brier"] > TARGET_BRIER,
        }

    if best and best["passes_target"]:
        report["recommendation"] = "A) promote best nonlinear architecture to deeper read-only validation"
    elif best and best["improvement_vs_best_phase2c_0_245729"] <= 0.001:
        report["recommendation"] = "C) redesign labels/targets"
    else:
        report["recommendation"] = "B) collect more outcomes and richer feature sources"

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(OUT_PATH))


if __name__ == "__main__":
    main()
