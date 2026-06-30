"""
CP-050: Feature / Label Quality Drilldown
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Diagnose why CP-049 improved WF to 0.586 but still did not reach 0.60 PASS gate.

Focus:
1. Per-fold confusion matrix
2. Per-fold feature importance
3. Error source distribution: historical_outcomes vs internal_paper_trades
4. Regime-level accuracy
5. Label imbalance and weak folds
"""
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp050_feature_label_quality_drilldown.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
PASS_THRESHOLD = 0.60
TOP_N_FEATURES = 20

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def get_feature_names(x_train, preprocessor):
    if hasattr(x_train, "columns"):
        return list(x_train.columns)
    if hasattr(preprocessor, "get_feature_names_out"):
        try:
            return [str(x) for x in preprocessor.get_feature_names_out()]
        except Exception:
            pass
    width = getattr(x_train, "shape", [0, 0])[1]
    return [f"feature_{i}" for i in range(width)]

def group_accuracy(frame, group_col):
    out = {}
    if group_col not in frame.columns:
        return out
    for key, group in frame.groupby(group_col, dropna=False):
        if len(group) == 0:
            continue
        out[str(key)] = {
            "rows": int(len(group)),
            "accuracy": round(float(accuracy_score(group["actual"], group["predicted"])), 4),
            "actual_distribution": group["actual"].value_counts().to_dict(),
            "predicted_distribution": group["predicted"].value_counts().to_dict(),
        }
    return out

def main():
    print("=== CP-050: Feature / Label Quality Drilldown ===")

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
        labels = ["LOSS", "WIN"]
        cm = confusion_matrix(y_test, pred, labels=labels).tolist()
        report = classification_report(y_test, pred, labels=labels, output_dict=True, zero_division=0)

        names = get_feature_names(x_train, preprocessor)
        importances = list(model.feature_importances_)
        pairs = list(zip(names[:len(importances)], importances))
        top_features = [
            {"feature": str(name), "importance": round(float(value), 6)}
            for name, value in sorted(pairs, key=lambda x: x[1], reverse=True)[:TOP_N_FEATURES]
        ]

        eval_frame = test[[
            "timestamp", "symbol", "source_artifact", "regime_name", "score", "target", "target_binary"
        ]].copy()
        eval_frame["actual"] = list(y_test)
        eval_frame["predicted"] = list(pred)
        eval_frame["is_error"] = eval_frame["actual"] != eval_frame["predicted"]

        error_frame = eval_frame[eval_frame["is_error"]].copy()
        for _, row in error_frame.iterrows():
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
            "accuracy": round(acc, 4),
            "train_range": {
                "start": str(train["timestamp"].min()),
                "end": str(train["timestamp"].max()),
            },
            "test_range": {
                "start": str(test["timestamp"].min()),
                "end": str(test["timestamp"].max()),
            },
            "train_label_distribution": train["target_binary"].value_counts().to_dict(),
            "test_label_distribution": test["target_binary"].value_counts().to_dict(),
            "test_source_distribution": test["source_artifact"].value_counts().to_dict(),
            "test_regime_distribution": test["regime_name"].value_counts().to_dict(),
            "confusion_matrix_labels": labels,
            "confusion_matrix": cm,
            "classification_report": report,
            "accuracy_by_source": group_accuracy(eval_frame, "source_artifact"),
            "accuracy_by_regime": group_accuracy(eval_frame, "regime_name"),
            "error_count": int(len(error_frame)),
            "error_by_source": error_frame["source_artifact"].value_counts().to_dict(),
            "error_by_regime": error_frame["regime_name"].value_counts().to_dict(),
            "top_features": top_features,
        }

        folds.append(fold_result)

        print(f"Fold {fold_id}: acc={acc:.3f} errors={len(error_frame)}")
        print("  Test labels:", fold_result["test_label_distribution"])
        print("  Error by source:", fold_result["error_by_source"])
        print("  Top 5 features:", [x["feature"] for x in top_features[:5]])

        fold_id += 1

    valid_acc = [f["accuracy"] for f in folds if f.get("accuracy") is not None]
    avg_acc = round(sum(valid_acc) / len(valid_acc), 4) if valid_acc else None

    weak_folds = [
        {
            "fold": f["fold"],
            "accuracy": f["accuracy"],
            "error_count": f.get("error_count"),
            "error_by_source": f.get("error_by_source"),
            "error_by_regime": f.get("error_by_regime"),
            "top_features": f.get("top_features", [])[:5],
        }
        for f in folds
        if f.get("accuracy") is not None and f["accuracy"] < PASS_THRESHOLD
    ]

    all_errors_frame = pd.DataFrame(all_errors)
    global_error_by_source = all_errors_frame["source_artifact"].value_counts().to_dict() if not all_errors_frame.empty else {}
    global_error_by_regime = all_errors_frame["regime_name"].value_counts().to_dict() if not all_errors_frame.empty else {}
    global_error_by_actual_pred = (
        all_errors_frame.groupby(["actual", "predicted"]).size().reset_index(name="count").to_dict("records")
        if not all_errors_frame.empty else []
    )

    if avg_acc is None:
        verdict = "INSUFFICIENT_DATA"
        reason = "No valid folds."
    elif avg_acc >= PASS_THRESHOLD:
        verdict = "PASS"
        reason = "WF average reached PASS gate."
    else:
        verdict = "REVIEW"
        reason = "WF average remains below PASS gate; drilldown identifies weak folds and error concentration."

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "rows": int(len(ds)),
        "avg_accuracy": avg_acc,
        "pass_threshold": PASS_THRESHOLD,
        "dataset_source_distribution": ds["source_artifact"].value_counts().to_dict(),
        "dataset_label_distribution": ds["target_binary"].value_counts().to_dict(),
        "dataset_regime_distribution": ds["regime_name"].value_counts().to_dict(),
        "folds": folds,
        "weak_folds": weak_folds,
        "global_error_by_source": global_error_by_source,
        "global_error_by_regime": global_error_by_regime,
        "global_error_by_actual_predicted": global_error_by_actual_pred,
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
    print("Global error by source:", global_error_by_source)
    print("Global error by regime:", global_error_by_regime)
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
