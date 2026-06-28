"""
CP-040A: Binary Production Universe Walkforward Validation
READ-ONLY - no runtime/execution/db changes
"""
import json, os, sys
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix

sys.path.insert(0, ".")
from ml_engine import _production_universe_dataset, fit_train_only_preprocessor, transform_with_train_preprocessor

REPORT_JSON = "reports/cp040a_binary_production_walkforward.json"
REPORT_CSV  = "reports/cp040a_binary_production_walkforward_folds.csv"
TRAIN_WINDOW = 500
TEST_WINDOW  = 100
PASS_THRESHOLD   = 0.60
REVIEW_THRESHOLD = 0.55

def run():
    print("=== CP-040A Binary Production Universe Walkforward ===")
    ds = _production_universe_dataset()
    ds = ds.sort_values("timestamp").reset_index(drop=True)
    total_rows = len(ds)
    print(f"Dataset rows: {total_rows}")

    # Binary target: TP1 HIT → WIN
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    orig_dist  = ds["target"].value_counts().to_dict()
    bin_dist   = ds["target_binary"].value_counts().to_dict()
    src_dist   = ds["source_artifact"].value_counts().to_dict() if "source_artifact" in ds.columns else {}

    print(f"Original: {orig_dist}")
    print(f"Binary:   {bin_dist}")
    print(f"Source:   {src_dist}")

    folds = []
    start, fold_id = 0, 1
    all_y_true, all_y_pred = [], []

    while start + TRAIN_WINDOW + TEST_WINDOW <= total_rows:
        train = ds.iloc[start : start + TRAIN_WINDOW].copy()
        test  = ds.iloc[start + TRAIN_WINDOW : start + TRAIN_WINDOW + TEST_WINDOW].copy()
        start += TEST_WINDOW

        train_labels = set(train["target_binary"].unique())
        test_labels  = set(test["target_binary"].unique())
        unseen = test_labels - train_labels
        class_coverage = "INVALID_CLASS_COVERAGE" if unseen else "OK"

        orig_train_dist = train["target"].value_counts().to_dict()
        orig_test_dist  = test["target"].value_counts().to_dict()
        bin_train_dist  = train["target_binary"].value_counts().to_dict()
        bin_test_dist   = test["target_binary"].value_counts().to_dict()

        acc = None
        if train["target_binary"].nunique() >= 2:
            try:
                pre = fit_train_only_preprocessor(train)
                X_tr = transform_with_train_preprocessor(train, pre)
                X_te = transform_with_train_preprocessor(test, pre)
                y_tr = train["target_binary"]
                y_te = test["target_binary"]

                clf = RandomForestClassifier(n_estimators=150, max_depth=5,
                                             class_weight="balanced", random_state=42)
                clf.fit(X_tr, y_tr)
                y_pred = clf.predict(X_te)
                acc = float(accuracy_score(y_te, y_pred))
                all_y_true.extend(y_te.tolist())
                all_y_pred.extend(y_pred.tolist())
            except Exception as e:
                acc = None
                print(f"  Fold {fold_id}: ERROR {e}")

        fold_result = {
            "fold_id": fold_id,
            "train_rows": len(train),
            "test_rows": len(test),
            "fold_class_coverage": class_coverage,
            "unseen_test_labels": list(unseen),
            "orig_train_dist": orig_train_dist,
            "orig_test_dist": orig_test_dist,
            "bin_train_dist": bin_train_dist,
            "bin_test_dist": bin_test_dist,
            "binary_accuracy": round(acc, 4) if acc is not None else None,
        }
        folds.append(fold_result)
        status = f"acc={acc:.3f}" if acc is not None else "SKIP"
        cov    = f" [{class_coverage}]" if class_coverage != "OK" else ""
        print(f"  Fold {fold_id}: {status}{cov}  binary_test={bin_test_dist}")
        fold_id += 1

    # Aggregate — only valid folds for accuracy
    valid_accs = [f["binary_accuracy"] for f in folds
                  if f["binary_accuracy"] is not None and f["fold_class_coverage"] == "OK"]
    all_accs   = [f["binary_accuracy"] for f in folds if f["binary_accuracy"] is not None]
    invalid_folds = [f["fold_id"] for f in folds if f["fold_class_coverage"] != "OK"]

    avg_valid = float(np.mean(valid_accs)) if valid_accs else None
    avg_all   = float(np.mean(all_accs))   if all_accs   else None

    if avg_valid is None:
        verdict = "FAIL"
    elif avg_valid >= PASS_THRESHOLD:
        verdict = "PASS"
    elif avg_valid >= REVIEW_THRESHOLD:
        verdict = "REVIEW"
    else:
        verdict = "FAIL"

    # Confusion matrix
    cm = {}
    if all_y_true:
        labels = sorted(set(all_y_true + all_y_pred))
        cm_arr = confusion_matrix(all_y_true, all_y_pred, labels=labels)
        cm = {str(labels[i]): {str(labels[j]): int(cm_arr[i][j])
              for j in range(len(labels))} for i in range(len(labels))}

    report = {
        "cp_id": "CP-040A",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sandbox": True,
        "dataset_rows": total_rows,
        "train_window": TRAIN_WINDOW,
        "test_window": TEST_WINDOW,
        "source_artifact_distribution": src_dist,
        "original_target_distribution": orig_dist,
        "binary_target_distribution": bin_dist,
        "fold_count": len(folds),
        "invalid_class_coverage_folds": invalid_folds,
        "average_accuracy_valid_folds": round(avg_valid, 4) if avg_valid else None,
        "average_accuracy_all_folds": round(avg_all, 4) if avg_all else None,
        "per_fold_accuracy": [f["binary_accuracy"] for f in folds],
        "confusion_matrix": cm,
        "verdict": verdict,
        "verdict_logic": {
            "PASS": f">= {PASS_THRESHOLD}",
            "REVIEW": f">= {REVIEW_THRESHOLD} and < {PASS_THRESHOLD}",
            "FAIL": f"< {REVIEW_THRESHOLD}",
        },
        "folds": folds,
        "notes": ["CP-040A read-only. TP1 HIT merged to WIN for binary view only.",
                  "Fold validity guard applied: INVALID_CLASS_COVERAGE excluded from avg."],
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # CSV
    fold_rows = []
    for fld in folds:
        fold_rows.append({
            "fold_id": fld["fold_id"],
            "train_rows": fld["train_rows"],
            "test_rows": fld["test_rows"],
            "fold_class_coverage": fld["fold_class_coverage"],
            "binary_accuracy": fld["binary_accuracy"],
            "bin_train_dist": str(fld["bin_train_dist"]),
            "bin_test_dist": str(fld["bin_test_dist"]),
            "orig_test_dist": str(fld["orig_test_dist"]),
        })
    pd.DataFrame(fold_rows).to_csv(REPORT_CSV, index=False)

    print()
    print(f"=== CP-040A RESULTS ===")
    print(f"Total folds:           {len(folds)}")
    print(f"Invalid class folds:   {invalid_folds}")
    print(f"Avg accuracy (valid):  {avg_valid:.4f}" if avg_valid else "Avg accuracy (valid):  N/A")
    print(f"Avg accuracy (all):    {avg_all:.4f}"   if avg_all   else "Avg accuracy (all):    N/A")
    print(f"VERDICT: {verdict}")
    print(f"Report: {REPORT_JSON}")
    return report

if __name__ == "__main__":
    run()
