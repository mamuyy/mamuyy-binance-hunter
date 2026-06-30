"""
CP-049: Post Feature-Bridge Fix WF Revalidation
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Re-run WF validation after CP-048 fixed production_universe_dataset feature zeroing.
Compare post-fix WF accuracy against CP-046 baseline avg_acc=0.532.
"""
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp049_post_feature_bridge_wf_revalidation.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
PASS_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.55
BASELINE_CP046_AVG_ACC = 0.532

FEATURES = [
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "regime_score",
]

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def run_wf(ds):
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    folds = []
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
        acc = float(accuracy_score(y_test, pred))

        folds.append({
            "fold": fold_id,
            "status": "OK",
            "train_start": str(train["timestamp"].min()),
            "train_end": str(train["timestamp"].max()),
            "test_start": str(test["timestamp"].min()),
            "test_end": str(test["timestamp"].max()),
            "accuracy": round(acc, 4),
            "train_labels": train["target_binary"].value_counts().to_dict(),
            "test_labels": test["target_binary"].value_counts().to_dict(),
            "train_source": train["source_artifact"].value_counts().to_dict(),
            "test_source": test["source_artifact"].value_counts().to_dict(),
        })

        print(f"Fold {fold_id}: acc={acc:.3f}")
        fold_id += 1

    valid = [f["accuracy"] for f in folds if f.get("accuracy") is not None]
    avg = round(sum(valid) / len(valid), 4) if valid else None
    min_acc = round(min(valid), 4) if valid else None
    max_acc = round(max(valid), 4) if valid else None

    if avg is None:
        verdict = "INSUFFICIENT_DATA"
    elif avg >= PASS_THRESHOLD:
        verdict = "PASS"
    elif avg >= REVIEW_THRESHOLD:
        verdict = "REVIEW"
    else:
        verdict = "FAIL"

    return {
        "folds": folds,
        "valid_fold_count": len(valid),
        "avg_accuracy": avg,
        "min_accuracy": min_acc,
        "max_accuracy": max_acc,
        "verdict": verdict,
    }

def main():
    print("=== CP-049: Post Feature-Bridge Fix WF Revalidation ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True)

    feature_stats = {}
    for col in FEATURES:
        series = ds[col].fillna(0)
        feature_stats[col] = {
            "nonzero": int((series != 0).sum()),
            "nunique": int(series.nunique()),
            "min": float(series.min()),
            "max": float(series.max()),
        }

    wf = run_wf(ds)
    delta = None if wf["avg_accuracy"] is None else round(wf["avg_accuracy"] - BASELINE_CP046_AVG_ACC, 4)

    if wf["avg_accuracy"] is None:
        gate_verdict = "INSUFFICIENT_DATA"
        gate_reason = "Could not compute post-fix WF average accuracy."
    elif wf["avg_accuracy"] >= PASS_THRESHOLD:
        gate_verdict = "POST_FIX_PASS"
        gate_reason = "Post CP-048 WF reaches the 0.60 PASS gate."
    elif delta is not None and delta >= 0.02:
        gate_verdict = "POST_FIX_IMPROVED_REVIEW"
        gate_reason = f"Post CP-048 WF improved by {delta:+.4f}, but remains below 0.60 PASS gate."
    else:
        gate_verdict = "POST_FIX_NOT_SUFFICIENT"
        gate_reason = f"Post CP-048 WF delta is {delta:+.4f}; feature bridge fix alone is not sufficient."

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_cp046_avg_accuracy": BASELINE_CP046_AVG_ACC,
        "post_fix_avg_accuracy": wf["avg_accuracy"],
        "delta_vs_cp046": delta,
        "gate_verdict": gate_verdict,
        "gate_reason": gate_reason,
        "rows": int(len(ds)),
        "source_artifact_distribution": ds["source_artifact"].value_counts().to_dict(),
        "label_distribution": ds["target"].value_counts().to_dict(),
        "feature_stats": feature_stats,
        "wf": wf,
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
    print("Post-fix avg accuracy:", wf["avg_accuracy"])
    print("Delta vs CP-046:", delta)
    print("WF verdict:", wf["verdict"])
    print("Gate verdict:", gate_verdict)
    print("Reason:", gate_reason)
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
