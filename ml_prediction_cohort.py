"""Phase 9D.1C-E row-level ML prediction cohort materialization.

Exports only out-of-sample predictions produced by the existing walk-forward
model evaluation path. This module is observational/PAPER_ONLY: it never routes
orders, writes broker state, tunes thresholds, promotes models, or fabricates
labels.
"""
from __future__ import annotations

import csv
from pathlib import Path
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from ml_engine import CATEGORICAL_FEATURES, NUMERIC_FEATURES, _historical_dataset, build_ml_dataset, fit_train_only_preprocessor, transform_with_train_preprocessor
from ml_prediction_ledger import LEDGER_FIELDS, append_prediction, canonical_ml_label, create_ledger_row, write_prediction_ledger_audit, audit_prediction_ledger, load_prediction_ledger

COHORT_FIELDS = [
    "prediction_id", "candidate_id", "symbol", "side", "prediction_timestamp",
    "feature_timestamp_max", "target_horizon", "target_timestamp", "target_label",
    "y_pred", "y_true", "predicted_probability", "model_version", "feature_schema_version",
    "fold_id", "train_window_start", "train_window_end", "test_window_start",
    "test_window_end", "label_source", "label_status", "evaluation_status",
    "evaluation_contract",
]


def _value(row: pd.Series, names: List[str]) -> Any:
    for name in names:
        if name in row.index and pd.notna(row.get(name)):
            return row.get(name)
    return None


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "" or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return str(value)
    return ts.isoformat()


def _require_temporal(row: Dict[str, Any]) -> None:
    pred = pd.to_datetime(row.get("prediction_timestamp"), errors="coerce", utc=True)
    feature = pd.to_datetime(row.get("feature_timestamp_max"), errors="coerce", utc=True)
    if pd.isna(pred):
        raise ValueError("prediction_timestamp is required for every cohort row")
    if pd.isna(feature):
        raise ValueError("feature_timestamp_max is required for every cohort row")
    if feature > pred:
        raise ValueError("feature_timestamp_max after prediction_timestamp")


def _probability(model: RandomForestClassifier, X_test: pd.DataFrame, pred: Any, offset: int) -> Optional[float]:
    if not hasattr(model, "predict_proba"):
        return None
    try:
        classes = list(model.classes_)
        probs = model.predict_proba(X_test)
        return float(probs[offset][classes.index(pred)]) if pred in classes else float(np.max(probs[offset]))
    except Exception:
        return None


