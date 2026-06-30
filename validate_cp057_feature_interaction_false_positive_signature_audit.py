"""
CP-057: Feature Interaction / False-Positive Signature Audit
READ-ONLY - no retrain artifact write, no model promotion, no execution/live change

Goal:
Follow up CP-054 / CP-055 / CP-056.

Question:
What feature patterns distinguish false positives from true positives?

Context:
- CP-054 confirmed false-positive bias: LOSS->WIN=130 vs WIN->LOSS=75
- CP-055 showed high precision possible only with low coverage
- CP-056 showed extended evidence windows do not solve precision/coverage tradeoff

Focus:
1. Compare true positive WIN vs false positive LOSS->WIN signatures
2. Find suspicious feature ranges where false positives concentrate
3. Simulate reject-only filters over predicted WIN candidates
4. Test single-feature and two-feature reject rules
5. Evidence only. No runtime rule or threshold change.
"""
import itertools
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

REPORT_JSON = "reports/cp057_feature_interaction_false_positive_signature_audit.json"

TRAIN_WINDOW = 500
TEST_WINDOW = 100

FEATURES = [
    "score",
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "regime_score",
]

CORE_FEATURES = [
    "score",
    "pressure_score",
    "regime_score",
    "squeeze_probability",
    "taker_delta",
    "funding_zscore",
    "oi_expansion_rate",
]

SINGLE_QUANTILES = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
PAIR_QUANTILES = [0.25, 0.50, 0.75]

TARGET_PRECISION = 0.75
MIN_KEPT_SELECTED = 80
MIN_FP_REMOVED_RATE = 0.25
MAX_TP_LOSS_RATE = 0.45

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

def numeric_summary(frame, feature):
    if frame.empty or feature not in frame.columns:
        return {}
    s = pd.to_numeric(frame[feature], errors="coerce").fillna(0.0).astype(float)
    return {
        "count": int(len(s)),
        "mean": safe_float(s.mean()),
        "std": safe_float(s.std()),
        "min": safe_float(s.min()),
        "p10": safe_float(s.quantile(0.10)),
        "p25": safe_float(s.quantile(0.25)),
        "median": safe_float(s.median()),
        "p75": safe_float(s.quantile(0.75)),
        "p90": safe_float(s.quantile(0.90)),
        "max": safe_float(s.max()),
        "nonzero_rate": safe_rate(int((s != 0).sum()), len(s)),
    }

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

        cols = [
            "timestamp", "symbol", "source_artifact", "regime_name",
            "score", "target", "target_binary",
            *[f for f in FEATURES if f in test.columns and f not in ["score"]],
        ]

        eval_frame = test[cols].copy()
        eval_frame["fold"] = fold_id
        eval_frame["actual"] = list(y_test)
        eval_frame["baseline_predicted"] = list(pred)
        eval_frame["prob_win"] = [float(x) for x in prob_win]

        acc = safe_float(accuracy_score(eval_frame["actual"], eval_frame["baseline_predicted"]), 4)
        cm = confusion_matrix(eval_frame["actual"], eval_frame["baseline_predicted"], labels=["LOSS", "WIN"]).tolist()

        fp = eval_frame[(eval_frame["actual"] == "LOSS") & (eval_frame["baseline_predicted"] == "WIN")]
        fn = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "LOSS")]
        tp = eval_frame[(eval_frame["actual"] == "WIN") & (eval_frame["baseline_predicted"] == "WIN")]
        predicted_win = eval_frame[eval_frame["baseline_predicted"] == "WIN"]

        folds.append({
            "fold": fold_id,
            "status": "OK",
            "rows": int(len(eval_frame)),
            "baseline_accuracy": acc,
            "predicted_win_rows": int(len(predicted_win)),
            "true_positive_win": int(len(tp)),
            "false_positive_loss_to_win": int(len(fp)),
            "false_negative_win_to_loss": int(len(fn)),
            "predicted_win_precision": safe_rate(len(tp), len(predicted_win)),
            "actual_distribution": value_counts_dict(eval_frame["actual"]),
            "confusion_matrix_labels": ["LOSS", "WIN"],
            "confusion_matrix": cm,
        })

        all_eval.append(eval_frame)
        print(f"Fold {fold_id}: acc={acc:.3f} predWIN={len(predicted_win)} TP={len(tp)} FP={len(fp)} FN={len(fn)}")
        fold_id += 1

    full_eval = pd.concat(all_eval, ignore_index=True) if all_eval else pd.DataFrame()
    return full_eval, folds

