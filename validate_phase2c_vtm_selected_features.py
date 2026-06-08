#!/usr/bin/env python3
"""Read-only Phase 2C validation for selected DB-derived VTM feature subsets.

Purpose:
- Compare baseline Phase 2C calibration features against smaller selected DB
  VTM feature subsets from data/ml_calibration_with_vtm_db_features.csv.
- Test whether selective VTM subsets perform better than the noisy full VTM
  bundle previously measured at Brier 0.258740.

Safety:
- PAPER_ONLY / READ_ONLY.
- Does not import execution, broker, telegram, flow, or orchestrator modules.
- Does not open or write to any database.
- Does not change production scoring or create Phase 3 behavior.

Output:
- logs/phase2c_vtm_selected_features_report.json
"""

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data/ml_calibration_with_vtm_db_features.csv"
OUT_PATH = ROOT / "logs/phase2c_vtm_selected_features_report.json"
TARGET_BRIER = 0.24
FULL_VTM_REFERENCE_BRIER = 0.258740
VALIDATION_FRACTION = 0.30

DB_VTM_FEATURE_CANDIDATES = [
    "db_volume",
    "db_volume_ratio_20",
    "db_ema20",
    "db_ema200",
    "db_ema_distance_pct",
    "db_btc_above_ema200",
    "db_atr14",
    "db_atr_percent",
    "db_funding_rate",
    "db_open_interest",
    "db_open_interest_delta_1",
    "db_open_interest_change_pct_1",
]

MANUAL_TOP2_FEATURES = [
    "db_volume_ratio_20",
    "db_funding_rate",
]

BASELINE_SOURCE_COLUMNS = [
    "score",
    "matched_regime_score",
    "regime_match_delta_seconds",
    "holding_candles",
    "entry",
    "sl",
    "tp1",
    "tp2",
]

BASELINE_DERIVED_COLUMNS = [
    "score_norm",
    "regime_score_norm",
    "delta_norm",
    "holding_norm",
    "sl_dist_pct",
    "tp1_dist_pct",
    "tp2_dist_pct",
    "rr1",
    "rr2",
]

TIME_COLUMNS = [
    "signal_timestamp",
    "timestamp",
    "created_at",
    "opened_at",
    "entry_time",
]

POSITIVE_LABELS = {"WIN", "TP1 HIT", "TP1_HIT"}
NEGATIVE_LABELS = {"LOSS"}


def require_sklearn():
    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import brier_score_loss, roc_auc_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError(
            "scikit-learn is required for this read-only validation. "
            "Install sklearn/scikit-learn, then rerun this script."
        ) from exc

    return {
        "ColumnTransformer": ColumnTransformer,
        "SimpleImputer": SimpleImputer,
        "LogisticRegression": LogisticRegression,
        "brier_score_loss": brier_score_loss,
        "roc_auc_score": roc_auc_score,
        "Pipeline": Pipeline,
        "StandardScaler": StandardScaler,
    }


def to_float(value):
    if value in ("", None):
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def to_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def label_from_win_loss(value):
    normalized = (value or "").strip().upper()
    if normalized in POSITIVE_LABELS:
        return 1
    if normalized in NEGATIVE_LABELS:
        return 0
    return None


def first_present(row, candidates):
    for name in candidates:
        if name in row and row.get(name):
            return name
    return None


def read_csv_rows(path):
    if not path.exists():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = reader.fieldnames or []

    if "win_loss" not in columns:
        raise RuntimeError("Input CSV must contain win_loss label column.")

    return rows, columns


