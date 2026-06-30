"""
CP-052: Source Isolation / Weighting Simulation
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Test whether source/domain mismatch found in CP-051 can be handled by:
1. Full additive baseline WF
2. Downweighting historical_outcomes during training
3. Excluding historical_outcomes if feasible
4. Source-only adaptive WF for diagnostic support

Important:
- Standard WF uses TRAIN=500 / TEST=100 for comparability.
- Source-only datasets may be too small for 500/100, so adaptive WF is diagnostic only.
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

REPORT_JSON = "reports/cp052_source_isolation_weighting_simulation.json"

STANDARD_TRAIN_WINDOW = 500
STANDARD_TEST_WINDOW = 100
ADAPTIVE_TRAIN_WINDOW = 300
ADAPTIVE_TEST_WINDOW = 50
PASS_THRESHOLD = 0.60

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def run_wf(ds, label, train_window, test_window, source_weights=None):
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    folds = []
    start = 0
    fold_id = 1

    if len(ds) < train_window + test_window:
        return {
            "label": label,
            "rows": int(len(ds)),
            "train_window": train_window,
            "test_window": test_window,
            "valid_fold_count": 0,
            "avg_accuracy": None,
            "min_accuracy": None,
            "max_accuracy": None,
            "verdict": "INSUFFICIENT_ROWS",
            "reason": f"Rows {len(ds)} < required {train_window + test_window}.",
            "folds": [],
        }

    while start + train_window + test_window <= len(ds):
        train = ds.iloc[start:start + train_window].copy()
        test = ds.iloc[start + train_window:start + train_window + test_window].copy()
        start += test_window

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

        sample_weight = None
        if source_weights:
            sample_weight = train["source_artifact"].map(source_weights).fillna(1.0).astype(float)

        model.fit(x_train, y_train, sample_weight=sample_weight)
        pred = model.predict(x_test)
        acc = round(float(accuracy_score(y_test, pred)), 4)

        fold = {
            "fold": fold_id,
            "status": "OK",
            "accuracy": acc,
            "train_source_distribution": train["source_artifact"].value_counts().to_dict(),
            "test_source_distribution": test["source_artifact"].value_counts().to_dict(),
            "train_label_distribution": train["target_binary"].value_counts().to_dict(),
            "test_label_distribution": test["target_binary"].value_counts().to_dict(),
        }
        folds.append(fold)
        print(f"[{label}] Fold {fold_id}: acc={acc:.3f}")
        fold_id += 1

    valid = [f["accuracy"] for f in folds if f.get("accuracy") is not None]
    avg = round(sum(valid) / len(valid), 4) if valid else None
    min_acc = round(min(valid), 4) if valid else None
    max_acc = round(max(valid), 4) if valid else None

    if avg is None:
        verdict = "INSUFFICIENT_VALID_FOLDS"
    elif avg >= PASS_THRESHOLD:
        verdict = "PASS_SIMULATION"
    else:
        verdict = "REVIEW"

    return {
        "label": label,
        "rows": int(len(ds)),
        "train_window": train_window,
        "test_window": test_window,
        "source_weights": source_weights or {},
        "valid_fold_count": len(valid),
        "avg_accuracy": avg,
        "min_accuracy": min_acc,
        "max_accuracy": max_acc,
        "verdict": verdict,
        "folds": folds,
    }

def main():
    print("=== CP-052: Source Isolation / Weighting Simulation ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()

    scenarios = []

    print("\n[1] Standard full additive baseline")
    scenarios.append(run_wf(
        ds,
        "standard_full_additive",
        STANDARD_TRAIN_WINDOW,
        STANDARD_TEST_WINDOW,
    ))

    print("\n[2] Standard source weighting simulations")
    weighting_configs = [
        ("standard_hist_weight_0_75", {"historical_outcomes": 0.75, "internal_paper_trades": 1.0}),
        ("standard_hist_weight_0_50", {"historical_outcomes": 0.50, "internal_paper_trades": 1.0}),
        ("standard_hist_weight_0_25", {"historical_outcomes": 0.25, "internal_paper_trades": 1.0}),
        ("standard_hist_weight_0_10", {"historical_outcomes": 0.10, "internal_paper_trades": 1.0}),
    ]
    for label, weights in weighting_configs:
        scenarios.append(run_wf(
            ds,
            label,
            STANDARD_TRAIN_WINDOW,
            STANDARD_TEST_WINDOW,
            source_weights=weights,
        ))

    print("\n[3] Standard source exclusion attempts")
    ipt = ds[ds["source_artifact"] == "internal_paper_trades"].copy()
    hist = ds[ds["source_artifact"] == "historical_outcomes"].copy()

    scenarios.append(run_wf(
        ipt,
        "standard_internal_paper_only",
        STANDARD_TRAIN_WINDOW,
        STANDARD_TEST_WINDOW,
    ))
    scenarios.append(run_wf(
        hist,
        "standard_historical_only",
        STANDARD_TRAIN_WINDOW,
        STANDARD_TEST_WINDOW,
    ))

    print("\n[4] Adaptive source-only diagnostics")
    scenarios.append(run_wf(
        ipt,
        "adaptive_internal_paper_only",
        ADAPTIVE_TRAIN_WINDOW,
        ADAPTIVE_TEST_WINDOW,
    ))
    scenarios.append(run_wf(
        hist,
        "adaptive_historical_only",
        ADAPTIVE_TRAIN_WINDOW,
        ADAPTIVE_TEST_WINDOW,
    ))

    comparable = [
        s for s in scenarios
        if s["train_window"] == STANDARD_TRAIN_WINDOW
        and s["test_window"] == STANDARD_TEST_WINDOW
        and s["avg_accuracy"] is not None
    ]

    best_comparable = max(comparable, key=lambda s: s["avg_accuracy"]) if comparable else None
    baseline = next((s for s in scenarios if s["label"] == "standard_full_additive"), None)

    if best_comparable and baseline and baseline["avg_accuracy"] is not None:
        delta_vs_baseline = round(best_comparable["avg_accuracy"] - baseline["avg_accuracy"], 4)
    else:
        delta_vs_baseline = None

    if best_comparable is None:
        gate_verdict = "INSUFFICIENT_DATA"
        gate_reason = "No comparable standard WF scenario produced valid folds."
    elif best_comparable["avg_accuracy"] >= PASS_THRESHOLD and best_comparable["valid_fold_count"] >= 5:
        gate_verdict = "SOURCE_WEIGHTING_PROMISING_REVIEW"
        gate_reason = (
            f"Best standard scenario {best_comparable['label']} reached "
            f"{best_comparable['avg_accuracy']} with {best_comparable['valid_fold_count']} folds. "
            "This is promising but remains simulation-only; no promotion allowed."
        )
    elif delta_vs_baseline is not None and delta_vs_baseline >= 0.02:
        gate_verdict = "SOURCE_WEIGHTING_HELPS_PARTIALLY"
        gate_reason = (
            f"Best standard scenario {best_comparable['label']} improved by "
            f"{delta_vs_baseline:+.4f}, but did not reach a robust PASS gate."
        )
    else:
        gate_verdict = "SOURCE_WEIGHTING_NOT_SUFFICIENT"
        gate_reason = (
            "Source weighting/isolation did not materially improve standard WF enough "
            "to justify model promotion."
        )

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": gate_verdict,
        "reason": gate_reason,
        "rows": int(len(ds)),
        "dataset_source_distribution": ds["source_artifact"].value_counts().to_dict(),
        "dataset_label_distribution": ds["target"].value_counts().to_dict(),
        "standard_train_window": STANDARD_TRAIN_WINDOW,
        "standard_test_window": STANDARD_TEST_WINDOW,
        "adaptive_train_window": ADAPTIVE_TRAIN_WINDOW,
        "adaptive_test_window": ADAPTIVE_TEST_WINDOW,
        "baseline_avg_accuracy": baseline["avg_accuracy"] if baseline else None,
        "best_comparable_label": best_comparable["label"] if best_comparable else None,
        "best_comparable_avg_accuracy": best_comparable["avg_accuracy"] if best_comparable else None,
        "delta_vs_baseline": delta_vs_baseline,
        "scenarios": scenarios,
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
    print("Gate verdict:", gate_verdict)
    print("Reason:", gate_reason)
    print("Baseline avg:", report["baseline_avg_accuracy"])
    print("Best comparable:", report["best_comparable_label"], report["best_comparable_avg_accuracy"])
    print("Delta vs baseline:", delta_vs_baseline)
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