def compare_feature_signatures(predicted_win):
    tp = predicted_win[predicted_win["actual"] == "WIN"].copy()
    fp = predicted_win[predicted_win["actual"] == "LOSS"].copy()

    out = {}
    for feature in FEATURES:
        if feature not in predicted_win.columns:
            continue

        tp_summary = numeric_summary(tp, feature)
        fp_summary = numeric_summary(fp, feature)

        tp_mean = tp_summary.get("mean")
        fp_mean = fp_summary.get("mean")
        tp_median = tp_summary.get("median")
        fp_median = fp_summary.get("median")

        mean_delta = None
        median_delta = None
        risk_direction = None

        if tp_mean is not None and fp_mean is not None:
            mean_delta = safe_float(fp_mean - tp_mean)
            risk_direction = "HIGHER_IN_FP" if fp_mean > tp_mean else "LOWER_IN_FP"

        if tp_median is not None and fp_median is not None:
            median_delta = safe_float(fp_median - tp_median)

        out[feature] = {
            "true_positive_summary": tp_summary,
            "false_positive_summary": fp_summary,
            "fp_minus_tp_mean_delta": mean_delta,
            "fp_minus_tp_median_delta": median_delta,
            "risk_direction": risk_direction,
        }

    return out

def quantile_fp_concentration(predicted_win):
    rows = []
    for feature in FEATURES:
        if feature not in predicted_win.columns:
            continue

        s = pd.to_numeric(predicted_win[feature], errors="coerce").fillna(0.0).astype(float)

        try:
            buckets = pd.qcut(s.rank(method="first"), q=5, labels=["Q1_low", "Q2", "Q3", "Q4", "Q5_high"])
        except Exception:
            continue

        temp = predicted_win.copy()
        temp["_bucket"] = buckets.astype(str)

        for bucket, g in temp.groupby("_bucket"):
            predicted_win_rows = len(g)
            fp_rows = int((g["actual"] == "LOSS").sum())
            tp_rows = int((g["actual"] == "WIN").sum())
            rows.append({
                "feature": feature,
                "bucket": str(bucket),
                "rows": int(predicted_win_rows),
                "true_positive_win": tp_rows,
                "false_positive_loss_to_win": fp_rows,
                "fp_rate_in_bucket": safe_rate(fp_rows, predicted_win_rows),
                "tp_rate_in_bucket": safe_rate(tp_rows, predicted_win_rows),
                "feature_min": safe_float(pd.to_numeric(g[feature], errors="coerce").fillna(0.0).min()),
                "feature_max": safe_float(pd.to_numeric(g[feature], errors="coerce").fillna(0.0).max()),
            })

    return sorted(rows, key=lambda x: (x["fp_rate_in_bucket"] or 0, x["false_positive_loss_to_win"]), reverse=True)

def evaluate_reject_rule(predicted_win, reject_mask, rule_label, rule_type):
    total = len(predicted_win)
    base_tp = int((predicted_win["actual"] == "WIN").sum())
    base_fp = int((predicted_win["actual"] == "LOSS").sum())
    base_precision = safe_rate(base_tp, total)

    rejected = predicted_win[reject_mask].copy()
    kept = predicted_win[~reject_mask].copy()

    rejected_tp = int((rejected["actual"] == "WIN").sum()) if len(rejected) else 0
    rejected_fp = int((rejected["actual"] == "LOSS").sum()) if len(rejected) else 0
    kept_tp = int((kept["actual"] == "WIN").sum()) if len(kept) else 0
    kept_fp = int((kept["actual"] == "LOSS").sum()) if len(kept) else 0

    kept_precision = safe_rate(kept_tp, len(kept))
    fp_removed_rate = safe_rate(rejected_fp, base_fp)
    tp_loss_rate = safe_rate(rejected_tp, base_tp)
    net_fp_minus_tp = None
    if fp_removed_rate is not None and tp_loss_rate is not None:
        net_fp_minus_tp = safe_float(fp_removed_rate - tp_loss_rate, 4)

    return {
        "rule_label": rule_label,
        "rule_type": rule_type,
        "baseline_predicted_win_rows": int(total),
        "baseline_true_positive_win": base_tp,
        "baseline_false_positive_loss_to_win": base_fp,
        "baseline_precision": base_precision,
        "rejected_rows": int(len(rejected)),
        "rejected_true_positive_win": rejected_tp,
        "rejected_false_positive_loss_to_win": rejected_fp,
        "kept_rows": int(len(kept)),
        "kept_true_positive_win": kept_tp,
        "kept_false_positive_loss_to_win": kept_fp,
        "kept_precision": kept_precision,
        "fp_removed_rate": fp_removed_rate,
        "tp_loss_rate": tp_loss_rate,
        "net_fp_removed_minus_tp_lost": net_fp_minus_tp,
        "selected_source_distribution_after_keep": value_counts_dict(kept["source_artifact"]) if len(kept) else {},
        "selected_regime_distribution_after_keep": value_counts_dict(kept["regime_name"]) if len(kept) else {},
    }