def add_baseline_features(row):
    entry = to_float(row.get("entry"))
    sl = to_float(row.get("sl"))
    tp1 = to_float(row.get("tp1"))
    tp2 = to_float(row.get("tp2"))

    sl_dist = abs((sl - entry) / entry) if valid_number(entry) and entry else math.nan
    tp1_dist = abs((tp1 - entry) / entry) if valid_number(entry) and entry else math.nan
    tp2_dist = abs((tp2 - entry) / entry) if valid_number(entry) and entry else math.nan

    row["score_norm"] = safe_transform(to_float(row.get("score")), lambda v: (v - 50.0) / 50.0)
    row["regime_score_norm"] = safe_transform(
        to_float(row.get("matched_regime_score")),
        lambda v: (v - 50.0) / 50.0,
    )
    row["delta_norm"] = safe_transform(
        to_float(row.get("regime_match_delta_seconds")),
        lambda v: min(v, 1800.0) / 1800.0,
    )
    row["holding_norm"] = safe_transform(to_float(row.get("holding_candles")), lambda v: v / 20.0)
    row["sl_dist_pct"] = safe_transform(sl_dist, lambda v: v * 100.0)
    row["tp1_dist_pct"] = safe_transform(tp1_dist, lambda v: v * 100.0)
    row["tp2_dist_pct"] = safe_transform(tp2_dist, lambda v: v * 100.0)
    row["rr1"] = tp1_dist / sl_dist if valid_number(sl_dist) and sl_dist else math.nan
    row["rr2"] = tp2_dist / sl_dist if valid_number(sl_dist) and sl_dist else math.nan


def safe_transform(value, transform):
    if not valid_number(value):
        return math.nan
    return transform(value)


def valid_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def load_labeled_rows():
    raw_rows, columns = read_csv_rows(CSV_PATH)
    time_column = first_present({col: col for col in columns}, TIME_COLUMNS)
    if not time_column:
        raise RuntimeError(f"Input CSV must contain one time column from: {TIME_COLUMNS}")

    vtm_feature_columns = [col for col in DB_VTM_FEATURE_CANDIDATES if col in columns]
    if not vtm_feature_columns:
        raise RuntimeError("Input CSV does not contain any selected DB VTM candidate columns.")

    rows = []
    ignored_labels = {}
    for raw in raw_rows:
        y = label_from_win_loss(raw.get("win_loss"))
        if y is None:
            label = (raw.get("win_loss") or "").strip() or "UNKNOWN"
            ignored_labels[label] = ignored_labels.get(label, 0) + 1
            continue

        dt = to_dt(raw.get(time_column))
        if not dt:
            continue

        row = dict(raw)
        row["_dt"] = dt
        row["_y"] = y
        for col in vtm_feature_columns:
            row[col] = to_float(row.get(col))
        add_baseline_features(row)
        rows.append(row)

    return sorted(rows, key=lambda r: r["_dt"]), columns, time_column, ignored_labels, vtm_feature_columns


def time_ordered_split(rows):
    if len(rows) < 2:
        raise RuntimeError("Need at least two labeled, timestamped rows for time-ordered validation.")
    split_index = int(len(rows) * (1.0 - VALIDATION_FRACTION))
    split_index = max(1, min(split_index, len(rows) - 1))
    return rows[:split_index], rows[split_index:]


def matrix(rows, columns):
    return [[row.get(col, math.nan) for col in columns] for row in rows]


def labels(rows):
    return [row["_y"] for row in rows]


def make_model_for_width(sk, width):
    numeric_pipeline = sk["Pipeline"](
        steps=[
            ("imputer", sk["SimpleImputer"](strategy="median")),
            ("scaler", sk["StandardScaler"]()),
        ]
    )
    preprocessor = sk["ColumnTransformer"](
        transformers=[("numeric", numeric_pipeline, list(range(width)))],
        remainder="drop",
    )
    model = sk["LogisticRegression"](
        max_iter=2000,
        solver="lbfgs",
        random_state=0,
    )
    return sk["Pipeline"](
        steps=[
            ("preprocess", preprocessor),
            ("model", model),
        ]
    )


