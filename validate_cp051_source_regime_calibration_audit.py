"""
CP-051: Source / Regime Calibration Audit
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Determine whether CP-050 error concentration is caused by:
1. source mismatch: historical_outcomes vs internal_paper_trades
2. regime hotspot: TRENDING BULL
3. calibration issue: model confidence/probability not aligned with actual labels

This is diagnostic only. No threshold, promotion, execution, or live changes.
"""
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp051_source_regime_calibration_audit.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
PASS_THRESHOLD = 0.60

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def safe_brier(actual_binary, prob_win):
    try:
        return round(float(brier_score_loss(actual_binary, prob_win)), 6)
    except Exception:
        return None

def safe_logloss(actual_binary, prob_win):
    try:
        return round(float(log_loss(actual_binary, prob_win, labels=[0, 1])), 6)
    except Exception:
        return None

def summarize_group(frame, group_col):
    out = {}
    if group_col not in frame.columns:
        return out

    for key, g in frame.groupby(group_col, dropna=False):
        if len(g) == 0:
            continue

        actual_bin = (g["actual"] == "WIN").astype(int)
        pred_bin = (g["predicted"] == "WIN").astype(int)

        out[str(key)] = {
            "rows": int(len(g)),
            "accuracy": round(float(accuracy_score(g["actual"], g["predicted"])), 4),
            "actual_win_rate": round(float(actual_bin.mean()), 4),
            "predicted_win_rate": round(float(pred_bin.mean()), 4),
            "avg_prob_win": round(float(g["prob_win"].mean()), 4),
            "calibration_gap_avg_prob_minus_actual": round(float(g["prob_win"].mean() - actual_bin.mean()), 4),
            "brier_score": safe_brier(actual_bin, g["prob_win"]),
            "log_loss": safe_logloss(actual_bin, g["prob_win"]),
            "actual_distribution": g["actual"].value_counts().to_dict(),
            "predicted_distribution": g["predicted"].value_counts().to_dict(),
            "error_count": int((g["actual"] != g["predicted"]).sum()),
        }
    return out

def calibration_bins(frame, group_col=None):
    data = frame.copy()
    if group_col is not None and group_col in data.columns:
        groups = data.groupby(group_col, dropna=False)
    else:
        groups = [("ALL", data)]

    result = {}
    for key, g in groups:
        if g.empty:
            continue
        g = g.copy()
        g["actual_win"] = (g["actual"] == "WIN").astype(int)
        g["prob_bin"] = pd.cut(
            g["prob_win"],
            bins=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            include_lowest=True,
        )
        bins = []
        for b, bg in g.groupby("prob_bin", observed=False):
            if len(bg) == 0:
                continue
            bins.append({
                "bin": str(b),
                "rows": int(len(bg)),
                "avg_prob_win": round(float(bg["prob_win"].mean()), 4),
                "actual_win_rate": round(float(bg["actual_win"].mean()), 4),
                "gap": round(float(bg["prob_win"].mean() - bg["actual_win"].mean()), 4),
            })
        result[str(key)] = bins
    return result

def main():
    print("=== CP-051: Source / Regime Calibration Audit ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    folds = []
    all_eval = []
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
        classes = list(model.classes_)
        prob = model.predict_proba(x_test)
        win_idx = classes.index("WIN") if "WIN" in classes else None
        prob_win = prob[:, win_idx] if win_idx is not None else [0.0] * len(test)

        eval_frame = test[[
            "timestamp", "symbol", "source_artifact", "regime_name", "score", "target", "target_binary"
        ]].copy()
        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["predicted"] = list(pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]
        eval_frame["is_error"] = eval_frame["actual"] != eval_frame["predicted"]

        acc = round(float(accuracy_score(eval_frame["actual"], eval_frame["predicted"])), 4)

        fold_summary = {
            "fold": fold_id,
            "status": "OK",
            "accuracy": acc,
            "rows": int(len(eval_frame)),
            "source_summary": summarize_group(eval_frame, "source_artifact"),
            "regime_summary": summarize_group(eval_frame, "regime_name"),
            "source_regime_summary": summarize_group(
                eval_frame.assign(source_regime=eval_frame["source_artifact"].astype(str) + "::" + eval_frame["regime_name"].astype(str)),
                "source_regime",
            ),
        }

        folds.append(fold_summary)
        all_eval.append(eval_frame)

        print(f"Fold {fold_id}: acc={acc:.3f}")
        print("  Source:", {k: v["accuracy"] for k, v in fold_summary["source_summary"].items()})
        print("  Regime:", {k: v["accuracy"] for k, v in fold_summary["regime_summary"].items()})

        fold_id += 1

    full_eval = pd.concat(all_eval, ignore_index=True) if all_eval else pd.DataFrame()
    valid_acc = [f["accuracy"] for f in folds if f.get("accuracy") is not None]
    avg_acc = round(sum(valid_acc) / len(valid_acc), 4) if valid_acc else None

    if full_eval.empty:
        verdict = "INSUFFICIENT_DATA"
        reason = "No valid evaluation folds."
        global_source_summary = {}
        global_regime_summary = {}
        global_source_regime_summary = {}
        bins_all = {}
        bins_by_source = {}
        bins_by_regime = {}
        hotspot = {}
    else:
        global_source_summary = summarize_group(full_eval, "source_artifact")
        global_regime_summary = summarize_group(full_eval, "regime_name")
        full_eval["source_regime"] = full_eval["source_artifact"].astype(str) + "::" + full_eval["regime_name"].astype(str)
        global_source_regime_summary = summarize_group(full_eval, "source_regime")

        bins_all = calibration_bins(full_eval)
        bins_by_source = calibration_bins(full_eval, "source_artifact")
        bins_by_regime = calibration_bins(full_eval, "regime_name")

        hotspot = {
            k: v for k, v in sorted(
                global_source_regime_summary.items(),
                key=lambda kv: (kv[1]["error_count"], kv[1]["rows"]),
                reverse=True,
            )[:10]
        }

        verdict = "REVIEW"
        reason = (
            "Calibration/source/regime mismatch remains below PASS gate. "
            "Use hotspot table to decide whether source weighting, regime calibration, "
            "or label-source separation is warranted."
        )

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "rows": int(len(ds)),
        "avg_accuracy": avg_acc,
        "pass_threshold": PASS_THRESHOLD,
        "dataset_source_distribution": ds["source_artifact"].value_counts().to_dict(),
        "dataset_regime_distribution": ds["regime_name"].value_counts().to_dict(),
        "folds": folds,
        "global_source_summary": global_source_summary,
        "global_regime_summary": global_regime_summary,
        "global_source_regime_summary_top_error_hotspots": hotspot,
        "calibration_bins_all": bins_all,
        "calibration_bins_by_source": bins_by_source,
        "calibration_bins_by_regime": bins_by_regime,
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
    print("Global source accuracy:", {k: v["accuracy"] for k, v in global_source_summary.items()})
    print("Global regime accuracy:", {k: v["accuracy"] for k, v in global_regime_summary.items()})
    print("Top source/regime hotspots:", {k: v["error_count"] for k, v in hotspot.items()})
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