def single_feature_rules(predicted_win):
    rules = []

    for feature in FEATURES + ["prob_win"]:
        if feature not in predicted_win.columns:
            continue

        s = pd.to_numeric(predicted_win[feature], errors="coerce").fillna(0.0).astype(float)
        thresholds = sorted(set([float(s.quantile(q)) for q in SINGLE_QUANTILES]))

        for threshold in thresholds:
            reject_low = s <= threshold
            reject_high = s >= threshold

            rules.append(evaluate_reject_rule(
                predicted_win,
                reject_low,
                f"reject_{feature}_lte_{threshold:.6f}",
                "single_feature_lte",
            ))

            rules.append(evaluate_reject_rule(
                predicted_win,
                reject_high,
                f"reject_{feature}_gte_{threshold:.6f}",
                "single_feature_gte",
            ))

    return rules

def pair_feature_rules(predicted_win):
    rules = []

    usable = [f for f in CORE_FEATURES if f in predicted_win.columns]
    for f1, f2 in itertools.combinations(usable, 2):
        s1 = pd.to_numeric(predicted_win[f1], errors="coerce").fillna(0.0).astype(float)
        s2 = pd.to_numeric(predicted_win[f2], errors="coerce").fillna(0.0).astype(float)

        t1s = sorted(set([float(s1.quantile(q)) for q in PAIR_QUANTILES]))
        t2s = sorted(set([float(s2.quantile(q)) for q in PAIR_QUANTILES]))

        for t1 in t1s:
            for t2 in t2s:
                cases = [
                    (s1 <= t1) & (s2 <= t2), f"reject_{f1}_lte_{t1:.6f}_AND_{f2}_lte_{t2:.6f}",
                    (s1 <= t1) & (s2 >= t2), f"reject_{f1}_lte_{t1:.6f}_AND_{f2}_gte_{t2:.6f}",
                    (s1 >= t1) & (s2 <= t2), f"reject_{f1}_gte_{t1:.6f}_AND_{f2}_lte_{t2:.6f}",
                    (s1 >= t1) & (s2 >= t2), f"reject_{f1}_gte_{t1:.6f}_AND_{f2}_gte_{t2:.6f}",
                ]

                for i in range(0, len(cases), 2):
                    mask = cases[i]
                    label = cases[i + 1]
                    rules.append(evaluate_reject_rule(
                        predicted_win,
                        mask,
                        label,
                        "pair_feature_and",
                    ))

    return rules

def score_probability_context(predicted_win):
    out = {}

    score_gates = [75, 80, 85, 90, 95, 98, 100]
    prob_gates = [0.50, 0.60, 0.70, 0.75, 0.80]

    for score_gate in score_gates:
        for prob_gate in prob_gates:
            subset = predicted_win[
                (pd.to_numeric(predicted_win["score"], errors="coerce").fillna(0.0) >= score_gate) &
                (pd.to_numeric(predicted_win["prob_win"], errors="coerce").fillna(0.0) >= prob_gate)
            ].copy()

            if subset.empty:
                continue

            key = f"score_gte_{score_gate}_prob_gte_{prob_gate:.2f}"
            tp = int((subset["actual"] == "WIN").sum())
            fp = int((subset["actual"] == "LOSS").sum())
            out[key] = {
                "rows": int(len(subset)),
                "true_positive_win": tp,
                "false_positive_loss_to_win": fp,
                "precision": safe_rate(tp, len(subset)),
                "source_distribution": value_counts_dict(subset["source_artifact"]),
                "regime_distribution": value_counts_dict(subset["regime_name"]),
            }

    return out

