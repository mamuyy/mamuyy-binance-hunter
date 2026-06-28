"""
CP-038D: Walk-Forward Validation Sandbox
Dataset: _production_universe_dataset() CP-039D (1043 rows)
Train window: 500, Test window: 100
Gate: WF accuracy >= 60%
SANDBOX ONLY - no model promotion, no runtime changes
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.preprocessing import LabelBinarizer

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_PATH = "logs/cp038d_wf_sandbox_report.json"
TRAIN_WINDOW = 500
TEST_WINDOW = 100
WF_ACCURACY_GATE = 0.60
BRIER_GATE = 0.24

def run_cp038d_sandbox():
    print("=== CP-038D Walk-Forward Sandbox ===")
    print(f"Train window: {TRAIN_WINDOW}, Test window: {TEST_WINDOW}")
    print(f"WF accuracy gate: {WF_ACCURACY_GATE}")
    print()

    # Load production universe dataset
    print("Loading _production_universe_dataset()...")
    dataset = _production_universe_dataset()
    total_rows = len(dataset)
    print(f"Dataset rows: {total_rows}")
    print(f"Label distribution: {dataset['target'].value_counts().to_dict()}")

    if total_rows < TRAIN_WINDOW + TEST_WINDOW:
        result = {
            "verdict": "INSUFFICIENT_DATA",
            "rows": total_rows,
            "required_rows": TRAIN_WINDOW + TEST_WINDOW,
            "wf_accuracy": None,
            "gate_passed": False,
        }
        _write_report(result)
        print(f"FAIL: Only {total_rows} rows, need {TRAIN_WINDOW + TEST_WINDOW}")
        return result

    # Sort by timestamp
    if "timestamp" in dataset.columns:
        dataset = dataset.sort_values("timestamp").reset_index(drop=True)
        print(f"Sorted by timestamp: {dataset['timestamp'].min()} → {dataset['timestamp'].max()}")

    # Walk-forward loop
    folds = []
    start = 0
    fold_id = 1

    while start + TRAIN_WINDOW + TEST_WINDOW <= len(dataset):
        train = dataset.iloc[start : start + TRAIN_WINDOW].copy()
        test = dataset.iloc[start + TRAIN_WINDOW : start + TRAIN_WINDOW + TEST_WINDOW].copy()
        start += TEST_WINDOW

        if train["target"].nunique() < 2:
            print(f"  Fold {fold_id}: skip (insufficient class diversity in train)")
            fold_id += 1
            continue

        try:
            preprocessor = fit_train_only_preprocessor(train)
            X_train = transform_with_train_preprocessor(train, preprocessor)
            X_test = transform_with_train_preprocessor(test, preprocessor)
            y_train = train["target"]
            y_test = test["target"]

            model = RandomForestClassifier(
                n_estimators=150,
                max_depth=5,
                class_weight="balanced",
                random_state=42,
            )
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)
            y_prob = model.predict_proba(X_test)

            test_accuracy = float(accuracy_score(y_test, y_pred))

            # Brier score (multiclass via one-vs-rest)
            lb = LabelBinarizer()
            y_test_bin = lb.fit_transform(y_test)
            if y_test_bin.shape[1] == 1:
                y_test_bin = np.hstack([1 - y_test_bin, y_test_bin])
            # align classes
            classes = model.classes_
            brier = float(np.mean([
                brier_score_loss(y_test_bin[:, i], y_prob[:, j])
                for i, c in enumerate(lb.classes_)
                for j, mc in enumerate(classes)
                if c == mc
            ])) if len(lb.classes_) > 0 else None

            train_ts = train["timestamp"].min() if "timestamp" in train.columns else None
            test_ts = test["timestamp"].max() if "timestamp" in test.columns else None

            fold_result = {
                "fold_id": fold_id,
                "train_rows": len(train),
                "test_rows": len(test),
                "train_start": str(train_ts) if train_ts else None,
                "test_end": str(test_ts) if test_ts else None,
                "test_accuracy": round(test_accuracy, 4),
                "brier_score": round(brier, 4) if brier else None,
                "label_dist_test": y_test.value_counts().to_dict(),
            }
            folds.append(fold_result)
            brier_display = f"{brier:.4f}" if brier is not None else "N/A"
            print(f"  Fold {fold_id}: accuracy={test_accuracy:.3f}, brier={brier_display}")

        except Exception as e:
            print(f"  Fold {fold_id}: ERROR - {e}")

        fold_id += 1

    if not folds:
        result = {"verdict": "NO_VALID_FOLDS", "folds": [], "gate_passed": False}
        _write_report(result)
        return result

    # Aggregate
    wf_accuracy = float(np.mean([f["test_accuracy"] for f in folds]))
    brier_scores = [f["brier_score"] for f in folds if f["brier_score"] is not None]
    avg_brier = float(np.mean(brier_scores)) if brier_scores else None

    gate_wf = wf_accuracy >= WF_ACCURACY_GATE
    gate_brier = (avg_brier <= BRIER_GATE) if avg_brier is not None else None
    overall_gate = gate_wf  # primary gate

    verdict = "GATE_PASSED" if gate_wf else "GATE_FAILED_WF_ACCURACY"

    result = {
        "cp_id": "CP-038D",
        "sandbox": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_rows": total_rows,
        "train_window": TRAIN_WINDOW,
        "test_window": TEST_WINDOW,
        "total_folds": len(folds),
        "wf_accuracy": round(wf_accuracy, 4),
        "wf_accuracy_gate": WF_ACCURACY_GATE,
        "wf_accuracy_gate_passed": gate_wf,
        "avg_brier_score": round(avg_brier, 4) if avg_brier else None,
        "brier_gate": BRIER_GATE,
        "brier_gate_passed": gate_brier,
        "gate_passed": overall_gate,
        "verdict": verdict,
        "folds": folds,
        "notes": [
            "CP-038D sandbox - no model promotion",
            f"Dataset source: _production_universe_dataset() CP-039D",
            f"Prior WF baseline (CP-038A): 54.22%",
        ],
    }

    _write_report(result)

    print()
    print(f"=== RESULTS ===")
    print(f"Total folds: {len(folds)}")
    print(f"WF Accuracy: {wf_accuracy:.4f} (gate: {WF_ACCURACY_GATE})")
    if avg_brier:
        print(f"Avg Brier:   {avg_brier:.4f} (gate: {BRIER_GATE})")
    print(f"GATE: {'PASSED ✅' if gate_wf else 'FAILED ❌'}")
    print(f"Report: {REPORT_PATH}")

    return result

def _write_report(data):
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

if __name__ == "__main__":
    run_cp038d_sandbox()
