"""
CP-058: TP-Preservation Rule Refinement Audit
READ-ONLY - no retrain artifact write, no model promotion, no runtime rule, no execution/live change

Goal:
Follow up CP-057 WEAK_SIGNATURE_REVIEW.

Question:
Can we refine false-positive reject rules so they reduce LOSS->WIN while preserving more true positives?

Context:
- CP-057 baseline predicted WIN rows: 332
- Baseline TP WIN: 202
- Baseline FP LOSS->WIN: 130
- Baseline WIN precision: 0.6084
- Best CP-057 precision rule reached precision 0.7596, but TP loss was too high at 0.6089

Focus:
1. Search refined reject-only rules
2. Include single-feature, band-feature, pair-feature, and targeted CP-057-inspired rules
3. Optimize for:
   - higher kept precision
   - lower TP loss
   - meaningful FP removal
   - enough kept rows
4. Evidence only. No runtime filter authorized.
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

REPORT_JSON = "reports/cp058_tp_preservation_rule_refinement_audit.json"

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
    "prob_win",
]

CORE_FEATURES = [
    "score",
    "pressure_score",
    "regime_score",
    "squeeze_probability",
    "taker_delta",
    "funding_zscore",
    "oi_expansion_rate",
    "volume_spike",
    "prob_win",
]

QUANTILES = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

# CP-058 governance targets
STRICT_TARGET_PRECISION = 0.72
SOFT_TARGET_PRECISION = 0.70
MIN_KEPT_ROWS_STRICT = 120
MIN_KEPT_ROWS_SOFT = 150
MAX_TP_LOSS_STRICT = 0.45
MAX_TP_LOSS_SOFT = 0.40
MIN_FP_REMOVED_STRICT = 0.45
MIN_FP_REMOVED_SOFT = 0.40
MIN_NET_FP_MINUS_TP = 0.10

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
        ]

        for f in FEATURES:
            if f in test.columns and f not in cols and f != "prob_win":
                cols.append(f)

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

def rule_metrics(predicted_win, reject_mask, rule_label, rule_type):
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
    kept_coverage = safe_rate(len(kept), total)

    precision_gain = None
    if kept_precision is not None and base_precision is not None:
        precision_gain = safe_float(kept_precision - base_precision, 4)

    net_fp_minus_tp = None
    if fp_removed_rate is not None and tp_loss_rate is not None:
        net_fp_minus_tp = safe_float(fp_removed_rate - tp_loss_rate, 4)

    # Composite ranking: prefer precision improvement, FP selectivity, and TP preservation.
    composite_score = None
    if precision_gain is not None and net_fp_minus_tp is not None and kept_coverage is not None:
        composite_score = safe_float(
            (precision_gain * 2.0) +
            (net_fp_minus_tp * 1.5) +
            (fp_removed_rate or 0) -
            ((tp_loss_rate or 0) * 1.25) +
            (kept_coverage * 0.25),
            6,
        )

    per_fold = []
    for fold_id, g in predicted_win.groupby("fold"):
        fold_mask = reject_mask.loc[g.index]
        fold_rejected = g[fold_mask].copy()
        fold_kept = g[~fold_mask].copy()

        fold_base_tp = int((g["actual"] == "WIN").sum())
        fold_base_fp = int((g["actual"] == "LOSS").sum())
        fold_kept_tp = int((fold_kept["actual"] == "WIN").sum()) if len(fold_kept) else 0
        fold_kept_fp = int((fold_kept["actual"] == "LOSS").sum()) if len(fold_kept) else 0
        fold_rejected_tp = int((fold_rejected["actual"] == "WIN").sum()) if len(fold_rejected) else 0
        fold_rejected_fp = int((fold_rejected["actual"] == "LOSS").sum()) if len(fold_rejected) else 0

        per_fold.append({
            "fold": int(fold_id),
            "baseline_predicted_win_rows": int(len(g)),
            "baseline_tp": fold_base_tp,
            "baseline_fp": fold_base_fp,
            "rejected_rows": int(len(fold_rejected)),
            "rejected_tp": fold_rejected_tp,
            "rejected_fp": fold_rejected_fp,
            "kept_rows": int(len(fold_kept)),
            "kept_tp": fold_kept_tp,
            "kept_fp": fold_kept_fp,
            "kept_precision": safe_rate(fold_kept_tp, len(fold_kept)),
            "fp_removed_rate": safe_rate(fold_rejected_fp, fold_base_fp),
            "tp_loss_rate": safe_rate(fold_rejected_tp, fold_base_tp),
        })

    fold_precisions = [x["kept_precision"] for x in per_fold if x["kept_precision"] is not None and x["kept_rows"] >= 10]
    fold_tp_losses = [x["tp_loss_rate"] for x in per_fold if x["tp_loss_rate"] is not None]
    fold_fp_removed = [x["fp_removed_rate"] for x in per_fold if x["fp_removed_rate"] is not None]

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
        "kept_coverage": kept_coverage,
        "precision_gain": precision_gain,
        "fp_removed_rate": fp_removed_rate,
        "tp_loss_rate": tp_loss_rate,
        "net_fp_removed_minus_tp_lost": net_fp_minus_tp,
        "composite_score": composite_score,
        "source_distribution_after_keep": value_counts_dict(kept["source_artifact"]) if len(kept) else {},
        "regime_distribution_after_keep": value_counts_dict(kept["regime_name"]) if len(kept) else {},
        "per_fold": per_fold,
        "worst_fold_precision_kept_rows_gte_10": safe_float(min(fold_precisions), 4) if fold_precisions else None,
        "avg_fold_precision_kept_rows_gte_10": safe_float(sum(fold_precisions) / len(fold_precisions), 4) if fold_precisions else None,
        "max_fold_tp_loss_rate": safe_float(max(fold_tp_losses), 4) if fold_tp_losses else None,
        "min_fold_fp_removed_rate": safe_float(min(fold_fp_removed), 4) if fold_fp_removed else None,
    }

def thresholds_for(predicted_win, feature):
    if feature not in predicted_win.columns:
        return []

    s = pd.to_numeric(predicted_win[feature], errors="coerce").fillna(0.0).astype(float)
    vals = [float(s.quantile(q)) for q in QUANTILES]
    vals += [float(s.min()), float(s.max())]

    # Add domain-inspired thresholds from CP-057 hotspots
    domain = {
        "regime_score": [0, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100],
        "squeeze_probability": [0, 10, 20, 25, 35, 40, 50, 60, 75, 90],
        "funding_zscore": [-2.0, -1.0, -0.644684, -0.410338, -0.103207, 0.0, 0.25, 0.5, 1.0],
        "oi_expansion_rate": [-2.0, -1.405459, -0.5, -0.141898, 0.0, 0.266625, 0.5, 1.0, 2.0],
        "taker_delta": [-1.0, -0.5, 0.0, 0.154356, 0.5, 1.0],
        "score": [75, 80, 85, 87.12, 89.66, 90, 92.785, 95, 98, 100],
        "volume_spike": [0, 1, 2, 3.288529, 5, 8, 12.403801],
        "prob_win": [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
    }

    vals += domain.get(feature, [])
    low = float(s.min())
    high = float(s.max())
    vals = sorted(set([round(v, 6) for v in vals if np.isfinite(v) and low <= v <= high]))
    return vals

def generate_single_rules(predicted_win):
    rules = []

    for feature in FEATURES:
        if feature not in predicted_win.columns:
            continue

        s = pd.to_numeric(predicted_win[feature], errors="coerce").fillna(0.0).astype(float)
        thresholds = thresholds_for(predicted_win, feature)

        for t in thresholds:
            rules.append(rule_metrics(
                predicted_win,
                s <= t,
                f"reject_{feature}_lte_{t:.6f}",
                "single_lte",
            ))

            rules.append(rule_metrics(
                predicted_win,
                s >= t,
                f"reject_{feature}_gte_{t:.6f}",
                "single_gte",
            ))

        # Band rules: reject only suspicious middle bands instead of everything below/above.
        for low, high in itertools.combinations(thresholds, 2):
            if low >= high:
                continue

            mask = (s >= low) & (s <= high)
            rejected_count = int(mask.sum())

            # Avoid huge brute-noise. Keep bands meaningful but not all-consuming.
            if rejected_count < 10 or rejected_count > int(len(predicted_win) * 0.70):
                continue

            rules.append(rule_metrics(
                predicted_win,
                mask,
                f"reject_{feature}_between_{low:.6f}_and_{high:.6f}",
                "single_between",
            ))

    return rules

def generate_pair_rules(predicted_win):
    rules = []
    usable = [f for f in CORE_FEATURES if f in predicted_win.columns]

    for f1, f2 in itertools.combinations(usable, 2):
        s1 = pd.to_numeric(predicted_win[f1], errors="coerce").fillna(0.0).astype(float)
        s2 = pd.to_numeric(predicted_win[f2], errors="coerce").fillna(0.0).astype(float)

        t1s = thresholds_for(predicted_win, f1)
        t2s = thresholds_for(predicted_win, f2)

        # Keep pair search bounded.
        t1s = t1s[::max(1, len(t1s)//8)] if len(t1s) > 10 else t1s
        t2s = t2s[::max(1, len(t2s)//8)] if len(t2s) > 10 else t2s

        for t1 in t1s:
            for t2 in t2s:
                candidates = [
                    ((s1 <= t1) & (s2 <= t2), f"reject_{f1}_lte_{t1:.6f}_AND_{f2}_lte_{t2:.6f}"),
                    ((s1 <= t1) & (s2 >= t2), f"reject_{f1}_lte_{t1:.6f}_AND_{f2}_gte_{t2:.6f}"),
                    ((s1 >= t1) & (s2 <= t2), f"reject_{f1}_gte_{t1:.6f}_AND_{f2}_lte_{t2:.6f}"),
                    ((s1 >= t1) & (s2 >= t2), f"reject_{f1}_gte_{t1:.6f}_AND_{f2}_gte_{t2:.6f}"),
                ]

                for mask, label in candidates:
                    rejected_count = int(mask.sum())
                    if rejected_count < 10 or rejected_count > int(len(predicted_win) * 0.70):
                        continue

                    rules.append(rule_metrics(
                        predicted_win,
                        mask,
                        label,
                        "pair_and",
                    ))

    return rules

def generate_targeted_rules(predicted_win):
    rules = []

    def s(feature):
        return pd.to_numeric(predicted_win[feature], errors="coerce").fillna(0.0).astype(float)

    available = set(predicted_win.columns)

    targeted = []

    if {"regime_score"}.issubset(available):
        targeted += [
            (s("regime_score") <= 55, "reject_regime_score_lte_55"),
            ((s("regime_score") > 55) & (s("regime_score") <= 60), "reject_regime_score_between_55_60"),
            (s("regime_score") <= 60, "reject_regime_score_lte_60"),
            ((s("regime_score") >= 55) & (s("regime_score") <= 70), "reject_regime_score_between_55_70"),
        ]

    if {"squeeze_probability"}.issubset(available):
        targeted += [
            (s("squeeze_probability") <= 0, "reject_squeeze_probability_lte_0"),
            (s("squeeze_probability") <= 20, "reject_squeeze_probability_lte_20"),
            (s("squeeze_probability") <= 35, "reject_squeeze_probability_lte_35"),
        ]

    if {"funding_zscore"}.issubset(available):
        targeted += [
            (s("funding_zscore") >= -0.410338, "reject_funding_zscore_gte_minus_0_410338"),
            (s("funding_zscore") >= -0.103207, "reject_funding_zscore_gte_minus_0_103207"),
            ((s("funding_zscore") >= -0.410338) & (s("funding_zscore") <= 0), "reject_funding_zscore_between_minus_0_410338_and_0"),
        ]

    if {"volume_spike"}.issubset(available):
        targeted += [
            (s("volume_spike") >= 3.288529, "reject_volume_spike_gte_3_288529"),
            (s("volume_spike") >= 5, "reject_volume_spike_gte_5"),
        ]

    if {"regime_score", "funding_zscore"}.issubset(available):
        targeted += [
            ((s("regime_score") <= 60) & (s("funding_zscore") >= -0.103207), "reject_regime_lte_60_AND_funding_gte_minus_0_103207"),
            ((s("regime_score") <= 60) & (s("funding_zscore") >= -0.410338), "reject_regime_lte_60_AND_funding_gte_minus_0_410338"),
            ((s("regime_score") > 55) & (s("regime_score") <= 60) & (s("funding_zscore") >= -0.103207), "reject_regime_55_60_AND_funding_gte_minus_0_103207"),
        ]

    if {"regime_score", "squeeze_probability"}.issubset(available):
        targeted += [
            ((s("regime_score") <= 60) & (s("squeeze_probability") <= 35), "reject_regime_lte_60_AND_squeeze_lte_35"),
            ((s("regime_score") > 55) & (s("regime_score") <= 60) & (s("squeeze_probability") <= 35), "reject_regime_55_60_AND_squeeze_lte_35"),
        ]

    if {"squeeze_probability", "funding_zscore"}.issubset(available):
        targeted += [
            ((s("squeeze_probability") <= 35) & (s("funding_zscore") >= -0.644684), "reject_squeeze_lte_35_AND_funding_gte_minus_0_644684"),
            ((s("squeeze_probability") <= 35) & (s("funding_zscore") >= -0.103207), "reject_squeeze_lte_35_AND_funding_gte_minus_0_103207"),
            ((s("squeeze_probability") <= 20) & (s("funding_zscore") >= -0.103207), "reject_squeeze_lte_20_AND_funding_gte_minus_0_103207"),
        ]

    if {"regime_score", "oi_expansion_rate"}.issubset(available):
        targeted += [
            ((s("regime_score") <= 60) & (s("oi_expansion_rate") >= -1.405459), "reject_regime_lte_60_AND_oi_gte_minus_1_405459"),
            ((s("regime_score") <= 60) & (s("oi_expansion_rate") >= 0), "reject_regime_lte_60_AND_oi_gte_0"),
        ]

    if {"regime_score", "taker_delta"}.issubset(available):
        targeted += [
            ((s("regime_score") <= 60) & (s("taker_delta") <= 0.154356), "reject_regime_lte_60_AND_taker_lte_0_154356"),
        ]

    if {"score", "funding_zscore"}.issubset(available):
        targeted += [
            ((s("score") <= 92.785) & (s("funding_zscore") >= -0.103207), "reject_score_lte_92_785_AND_funding_gte_minus_0_103207"),
            ((s("score") >= 85) & (s("regime_score") <= 60), "reject_score_gte_85_AND_regime_lte_60") if "regime_score" in available else None,
        ]

    for item in targeted:
        if item is None:
            continue
        mask, label = item
        rejected_count = int(mask.sum())
        if rejected_count < 5 or rejected_count >= len(predicted_win):
            continue

        rules.append(rule_metrics(
            predicted_win,
            mask,
            label,
            "targeted_cp057_refinement",
        ))

    return rules

def main():
    print("=== CP-058: TP-Preservation Rule Refinement Audit ===")

    full_eval, folds = run_wf_predictions()

    if full_eval.empty:
        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": "INSUFFICIENT_DATA",
            "reason": "No WF predictions available.",
            "governance": {
                "read_only_validation": True,
                "runtime_rule_changed": False,
                "threshold_changed": False,
                "model_promoted": False,
                "live_unlock": False,
                "execution_changed": False,
            },
        }
    else:
        predicted_win = full_eval[full_eval["baseline_predicted"] == "WIN"].copy()

        baseline_tp = int((predicted_win["actual"] == "WIN").sum())
        baseline_fp = int((predicted_win["actual"] == "LOSS").sum())
        baseline_precision = safe_rate(baseline_tp, len(predicted_win))

        single_rules = generate_single_rules(predicted_win)
        pair_rules = generate_pair_rules(predicted_win)
        targeted_rules = generate_targeted_rules(predicted_win)

        all_rules = single_rules + pair_rules + targeted_rules

        # Remove duplicate rule labels, keep best metric per label.
        by_label = {}
        for r in all_rules:
            label = r["rule_label"]
            prev = by_label.get(label)
            if prev is None:
                by_label[label] = r
            else:
                prev_score = prev.get("composite_score") or -999
                new_score = r.get("composite_score") or -999
                if new_score > prev_score:
                    by_label[label] = r

        all_rules = list(by_label.values())

        precision_improvers = [
            r for r in all_rules
            if r["kept_precision"] is not None
            and r["precision_gain"] is not None
            and r["precision_gain"] > 0
            and r["kept_rows"] >= 40
        ]

        strict_candidates = [
            r for r in precision_improvers
            if r["kept_precision"] >= STRICT_TARGET_PRECISION
            and r["kept_rows"] >= MIN_KEPT_ROWS_STRICT
            and (r["fp_removed_rate"] or 0) >= MIN_FP_REMOVED_STRICT
            and (r["tp_loss_rate"] or 1) <= MAX_TP_LOSS_STRICT
            and (r["net_fp_removed_minus_tp_lost"] or -999) >= MIN_NET_FP_MINUS_TP
        ]

        soft_candidates = [
            r for r in precision_improvers
            if r["kept_precision"] >= SOFT_TARGET_PRECISION
            and r["kept_rows"] >= MIN_KEPT_ROWS_SOFT
            and (r["fp_removed_rate"] or 0) >= MIN_FP_REMOVED_SOFT
            and (r["tp_loss_rate"] or 1) <= MAX_TP_LOSS_SOFT
            and (r["net_fp_removed_minus_tp_lost"] or -999) >= MIN_NET_FP_MINUS_TP
        ]

        best_composite = sorted(
            precision_improvers,
            key=lambda x: (
                x["composite_score"] or -999,
                x["kept_precision"] or 0,
                x["kept_rows"],
                x["net_fp_removed_minus_tp_lost"] or -999,
            ),
            reverse=True,
        )[:30]

        best_precision_preserved = sorted(
            [
                r for r in precision_improvers
                if (r["tp_loss_rate"] or 1) <= MAX_TP_LOSS_STRICT
                and r["kept_rows"] >= 80
            ],
            key=lambda x: (
                x["kept_precision"] or 0,
                x["kept_rows"],
                x["fp_removed_rate"] or 0,
            ),
            reverse=True,
        )[:30]

        best_tp_preservation = sorted(
            [
                r for r in precision_improvers
                if r["kept_rows"] >= 120
                and (r["fp_removed_rate"] or 0) >= 0.25
            ],
            key=lambda x: (
                -(x["tp_loss_rate"] or 999),
                x["kept_precision"] or 0,
                x["fp_removed_rate"] or 0,
                x["kept_rows"],
            ),
            reverse=True,
        )[:30]

        best_fp_selective = sorted(
            precision_improvers,
            key=lambda x: (
                x["net_fp_removed_minus_tp_lost"] or -999,
                x["fp_removed_rate"] or 0,
                -(x["tp_loss_rate"] or 999),
                x["kept_precision"] or 0,
            ),
            reverse=True,
        )[:30]

        if strict_candidates:
            best = sorted(
                strict_candidates,
                key=lambda x: (x["composite_score"] or -999, x["kept_precision"], x["kept_rows"]),
                reverse=True,
            )[0]
            verdict = "TP_PRESERVATION_CANDIDATE_REVIEW"
            reason = (
                f"At least one strict TP-preserving reject rule candidate was found. "
                f"Best={best['rule_label']} kept_precision={best['kept_precision']} "
                f"kept_rows={best['kept_rows']} tp_loss={best['tp_loss_rate']} "
                f"fp_removed={best['fp_removed_rate']}. Review-only; no runtime rule authorized."
            )
        elif soft_candidates:
            best = sorted(
                soft_candidates,
                key=lambda x: (x["composite_score"] or -999, x["kept_precision"], x["kept_rows"]),
                reverse=True,
            )[0]
            verdict = "SOFT_TP_PRESERVATION_CANDIDATE_REVIEW"
            reason = (
                f"Soft TP-preserving reject rule candidate found but strict criteria not met. "
                f"Best={best['rule_label']} kept_precision={best['kept_precision']} "
                f"kept_rows={best['kept_rows']} tp_loss={best['tp_loss_rate']} "
                f"fp_removed={best['fp_removed_rate']}. Review-only; no runtime rule authorized."
            )
        elif best_precision_preserved:
            best = best_precision_preserved[0]
            verdict = "TP_PRESERVATION_WEAK_REVIEW"
            reason = (
                f"Some rules improve precision while keeping TP loss within guardrail, but they do not meet full FP-removal/coverage criteria. "
                f"Best preserved-precision rule={best['rule_label']} kept_precision={best['kept_precision']} "
                f"kept_rows={best['kept_rows']} tp_loss={best['tp_loss_rate']} fp_removed={best['fp_removed_rate']}."
            )
        elif precision_improvers:
            best = best_composite[0]
            verdict = "NO_TP_PRESERVING_RULE_FOUND"
            reason = (
                f"Precision-improving rules exist, but none preserve TP sufficiently. "
                f"Best composite={best['rule_label']} kept_precision={best['kept_precision']} "
                f"kept_rows={best['kept_rows']} tp_loss={best['tp_loss_rate']} fp_removed={best['fp_removed_rate']}."
            )
        else:
            verdict = "NO_PRECISION_IMPROVING_RULE_FOUND"
            reason = "No tested reject rule improved baseline predicted-WIN precision."

        report = {
            "audit_time_utc": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "reason": reason,
            "baseline_predicted_win_rows": int(len(predicted_win)),
            "baseline_true_positive_win": baseline_tp,
            "baseline_false_positive_loss_to_win": baseline_fp,
            "baseline_predicted_win_precision": baseline_precision,
            "strict_targets": {
                "precision": STRICT_TARGET_PRECISION,
                "min_kept_rows": MIN_KEPT_ROWS_STRICT,
                "max_tp_loss": MAX_TP_LOSS_STRICT,
                "min_fp_removed": MIN_FP_REMOVED_STRICT,
                "min_net_fp_minus_tp": MIN_NET_FP_MINUS_TP,
            },
            "soft_targets": {
                "precision": SOFT_TARGET_PRECISION,
                "min_kept_rows": MIN_KEPT_ROWS_SOFT,
                "max_tp_loss": MAX_TP_LOSS_SOFT,
                "min_fp_removed": MIN_FP_REMOVED_SOFT,
                "min_net_fp_minus_tp": MIN_NET_FP_MINUS_TP,
            },
            "folds": folds,
            "rules_tested": {
                "single": len(single_rules),
                "pair": len(pair_rules),
                "targeted": len(targeted_rules),
                "deduped_total": len(all_rules),
                "precision_improvers": len(precision_improvers),
                "strict_candidates": len(strict_candidates),
                "soft_candidates": len(soft_candidates),
            },
            "strict_candidates_top25": sorted(
                strict_candidates,
                key=lambda x: (x["composite_score"] or -999, x["kept_precision"], x["kept_rows"]),
                reverse=True,
            )[:25],
            "soft_candidates_top25": sorted(
                soft_candidates,
                key=lambda x: (x["composite_score"] or -999, x["kept_precision"], x["kept_rows"]),
                reverse=True,
            )[:25],
            "best_composite_rules_top30": best_composite,
            "best_precision_with_tp_loss_guardrail_top30": best_precision_preserved,
            "best_tp_preservation_rules_top30": best_tp_preservation,
            "best_fp_selective_rules_top30": best_fp_selective,
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
    print("Verdict:", report["verdict"])
    print("Reason:", report["reason"])
    print("Baseline predicted WIN rows:", report.get("baseline_predicted_win_rows"))
    print("Baseline TP WIN:", report.get("baseline_true_positive_win"))
    print("Baseline FP LOSS->WIN:", report.get("baseline_false_positive_loss_to_win"))
    print("Baseline WIN precision:", report.get("baseline_predicted_win_precision"))
    print("Rules tested:", report.get("rules_tested"))
    print("Strict candidates:", len(report.get("strict_candidates_top25", [])))
    print("Soft candidates:", len(report.get("soft_candidates_top25", [])))
    print("Best composite rules:", [
        (
            r["rule_label"],
            r["kept_precision"],
            r["kept_rows"],
            r["kept_true_positive_win"],
            r["kept_false_positive_loss_to_win"],
            r["fp_removed_rate"],
            r["tp_loss_rate"],
            r["net_fp_removed_minus_tp_lost"],
            r["composite_score"],
        )
        for r in report.get("best_composite_rules_top30", [])[:10]
    ])
    print("Best precision with TP-loss guardrail:", [
        (
            r["rule_label"],
            r["kept_precision"],
            r["kept_rows"],
            r["fp_removed_rate"],
            r["tp_loss_rate"],
            r["net_fp_removed_minus_tp_lost"],
        )
        for r in report.get("best_precision_with_tp_loss_guardrail_top30", [])[:10]
    ])
    print("Best FP-selective:", [
        (
            r["rule_label"],
            r["kept_precision"],
            r["kept_rows"],
            r["fp_removed_rate"],
            r["tp_loss_rate"],
            r["net_fp_removed_minus_tp_lost"],
        )
        for r in report.get("best_fp_selective_rules_top30", [])[:10]
    ])
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