def main():
    print("=== CP-057: Feature Interaction / False-Positive Signature Audit ===")

    full_eval, folds = run_wf_predictions()

    if full_eval.empty:
        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": "INSUFFICIENT_DATA",
            "reason": "No WF predictions available.",
            "governance": {
                "read_only_validation": True,
                "runtime_execution_changed": False,
                "model_promoted": False,
                "live_unlock": False,
                "threshold_changed": False,
            },
        }
    else:
        predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"].copy()
        tp = predicted_win[predicted_win["actual"] == "WIN"].copy()
        fp = predicted_win[predicted_win["actual"] == "LOSS"].copy()

        baseline_precision = safe_rate(len(tp), len(predicted_win))
        baseline_fp = int(len(fp))
        baseline_tp = int(len(tp))

        feature_signatures = compare_feature_signatures(predicted_win)
        quantile_concentration = quantile_fp_concentration(predicted_win)
        score_prob_context = score_probability_context(predicted_win)

        single_rules = single_feature_rules(predicted_win)
        pair_rules = pair_feature_rules(predicted_win)
        all_rules = single_rules + pair_rules

        viable_rules = [
            r for r in all_rules
            if r["kept_precision"] is not None
            and r["kept_precision"] >= TARGET_PRECISION
            and r["kept_rows"] >= MIN_KEPT_SELECTED
            and (r["fp_removed_rate"] or 0) >= MIN_FP_REMOVED_RATE
            and (r["tp_loss_rate"] or 1) <= MAX_TP_LOSS_RATE
        ]

        precision_improvers = [
            r for r in all_rules
            if r["kept_precision"] is not None
            and baseline_precision is not None
            and r["kept_precision"] > baseline_precision
            and r["kept_rows"] >= 40
        ]

        best_rules = sorted(
            precision_improvers,
            key=lambda x: (
                x["kept_precision"],
                x["kept_rows"],
                x["net_fp_removed_minus_tp_lost"] or -999,
            ),
            reverse=True,
        )[:25]

        best_fp_selective_rules = sorted(
            precision_improvers,
            key=lambda x: (
                x["net_fp_removed_minus_tp_lost"] or -999,
                x["fp_removed_rate"] or 0,
                x["kept_precision"] or 0,
            ),
            reverse=True,
        )[:25]

        if viable_rules:
            verdict = "FALSE_POSITIVE_SIGNATURE_CANDIDATE_REVIEW"
            best = sorted(
                viable_rules,
                key=lambda x: (x["kept_precision"], x["kept_rows"], x["net_fp_removed_minus_tp_lost"] or -999),
                reverse=True,
            )[0]
            reason = (
                f"At least one reject-rule candidate reached target precision and coverage in simulation. "
                f"Best: {best['rule_label']} kept_precision={best['kept_precision']} kept_rows={best['kept_rows']}. "
                "Review-only; no runtime rule authorized."
            )
        elif precision_improvers:
            best = best_rules[0]
            verdict = "WEAK_SIGNATURE_REVIEW"
            reason = (
                f"Some simulated reject rules improve precision but do not satisfy full criteria. "
                f"Best precision improver: {best['rule_label']} kept_precision={best['kept_precision']} kept_rows={best['kept_rows']}."
            )
        else:
            verdict = "NO_USEFUL_SIGNATURE_FOUND"
            reason = "No tested feature interaction produced a useful false-positive signature."

        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "reason": reason,
            "rows_evaluated": int(len(full_eval)),
            "predicted_win_rows": int(len(predicted_win)),
            "baseline_true_positive_win": baseline_tp,
            "baseline_false_positive_loss_to_win": baseline_fp,
            "baseline_predicted_win_precision": baseline_precision,
            "folds": folds,
            "feature_signatures": feature_signatures,
            "quantile_false_positive_concentration_top30": quantile_concentration[:30],
            "score_probability_context": score_prob_context,
            "viable_rules": sorted(
                viable_rules,
                key=lambda x: (x["kept_precision"], x["kept_rows"], x["net_fp_removed_minus_tp_lost"] or -999),
                reverse=True,
            )[:25],
            "best_precision_improver_rules_top25": best_rules,
            "best_fp_selective_rules_top25": best_fp_selective_rules,
            "all_single_feature_rules_count": len(single_rules),
            "all_pair_feature_rules_count": len(pair_rules),
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
    print("Predicted WIN rows:", report.get("predicted_win_rows"))
    print("Baseline TP WIN:", report.get("baseline_true_positive_win"))
    print("Baseline FP LOSS->WIN:", report.get("baseline_false_positive_loss_to_win"))
    print("Baseline WIN precision:", report.get("baseline_predicted_win_precision"))
    print("Viable rules:", len(report.get("viable_rules", [])))
    print("Single feature rules tested:", report.get("all_single_feature_rules_count"))
    print("Pair feature rules tested:", report.get("all_pair_feature_rules_count"))
    print("Top FP concentration:", [
        (
            x["feature"],
            x["bucket"],
            x["fp_rate_in_bucket"],
            x["false_positive_loss_to_win"],
            x["true_positive_win"],
            x["feature_min"],
            x["feature_max"],
        )
        for x in report.get("quantile_false_positive_concentration_top30", [])[:8]
    ])
    print("Best precision rules:", [
        (
            r["rule_label"],
            r["kept_precision"],
            r["kept_rows"],
            r["kept_true_positive_win"],
            r["kept_false_positive_loss_to_win"],
            r["fp_removed_rate"],
            r["tp_loss_rate"],
            r["net_fp_removed_minus_tp_lost"],
        )
        for r in report.get("best_precision_improver_rules_top25", [])[:8]
    ])
    print("Best FP-selective rules:", [
        (
            r["rule_label"],
            r["kept_precision"],
            r["kept_rows"],
            r["fp_removed_rate"],
            r["tp_loss_rate"],
            r["net_fp_removed_minus_tp_lost"],
        )
        for r in report.get("best_fp_selective_rules_top25", [])[:8]
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