def materialize_prediction_cohort(
    dataset: pd.DataFrame,
    cohort_path: str | Path = "reports/ml_prediction_cohort.csv",
    ledger_path: Optional[str | Path] = "reports/ml_prediction_ledger.jsonl",
    train_window: int = 30,
    test_window: int = 10,
    max_folds: Optional[int] = None,
    model_version: str = "walkforward_random_forest_v1",
    feature_schema_version: str = "ml_engine_features_v1",
    label_source: str = "ml_engine.build_ml_dataset.target",
) -> Dict[str, Any]:
    """Write row-level OOS prediction cohort and optional ledger rows.

    The supplied dataset must already be the real evaluation population. Rows are
    emitted only for walk-forward test folds after fitting preprocessing/model on
    each fold's train slice.
    """
    cohort_path = Path(cohort_path)
    cohort_path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    existing_prediction_ids = set()
    ledger_rows_appended = 0
    ledger_duplicates_skipped = 0
    folds_evaluated = 0
    effective_max_folds = None if max_folds is None or int(max_folds) <= 0 else int(max_folds)
    export_truncated = False
    export_truncation_reason = None
    if ledger_path:
        existing_prediction_ids = {str(row.get("prediction_id")) for row in load_prediction_ledger(ledger_path) if row.get("prediction_id")}
    if dataset.empty or len(dataset) < train_window + test_window or "target" not in dataset.columns or dataset["target"].nunique() < 2:
        pd.DataFrame(rows, columns=COHORT_FIELDS).to_csv(cohort_path, index=False)
        if ledger_path:
            write_prediction_ledger_audit(audit_prediction_ledger(ledger_path), Path(ledger_path).with_name("ml_prediction_ledger_audit.json"))
        return {"cohort_path": str(cohort_path), "ledger_path": str(ledger_path) if ledger_path else None, "rows": 0, "folds": 0, "ledger_rows_appended": 0, "ledger_duplicates_skipped": 0, "max_folds": effective_max_folds, "folds_evaluated": 0, "export_truncated": False, "export_truncation_reason": None}

    frame = dataset.copy().reset_index(drop=True)
    if "prediction_timestamp" not in frame.columns:
        frame["prediction_timestamp"] = frame.get("timestamp")
    if "feature_timestamp_max" not in frame.columns:
        frame["feature_timestamp_max"] = frame["prediction_timestamp"]
    frame["prediction_timestamp"] = pd.to_datetime(frame["prediction_timestamp"], errors="coerce", utc=True)
    frame = frame.sort_values("prediction_timestamp").reset_index(drop=True)

    fold_id = 1
    start = 0
    while start + train_window + test_window <= len(frame):
        if effective_max_folds is not None and folds_evaluated >= effective_max_folds:
            export_truncated = True
            export_truncation_reason = f"max_folds_reached:{effective_max_folds}"
            break
        train = frame.iloc[start:start + train_window].copy()
        test = frame.iloc[start + train_window:start + train_window + test_window].copy()
        start += test_window
        if train["target"].nunique() < 2:
            continue
        preprocessor = fit_train_only_preprocessor(train)
        X_train = transform_with_train_preprocessor(train, preprocessor)
        X_test = transform_with_train_preprocessor(test, preprocessor)
        model = RandomForestClassifier(n_estimators=150, max_depth=5, class_weight="balanced", random_state=42)
        model.fit(X_train, train["target"])
        preds = model.predict(X_test)
        train_start = _iso(train["prediction_timestamp"].iloc[0])
        train_end = _iso(train["prediction_timestamp"].iloc[-1])
        test_start = _iso(test["prediction_timestamp"].iloc[0])
        test_end = _iso(test["prediction_timestamp"].iloc[-1])
        for offset, (idx, row) in enumerate(test.iterrows()):
            y_true = canonical_ml_label(row.get("target"))
            if y_true in {"PENDING", "UNKNOWN"}:
                label_status = "PENDING" if y_true == "PENDING" else "MISSING"
                evaluation_status = "PENDING" if label_status == "PENDING" else "BLOCKED_MISSING_LABEL"
                y_true_value = None if y_true == "PENDING" else y_true
            else:
                label_status = "MATURED"
                evaluation_status = "READY"
                y_true_value = y_true
            target_ts = _iso(_value(row, ["target_timestamp", "label_timestamp", "outcome_timestamp"]))
            out = {
                "candidate_id": _value(row, ["candidate_id", "signal_id", "id"]),
                "symbol": _value(row, ["symbol", "asset"]),
                "side": _value(row, ["side", "direction"]),
                "prediction_timestamp": _iso(row.get("prediction_timestamp")),
                "feature_timestamp_max": _iso(row.get("feature_timestamp_max")),
                "target_horizon": _value(row, ["target_horizon", "horizon"]),
                "target_timestamp": target_ts,
                "target_label": y_true_value,
                "y_pred": canonical_ml_label(preds[offset]),
                "y_true": y_true_value,
                "predicted_probability": _probability(model, X_test, preds[offset], offset),
                "model_version": model_version,
                "feature_schema_version": feature_schema_version,
                "fold_id": fold_id,
                "train_window_start": train_start,
                "train_window_end": train_end,
                "test_window_start": test_start,
                "test_window_end": test_end,
                "label_source": label_source,
                "label_status": label_status,
                "evaluation_status": evaluation_status,
                "evaluation_contract": "canonical_ml_label_v1_walkforward_oos",
            }
            _require_temporal(out)
            ledger_row = create_ledger_row(**out)
            out["prediction_id"] = ledger_row["prediction_id"]
            rows.append(out)
            if ledger_path:
                prediction_id = str(ledger_row.get("prediction_id"))
                if prediction_id in existing_prediction_ids:
                    ledger_duplicates_skipped += 1
                else:
                    append_prediction(ledger_path, {k: ledger_row.get(k) for k in LEDGER_FIELDS})
                    existing_prediction_ids.add(prediction_id)
                    ledger_rows_appended += 1
        folds_evaluated += 1
        fold_id += 1
    pd.DataFrame(rows, columns=COHORT_FIELDS).to_csv(cohort_path, index=False, quoting=csv.QUOTE_MINIMAL)
    if ledger_path:
        write_prediction_ledger_audit(audit_prediction_ledger(ledger_path), Path(ledger_path).with_name("ml_prediction_ledger_audit.json"))
    return {"cohort_path": str(cohort_path), "ledger_path": str(ledger_path) if ledger_path else None, "rows": len(rows), "folds": folds_evaluated, "ledger_rows_appended": ledger_rows_appended, "ledger_duplicates_skipped": ledger_duplicates_skipped, "max_folds": effective_max_folds, "folds_evaluated": folds_evaluated, "export_truncated": export_truncated, "export_truncation_reason": export_truncation_reason}


