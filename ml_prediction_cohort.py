"""Phase 9D.1C-E row-level ML prediction cohort materialization.

Exports only out-of-sample predictions produced by the existing walk-forward
model evaluation path. This module is observational/PAPER_ONLY: it never routes
orders, writes broker state, tunes thresholds, promotes models, or fabricates
labels.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from ml_engine import build_ml_dataset, fit_train_only_preprocessor, transform_with_train_preprocessor
from ml_prediction_ledger import LEDGER_FIELDS, append_prediction, canonical_ml_label, create_ledger_row, write_prediction_ledger_audit, audit_prediction_ledger

COHORT_FIELDS = [
    "prediction_id", "candidate_id", "symbol", "side", "prediction_timestamp",
    "feature_timestamp_max", "target_horizon", "target_timestamp", "y_pred",
    "y_true", "predicted_probability", "model_version", "feature_schema_version",
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
    if dataset.empty or len(dataset) < train_window + test_window or "target" not in dataset.columns or dataset["target"].nunique() < 2:
        pd.DataFrame(rows, columns=COHORT_FIELDS).to_csv(cohort_path, index=False)
        if ledger_path:
            write_prediction_ledger_audit(audit_prediction_ledger(ledger_path), Path(ledger_path).with_name("ml_prediction_ledger_audit.json"))
        return {"cohort_path": str(cohort_path), "ledger_path": str(ledger_path) if ledger_path else None, "rows": 0, "folds": 0}

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
                append_prediction(ledger_path, {k: ledger_row.get(k) for k in LEDGER_FIELDS})
        fold_id += 1
    pd.DataFrame(rows, columns=COHORT_FIELDS).to_csv(cohort_path, index=False, quoting=csv.QUOTE_MINIMAL)
    if ledger_path:
        write_prediction_ledger_audit(audit_prediction_ledger(ledger_path), Path(ledger_path).with_name("ml_prediction_ledger_audit.json"))
    return {"cohort_path": str(cohort_path), "ledger_path": str(ledger_path) if ledger_path else None, "rows": len(rows), "folds": fold_id - 1}


def run_prediction_cohort_export(
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    database_path: str = "mamuyy_hunter.db",
    cohort_path: str = "reports/ml_prediction_cohort.csv",
    ledger_path: str = "reports/ml_prediction_ledger.jsonl",
    train_window: int = 30,
    test_window: int = 10,
) -> Dict[str, Any]:
    dataset = build_ml_dataset(paper_trades_path, signals_log_path, "__missing_flow_log.csv", database_path=database_path)
    return materialize_prediction_cohort(dataset, cohort_path=cohort_path, ledger_path=ledger_path, train_window=train_window, test_window=test_window)
