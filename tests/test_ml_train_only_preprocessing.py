import pandas as pd
import pytest

from ml_engine import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)
from ml_metric_reconciliation import audit_train_only_preprocessing
from walkforward import run_walkforward_validation


def _row(i, regime="A", whale="LOW", funding="NO", target="WIN"):
    row = {column: float(i) for column in NUMERIC_FEATURES}
    row.update({
        "timestamp": f"2024-01-{i + 1:02d}T00:00:00Z",
        "symbol": f"S{i}",
        "regime_name": regime,
        "whale_activity": whale,
        "funding_warning": funding,
        "target": target,
        "status": target,
        "pnl_percent": 1.0 if target in {"WIN", "TP1 HIT", "TP2 HIT"} else -1.0,
    })
    return row


def test_transformer_fitted_only_on_train_rows_passes():
    train = pd.DataFrame([_row(0, regime="TRAIN_ONLY_A"), _row(1, regime="TRAIN_ONLY_B", target="LOSS")])
    preprocessor = fit_train_only_preprocessor(train)
    assert preprocessor["fit_scope"] == "TRAIN_ONLY"
    assert preprocessor["fit_row_count"] == len(train)
    assert any("TRAIN_ONLY_A" in name for name in preprocessor["feature_names"])

    audit = audit_train_only_preprocessing()
    assert audit["train_only_preprocessing_status"] == "PASS"
    assert audit["full_dataset_fit_violation_count"] == 0


def test_full_dataset_or_unverified_fit_scope_is_blocked_for_transform():
    test = pd.DataFrame([_row(2, regime="FUTURE_ONLY", target="LOSS")])
    with pytest.raises(ValueError):
        transform_with_train_preprocessor(test, {"fit_scope": "FULL_DATASET", "feature_names": []})


def test_future_rows_transform_with_train_fitted_encoder_only():
    train = pd.DataFrame([_row(0, regime="TRAIN_A"), _row(1, regime="TRAIN_B", target="LOSS")])
    future = pd.DataFrame([_row(2, regime="FUTURE_UNSEEN", target="WIN")])
    preprocessor = fit_train_only_preprocessor(train)
    transformed_future = transform_with_train_preprocessor(future, preprocessor)

    assert list(transformed_future.columns) == preprocessor["feature_names"]
    assert not any("FUTURE_UNSEEN" in column for column in transformed_future.columns)
    assert len(transformed_future) == 1


def test_walkforward_folds_keep_preprocessing_isolated_per_fold(tmp_path):
    rows = []
    labels = ["WIN", "LOSS", "TP1 HIT", "LOSS"] * 4
    for i in range(16):
        regime = "EARLY" if i < 8 else "LATE_UNSEEN"
        rows.append(_row(i, regime=regime, target=labels[i]))
    trades = tmp_path / "paper_trades.csv"
    pd.DataFrame(rows).to_csv(trades, index=False)

    result = run_walkforward_validation(
        paper_trades_path=str(trades),
        signals_log_path=str(tmp_path / "missing_signals.csv"),
        output_path=str(tmp_path / "walkforward_results.csv"),
        chart_dir=str(tmp_path / "charts"),
        database_path=str(tmp_path / "missing.db"),
        train_window=8,
        test_window=4,
    )
    assert result["folds"] >= 1
    assert (tmp_path / "walkforward_results.csv").exists()
