"""
CP-055: High-Precision Abstention Policy Simulation
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Simulate an abstention policy after CP-054 confirmed false-positive bias.

Context:
- CP-054 baseline false positives: LOSS->WIN = 130
- CP-054 baseline false negatives: WIN->LOSS = 75
- Best precision scenario was model_prob_win >= 0.80, but coverage was small.

Focus:
1. Simulate trade-only-when model_prob_win >= threshold
2. Measure precision vs coverage vs missed WIN
3. Check per-fold stability
4. Check whether selected trade count is enough for Phase 3 evidence
5. Do NOT change runtime thresholds
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

REPORT_JSON = "reports/cp055_high_precision_abstention_policy_simulation.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100

PROB_THRESHOLDS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
SOURCE_SCORE_GATES = [None, 85, 90, 95, 98]

TARGET_PRECISION = 0.75
HIGH_PRECISION = 0.80
MIN_TOTAL_SELECTED_FOR_EVIDENCE = 100
MIN_SELECTED_PER_FOLD_FOR_STABILITY = 5

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

def run_wf_predictions():
    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    all_eval = []
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

        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["baseline_predicted"] = list(pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]

        baseline_acc = safe_round(accuracy_score(eval_frame["actual"], eval_frame["baseline_predicted"]))
        cm = confusion_matrix(eval_frame["actual"], eval_frame["baseline_predicted"], labels=["LOSS", "WIN"]).tolist()

        fp = eval_frame[(eval_frame["actual"] == "LOSS") & (eval_frame["baseline_predicted"] == "WIN")]
        fn = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "LOSS")]
        tp = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "WIN")]
        predicted_win = eval_frame[eval_frame["baseline_predicted"] == "WIN"]

        folds.append({
            "fold": fold_id,
            "status": "OK",
            "rows": int(len(eval_frame)),
            "baseline_accuracy": baseline_acc,
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
        print(f"Fold {fold_id}: baseline_acc={baseline_acc:.3f} baseline_FP={len(fp)} baseline_FN={len(fn)}")
        fold_id += 1

    full_eval = pd.concat(all_eval, ignore_index=True) if all_eval else pd.DataFrame()
    return full_eval, folds

def evaluate_policy(full_eval, prob_threshold, source_score_gate=None):
    if source_score_gate is None:
        label = f"prob_win_gte_{prob_threshold:.2f}"
        mask = full_eval["prob_win"] >= prob_threshold
    else:
        label = f"prob_win_gte_{prob_threshold:.2f}_score_gte_{source_score_gate}"
        mask = (full_eval["prob_win"] >= prob_threshold) & (full_eval["score"] >= source_score_gate)

    selected = full_eval[mask].copy()
    total_rows = int(len(full_eval))
    total_actual_win = int((full_eval["actual"] == "WIN").sum())
    total_actual_loss = int((full_eval["actual"] == "LOSS").sum())

    baseline_fp = int(((full_eval["baseline_predicted"] == "WIN") & (full_eval["actual"] == "LOSS")).sum())
    baseline_predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"]
    baseline_tp = baseline_predicted_win[baseline_predicted_win["actual"] == "WIN"]

    selected_win = int((selected["actual"] == "WIN").sum())
    selected_loss = int((selected["actual"] == "LOSS").sum())
    selected_rows = int(len(selected))

    precision = selected_win / selected_rows if selected_rows else None
    recall = selected_win / total_actual_win if total_actual_win else None
    coverage = selected_rows / total_rows if total_rows else None
    missed_win = total_actual_win - selected_win
    abstained_rows = total_rows - selected_rows
    fp_reduction = (baseline_fp - selected_loss) / baseline_fp if baseline_fp else None

    per_fold = []
    for fold_id, g in full_eval.groupby("fold"):
        gsel = g[mask.loc[g.index]].copy()
        fold_actual_win = int((g["actual"] == "WIN").sum())
        fold_selected_win = int((gsel["actual"] == "WIN").sum())
        fold_selected_loss = int((gsel["actual"] == "LOSS").sum())
        fold_selected_rows = int(len(gsel))
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

    folds_with_selection = [f for f in per_fold if f["selected_rows"] > 0]
    folds_with_min_selection = [
        f for f in per_fold
        if f["selected_rows"] >= MIN_SELECTED_PER_FOLD_FOR_STABILITY
    ]
    folds_precision_pass = [
        f for f in folds_with_min_selection
        if f["precision"] is not None and f["precision"] >= TARGET_PRECISION
    ]

    precision_values = [
        f["precision"] for f in per_fold
        if f["precision"] is not None and f["selected_rows"] >= MIN_SELECTED_PER_FOLD_FOR_STABILITY
    ]

    worst_fold_precision = min(precision_values) if precision_values else None
    avg_fold_precision = sum(precision_values) / len(precision_values) if precision_values else None

    return {
        "label": label,
        "prob_threshold": prob_threshold,
        "source_score_gate": source_score_gate,
        "selected_rows": selected_rows,
        "coverage": safe_round(coverage),
        "abstained_rows": int(abstained_rows),
        "actual_win_selected": selected_win,
        "actual_loss_false_positive": selected_loss,
        "total_actual_win_available": total_actual_win,
        "total_actual_loss_available": total_actual_loss,
        "missed_win": int(missed_win),
        "precision": safe_round(precision),
        "recall": safe_round(recall),
        "false_positive_reduction_vs_baseline": safe_round(fp_reduction),
        "baseline_false_positive_loss_to_win": baseline_fp,
        "baseline_predicted_win_rows": int(len(baseline_predicted_win)),
        "baseline_true_positive_win": int(len(baseline_tp)),
        "selected_source_distribution": value_counts_dict(selected["source_artifact"]),
        "selected_regime_distribution": value_counts_dict(selected["regime_name"]),
        "selected_symbol_top20": dict(list(value_counts_dict(selected["symbol"]).items())[:20]),
        "folds_with_selection": int(len(folds_with_selection)),
        "folds_with_min_selection": int(len(folds_with_min_selection)),
        "folds_precision_pass_count": int(len(folds_precision_pass)),
        "worst_fold_precision_with_min_selection": safe_round(worst_fold_precision),
        "avg_fold_precision_with_min_selection": safe_round(avg_fold_precision),
        "per_fold": per_fold,
        "phase3_evidence_selected_count_pass": selected_rows >= MIN_TOTAL_SELECTED_FOR_EVIDENCE,
        "stable_all_folds_min_selection": len(folds_with_min_selection) == int(full_eval["fold"].nunique()),
        "stable_precision_all_min_folds": (
            len(folds_with_min_selection) > 0 and
            len(folds_precision_pass) == len(folds_with_min_selection)
        ),
    }

def main():
    print("=== CP-055: High-Precision Abstention Policy Simulation ===")

    full_eval, baseline_folds = run_wf_predictions()

    if full_eval.empty:
        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": "INSUFFICIENT_DATA",
            "reason": "No valid WF prediction rows available.",
            "governance": {
                "read_only_validation": True,
                "runtime_execution_changed": False,
                "model_promoted": False,
                "live_unlock": False,
                "threshold_changed": False,
            },
        }
    else:
        policies = []
        for prob_threshold in PROB_THRESHOLDS:
            for source_score_gate in SOURCE_SCORE_GATES:
                policies.append(evaluate_policy(full_eval, prob_threshold, source_score_gate))

        baseline_fp = int(((full_eval["baseline_predicted"] == "WIN") & (full_eval["actual"] == "LOSS")).sum())
        baseline_fn = int(((full_eval["baseline_predicted"] == "LOSS") & (full_eval["actual"] == "WIN")).sum())
        baseline_predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"]
        baseline_tp = baseline_predicted_win[baseline_predicted_win["actual"] == "WIN"]
        baseline_precision = len(baseline_tp) / len(baseline_predicted_win) if len(baseline_predicted_win) else None
        baseline_acc = accuracy_score(full_eval["actual"], full_eval["baseline_predicted"])

        viable = [
            p for p in policies
            if p["selected_rows"] >= 20 and p["precision"] is not None
        ]

        best_precision = sorted(
            viable,
            key=lambda x: (
                x["precision"],
                x["actual_win_selected"],
                x["selected_rows"],
                -(x["actual_loss_false_positive"]),
            ),
            reverse=True,
        )[:15]

        evidence_viable = [
            p for p in policies
            if p["phase3_evidence_selected_count_pass"]
            and p["stable_all_folds_min_selection"]
            and p["stable_precision_all_min_folds"]
            and p["precision"] is not None
            and p["precision"] >= TARGET_PRECISION
        ]

        high_precision_low_coverage = [
            p for p in policies
            if p["precision"] is not None
            and p["precision"] >= HIGH_PRECISION
            and p["selected_rows"] < MIN_TOTAL_SELECTED_FOR_EVIDENCE
            and p["selected_rows"] >= 20
        ]

        stable_candidates = [
            p for p in policies
            if p["stable_all_folds_min_selection"]
            and p["precision"] is not None
            and p["precision"] >= TARGET_PRECISION
        ]

        if evidence_viable:
            verdict = "ABSTENTION_POLICY_EVIDENCE_VIABLE_REVIEW"
            reason = (
                "At least one simulated abstention policy passes precision, stability, "
                "and minimum selected-count checks. Still review-only; no runtime change authorized."
            )
        elif high_precision_low_coverage:
            best = sorted(
                high_precision_low_coverage,
                key=lambda x: (x["precision"], x["actual_win_selected"]),
                reverse=True,
            )[0]
            verdict = "HIGH_PRECISION_LOW_COVERAGE_REVIEW"
            reason = (
                f"High precision is achievable in simulation via {best['label']} "
                f"(precision={best['precision']}, selected_rows={best['selected_rows']}), "
                "but selected count is below evidence threshold, so promotion remains blocked."
            )
        elif stable_candidates:
            best = sorted(
                stable_candidates,
                key=lambda x: (x["precision"], x["selected_rows"]),
                reverse=True,
            )[0]
            verdict = "STABLE_BUT_INSUFFICIENT_EVIDENCE_REVIEW"
            reason = (
                f"Stable candidate exists via {best['label']}, but evidence requirements "
                "are not fully satisfied."
            )
        else:
            verdict = "ABSTENTION_NOT_STABLE_ENOUGH"
            reason = (
                "No simulated abstention policy provides enough precision, coverage, and "
                "per-fold stability for Phase 3 evidence."
            )

        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "reason": reason,
            "rows_evaluated": int(len(full_eval)),
            "fold_count": int(full_eval["fold"].nunique()),
            "baseline_accuracy": safe_round(baseline_acc),
            "baseline_false_positive_loss_to_win": baseline_fp,
            "baseline_false_negative_win_to_loss": baseline_fn,
            "baseline_predicted_win_rows": int(len(baseline_predicted_win)),
            "baseline_true_positive_win": int(len(baseline_tp)),
            "baseline_predicted_win_precision": safe_round(baseline_precision),
            "target_precision": TARGET_PRECISION,
            "high_precision": HIGH_PRECISION,
            "min_total_selected_for_evidence": MIN_TOTAL_SELECTED_FOR_EVIDENCE,
            "min_selected_per_fold_for_stability": MIN_SELECTED_PER_FOLD_FOR_STABILITY,
            "baseline_folds": baseline_folds,
            "policies": policies,
            "best_precision_policies_top15": best_precision,
            "evidence_viable_policies": evidence_viable,
            "high_precision_low_coverage_policies": high_precision_low_coverage,
            "stable_candidates": stable_candidates,
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
    print("Verdict:", report["verdict"])
    print("Reason:", report["reason"])
    print("Baseline accuracy:", report.get("baseline_accuracy"))
    print("Baseline FP LOSS->WIN:", report.get("baseline_false_positive_loss_to_win"))
    print("Baseline WIN precision:", report.get("baseline_predicted_win_precision"))
    print("Evidence viable policies:", len(report.get("evidence_viable_policies", [])))
    print("High precision low coverage policies:", len(report.get("high_precision_low_coverage_policies", [])))
    print("Best precision policies:", [
        (
            p["label"],
            p["precision"],
            p["selected_rows"],
            p["actual_win_selected"],
            p["actual_loss_false_positive"],
            p["recall"],
            p["worst_fold_precision_with_min_selection"],
        )
        for p in report.get("best_precision_policies_top15", [])[:8]
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
