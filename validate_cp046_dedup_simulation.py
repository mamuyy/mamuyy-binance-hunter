"""
CP-046: Dedup-Aware Dataset Simulation & WF Re-Validation
READ-ONLY - no runtime/execution/db changes, no entry generator modification

Goal:
1. Define a per-symbol cooldown dedup rule (spec only, not implemented in runtime)
2. Apply it retroactively to CP-039D production_universe_dataset() in-memory
3. Re-run binary WF validation (same method as CP-040A) on the deduped dataset
4. Compare WF accuracy: original vs deduped
5. Determine if dedup alone closes the gap to the 0.60 PASS gate
"""
import json
import sys
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score

sys.path.insert(0, ".")
from ml_engine import _production_universe_dataset, fit_train_only_preprocessor, transform_with_train_preprocessor

REPORT_JSON = "reports/cp046_dedup_simulation.json"
TRAIN_WINDOW = 500
TEST_WINDOW = 100
COOLDOWN_SECONDS = 300  # same window used in CP-045 cluster detection
PASS_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.55

def dedup_keep_first(df, symbol_col="symbol", ts_col="timestamp", cooldown_s=COOLDOWN_SECONDS):
    """
    Spec: for each symbol, if a new row's timestamp is within cooldown_s
    of the previously KEPT row for that symbol, drop it (keep first-in-window only).
    This simulates a per-symbol cooldown that would have existed in the entry generator.
    """
    df = df.sort_values(ts_col).reset_index(drop=True)
    keep_mask = np.ones(len(df), dtype=bool)
    last_kept_ts = {}

    for idx, row in df.iterrows():
        sym = row[symbol_col]
        ts = row[ts_col]
        if sym in last_kept_ts:
            delta = (ts - last_kept_ts[sym]).total_seconds()
            if delta <= cooldown_s:
                keep_mask[idx] = False
                continue
        last_kept_ts[sym] = ts

    return df[keep_mask].reset_index(drop=True)

def run_binary_wf(ds, label):
    """Run binary WF validation, same method as CP-040A."""
    ds = ds.sort_values("timestamp").reset_index(drop=True)
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})
    total_rows = len(ds)

    if total_rows < TRAIN_WINDOW + TEST_WINDOW:
        return {
            "label": label,
            "rows": total_rows,
            "verdict": "INSUFFICIENT_DATA",
            "folds": [],
            "avg_accuracy": None,
        }

    folds = []
    start = 0
    fold_id = 1

    while start + TRAIN_WINDOW + TEST_WINDOW <= total_rows:
        train = ds.iloc[start : start + TRAIN_WINDOW].copy()
        test = ds.iloc[start + TRAIN_WINDOW : start + TRAIN_WINDOW + TEST_WINDOW].copy()
        start += TEST_WINDOW

        train_labels = set(train["target_binary"].unique())
        test_labels = set(test["target_binary"].unique())
        unseen = test_labels - train_labels
        class_coverage = "INVALID_CLASS_COVERAGE" if unseen else "OK"

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
            except Exception as e:
                print(f"  [{label}] Fold {fold_id} ERROR: {e}")

        folds.append({
            "fold_id": fold_id,
            "train_rows": len(train),
            "test_rows": len(test),
            "fold_class_coverage": class_coverage,
            "binary_accuracy": round(acc, 4) if acc is not None else None,
            "bin_test_dist": test["target_binary"].value_counts().to_dict(),
        })
        status = f"acc={acc:.3f}" if acc is not None else "SKIP"
        print(f"  [{label}] Fold {fold_id}: {status} [{class_coverage}]")
        fold_id += 1

    valid_accs = [f["binary_accuracy"] for f in folds
                  if f["binary_accuracy"] is not None and f["fold_class_coverage"] == "OK"]
    avg_valid = float(np.mean(valid_accs)) if valid_accs else None

    if avg_valid is None:
        verdict = "FAIL"
    elif avg_valid >= PASS_THRESHOLD:
        verdict = "PASS"
    elif avg_valid >= REVIEW_THRESHOLD:
        verdict = "REVIEW"
    else:
        verdict = "FAIL"

    return {
        "label": label,
        "rows": total_rows,
        "total_folds": len(folds),
        "avg_accuracy": round(avg_valid, 4) if avg_valid else None,
        "verdict": verdict,
        "folds": folds,
    }

