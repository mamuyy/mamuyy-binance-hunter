"""
CP-054: False Positive / Decision Boundary Audit
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Diagnose CP-053 false-positive bias:
- LOSS->WIN = 130
- WIN->LOSS = 75

Focus:
1. Where false positives concentrate by score bucket, source, regime, symbol
2. Whether stricter model probability boundary reduces false positives
3. Whether source score thresholds 75/80/85/90/95 reduce false positives
4. Precision/recall tradeoff for WIN decisions

This is simulation only. It does NOT change runtime thresholds.
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, precision_score, recall_score

sys.path.insert(0, ".")
from ml_engine import (
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp054_false_positive_decision_boundary_audit.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
PASS_THRESHOLD = 0.60

MODEL_PROB_THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
SOURCE_SCORE_THRESHOLDS = [75, 80, 85, 90, 95, 98, 100]

SCORE_BUCKET_BINS = [-np.inf, 75, 80, 85, 90, 95, 98, 100, np.inf]
SCORE_BUCKET_LABELS = ["<75", "75-80", "80-85", "85-90", "90-95", "95-98", "98-100", ">100"]

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def value_counts_dict(series):
    if series is None or len(series) == 0:
        return {}
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}

def safe_float(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)

def selected_win_metrics(frame, mask, label):
    selected = frame[mask].copy()
    total_actual_win = int((frame["actual"] == "WIN").sum())
    total_actual_loss = int((frame["actual"] == "LOSS").sum())

    if selected.empty:
        return {
            "label": label,
            "selected_rows": 0,
            "selection_rate": 0.0,
            "actual_win": 0,
            "actual_loss_false_positive": 0,
            "win_precision": None,
            "win_recall": 0.0 if total_actual_win else None,
            "false_positive_rate_among_selected": None,
            "false_positive_reduction_vs_baseline_fp": None,
            "actual_distribution": {},
            "source_distribution": {},
            "regime_distribution": {},
        }

    actual_win = int((selected["actual"] == "WIN").sum())
    actual_loss = int((selected["actual"] == "LOSS").sum())
    precision = actual_win / len(selected) if len(selected) else None
    recall = actual_win / total_actual_win if total_actual_win else None
    fp_rate = actual_loss / len(selected) if len(selected) else None

    baseline_fp = int(((frame["baseline_predicted"] == "WIN") & (frame["actual"] == "LOSS")).sum())
    fp_reduction = None
    if baseline_fp > 0:
        fp_reduction = (baseline_fp - actual_loss) / baseline_fp

    return {
        "label": label,
        "selected_rows": int(len(selected)),
        "selection_rate": round(float(len(selected) / len(frame)), 4) if len(frame) else None,
        "actual_win": actual_win,
        "actual_loss_false_positive": actual_loss,
        "total_actual_win_available": total_actual_win,
        "total_actual_loss_available": total_actual_loss,
        "win_precision": round(float(precision), 4) if precision is not None else None,
        "win_recall": round(float(recall), 4) if recall is not None else None,
        "false_positive_rate_among_selected": round(float(fp_rate), 4) if fp_rate is not None else None,
        "false_positive_reduction_vs_baseline_fp": round(float(fp_reduction), 4) if fp_reduction is not None else None,
        "avg_prob_win": safe_float(selected["prob_win"].mean()),
        "avg_source_score": safe_float(selected["score"].mean()),
        "actual_distribution": value_counts_dict(selected["actual"]),
        "source_distribution": value_counts_dict(selected["source_artifact"]),
        "regime_distribution": value_counts_dict(selected["regime_name"]),
    }

def summarize_group(frame, group_col):
    out = {}
    if frame.empty or group_col not in frame.columns:
        return out

    for key, g in frame.groupby(group_col, dropna=False):
        predicted_win = g[g["baseline_predicted"] == "WIN"]
        fp = g[(g["baseline_predicted"] == "WIN") & (g["actual"] == "LOSS")]
        tp = g[(g["baseline_predicted"] == "WIN") & (g["actual"] == "WIN")]

        precision = len(tp) / len(predicted_win) if len(predicted_win) else None

        out[str(key)] = {
            "rows": int(len(g)),
            "actual_distribution": value_counts_dict(g["actual"]),
            "baseline_predicted_distribution": value_counts_dict(g["baseline_predicted"]),
            "predicted_win_rows": int(len(predicted_win)),
            "false_positive_loss_to_win": int(len(fp)),
            "true_positive_win": int(len(tp)),
            "predicted_win_precision": round(float(precision), 4) if precision is not None else None,
            "avg_score": safe_float(g["score"].mean()),
            "avg_prob_win": safe_float(g["prob_win"].mean()),
        }
    return out

def model_probability_scenarios(frame):
    scenarios = []
    for threshold in MODEL_PROB_THRESHOLDS:
        mask = frame["prob_win"] >= threshold
        scenarios.append(selected_win_metrics(
            frame,
            mask,
            f"model_prob_win_gte_{threshold:.2f}",
        ))
    return scenarios

def source_score_scenarios(frame):
    scenarios = []
    for threshold in SOURCE_SCORE_THRESHOLDS:
        mask = (frame["score"] >= threshold) & (frame["baseline_predicted"] == "WIN")
        scenarios.append(selected_win_metrics(
            frame,
            mask,
            f"source_score_gte_{threshold}_and_model_predicts_WIN",
        ))
    return scenarios

def combined_scenarios(frame):
    scenarios = []
    for score_threshold in [85, 90, 95]:
        for prob_threshold in [0.55, 0.60, 0.65, 0.70]:
            mask = (frame["score"] >= score_threshold) & (frame["prob_win"] >= prob_threshold)
            scenarios.append(selected_win_metrics(
                frame,
                mask,
                f"score_gte_{score_threshold}_prob_gte_{prob_threshold:.2f}",
            ))
    return scenarios

def run():
    print("=== CP-054: False Positive / Decision Boundary Audit ===")

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

        baseline_pred = model.predict(x_test)
        classes = list(model.classes_)
        prob = model.predict_proba(x_test)
        win_idx = classes.index("WIN") if "WIN" in classes else None
        prob_win = prob[:, win_idx] if win_idx is not None else np.zeros(len(test))

        eval_frame = test[[
            "timestamp", "symbol", "source_artifact", "regime_name", "score",
            "target", "target_binary", "volume_spike", "pressure_score",
            "squeeze_probability", "regime_score", "taker_delta", "funding_zscore",
            "oi_expansion_rate"
        ]].copy()

        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["baseline_predicted"] = list(baseline_pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]
        eval_frame["score_bucket"] = pd.cut(
            pd.to_numeric(eval_frame["score"], errors="coerce").fillna(0.0),
            bins=SCORE_BUCKET_BINS,
            labels=SCORE_BUCKET_LABELS,
        ).astype(str)

        acc = round(float(accuracy_score(eval_frame["actual"], eval_frame["baseline_predicted"])), 4)
        cm = confusion_matrix(eval_frame["actual"], eval_frame["baseline_predicted"], labels=["LOSS", "WIN"]).tolist()

        fp = eval_frame[(eval_frame["actual"] == "LOSS") & (eval_frame["baseline_predicted"] == "WIN")]
        fn = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "LOSS")]
        tp = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "WIN")]
        predicted_win = eval_frame[eval_frame["baseline_predicted"] == "WIN"]

        fold_summary = {
            "fold": fold_id,
            "status": "OK",
            "accuracy": acc,
            "rows": int(len(eval_frame)),
            "confusion_matrix_labels": ["LOSS", "WIN"],
            "confusion_matrix": cm,
            "predicted_win_rows": int(len(predicted_win)),
            "true_positive_win": int(len(tp)),
            "false_positive_loss_to_win": int(len(fp)),
            "false_negative_win_to_loss": int(len(fn)),
            "predicted_win_precision": round(float(len(tp) / len(predicted_win)), 4) if len(predicted_win) else None,
            "win_recall": round(float(len(tp) / max((eval_frame["actual"] == "WIN").sum(), 1)), 4),
            "false_positive_by_score_bucket": value_counts_dict(fp["score_bucket"]),
            "false_positive_by_source": value_counts_dict(fp["source_artifact"]),
            "false_positive_by_regime": value_counts_dict(fp["regime_name"]),
            "false_positive_by_symbol_top20": value_counts_dict(fp["symbol"]).copy(),
            "model_probability_scenarios": model_probability_scenarios(eval_frame),
            "source_score_scenarios": source_score_scenarios(eval_frame),
        }

        folds.append(fold_summary)
        all_eval.append(eval_frame)

        print(f"Fold {fold_id}: acc={acc:.3f} FP LOSS->WIN={len(fp)} FN WIN->LOSS={len(fn)}")
        print("  FP by score bucket:", fold_summary["false_positive_by_score_bucket"])
        print("  FP by regime:", fold_summary["false_positive_by_regime"])

        fold_id += 1

    full_eval = pd.concat(all_eval, ignore_index=True) if all_eval else pd.DataFrame()

    if full_eval.empty:
        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": "INSUFFICIENT_DATA",
            "reason": "No valid WF evaluation rows.",
            "folds": folds,
            "governance": {
                "read_only_validation": True,
                "runtime_execution_changed": False,
                "model_promoted": False,
                "live_unlock": False,
                "threshold_changed": False,
            },
        }
    else:
        baseline_acc = round(float(accuracy_score(full_eval["actual"], full_eval["baseline_predicted"])), 4)
        fp_all = full_eval[(full_eval["actual"] == "LOSS") & (full_eval["baseline_predicted"] == "WIN")]
        fn_all = full_eval[(full_eval["actual"] == "WIN") & (full_eval["baseline_predicted"] == "LOSS")]
        tp_all = full_eval[(full_eval["actual"] == "WIN") & (full_eval["baseline_predicted"] == "WIN")]
        predicted_win_all = full_eval[full_eval["baseline_predicted"] == "WIN"]

        global_prob_scenarios = model_probability_scenarios(full_eval)
        global_score_scenarios = source_score_scenarios(full_eval)
        global_combined_scenarios = combined_scenarios(full_eval)

        viable_scenarios = [
            s for s in global_prob_scenarios + global_score_scenarios + global_combined_scenarios
            if s["selected_rows"] >= 20 and s["win_precision"] is not None
        ]
        best_precision = sorted(
            viable_scenarios,
            key=lambda x: (x["win_precision"], x["actual_win"], -x["actual_loss_false_positive"]),
            reverse=True,
        )[:10]

        baseline_precision = len(tp_all) / len(predicted_win_all) if len(predicted_win_all) else None
        baseline_fp = int(len(fp_all))

        best_reducing_fp = [
            s for s in viable_scenarios
            if s["actual_loss_false_positive"] < baseline_fp
        ]
        best_reducing_fp = sorted(
            best_reducing_fp,
            key=lambda x: (
                x["false_positive_reduction_vs_baseline_fp"] or 0,
                x["win_precision"] or 0,
                x["actual_win"],
            ),
            reverse=True,
        )[:10]

        # Conservative verdict: even if a simulated boundary helps, it is not a runtime change.
        if baseline_fp > len(fn_all):
            verdict = "FALSE_POSITIVE_BIAS_CONFIRMED"
            reason = (
                f"False positives dominate baseline WF errors: LOSS->WIN={baseline_fp}, "
                f"WIN->LOSS={len(fn_all)}. Decision boundary tightening may reduce risk, "
                "but this is simulation-only and cannot unlock promotion."
            )
        else:
            verdict = "FALSE_POSITIVE_BIAS_NOT_DOMINANT"
            reason = "False positives do not dominate baseline WF errors."

        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "reason": reason,
            "rows_evaluated": int(len(full_eval)),
            "baseline_accuracy": baseline_acc,
            "baseline_predicted_win_rows": int(len(predicted_win_all)),
            "baseline_true_positive_win": int(len(tp_all)),
            "baseline_false_positive_loss_to_win": int(len(fp_all)),
            "baseline_false_negative_win_to_loss": int(len(fn_all)),
            "baseline_predicted_win_precision": round(float(baseline_precision), 4) if baseline_precision is not None else None,
            "global_false_positive_by_score_bucket": value_counts_dict(fp_all["score_bucket"]),
            "global_false_positive_by_source": value_counts_dict(fp_all["source_artifact"]),
            "global_false_positive_by_regime": value_counts_dict(fp_all["regime_name"]),
            "global_false_positive_by_symbol_top30": dict(list(value_counts_dict(fp_all["symbol"]).items())[:30]),
            "global_source_summary": summarize_group(full_eval, "source_artifact"),
            "global_regime_summary": summarize_group(full_eval, "regime_name"),
            "global_score_bucket_summary": summarize_group(full_eval, "score_bucket"),
            "global_model_probability_scenarios": global_prob_scenarios,
            "global_source_score_scenarios": global_score_scenarios,
            "global_combined_scenarios": global_combined_scenarios,
            "best_precision_scenarios_top10": best_precision,
            "best_false_positive_reduction_scenarios_top10": best_reducing_fp,
            "folds": folds,
            "false_positive_samples": fp_all.sort_values(["prob_win", "score"], ascending=False).head(50).to_dict(orient="records"),
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
    print("Baseline FN WIN->LOSS:", report.get("baseline_false_negative_win_to_loss"))
    print("Baseline predicted WIN precision:", report.get("baseline_predicted_win_precision"))
    print("FP by score bucket:", report.get("global_false_positive_by_score_bucket"))
    print("FP by regime:", report.get("global_false_positive_by_regime"))
    print("Best precision scenarios:", [
        (x["label"], x["win_precision"], x["actual_loss_false_positive"], x["actual_win"])
        for x in report.get("best_precision_scenarios_top10", [])[:5]
    ])
    print("Best FP reduction scenarios:", [
        (x["label"], x["false_positive_reduction_vs_baseline_fp"], x["win_precision"], x["actual_loss_false_positive"], x["actual_win"])
        for x in report.get("best_false_positive_reduction_scenarios_top10", [])[:5]
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    run()
