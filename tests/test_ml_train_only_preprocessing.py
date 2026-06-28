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


def test_production_universe_prefers_closed_internal_paper_trades(tmp_path, monkeypatch):
    import json
    import sqlite3

    from ml_engine import build_ml_dataset

    db = tmp_path / "hunter.db"
    report = tmp_path / "logs" / "production_universe_dataset_build_report.json"
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE internal_paper_trades(
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                source_signal_timestamp TEXT,
                symbol TEXT,
                exit_price REAL,
                pnl REAL,
                confidence REAL,
                regime TEXT,
                status TEXT,
                exit_reason TEXT,
                updated_at TEXT,
                target_timestamp TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE historical_outcomes(
                id INTEGER PRIMARY KEY,
                signal_timestamp TEXT,
                close_timestamp TEXT,
                symbol TEXT,
                pnl_pct REAL,
                status TEXT,
                win_loss TEXT,
                score REAL,
                exit_reason TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO internal_paper_trades
            (source_signal_timestamp, symbol, pnl, confidence, regime, status, exit_reason, updated_at, target_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-01T00:00:00Z", "BTCUSDT", 2.0, 91, "TREND", "CLOSED", "TAKE_PROFIT_2", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
                ("2026-01-02T00:00:00Z", "ETHUSDT", -1.0, 88, "RANGE", "CLOSED", "STOP_LOSS", "2026-01-02T01:00:00Z", "2026-01-02T01:00:00Z"),
                ("2026-01-03T00:00:00Z", "SOLUSDT", 0.5, 80, "RANGE", "OPEN", "TAKE_PROFIT_1", "2026-01-03T01:00:00Z", "2026-01-03T01:00:00Z"),
                ("2026-01-04T00:00:00Z", "BNBUSDT", 0.0, 80, "RANGE", "CLOSED", "EXECUTION_SIMULATED", "2026-01-04T01:00:00Z", "2026-01-04T01:00:00Z"),
            ],
        )
        connection.execute(
            """
            INSERT INTO historical_outcomes(signal_timestamp, close_timestamp, symbol, pnl_pct, status, win_loss, score, exit_reason)
            VALUES ('2026-01-05T00:00:00Z', '2026-01-05T01:00:00Z', 'XRPUSDT', 1.0, 'WIN', 'WIN', 95, 'TAKE_PROFIT_2')
            """
        )
        connection.commit()

    dataset = build_ml_dataset("missing.csv", "missing.csv", "missing.csv", database_path=str(db), use_production_universe=True)

    assert set(dataset["target"]) == {"WIN", "LOSS"}
    assert set(dataset["source_artifact"]) == {"internal_paper_trades"}
    assert set(["dataset_contract_version", "dataset_build_hash", "production_score_threshold", "label_mapping_version"]).issubset(dataset.columns)
    payload = json.loads(report.read_text())
    assert payload["selected_source"] == "internal_paper_trades"
    assert payload["production_score_threshold"] == 75


def test_production_universe_falls_back_to_thresholded_historical_outcomes(tmp_path, monkeypatch):
    import sqlite3

    from ml_engine import build_ml_dataset

    db = tmp_path / "hunter.db"
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE internal_paper_trades(
                id INTEGER PRIMARY KEY,
                source_signal_timestamp TEXT,
                symbol TEXT,
                pnl REAL,
                confidence REAL,
                regime TEXT,
                status TEXT,
                exit_reason TEXT,
                updated_at TEXT,
                target_timestamp TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE historical_outcomes(
                id INTEGER PRIMARY KEY,
                signal_timestamp TEXT,
                close_timestamp TEXT,
                symbol TEXT,
                pnl_pct REAL,
                status TEXT,
                win_loss TEXT,
                score REAL,
                exit_reason TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO historical_outcomes(signal_timestamp, close_timestamp, symbol, pnl_pct, status, win_loss, score, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-02-01T00:00:00Z", "2026-02-01T01:00:00Z", "BTCUSDT", 1.0, "WIN", "WIN", 80, "TAKE_PROFIT_1"),
                ("2026-02-02T00:00:00Z", "2026-02-02T01:00:00Z", "ETHUSDT", -1.0, "LOSS", "LOSS", 74, "STOP_LOSS"),
                ("2026-02-03T00:00:00Z", "2026-02-03T01:00:00Z", "SOLUSDT", 0.0, "UNKNOWN", "UNKNOWN", 90, "UNKNOWN"),
            ],
        )
        connection.commit()

    dataset = build_ml_dataset("missing.csv", "missing.csv", "missing.csv", database_path=str(db), use_production_universe=True, production_score_threshold=80)

    assert dataset["target"].tolist() == ["TP1 HIT"]
    assert dataset["source_artifact"].tolist() == ["historical_outcomes"]
    assert dataset["production_score_threshold"].tolist() == [80]


def test_build_ml_dataset_default_behavior_unchanged_with_paper_trades(tmp_path):
    from ml_engine import build_ml_dataset

    trades = tmp_path / "paper_trades.csv"
    pd.DataFrame([_row(1, target="WIN"), _row(2, target="LOSS")]).to_csv(trades, index=False)

    dataset = build_ml_dataset(
        str(trades),
        str(tmp_path / "missing_signals.csv"),
        str(tmp_path / "missing_flow.csv"),
        database_path=str(tmp_path / "missing.db"),
    )

    assert len(dataset) == 2
    assert set(dataset["target"]) == {"WIN", "LOSS"}
    assert "dataset_contract_version" not in dataset.columns


def test_production_universe_report_tracks_exclusions_distribution_and_range(tmp_path, monkeypatch):
    import json
    import sqlite3

    from ml_engine import build_ml_dataset

    db = tmp_path / "hunter.db"
    report = tmp_path / "logs" / "production_universe_dataset_build_report.json"
    monkeypatch.chdir(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE historical_outcomes(
                id INTEGER PRIMARY KEY,
                signal_timestamp TEXT,
                close_timestamp TEXT,
                symbol TEXT,
                pnl_pct REAL,
                status TEXT,
                win_loss TEXT,
                confidence REAL,
                exit_reason TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO historical_outcomes(signal_timestamp, close_timestamp, symbol, pnl_pct, status, win_loss, confidence, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z", "BTCUSDT", 2.0, "WIN", "WIN", 90, "TAKE_PROFIT_2"),
                ("2026-03-02T00:00:00Z", "2026-03-02T01:00:00Z", "ETHUSDT", -1.0, "LOSS", "LOSS", 90, "STOP_LOSS"),
                ("2026-03-03T00:00:00Z", "2026-03-03T01:00:00Z", "SOLUSDT", 0.0, "OPEN", "UNKNOWN", 90, "OPEN"),
                ("2026-03-04T00:00:00Z", "2026-03-04T01:00:00Z", "BNBUSDT", 0.0, "UNKNOWN", "UNKNOWN", 90, "UNKNOWN"),
                ("2026-03-05T00:00:00Z", "2026-03-05T01:00:00Z", "ADAUSDT", 0.0, "UNKNOWN", "UNKNOWN", 90, "EXECUTION_SIMULATED"),
                ("2026-03-06T00:00:00Z", "2026-03-06T01:00:00Z", "XRPUSDT", 0.0, "UNKNOWN", "UNKNOWN", 90, "EXPIRED_ORPHANED"),
            ],
        )
        connection.commit()

    dataset = build_ml_dataset(
        "missing.csv",
        "missing.csv",
        "missing.csv",
        database_path=str(db),
        use_production_universe=True,
        production_score_threshold=75,
    )
    payload = json.loads(report.read_text())

    assert set(dataset["target"]) == {"WIN", "LOSS"}
    assert not set(["OPEN", "UNKNOWN", "EXECUTION_SIMULATED", "EXPIRED_ORPHANED"]).intersection(set(dataset["target"]))
    assert payload["excluded_row_counts_by_reason"] == {
        "OPEN": 1,
        "UNKNOWN": 1,
        "EXECUTION_SIMULATED": 1,
        "EXPIRED_ORPHANED": 1,
    }
    assert payload["final_label_distribution"] == {"WIN": 1, "LOSS": 1}
    assert payload["timestamp_range"]["min"].startswith("2026-03-01T00:00:00")
    assert payload["timestamp_range"]["max"].startswith("2026-03-02T00:00:00")
    assert payload["source_priority_used"] == "historical_outcomes"
    assert payload["source_row_counts"]["historical_outcomes"]["total_rows"] == 6
