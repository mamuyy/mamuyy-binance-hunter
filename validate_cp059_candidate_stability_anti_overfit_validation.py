"""
CP-059: Candidate Stability / Anti-Overfit Validation
READ-ONLY - no runtime rule, no threshold change, no model promotion, no execution/live change

Goal:
Validate whether the CP-058 best candidate is stable outside the original 500/100 WF baseline.

Candidate from CP-058:
Reject predicted WIN when:
- squeeze_probability <= 35
- funding_zscore >= -0.410338

Context:
- CP-058 baseline predicted WIN precision: 0.6084
- CP-058 candidate kept precision: 0.7785
- CP-058 candidate kept rows: 158
- CP-058 candidate FP removed: 73.08%
- CP-058 candidate TP loss: 39.11%

Question:
Is this candidate robust across alternate train/test windows, folds, and regimes,
or is it overfit to the 500/100 baseline?

This is evidence only. It does NOT authorize runtime implementation.
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

REPORT_JSON = "reports/cp059_candidate_stability_anti_overfit_validation.json"

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

CANDIDATE = {
    "name": "cp058_best_reject_squeeze_lte_35_and_funding_gte_minus_0_410338",
    "squeeze_lte": 35.0,
    "funding_gte": -0.410338,
}

# Stability criteria are intentionally strict enough to detect overfit.
WINDOW_PASS_PRECISION = 0.70
WINDOW_PASS_MIN_KEPT_ROWS = 50
WINDOW_PASS_MIN_FP_REMOVED = 0.40
WINDOW_PASS_MAX_TP_LOSS = 0.50
WINDOW_PASS_MIN_NET = 0.05

ROBUST_MIN_PASS_WINDOWS = 4
ROBUST_MIN_PASS_RATE = 0.60
ROBUST_WORST_PRECISION = 0.65
ROBUST_AVG_PRECISION = 0.70

def transform(frame, preprocessor):
    try:
        return transform_with_train_preprocessor(frame, preprocessor)
    except TypeError:
        return transform_with_train_preprocessor(preprocessor, frame)

def value_counts_dict(series):
    if series is None or len(series) == 0:
        return {}
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}

def safe_float(value, ndigits=6):
    if value is None or pd.isna(value):
        return None
    return round(float(value), ndigits)

def safe_rate(num, den, ndigits=4):
    if den == 0:
        return None
    return round(float(num / den), ndigits)

def run_window(ds, config):
    label = config["label"]
    train_window = config["train_window"]
    test_window = config["test_window"]

    if len(ds) < train_window + test_window:
        return {
            "window_label": label,
            "status": "INSUFFICIENT_ROWS",
            "rows_available": int(len(ds)),
            "required_rows": int(train_window + test_window),
        }

    all_eval = []
    folds = []
    start = 0
    fold_id = 1

    while start + train_window + test_window <= len(ds):
        train = ds.iloc[start:start + train_window].copy()
        test = ds.iloc[start + train_window:start + train_window + test_window].copy()
        start += test_window

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

        cols = [
            "timestamp",
            "symbol",
            "source_artifact",
            "regime_name",
            "score",
            "target",
            "target_binary",
            "squeeze_probability",
            "funding_zscore",
            "taker_delta",
            "pressure_score",
            "regime_score",
            "oi_expansion_rate",
            "volume_spike",
        ]
        cols = [c for c in cols if c in test.columns]

        eval_frame = test[cols].copy()
        eval_frame["window_label"] = label
        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["baseline_predicted"] = list(pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]

        cm = confusion_matrix(eval_frame["actual"], eval_frame["baseline_predicted"], labels=["LOSS", "WIN"]).tolist()
        acc = safe_float(accuracy_score(eval_frame["actual"], eval_frame["baseline_predicted"]), 4)

        predicted_win = eval_frame[eval_frame["baseline_predicted"] == "WIN"]
        tp = predicted_win[predicted_win["actual"] == "WIN"]
        fp = predicted_win[predicted_win["actual"] == "LOSS"]
        fn = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "LOSS")]

        folds.append({
            "fold": fold_id,
            "status": "OK",
            "rows": int(len(eval_frame)),
            "baseline_accuracy": acc,
            "baseline_predicted_win_rows": int(len(predicted_win)),
            "baseline_tp_win": int(len(tp)),
            "baseline_fp_loss_to_win": int(len(fp)),
            "baseline_fn_win_to_loss": int(len(fn)),
            "baseline_predicted_win_precision": safe_rate(len(tp), len(predicted_win)),
            "actual_distribution": value_counts_dict(eval_frame["actual"]),
            "confusion_matrix_labels": ["LOSS", "WIN"],
            "confusion_matrix": cm,
        })

        all_eval.append(eval_frame)
        fold_id += 1

    if not all_eval:
        return {
            "window_label": label,
            "status": "NO_VALID_FOLDS",
            "fold_count": 0,
        }

    full_eval = pd.concat(all_eval, ignore_index=True)
    predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"].copy()

    if predicted_win.empty:
        return {
            "window_label": label,
            "status": "NO_PREDICTED_WIN",
            "fold_count": int(full_eval["fold"].nunique()),
            "rows_evaluated": int(len(full_eval)),
            "folds": folds,
        }

    squeeze = pd.to_numeric(predicted_win["squeeze_probability"], errors="coerce").fillna(0.0)
    funding = pd.to_numeric(predicted_win["funding_zscore"], errors="coerce").fillna(0.0)

    reject_mask = (
        (squeeze <= CANDIDATE["squeeze_lte"]) &
        (funding >= CANDIDATE["funding_gte"])
    )

    rejected = predicted_win[reject_mask].copy()
    kept = predicted_win[~reject_mask].copy()

    base_tp = int((predicted_win["actual"] == "WIN").sum())
    base_fp = int((predicted_win["actual"] == "LOSS").sum())
    kept_tp = int((kept["actual"] == "WIN").sum()) if len(kept) else 0
    kept_fp = int((kept["actual"] == "LOSS").sum()) if len(kept) else 0
    rejected_tp = int((rejected["actual"] == "WIN").sum()) if len(rejected) else 0
    rejected_fp = int((rejected["actual"] == "LOSS").sum()) if len(rejected) else 0

    baseline_precision = safe_rate(base_tp, len(predicted_win))
    kept_precision = safe_rate(kept_tp, len(kept))
    fp_removed_rate = safe_rate(rejected_fp, base_fp)
    tp_loss_rate = safe_rate(rejected_tp, base_tp)
    precision_gain = None
    if baseline_precision is not None and kept_precision is not None:
        precision_gain = safe_float(kept_precision - baseline_precision, 4)

    net_fp_minus_tp = None
    if fp_removed_rate is not None and tp_loss_rate is not None:
        net_fp_minus_tp = safe_float(fp_removed_rate - tp_loss_rate, 4)

    per_fold_candidate = []
    for fold_id, g in predicted_win.groupby("fold"):
        g_squeeze = pd.to_numeric(g["squeeze_probability"], errors="coerce").fillna(0.0)
        g_funding = pd.to_numeric(g["funding_zscore"], errors="coerce").fillna(0.0)

        g_reject_mask = (
            (g_squeeze <= CANDIDATE["squeeze_lte"]) &
            (g_funding >= CANDIDATE["funding_gte"])
        )

        g_rejected = g[g_reject_mask].copy()
        g_kept = g[~g_reject_mask].copy()

        g_base_tp = int((g["actual"] == "WIN").sum())
        g_base_fp = int((g["actual"] == "LOSS").sum())
        g_kept_tp = int((g_kept["actual"] == "WIN").sum()) if len(g_kept) else 0
        g_kept_fp = int((g_kept["actual"] == "LOSS").sum()) if len(g_kept) else 0
        g_rejected_tp = int((g_rejected["actual"] == "WIN").sum()) if len(g_rejected) else 0
        g_rejected_fp = int((g_rejected["actual"] == "LOSS").sum()) if len(g_rejected) else 0

        per_fold_candidate.append({
            "fold": int(fold_id),
            "baseline_predicted_win_rows": int(len(g)),
            "baseline_tp": g_base_tp,
            "baseline_fp": g_base_fp,
            "kept_rows": int(len(g_kept)),
            "kept_tp": g_kept_tp,
            "kept_fp": g_kept_fp,
            "kept_precision": safe_rate(g_kept_tp, len(g_kept)),
            "rejected_rows": int(len(g_rejected)),
            "rejected_tp": g_rejected_tp,
            "rejected_fp": g_rejected_fp,
            "fp_removed_rate": safe_rate(g_rejected_fp, g_base_fp),
            "tp_loss_rate": safe_rate(g_rejected_tp, g_base_tp),
        })

    fold_precisions = [
        x["kept_precision"]
        for x in per_fold_candidate
        if x["kept_precision"] is not None and x["kept_rows"] >= 10
    ]
    fold_tp_losses = [x["tp_loss_rate"] for x in per_fold_candidate if x["tp_loss_rate"] is not None]
    fold_fp_removed = [x["fp_removed_rate"] for x in per_fold_candidate if x["fp_removed_rate"] is not None]

    window_pass = (
        kept_precision is not None
        and kept_precision >= WINDOW_PASS_PRECISION
        and len(kept) >= WINDOW_PASS_MIN_KEPT_ROWS
        and (fp_removed_rate or 0) >= WINDOW_PASS_MIN_FP_REMOVED
        and (tp_loss_rate or 1) <= WINDOW_PASS_MAX_TP_LOSS
        and (net_fp_minus_tp or -999) >= WINDOW_PASS_MIN_NET
    )

    return {
        "window_label": label,
        "status": "OK",
        "train_window": int(train_window),
        "test_window": int(test_window),
        "fold_count": int(full_eval["fold"].nunique()),
        "rows_evaluated": int(len(full_eval)),
        "baseline_accuracy": safe_float(accuracy_score(full_eval["actual"], full_eval["baseline_predicted"]), 4),
        "baseline_predicted_win_rows": int(len(predicted_win)),
        "baseline_tp_win": base_tp,
        "baseline_fp_loss_to_win": base_fp,
        "baseline_predicted_win_precision": baseline_precision,
        "candidate_name": CANDIDATE["name"],
        "candidate_rejected_rows": int(len(rejected)),
        "candidate_rejected_tp": rejected_tp,
        "candidate_rejected_fp": rejected_fp,
        "candidate_kept_rows": int(len(kept)),
        "candidate_kept_tp": kept_tp,
        "candidate_kept_fp": kept_fp,
        "candidate_kept_precision": kept_precision,
        "candidate_precision_gain": precision_gain,
        "candidate_fp_removed_rate": fp_removed_rate,
        "candidate_tp_loss_rate": tp_loss_rate,
        "candidate_net_fp_removed_minus_tp_lost": net_fp_minus_tp,
        "candidate_source_distribution_after_keep": value_counts_dict(kept["source_artifact"]) if len(kept) and "source_artifact" in kept.columns else {},
        "candidate_regime_distribution_after_keep": value_counts_dict(kept["regime_name"]) if len(kept) and "regime_name" in kept.columns else {},
        "candidate_per_fold": per_fold_candidate,
        "candidate_worst_fold_precision_kept_rows_gte_10": safe_float(min(fold_precisions), 4) if fold_precisions else None,
        "candidate_avg_fold_precision_kept_rows_gte_10": safe_float(sum(fold_precisions) / len(fold_precisions), 4) if fold_precisions else None,
        "candidate_max_fold_tp_loss_rate": safe_float(max(fold_tp_losses), 4) if fold_tp_losses else None,
        "candidate_min_fold_fp_removed_rate": safe_float(min(fold_fp_removed), 4) if fold_fp_removed else None,
        "candidate_window_pass": bool(window_pass),
        "folds": folds,
    }

def main():
    print("=== CP-059: Candidate Stability / Anti-Overfit Validation ===")

    ds = _production_universe_dataset()
    ds["timestamp"] = pd.to_datetime(ds["timestamp"], utc=True)
    ds = ds.sort_values("timestamp").reset_index(drop=True).copy()
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    window_results = []
    for config in WINDOW_CONFIGS:
        result = run_window(ds, config)
        window_results.append(result)

        if result.get("status") == "OK":
            print(
                f"{result['window_label']}: "
                f"folds={result['fold_count']} "
                f"baseline_precision={result['baseline_predicted_win_precision']} "
                f"kept_precision={result['candidate_kept_precision']} "
                f"kept_rows={result['candidate_kept_rows']} "
                f"fp_removed={result['candidate_fp_removed_rate']} "
                f"tp_loss={result['candidate_tp_loss_rate']} "
                f"pass={result['candidate_window_pass']}"
            )
        else:
            print(f"{result['window_label']}: {result.get('status')}")

    ok_windows = [w for w in window_results if w.get("status") == "OK"]
    pass_windows = [w for w in ok_windows if w.get("candidate_window_pass")]

    precision_values = [
        w["candidate_kept_precision"]
        for w in ok_windows
        if w.get("candidate_kept_precision") is not None
    ]
    precision_gains = [
        w["candidate_precision_gain"]
        for w in ok_windows
        if w.get("candidate_precision_gain") is not None
    ]
    tp_losses = [
        w["candidate_tp_loss_rate"]
        for w in ok_windows
        if w.get("candidate_tp_loss_rate") is not None
    ]
    fp_removeds = [
        w["candidate_fp_removed_rate"]
        for w in ok_windows
        if w.get("candidate_fp_removed_rate") is not None
    ]

    pass_rate = safe_rate(len(pass_windows), len(ok_windows))
    avg_precision = safe_float(sum(precision_values) / len(precision_values), 4) if precision_values else None
    worst_precision = safe_float(min(precision_values), 4) if precision_values else None
    avg_precision_gain = safe_float(sum(precision_gains) / len(precision_gains), 4) if precision_gains else None
    avg_tp_loss = safe_float(sum(tp_losses) / len(tp_losses), 4) if tp_losses else None
    max_tp_loss = safe_float(max(tp_losses), 4) if tp_losses else None
    avg_fp_removed = safe_float(sum(fp_removeds) / len(fp_removeds), 4) if fp_removeds else None
    min_fp_removed = safe_float(min(fp_removeds), 4) if fp_removeds else None

    if (
        len(pass_windows) >= ROBUST_MIN_PASS_WINDOWS
        and (pass_rate or 0) >= ROBUST_MIN_PASS_RATE
        and worst_precision is not None
        and worst_precision >= ROBUST_WORST_PRECISION
        and avg_precision is not None
        and avg_precision >= ROBUST_AVG_PRECISION
    ):
        verdict = "CANDIDATE_STABILITY_CONFIRMED_REVIEW"
        reason = (
            f"CP-058 candidate passed stability criteria across {len(pass_windows)}/{len(ok_windows)} windows. "
            "Still review-only; runtime implementation requires a separate governance CP."
        )
    elif pass_windows:
        verdict = "CANDIDATE_PARTIAL_STABILITY_REVIEW"
        reason = (
            f"CP-058 candidate passed only {len(pass_windows)}/{len(ok_windows)} tested windows. "
            "Signal exists but robustness is not fully confirmed."
        )
    else:
        verdict = "CANDIDATE_OVERFIT_RISK_REVIEW"
        reason = (
            "CP-058 candidate failed stability criteria across alternate windows, indicating possible overfit."
        )

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "candidate": CANDIDATE,
        "dataset_rows": int(len(ds)),
        "dataset_target_distribution": value_counts_dict(ds["target_binary"]),
        "window_pass_criteria": {
            "precision_gte": WINDOW_PASS_PRECISION,
            "min_kept_rows": WINDOW_PASS_MIN_KEPT_ROWS,
            "min_fp_removed": WINDOW_PASS_MIN_FP_REMOVED,
            "max_tp_loss": WINDOW_PASS_MAX_TP_LOSS,
            "min_net_fp_minus_tp": WINDOW_PASS_MIN_NET,
        },
        "robustness_criteria": {
            "min_pass_windows": ROBUST_MIN_PASS_WINDOWS,
            "min_pass_rate": ROBUST_MIN_PASS_RATE,
            "worst_precision_gte": ROBUST_WORST_PRECISION,
            "avg_precision_gte": ROBUST_AVG_PRECISION,
        },
        "summary": {
            "ok_windows": len(ok_windows),
            "pass_windows": len(pass_windows),
            "pass_rate": pass_rate,
            "avg_candidate_kept_precision": avg_precision,
            "worst_candidate_kept_precision": worst_precision,
            "avg_precision_gain": avg_precision_gain,
            "avg_tp_loss_rate": avg_tp_loss,
            "max_tp_loss_rate": max_tp_loss,
            "avg_fp_removed_rate": avg_fp_removed,
            "min_fp_removed_rate": min_fp_removed,
        },
        "window_results": window_results,
        "governance": {
            "read_only_validation": True,
            "runtime_rule_changed": False,
            "threshold_changed": False,
            "model_promoted": False,
            "live_unlock": False,
            "execution_changed": False,
        },
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n=== RESULT ===")
    print("Verdict:", verdict)
    print("Reason:", reason)
    print("Candidate:", CANDIDATE)
    print("Summary:", report["summary"])
    print("Window results:", [
        (
            w.get("window_label"),
            w.get("candidate_kept_precision"),
            w.get("candidate_kept_rows"),
            w.get("candidate_fp_removed_rate"),
            w.get("candidate_tp_loss_rate"),
            w.get("candidate_precision_gain"),
            w.get("candidate_window_pass"),
        )
        for w in ok_windows
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