def run():
    print("=== CP-046: Dedup-Aware Dataset Simulation & WF Re-Validation ===\n")

    print("[1] Loading original CP-039D dataset...")
    ds_original = _production_universe_dataset()
    ds_original["timestamp"] = pd.to_datetime(ds_original["timestamp"], utc=True)
    print(f"    Original rows: {len(ds_original)}")
    print(f"    Original target dist: {ds_original['target'].value_counts().to_dict()}\n")

    print(f"[2] Applying per-symbol cooldown dedup spec (cooldown={COOLDOWN_SECONDS}s)...")
    print(f"    Spec: for each symbol, drop rows within {COOLDOWN_SECONDS}s of the last KEPT row")
    print(f"    (only applied within-source; historical_outcomes and internal_paper_trades")
    print(f"     are deduped independently to avoid artificial cross-source merging)\n")

    deduped_parts = []
    for source in ds_original["source_artifact"].unique():
        part = ds_original[ds_original["source_artifact"] == source].copy()
        deduped_part = dedup_keep_first(part)
        print(f"    {source}: {len(part)} -> {len(deduped_part)} rows "
              f"(removed {len(part) - len(deduped_part)})")
        deduped_parts.append(deduped_part)

    ds_deduped = pd.concat(deduped_parts, ignore_index=True)
    ds_deduped = ds_deduped.sort_values("timestamp").reset_index(drop=True)
    print(f"\n    Total deduped rows: {len(ds_deduped)} (removed {len(ds_original) - len(ds_deduped)} total)")
    print(f"    Deduped target dist: {ds_deduped['target'].value_counts().to_dict()}\n")

    print("[3] Running binary WF on ORIGINAL dataset...")
    result_original = run_binary_wf(ds_original, "ORIGINAL")
    print()

    print("[4] Running binary WF on DEDUPED dataset...")
    result_deduped = run_binary_wf(ds_deduped, "DEDUPED")
    print()

    print("=== COMPARISON ===")
    print(f"Original: rows={result_original['rows']}, folds={result_original.get('total_folds')}, "
          f"avg_acc={result_original['avg_accuracy']}, verdict={result_original['verdict']}")
    print(f"Deduped:  rows={result_deduped['rows']}, folds={result_deduped.get('total_folds')}, "
          f"avg_acc={result_deduped['avg_accuracy']}, verdict={result_deduped['verdict']}")

    delta = None
    if result_original["avg_accuracy"] and result_deduped["avg_accuracy"]:
        delta = round(result_deduped["avg_accuracy"] - result_original["avg_accuracy"], 4)
        print(f"\nDelta (deduped - original): {delta:+.4f}")

    # Verdict on whether dedup alone solves the gate problem
    if result_deduped["avg_accuracy"] and result_deduped["avg_accuracy"] >= PASS_THRESHOLD:
        gate_verdict = "DEDUP_SUFFICIENT"
        gate_reason = "Deduped dataset alone reaches PASS gate (>=0.60). Dedup is a viable primary fix."
    elif delta is not None and delta > 0.02:
        gate_verdict = "DEDUP_HELPS_PARTIALLY"
        gate_reason = f"Dedup improves accuracy by {delta:+.4f} but does not alone reach PASS gate. Combine with other fixes (e.g. CP-040B window sensitivity, more data)."
    elif delta is not None and delta <= 0:
        gate_verdict = "DEDUP_NOT_SUFFICIENT"
        gate_reason = "Dedup does not meaningfully improve WF accuracy. Root cause of WF instability lies elsewhere (e.g. feature quality, regime coverage, insufficient unique signal diversity)."
    else:
        gate_verdict = "INCONCLUSIVE"
        gate_reason = "Could not compute a reliable delta (insufficient folds after dedup)."

    print(f"\n=== GATE VERDICT: {gate_verdict} ===")
    print(f"Reason: {gate_reason}")

    report = {
        "cp_id": "CP-046",
        "title": "Dedup-Aware Dataset Simulation & WF Re-Validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "paper_only": True,
        "dedup_spec": {
            "method": "per-symbol cooldown, keep-first-in-window",
            "cooldown_seconds": COOLDOWN_SECONDS,
            "applied_independently_per_source": True,
            "note": "This is a SPEC SIMULATION only. No entry generator code was modified."
        },
        "original": result_original,
        "deduped": result_deduped,
        "delta_accuracy": delta,
        "gate_verdict": gate_verdict,
        "gate_reason": gate_reason,
        "next_steps": [
            "If DEDUP_SUFFICIENT: design real-time per-symbol cooldown in entry generator (separate CP, runtime change, requires explicit approval)",
            "If DEDUP_HELPS_PARTIALLY: combine dedup with CP-040B window sensitivity audit",
            "If DEDUP_NOT_SUFFICIENT: investigate feature quality / regime coverage / data volume as primary WF blockers",
        ],
        "notes": [
            "CP-046 is READ-ONLY. No runtime, execution, or DB changes made.",
            "No model retraining/promotion performed against live system.",
            "Dedup applied in-memory only for simulation purposes."
        ],
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nReport: {REPORT_JSON}")
    return report

if __name__ == "__main__":
    run()
