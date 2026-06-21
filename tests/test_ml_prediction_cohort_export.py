import json
from pathlib import Path

import pandas as pd
import pytest

from ml_prediction_cohort import COHORT_FIELDS, materialize_prediction_cohort
from ml_prediction_ledger import load_prediction_ledger
from ml_metric_reconciliation import run_ml_metric_reconciliation


def _dataset(rows=12):
    labels = ["WIN", "LOSS"] * (rows // 2)
    return pd.DataFrame({
        "timestamp": [f"2024-01-{i+1:02d}T00:00:00Z" for i in range(rows)],
        "prediction_timestamp": [f"2024-01-{i+1:02d}T00:00:00Z" for i in range(rows)],
        "feature_timestamp_max": [f"2024-01-{i+1:02d}T00:00:00Z" for i in range(rows)],
        "target_timestamp": [f"2024-01-{i+2:02d}T00:00:00Z" for i in range(rows)],
        "candidate_id": [f"c{i}" for i in range(rows)],
        "symbol": ["BTCUSDT"] * rows,
        "side": ["LONG"] * rows,
        "score": list(range(rows)),
        "volume_spike": [1] * rows,
        "breakout": [1] * rows,
        "liquidity_sweep": [0] * rows,
        "funding_zscore": [0.1] * rows,
        "oi_expansion_rate": [0.2] * rows,
        "taker_delta": [0.3] * rows,
        "pressure_score": [0.4] * rows,
        "squeeze_probability": [0.5] * rows,
        "regime_score": [0.6] * rows,
        "regime_name": ["TREND"] * rows,
        "whale_activity": ["LOW"] * rows,
        "funding_warning": ["NO"] * rows,
        "target": labels,
    })


def test_prediction_cohort_export_creates_expected_csv_fields(tmp_path):
    cohort = tmp_path / "reports" / "ml_prediction_cohort.csv"
    ledger = tmp_path / "reports" / "ml_prediction_ledger.jsonl"
    result = materialize_prediction_cohort(_dataset(), cohort, ledger, train_window=6, test_window=3)
    frame = pd.read_csv(cohort)
    assert result["rows"] == 6
    assert list(frame.columns) == COHORT_FIELDS
    assert set(["prediction_id", "y_pred", "y_true", "label_status", "evaluation_status"]).issubset(frame.columns)


def test_ledger_writer_appends_real_rows_using_canonical_label_contract(tmp_path):
    cohort = tmp_path / "reports" / "ml_prediction_cohort.csv"
    ledger = tmp_path / "reports" / "ml_prediction_ledger.jsonl"
    materialize_prediction_cohort(_dataset(), cohort, ledger, train_window=6, test_window=3)
    rows = load_prediction_ledger(ledger)
    assert len(rows) == 6
    assert {row["label_status"] for row in rows} == {"MATURED"}
    assert {row["evaluation_status"] for row in rows} == {"READY"}
    assert {row["y_true"] for row in rows} <= {"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"}


def test_pending_unmatured_rows_do_not_pretend_evaluated(tmp_path):
    data = _dataset()
    data.loc[6:, "target"] = "PENDING"
    cohort = tmp_path / "cohort.csv"
    ledger = tmp_path / "ledger.jsonl"
    materialize_prediction_cohort(data, cohort, ledger, train_window=6, test_window=3)
    rows = load_prediction_ledger(ledger)
    assert rows
    assert all(row["label_status"] == "PENDING" for row in rows)
    assert all(row["evaluation_status"] == "PENDING" for row in rows)
    assert all(row["y_true"] is None for row in rows)


def test_matured_rows_with_valid_y_true_become_ready_matured(tmp_path):
    cohort = tmp_path / "cohort.csv"
    ledger = tmp_path / "ledger.jsonl"
    materialize_prediction_cohort(_dataset(), cohort, ledger, train_window=6, test_window=3)
    assert all(row["label_status"] == "MATURED" and row["evaluation_status"] == "READY" for row in load_prediction_ledger(ledger))


def test_temporal_guard_blocks_future_feature_timestamps(tmp_path):
    data = _dataset()
    data.loc[6, "feature_timestamp_max"] = "2024-02-01T00:00:00Z"
    with pytest.raises(ValueError, match="feature_timestamp_max after prediction_timestamp"):
        materialize_prediction_cohort(data, tmp_path / "cohort.csv", tmp_path / "ledger.jsonl", train_window=6, test_window=3)


def test_reconciliation_discovers_default_cohort_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    materialize_prediction_cohort(_dataset(18), "reports/ml_prediction_cohort.csv", "reports/ml_prediction_ledger.jsonl", train_window=6, test_window=3)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["row_level_walkforward_status"] != "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS"
    assert report["row_level_walkforward_rows"] > 0
    assert Path("reports/ml_prediction_ledger_audit.json").exists()


def test_governance_invariants_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["governance"]["model_promotion_allowed"] is False