def fit_predict_brier(sk, train, valid, feature_columns):
    x_train = matrix(train, feature_columns)
    y_train = labels(train)
    x_valid = matrix(valid, feature_columns)
    y_valid = labels(valid)

    if len(set(y_train)) < 2:
        raise RuntimeError("Training split has only one class; LogisticRegression cannot be fit.")
    if len(set(y_valid)) < 2:
        raise RuntimeError("Validation split has only one class; Brier comparison would be unreliable.")

    model = make_model_for_width(sk, len(feature_columns))
    model.fit(x_train, y_train)
    probabilities = model.predict_proba(x_valid)[:, 1]
    return float(sk["brier_score_loss"](y_valid, probabilities))


def null_rate(rows, column):
    if not rows:
        return None
    missing = 0
    for row in rows:
        value = row.get(column)
        if value in ("", None) or (isinstance(value, float) and math.isnan(value)):
            missing += 1
    return missing / len(rows)


def auc_for_feature(sk, rows, column):
    usable = [
        (row["_y"], row.get(column))
        for row in rows
        if valid_number(row.get(column))
    ]
    if len(usable) < 2:
        return {
            "auc": None,
            "directional_auc": None,
            "usable_rows": len(usable),
            "reason": "insufficient_non_null_rows",
        }

    y_values = [y for y, _ in usable]
    x_values = [x for _, x in usable]
    if len(set(y_values)) < 2:
        return {
            "auc": None,
            "directional_auc": None,
            "usable_rows": len(usable),
            "reason": "single_label_class",
        }
    if len(set(x_values)) < 2:
        return {
            "auc": None,
            "directional_auc": None,
            "usable_rows": len(usable),
            "reason": "constant_feature",
        }

    auc = float(sk["roc_auc_score"](y_values, x_values))
    directional_auc = max(auc, 1.0 - auc)
    return {
        "auc": round(auc, 6),
        "directional_auc": round(directional_auc, 6),
        "usable_rows": len(usable),
        "reason": None,
    }


def train_auc_ranking(sk, train, vtm_feature_columns):
    ranking = []
    for feature in vtm_feature_columns:
        stats = auc_for_feature(sk, train, feature)
        ranking.append({"feature": feature, **stats})
    return sorted(
        ranking,
        key=lambda item: (
            item["directional_auc"] is not None,
            item["directional_auc"] if item["directional_auc"] is not None else -1.0,
            item["usable_rows"],
        ),
        reverse=True,
    )


def ranked_feature_names(ranking, limit):
    return [
        item["feature"]
        for item in ranking
        if item["directional_auc"] is not None
    ][:limit]


def build_subset_specs(vtm_feature_columns, ranking):
    manual_top2 = [col for col in MANUAL_TOP2_FEATURES if col in vtm_feature_columns]
    specs = [
        ("baseline_only", []),
        ("baseline_plus_top2_manual", manual_top2),
        ("baseline_plus_train_auc_top1", ranked_feature_names(ranking, 1)),
        ("baseline_plus_train_auc_top2", ranked_feature_names(ranking, 2)),
        ("baseline_plus_train_auc_top3", ranked_feature_names(ranking, 3)),
        ("baseline_plus_train_auc_top5", ranked_feature_names(ranking, 5)),
    ]
    for feature in vtm_feature_columns:
        specs.append((f"baseline_plus_single_{feature}", [feature]))
    return specs


def evaluate_subsets(sk, train, valid, baseline_feature_columns, subset_specs):
    results = []
    for name, vtm_features in subset_specs:
        feature_columns = baseline_feature_columns + vtm_features
        brier = fit_predict_brier(sk, train, valid, feature_columns)
        results.append(
            {
                "subset": name,
                "vtm_features": vtm_features,
                "feature_count": len(feature_columns),
                "brier": round_or_none(brier),
            }
        )
    return results


def with_improvements(results, baseline_brier):
    for result in results:
        result["improvement_vs_baseline"] = round_or_none(baseline_brier - result["brier"])
        result["passes_target"] = bool(result["brier"] <= TARGET_BRIER)
    return results


