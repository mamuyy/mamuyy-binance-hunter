"""
CP-056: Extended Evidence Window / More Rows Simulation
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Follow up CP-055 HIGH_PRECISION_LOW_COVERAGE_REVIEW.

Question:
Is the low coverage caused by the fixed 500/100 WF window, and can alternative
evidence windows produce enough high-precision selected rows?

Focus:
1. Simulate multiple train/test windows
2. Re-test abstention policies prob_win >= 0.70 / 0.75 / 0.80
3. Measure precision, selected rows, recall, false positives, and fold stability
4. Check whether any policy reaches:
   - precision >= 0.75
   - selected rows >= 100
   - stable enough across folds
5. No runtime threshold change.
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

REPORT_JSON = "reports/cp056_extended_evidence_window_simulation.json"

WINDOW_CONFIGS = [
    {"label": "wf_300_50", "train_window": 300, "test_window": 50},
    {"label": "wf_400_50", "train_window": 400, "test_window": 50},
    {"label": "wf_400_75", "train_window": 400, "test_window": 75},
    {"label": "wf_400_100", "train_window": 400, "test_window": 100},
    {"label": "wf_500_100_baseline", "train_window": 500, "test_window": 100},
    {"label": "wf_600_100", "train_window": 600, "test_window": 100},
    {"label": "wf_700_100", "train_window": 700, "test_window": 100},
    {"label": "wf_800_100", "train_window": 800, "test_window": 100},
]

PROB_THRESHOLDS = [0.65, 0.70, 0.75, 0.80, 0.85]
SOURCE_SCORE_GATES = [None, 85, 90, 95]

TARGET_PRECISION = 0.75
HIGH_PRECISION = 0.80
MIN_TOTAL_SELECTED_FOR_EVIDENCE = 100
MIN_SELECTED_PER_FOLD_FOR_STABILITY = 5
MIN_FOLDS_FOR_RELIABLE_WINDOW = 3

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def value_counts_dict(series):
    if series is None or len(series) == 0:
        return {}
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}

def safe_round(value, ndigits=4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), ndigits)

def build_predictions(ds, config):
    train_window = config["train_window"]
    test_window = config["test_window"]
    label = config["label"]

    all_eval = []
    baseline_folds = []
    start = 0
    fold_id = 1

    while start + train_window + test_window <= len(ds):
        train = ds.iloc[start:start + train_window].copy()
        test = ds.iloc[start + train_window:start + train_window + test_window].copy()
        start += test_window

        if train["target_binary"].nunique() < 2:
            baseline_folds.append({
                "fold": fold_id,
                "status": "SKIPPED_SINGLE_CLASS_TRAIN",
                "rows": int(len(test)),
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
        prob = model.predict_proba(x_test)
        classes = list(model.classes_)
        win_idx = classes.index("WIN") if "WIN" in classes else None
        prob_win = prob[:, win_idx] if win_idx is not None else np.zeros(len(test))

        eval_frame = test[[
            "timestamp",
            "symbol",
            "source_artifact",
            "regime_name",
            "score",
            "target",
            "target_binary",
            "volume_spike",
            "pressure_score",
            "squeeze_probability",
            "regime_score",
            "taker_delta",
            "funding_zscore",
            "oi_expansion_rate",
        ]].copy()

        eval_frame["window_label"] = label
        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["baseline_predicted"] = list(pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]

        acc = safe_round(accuracy_score(eval_frame["actual"], eval_frame["baseline_predicted"]))
        cm = confusion_matrix(eval_frame["actual"], eval_frame["baseline_predicted"], labels=["LOSS", "WIN"]).tolist()

        fp = eval_frame[(eval_frame["actual"] == "LOSS") & (eval_frame["baseline_predicted"] == "WIN")]
        fn = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "LOSS")]
        tp = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "WIN")]
        predicted_win = eval_frame[eval_frame["baseline_predicted"] == "WIN"]

        baseline_folds.append({
            "fold": fold_id,
            "status": "OK",
            "rows": int(len(eval_frame)),
            "baseline_accuracy": acc,
            "baseline_predicted_win_rows": int(len(predicted_win)),
            "baseline_true_positive_win": int(len(tp)),
            "baseline_false_positive_loss_to_win": int(len(fp)),
            "baseline_false_negative_win_to_loss": int(len(fn)),
            "baseline_predicted_win_precision": safe_round(len(tp) / len(predicted_win)) if len(predicted_win) else None,
            "actual_distribution": value_counts_dict(eval_frame["actual"]),
            "confusion_matrix_labels": ["LOSS", "WIN"],
            "confusion_matrix": cm,
        })

        all_eval.append(eval_frame)
        fold_id += 1

    full_eval = pd.concat(all_eval, ignore_index=True) if all_eval else pd.DataFrame()
    return full_eval, baseline_folds

def evaluate_policy(full_eval, prob_threshold, source_score_gate=None):
    if source_score_gate is None:
        policy_label = f"prob_win_gte_{prob_threshold:.2f}"
        mask = full_eval["prob_win"] >= prob_threshold
    else:
        policy_label = f"prob_win_gte_{prob_threshold:.2f}_score_gte_{source_score_gate}"
        mask = (full_eval["prob_win"] >= prob_threshold) & (full_eval["score"] >= source_score_gate)

    selected = full_eval[mask].copy()

    total_rows = int(len(full_eval))
    total_actual_win = int((full_eval["actual"] == "WIN").sum())
    baseline_fp = int(((full_eval["baseline_predicted"] == "WIN") & (full_eval["actual"] == "LOSS")).sum())
    baseline_predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"]
    baseline_tp = baseline_predicted_win[baseline_predicted_win["actual"] == "WIN"]

    selected_rows = int(len(selected))
    selected_win = int((selected["actual"] == "WIN").sum()) if selected_rows else 0
    selected_loss = int((selected["actual"] == "LOSS").sum()) if selected_rows else 0

    precision = selected_win / selected_rows if selected_rows else None
    recall = selected_win / total_actual_win if total_actual_win else None
    coverage = selected_rows / total_rows if total_rows else None
    fp_reduction = (baseline_fp - selected_loss) / baseline_fp if baseline_fp else None

    per_fold = []
    for fold_id, g in full_eval.groupby("fold"):
        gmask = mask.loc[g.index]
        gsel = g[gmask].copy()

        fold_actual_win = int((g["actual"] == "WIN").sum())
        fold_selected_rows = int(len(gsel))
        fold_selected_win = int((gsel["actual"] == "WIN").sum()) if fold_selected_rows else 0
        fold_selected_loss = int((gsel["actual"] == "LOSS").sum()) if fold_selected_rows else 0

        fold_precision = fold_selected_win / fold_selected_rows if fold_selected_rows else None
        fold_recall = fold_selected_win / fold_actual_win if fold_actual_win else None

        per_fold.append({
            "fold": int(fold_id),
            "selected_rows": fold_selected_rows,
            "actual_win_selected": fold_selected_win,
            "actual_loss_false_positive": fold_selected_loss,
            "precision": safe_round(fold_precision),
            "recall": safe_round(fold_recall),
            "actual_distribution_selected": value_counts_dict(gsel["actual"]),
            "source_distribution_selected": value_counts_dict(gsel["source_artifact"]),
            "regime_distribution_selected": value_counts_dict(gsel["regime_name"]),
        })

    folds_with_min_selection = [
        f for f in per_fold
        if f["selected_rows"] >= MIN_SELECTED_PER_FOLD_FOR_STABILITY
    ]

    fold_precisions = [
        f["precision"] for f in folds_with_min_selection
        if f["precision"] is not None
    ]

    folds_precision_pass = [
        f for f in folds_with_min_selection
        if f["precision"] is not None and f["precision"] >= TARGET_PRECISION
    ]

    window_fold_count = int(full_eval["fold"].nunique())

    stable_all_folds_min_selection = len(folds_with_min_selection) == window_fold_count
    stable_precision_all_min_folds = (
        len(folds_with_min_selection) > 0 and
        len(folds_precision_pass) == len(folds_with_min_selection)
    )

    return {
        "policy_label": policy_label,
        "prob_threshold": prob_threshold,
        "source_score_gate": source_score_gate,
        "selected_rows": selected_rows,
        "coverage": safe_round(coverage),
        "actual_win_selected": selected_win,
        "actual_loss_false_positive": selected_loss,
        "missed_win": int(total_actual_win - selected_win),
        "precision": safe_round(precision),
        "recall": safe_round(recall),
        "false_positive_reduction_vs_baseline": safe_round(fp_reduction),
        "baseline_false_positive_loss_to_win": baseline_fp,
        "baseline_predicted_win_rows": int(len(baseline_predicted_win)),
        "baseline_true_positive_win": int(len(baseline_tp)),
        "selected_source_distribution": value_counts_dict(selected["source_artifact"]) if selected_rows else {},
        "selected_regime_distribution": value_counts_dict(selected["regime_name"]) if selected_rows else {},
        "folds_with_min_selection": int(len(folds_with_min_selection)),
        "folds_precision_pass_count": int(len(folds_precision_pass)),
        "worst_fold_precision_with_min_selection": safe_round(min(fold_precisions)) if fold_precisions else None,
        "avg_fold_precision_with_min_selection": safe_round(sum(fold_precisions) / len(fold_precisions)) if fold_precisions else None,
        "stable_all_folds_min_selection": stable_all_folds_min_selection,
        "stable_precision_all_min_folds": stable_precision_all_min_folds,
        "phase3_evidence_selected_count_pass": selected_rows >= MIN_TOTAL_SELECTED_FOR_EVIDENCE,
        "per_fold": per_fold,
    }

def evaluate_window(ds, config):
    full_eval, baseline_folds = build_predictions(ds, config)

    if full_eval.empty:
        return {
            "window_label": config["label"],
            "train_window": config["train_window"],
            "test_window": config["test_window"],
            "status": "INSUFFICIENT_DATA",
            "fold_count": 0,
            "rows_evaluated": 0,
            "policies": [],
        }

    policies = []
    for prob_threshold in PROB_THRESHOLDS:
        for source_score_gate in SOURCE_SCORE_GATES:
            policies.append(evaluate_policy(full_eval, prob_threshold, source_score_gate))

    baseline_accuracy = safe_round(accuracy_score(full_eval["actual"], full_eval["baseline_predicted"]))
    baseline_fp = int(((full_eval["baseline_predicted"] == "WIN") & (full_eval["actual"] == "LOSS")).sum())
    baseline_fn = int(((full_eval["baseline_predicted"] == "LOSS") & (full_eval["actual"] == "WIN")).sum())
    baseline_predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"]
    baseline_tp = baseline_predicted_win[baseline_predicted_win["actual"] == "WIN"]
    baseline_precision = len(baseline_tp) / len(baseline_predicted_win) if len(baseline_predicted_win) else None

    evidence_viable = [
        p for p in policies
        if p["precision"] is not None
        and p["precision"] >= TARGET_PRECISION
        and p["phase3_evidence_selected_count_pass"]
        and p["stable_all_folds_min_selection"]
        and p["stable_precision_all_min_folds"]
    ]

    high_precision_low_coverage = [
        p for p in policies
        if p["precision"] is not None
        and p["precision"] >= HIGH_PRECISION
        and not p["phase3_evidence_selected_count_pass"]
        and p["selected_rows"] >= 20
    ]

    best_precision = sorted(
        [p for p in policies if p["precision"] is not None and p["selected_rows"] >= 20],
        key=lambda x: (x["precision"], x["selected_rows"], x["actual_win_selected"]),
        reverse=True,
    )[:10]

    best_evidence_like = sorted(
        [p for p in policies if p["selected_rows"] >= MIN_TOTAL_SELECTED_FOR_EVIDENCE and p["precision"] is not None],
        key=lambda x: (x["precision"], x["actual_win_selected"], x["selected_rows"]),
        reverse=True,
    )[:10]

    return {
        "window_label": config["label"],
        "train_window": config["train_window"],
        "test_window": config["test_window"],
        "status": "OK",
        "fold_count": int(full_eval["fold"].nunique()),
        "rows_evaluated": int(len(full_eval)),
        "actual_distribution": value_counts_dict(full_eval["actual"]),
        "source_distribution": value_counts_dict(full_eval["source_artifact"]),
        "regime_distribution": value_counts_dict(full_eval["regime_name"]),
        "baseline_accuracy": baseline_accuracy,
        "baseline_false_positive_loss_to_win": baseline_fp,
        "baseline_false_negative_win_to_loss": baseline_fn,
        "baseline_predicted_win_rows": int(len(baseline_predicted_win)),
        "baseline_true_positive_win": int(len(baseline_tp)),
        "baseline_predicted_win_precision": safe_round(baseline_precision),
        "baseline_folds": baseline_folds,
        "policies": policies,
        "evidence_viable_policies": evidence_viable,
        "high_precision_low_coverage_policies": high_precision_low_coverage,
        "best_precision_policies_top10": best_precision,
        "best_evidence_like_policies_top10": best_evidence_like,
    }

def main():
    print("=== CP-056: Extended Evidence Window / More Rows Simulation ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    print(f"Dataset rows: {len(ds)}")
    print("Target:", value_counts_dict(ds["target_binary"]))

    windows = []
    for config in WINDOW_CONFIGS:
        required = config["train_window"] + config["test_window"]
        if len(ds) < required:
            print(f"{config['label']}: SKIP rows {len(ds)} < required {required}")
            windows.append({
                "window_label": config["label"],
                "train_window": config["train_window"],
                "test_window": config["test_window"],
                "status": "INSUFFICIENT_ROWS",
                "required_rows": required,
                "rows_available": int(len(ds)),
            })
            continue

        result = evaluate_window(ds, config)
        windows.append(result)

        print(
            f"{config['label']}: folds={result.get('fold_count')} "
            f"rows_eval={result.get('rows_evaluated')} "
            f"baseline_acc={result.get('baseline_accuracy')} "
            f"baseline_precision={result.get('baseline_predicted_win_precision')}"
        )

        best = result.get("best_precision_policies_top10", [])[:3]
        print("  Best precision:", [
            (
                p["policy_label"],
                p["precision"],
                p["selected_rows"],
                p["actual_win_selected"],
                p["actual_loss_false_positive"],
                p["recall"],
            )
            for p in best
        ])

        evidence_like = result.get("best_evidence_like_policies_top10", [])[:3]
        print("  Best evidence-like:", [
            (
                p["policy_label"],
                p["precision"],
                p["selected_rows"],
                p["actual_win_selected"],
                p["actual_loss_false_positive"],
                p["recall"],
            )
            for p in evidence_like
        ])

    evidence_viable_all = []
    high_precision_low_coverage_all = []
    best_evidence_like_all = []

    for window in windows:
        if window.get("status") != "OK":
            continue

        for p in window.get("evidence_viable_policies", []):
            evidence_viable_all.append({
                "window_label": window["window_label"],
                **p,
            })

        for p in window.get("high_precision_low_coverage_policies", []):
            high_precision_low_coverage_all.append({
                "window_label": window["window_label"],
                **p,
            })

        for p in window.get("best_evidence_like_policies_top10", []):
            best_evidence_like_all.append({
                "window_label": window["window_label"],
                **p,
            })

    best_evidence_like_all = sorted(
        best_evidence_like_all,
        key=lambda x: (x["precision"] or 0, x["actual_win_selected"], x["selected_rows"]),
        reverse=True,
    )[:15]

    high_precision_low_coverage_all = sorted(
        high_precision_low_coverage_all,
        key=lambda x: (x["precision"] or 0, x["selected_rows"], x["actual_win_selected"]),
        reverse=True,
    )[:15]

    if evidence_viable_all:
        verdict = "EXTENDED_WINDOW_EVIDENCE_VIABLE_REVIEW"
        reason = (
            "At least one extended-window simulation meets precision, selected-count, "
            "and stability criteria. Review-only; no promotion or threshold change authorized."
        )
    elif best_evidence_like_all:
        best = best_evidence_like_all[0]
        verdict = "EXTENDED_WINDOW_NOT_SUFFICIENT"
        reason = (
            f"No policy meets full evidence criteria. Best evidence-like policy is "
            f"{best['window_label']}::{best['policy_label']} with precision={best['precision']} "
            f"and selected_rows={best['selected_rows']}."
        )
    elif high_precision_low_coverage_all:
        best = high_precision_low_coverage_all[0]
        verdict = "HIGH_PRECISION_REMAINS_LOW_COVERAGE"
        reason = (
            f"High precision remains possible via {best['window_label']}::{best['policy_label']} "
            f"but selected_rows={best['selected_rows']} remains below evidence threshold."
        )
    else:
        verdict = "NO_HIGH_PRECISION_EVIDENCE_POLICY"
        reason = "No tested extended evidence window produced a viable high-precision policy."

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "dataset_rows": int(len(ds)),
        "dataset_target_distribution": value_counts_dict(ds["target_binary"]),
        "target_precision": TARGET_PRECISION,
        "high_precision": HIGH_PRECISION,
        "min_total_selected_for_evidence": MIN_TOTAL_SELECTED_FOR_EVIDENCE,
        "min_selected_per_fold_for_stability": MIN_SELECTED_PER_FOLD_FOR_STABILITY,
        "min_folds_for_reliable_window": MIN_FOLDS_FOR_RELIABLE_WINDOW,
        "windows": windows,
        "evidence_viable_policies_all_windows": evidence_viable_all,
        "high_precision_low_coverage_all_windows_top15": high_precision_low_coverage_all,
        "best_evidence_like_policies_all_windows_top15": best_evidence_like_all,
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
    print("Verdict:", verdict)
    print("Reason:", reason)
    print("Evidence viable policies:", len(evidence_viable_all))
    print("High precision low coverage policies:", len(high_precision_low_coverage_all))
    print("Best evidence-like policies:", [
        (
            p["window_label"],
            p["policy_label"],
            p["precision"],
            p["selected_rows"],
            p["actual_win_selected"],
            p["actual_loss_false_positive"],
            p["recall"],
        )
        for p in best_evidence_like_all[:8]
    ])
    print("High precision low coverage:", [
        (
            p["window_label"],
            p["policy_label"],
            p["precision"],
            p["selected_rows"],
            p["actual_win_selected"],
            p["actual_loss_false_positive"],
            p["recall"],
        )
        for p in high_precision_low_coverage_all[:8]
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
