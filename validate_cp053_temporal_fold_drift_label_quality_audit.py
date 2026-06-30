"""
CP-053: Temporal Fold Drift / Label Quality Audit
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Diagnose why post-CP048/CP049/CP050 WF remains below the 0.60 PASS gate.

Focus:
1. Temporal drift between train/test windows
2. Label distribution drift across folds
3. Feature distribution drift across folds
4. Weak-fold diagnosis
5. Sample scarcity / unstable fold evidence
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp053_temporal_fold_drift_label_quality_audit.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
PASS_THRESHOLD = 0.60

DRIFT_FEATURES = [
    "score",
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "regime_score",
    "pnl_percent",
]

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def value_counts_dict(series):
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}

def label_stats(frame):
    binary = frame["target_binary"]
    original = frame["target"]
    return {
        "rows": int(len(frame)),
        "binary_distribution": value_counts_dict(binary),
        "target_distribution": value_counts_dict(original),
        "win_rate_binary": round(float((binary == "WIN").mean()), 4) if len(frame) else None,
        "loss_rate_binary": round(float((binary == "LOSS").mean()), 4) if len(frame) else None,
        "tp1_hit_rate_original": round(float((original == "TP1 HIT").mean()), 4) if len(frame) else None,
    }

def psi(train_values, test_values, bins=10):
    train = pd.to_numeric(train_values, errors="coerce").fillna(0.0).astype(float)
    test = pd.to_numeric(test_values, errors="coerce").fillna(0.0).astype(float)

    if train.nunique() <= 1 and test.nunique() <= 1:
        return 0.0

    try:
        edges = np.unique(np.quantile(train, np.linspace(0, 1, bins + 1)))
        if len(edges) < 3:
            min_v = min(float(train.min()), float(test.min()))
            max_v = max(float(train.max()), float(test.max()))
            if min_v == max_v:
                return 0.0
            edges = np.linspace(min_v, max_v, bins + 1)

        edges[0] = -np.inf
        edges[-1] = np.inf

        train_hist, _ = np.histogram(train, bins=edges)
        test_hist, _ = np.histogram(test, bins=edges)

        train_pct = train_hist / max(train_hist.sum(), 1)
        test_pct = test_hist / max(test_hist.sum(), 1)

        eps = 1e-6
        value = np.sum((test_pct - train_pct) * np.log((test_pct + eps) / (train_pct + eps)))
        return round(float(value), 6)
    except Exception:
        return None

def feature_drift(train, test):
    out = {}
    for col in DRIFT_FEATURES:
        if col not in train.columns or col not in test.columns:
            continue

        tr = pd.to_numeric(train[col], errors="coerce").fillna(0.0).astype(float)
        te = pd.to_numeric(test[col], errors="coerce").fillna(0.0).astype(float)

        tr_mean = float(tr.mean())
        te_mean = float(te.mean())
        tr_std = float(tr.std())
        te_std = float(te.std())
        pooled_std = float(np.sqrt((tr_std ** 2 + te_std ** 2) / 2.0)) if (tr_std or te_std) else 0.0
        std_delta = (te_mean - tr_mean) / pooled_std if pooled_std > 0 else 0.0

        out[col] = {
            "train_mean": round(tr_mean, 6),
            "test_mean": round(te_mean, 6),
            "mean_delta": round(te_mean - tr_mean, 6),
            "train_std": round(tr_std, 6),
            "test_std": round(te_std, 6),
            "standardized_mean_delta": round(float(std_delta), 6),
            "train_median": round(float(tr.median()), 6),
            "test_median": round(float(te.median()), 6),
            "train_nonzero": int((tr != 0).sum()),
            "test_nonzero": int((te != 0).sum()),
            "psi": psi(tr, te),
        }
    return out

def top_drift_features(drift, n=8):
    ranked = []
    for feature, stats in drift.items():
        psi_value = stats.get("psi")
        std_delta = abs(float(stats.get("standardized_mean_delta") or 0.0))
        psi_rank = abs(float(psi_value or 0.0))
        ranked.append({
            "feature": feature,
            "standardized_mean_delta_abs": round(std_delta, 6),
            "psi": psi_value,
            "train_mean": stats.get("train_mean"),
            "test_mean": stats.get("test_mean"),
        })
    return sorted(ranked, key=lambda x: (x["standardized_mean_delta_abs"], abs(float(x["psi"] or 0.0))), reverse=True)[:n]

def run_model(train, test):
    preprocessor = fit_train_only_preprocessor(train)
    x_train = transform(train, preprocessor)
    x_test = transform(test, preprocessor)

    y_train = train["target_binary"]
    y_test = test["target_binary"]

    model = RandomForestClassifier(
        n_estimators=200,
        random_state=42,
        class_weight="balanced_subsample",
        min_samples_leaf=5,
    )
    model.fit(x_train, y_train)
    pred = model.predict(x_test)

    acc = round(float(accuracy_score(y_test, pred)), 4)
    labels = ["LOSS", "WIN"]
    cm = confusion_matrix(y_test, pred, labels=labels).tolist()

    eval_frame = test[["timestamp", "symbol", "source_artifact", "regime_name", "score", "target", "target_binary"]].copy()
    eval_frame["actual"] = list(y_test)
    eval_frame["predicted"] = list(pred)
    eval_frame["is_error"] = eval_frame["actual"] != eval_frame["predicted"]

    return acc, cm, eval_frame

def main():
    print("=== CP-053: Temporal Fold Drift / Label Quality Audit ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    folds = []
    all_errors = []
    start = 0
    fold_id = 1

    while start + TRAIN_WINDOW + TEST_WINDOW <= len(ds):
        train = ds.iloc[start:start + TRAIN_WINDOW].copy()
        test = ds.iloc[start + TRAIN_WINDOW:start + TRAIN_WINDOW + TEST_WINDOW].copy()
        start += TEST_WINDOW

        if train["target_binary"].nunique() < 2:
            folds.append({
                "fold": fold_id,
                "status": "SKIPPED_SINGLE_CLASS_TRAIN",
                "accuracy": None,
            })
            fold_id += 1
            continue

        acc, cm, eval_frame = run_model(train, test)
        drift = feature_drift(train, test)
        train_labels = label_stats(train)
        test_labels = label_stats(test)
        win_rate_gap = None
        if train_labels["win_rate_binary"] is not None and test_labels["win_rate_binary"] is not None:
            win_rate_gap = round(test_labels["win_rate_binary"] - train_labels["win_rate_binary"], 4)

        errors = eval_frame[eval_frame["is_error"]].copy()
        for _, row in errors.iterrows():
            all_errors.append({
                "fold": fold_id,
                "timestamp": str(row["timestamp"]),
                "symbol": str(row["symbol"]),
                "source_artifact": str(row["source_artifact"]),
                "regime_name": str(row["regime_name"]),
                "score": float(row["score"]) if pd.notna(row["score"]) else None,
                "target": str(row["target"]),
                "actual": str(row["actual"]),
                "predicted": str(row["predicted"]),
            })

        fold_result = {
            "fold": fold_id,
            "status": "OK",
            "accuracy": acc,
            "train_range": {
                "start": str(train["timestamp"].min()),
                "end": str(train["timestamp"].max()),
            },
            "test_range": {
                "start": str(test["timestamp"].min()),
                "end": str(test["timestamp"].max()),
            },
            "train_source_distribution": value_counts_dict(train["source_artifact"]),
            "test_source_distribution": value_counts_dict(test["source_artifact"]),
            "train_regime_distribution": value_counts_dict(train["regime_name"]),
            "test_regime_distribution": value_counts_dict(test["regime_name"]),
            "train_label_stats": train_labels,
            "test_label_stats": test_labels,
            "test_minus_train_win_rate_gap": win_rate_gap,
            "confusion_matrix_labels": ["LOSS", "WIN"],
            "confusion_matrix": cm,
            "error_count": int(len(errors)),
            "error_by_source": value_counts_dict(errors["source_artifact"]) if len(errors) else {},
            "error_by_regime": value_counts_dict(errors["regime_name"]) if len(errors) else {},
            "error_by_actual_predicted": value_counts_dict(errors["actual"] + "->" + errors["predicted"]) if len(errors) else {},
            "top_drift_features": top_drift_features(drift),
            "feature_drift": drift,
        }

        folds.append(fold_result)

        print(f"Fold {fold_id}: acc={acc:.3f} errors={len(errors)} win_gap={win_rate_gap:+.4f}")
        print("  Train labels:", train_labels["binary_distribution"], "Test labels:", test_labels["binary_distribution"])
        print("  Error actual->pred:", fold_result["error_by_actual_predicted"])
        print("  Top drift:", [x["feature"] for x in fold_result["top_drift_features"][:5]])

        fold_id += 1

    valid_acc = [f["accuracy"] for f in folds if f.get("accuracy") is not None]
    avg_acc = round(sum(valid_acc) / len(valid_acc), 4) if valid_acc else None
    weak_folds = [f for f in folds if f.get("accuracy") is not None and f["accuracy"] < PASS_THRESHOLD]

    all_errors_frame = pd.DataFrame(all_errors)
    global_error_by_source = value_counts_dict(all_errors_frame["source_artifact"]) if not all_errors_frame.empty else {}
    global_error_by_regime = value_counts_dict(all_errors_frame["regime_name"]) if not all_errors_frame.empty else {}
    global_error_by_actual_predicted = value_counts_dict(all_errors_frame["actual"] + "->" + all_errors_frame["predicted"]) if not all_errors_frame.empty else {}

    max_abs_win_gap = max([abs(f["test_minus_train_win_rate_gap"]) for f in folds if f.get("test_minus_train_win_rate_gap") is not None] or [0])
    weak_fold_count = len(weak_folds)

    drift_hotspots = []
    for f in folds:
        for item in f.get("top_drift_features", [])[:5]:
            drift_hotspots.append({
                "fold": f["fold"],
                **item,
                "fold_accuracy": f["accuracy"],
            })
    drift_hotspots = sorted(
        drift_hotspots,
        key=lambda x: (x["standardized_mean_delta_abs"], abs(float(x["psi"] or 0.0))),
        reverse=True,
    )[:15]

    if avg_acc is None:
        verdict = "INSUFFICIENT_DATA"
        reason = "No valid folds available."
    elif avg_acc >= PASS_THRESHOLD and weak_fold_count == 0:
        verdict = "TEMPORAL_DRIFT_PASS"
        reason = "Average accuracy and all folds pass the 0.60 gate."
    elif max_abs_win_gap >= 0.15:
        verdict = "TEMPORAL_LABEL_DRIFT_REVIEW"
        reason = f"WF remains below gate and train/test win-rate gap reaches {max_abs_win_gap:.4f}, indicating temporal label drift."
    elif weak_fold_count >= 3:
        verdict = "TEMPORAL_FOLD_INSTABILITY_REVIEW"
        reason = f"{weak_fold_count} folds remain below 0.60, indicating fold instability/sample scarcity."
    else:
        verdict = "TEMPORAL_DRIFT_NOT_CONCLUSIVE"
        reason = "Temporal drift is present but not clearly sufficient alone to explain the remaining gap."

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "rows": int(len(ds)),
        "avg_accuracy": avg_acc,
        "pass_threshold": PASS_THRESHOLD,
        "valid_fold_count": len(valid_acc),
        "weak_fold_count": weak_fold_count,
        "max_abs_test_minus_train_win_rate_gap": round(float(max_abs_win_gap), 4),
        "dataset_source_distribution": value_counts_dict(ds["source_artifact"]),
        "dataset_target_distribution": value_counts_dict(ds["target"]),
        "dataset_binary_distribution": value_counts_dict(ds["target_binary"]),
        "dataset_regime_distribution": value_counts_dict(ds["regime_name"]),
        "folds": folds,
        "weak_fold_summary": [
            {
                "fold": f["fold"],
                "accuracy": f["accuracy"],
                "win_rate_gap": f["test_minus_train_win_rate_gap"],
                "error_count": f["error_count"],
                "error_by_actual_predicted": f["error_by_actual_predicted"],
                "error_by_source": f["error_by_source"],
                "top_drift_features": f["top_drift_features"][:5],
            }
            for f in weak_folds
        ],
        "global_error_by_source": global_error_by_source,
        "global_error_by_regime": global_error_by_regime,
        "global_error_by_actual_predicted": global_error_by_actual_predicted,
        "drift_hotspots": drift_hotspots,
        "error_samples": all_errors[:50],
        "governance": {
            "read_only_validation": True,
            "runtime_execution_changed": False,
            "model_promoted": False,
            "live_unlock": False,
            "threshold_changed": False,
        },
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n=== RESULT ===")
    print("Avg accuracy:", avg_acc)
    print("Verdict:", verdict)
    print("Reason:", reason)
    print("Weak fold count:", weak_fold_count)
    print("Max abs win-rate gap:", round(float(max_abs_win_gap), 4))
    print("Global error actual->pred:", global_error_by_actual_predicted)
    print("Top drift hotspots:", [(x["fold"], x["feature"], x["standardized_mean_delta_abs"]) for x in drift_hotspots[:8]])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