def select_recommendation(results, baseline_brier):
    selected_results = [result for result in results if result["subset"] != "baseline_only"]
    improved_results = [result for result in selected_results if result["brier"] < baseline_brier]
    target_results = [result for result in selected_results if result["brier"] <= TARGET_BRIER]
    if target_results:
        return "CANDIDATE_FOR_FURTHER_RESEARCH_REVIEW"
    if improved_results:
        return "REVIEW_SELECTED_SUBSET_ONLY"
    return "CLOSE_VTM_FEATURE_TRACK"


def round_or_none(value):
    return round(value, 6) if value is not None else None


def write_report(report):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    sk = require_sklearn()
    rows, input_columns, time_column, ignored_labels, vtm_feature_columns = load_labeled_rows()
    train, valid = time_ordered_split(rows)

    baseline_feature_columns = list(BASELINE_DERIVED_COLUMNS)
    ranking = train_auc_ranking(sk, train, vtm_feature_columns)
    subset_specs = build_subset_specs(vtm_feature_columns, ranking)
    results = evaluate_subsets(sk, train, valid, baseline_feature_columns, subset_specs)

    baseline_result = next(result for result in results if result["subset"] == "baseline_only")
    baseline_brier = baseline_result["brier"]
    results = with_improvements(results, baseline_brier)
    best_result = min(results, key=lambda result: result["brier"])

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PAPER_ONLY_PHASE2C_VTM_SELECTED_FEATURE_VALIDATION",
        "input_csv": str(CSV_PATH.relative_to(ROOT)),
        "output_json": str(OUT_PATH.relative_to(ROOT)),
        "time_column": time_column,
        "rows_used": len(rows),
        "train_rows": len(train),
        "validation_rows": len(valid),
        "target_brier": TARGET_BRIER,
        "baseline_brier": baseline_brier,
        "full_vtm_reference_brier": FULL_VTM_REFERENCE_BRIER,
        "results_by_subset": results,
        "best_subset": best_result["subset"],
        "best_subset_brier": best_result["brier"],
        "best_subset_improvement_vs_baseline": best_result["improvement_vs_baseline"],
        "passes_target": bool(best_result["brier"] <= TARGET_BRIER),
        "train_auc_ranking": ranking,
        "null_rate_per_vtm_feature": {
            col: round_or_none(null_rate(rows, col)) for col in vtm_feature_columns
        },
        "baseline_source_columns": [c for c in BASELINE_SOURCE_COLUMNS if c in input_columns],
        "baseline_feature_columns": baseline_feature_columns,
        "vtm_candidate_features_requested": DB_VTM_FEATURE_CANDIDATES,
        "vtm_feature_columns_used": vtm_feature_columns,
        "label_mapping": {
            "WIN": 1,
            "TP1 HIT": 1,
            "LOSS": 0,
            "ignored_unknown_labels": ignored_labels,
        },
        "split": {
            "method": "time_ordered",
            "validation_fraction": VALIDATION_FRACTION,
            "train_start": train[0]["_dt"].isoformat() if train else None,
            "train_end": train[-1]["_dt"].isoformat() if train else None,
            "validation_start": valid[0]["_dt"].isoformat() if valid else None,
            "validation_end": valid[-1]["_dt"].isoformat() if valid else None,
        },
        "safety": {
            "read_only": True,
            "paper_only": True,
            "db_writes": False,
            "db_connections": False,
            "imports_execution_engine": False,
            "imports_broker": False,
            "imports_telegram": False,
            "imports_flow_engine": False,
            "imports_orchestrator": False,
            "production_scoring_changed": False,
            "phase3_behavior_created": False,
        },
        "sklearn": {
            "required": True,
            "model": "LogisticRegression",
            "preprocessing": ["SimpleImputer(strategy=median)", "StandardScaler"],
        },
        "recommendation": select_recommendation(results, baseline_brier),
    }
    write_report(report)


if __name__ == "__main__":
    main()