def _canonical_target_counts(dataset: pd.DataFrame) -> Dict[str, int]:
    if dataset.empty or "target" not in dataset.columns:
        return {}
    labels = dataset["target"].apply(canonical_ml_label)
    labels = labels[labels.isin({"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"})]
    return {str(label): int(count) for label, count in labels.value_counts().sort_index().items()}


def _default_max_folds() -> Optional[int]:
    value = os.environ.get("ML_PREDICTION_COHORT_MAX_FOLDS", "250").strip()
    if value.lower() in {"", "none", "unlimited", "full"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return 250
    return None if parsed <= 0 else parsed


def _raw_candidate_rows(path_or_table: str, database_path: str = "mamuyy_hunter.db") -> int:
    if path_or_table.endswith(".csv"):
        path = Path(path_or_table)
        if not path.exists():
            return 0
        try:
            return int(len(pd.read_csv(path)))
        except (pd.errors.EmptyDataError, OSError):
            return 0
    if not os.path.exists(database_path):
        return 0
    try:
        with sqlite3.connect(database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (path_or_table,),
            ).fetchone()
            if not exists:
                return 0
            return int(connection.execute(f"SELECT COUNT(*) FROM {path_or_table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _reject_reasons(dataset: pd.DataFrame, train_window: int, test_window: int) -> List[str]:
    reasons: List[str] = []
    if len(dataset) < train_window + test_window:
        reasons.append(f"prepared_rows_below_required_window:{len(dataset)}<{train_window + test_window}")
    if "target" not in dataset.columns:
        reasons.append("missing_target_column")
    elif len(_canonical_target_counts(dataset)) < 2:
        reasons.append("target_has_fewer_than_2_canonical_classes")
    if "prediction_timestamp" in dataset.columns:
        timestamps = pd.to_datetime(dataset["prediction_timestamp"], errors="coerce", utc=True)
    elif "timestamp" in dataset.columns:
        timestamps = pd.to_datetime(dataset["timestamp"], errors="coerce", utc=True)
    else:
        timestamps = pd.Series(pd.NaT, index=dataset.index)
        reasons.append("missing_prediction_timestamp")
    if len(dataset) and timestamps.isna().any():
        reasons.append("unresolved_prediction_timestamps")
    if "feature_timestamp_max" in dataset.columns and len(dataset):
        features = pd.to_datetime(dataset["feature_timestamp_max"], errors="coerce", utc=True)
        if features.isna().any():
            reasons.append("unresolved_feature_timestamps")
        if (~features.isna() & ~timestamps.isna() & (features > timestamps)).any():
            reasons.append("feature_timestamp_after_prediction_timestamp")
    return reasons


def _internal_paper_dataset(database_path: str) -> pd.DataFrame:
    if not os.path.exists(database_path):
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    try:
        with sqlite3.connect(database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_paper_trades'"
            ).fetchone()
            if not exists:
                return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
            frame = pd.read_sql_query("SELECT * FROM internal_paper_trades ORDER BY timestamp ASC", connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    if frame.empty:
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    frame = frame.rename(columns={"confidence": "score", "regime": "regime_name", "pnl": "pnl_percent"})
    frame["prediction_timestamp"] = frame.get("source_signal_timestamp", frame.get("timestamp"))
    frame["feature_timestamp_max"] = frame["prediction_timestamp"]
    frame["target_timestamp"] = frame.get("updated_at", frame.get("timestamp"))
    frame["target"] = frame.get("status", "")
    for column in NUMERIC_FEATURES:
        if column not in frame.columns:
            frame[column] = 0.0
    for column in CATEGORICAL_FEATURES:
        if column not in frame.columns:
            frame[column] = "UNKNOWN"
    frame["target"] = frame["target"].apply(canonical_ml_label)
    return frame[frame["target"].isin({"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"})][
        ["timestamp", "prediction_timestamp", "feature_timestamp_max", "target_timestamp", "symbol", "side", *NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"]
    ].copy()


def _select_prediction_cohort_source(
    paper_trades_path: str,
    signals_log_path: str,
    database_path: str,
    train_window: int,
    test_window: int,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    candidates: List[Tuple[str, str, pd.DataFrame, int]] = []
    paper_dataset = build_ml_dataset(paper_trades_path, signals_log_path, "__missing_flow_log.csv", database_path="__missing_ml_cohort_source.db")
    candidates.append(("paper_trades", paper_trades_path, paper_dataset, _raw_candidate_rows(paper_trades_path)))

    historical_dataset = _historical_dataset(database_path)
    candidates.append(("historical_outcomes", database_path, historical_dataset, _raw_candidate_rows("historical_outcomes", database_path)))

    internal_dataset = _internal_paper_dataset(database_path)
    candidates.append(("internal_paper_trades", database_path, internal_dataset, _raw_candidate_rows("internal_paper_trades", database_path)))

    diagnostics: Dict[str, Any] = {
        "selected_source": None,
        "selected_source_path": None,
        "source_candidates": [name for name, _, _, _ in candidates],
        "source_candidate_rows": {name: rows for name, _, _, rows in candidates},
        "prepared_rows": 0,
        "target_counts": {},
        "source_reject_reasons": {},
    }
    for name, path, dataset, _raw_rows in candidates:
        reasons = _reject_reasons(dataset, train_window, test_window)
        if reasons:
            diagnostics["source_reject_reasons"][name] = reasons
            continue
        diagnostics["selected_source"] = name
        diagnostics["selected_source_path"] = path
        diagnostics["prepared_rows"] = int(len(dataset))
        diagnostics["target_counts"] = _canonical_target_counts(dataset)
        return dataset, diagnostics
    return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"]), diagnostics


def run_prediction_cohort_export(
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    database_path: str = "mamuyy_hunter.db",
    cohort_path: str = "reports/ml_prediction_cohort.csv",
    ledger_path: str = "reports/ml_prediction_ledger.jsonl",
    train_window: int = 30,
    test_window: int = 10,
    max_folds: Optional[int] = None,
) -> Dict[str, Any]:
    effective_max_folds = _default_max_folds() if max_folds is None else (None if int(max_folds) <= 0 else int(max_folds))
    dataset, diagnostics = _select_prediction_cohort_source(
        paper_trades_path,
        signals_log_path,
        database_path,
        train_window,
        test_window,
    )
    result = materialize_prediction_cohort(dataset, cohort_path=cohort_path, ledger_path=ledger_path, train_window=train_window, test_window=test_window, max_folds=effective_max_folds)
    result.update(diagnostics)
    return result
