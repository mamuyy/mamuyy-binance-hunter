import json
from pathlib import Path

import pandas as pd
import pytest

from database import init_db
from ml_prediction_cohort import COHORT_FIELDS, materialize_prediction_cohort, run_prediction_cohort_export
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


def _historical_db(path: Path, rows: int = 18) -> None:
    init_db(str(path))
    labels = ["WIN", "LOSS", "TP1 HIT"]
    with __import__("sqlite3").connect(path) as connection:
        for i in range(rows):
            ts = f"2024-02-{i+1:02d}T00:00:00Z"
            symbol = "BTCUSDT" if i % 2 == 0 else "ETHUSDT"
            connection.execute(
                """
                INSERT INTO historical_outcomes
                (signal_timestamp, close_timestamp, symbol, entry, exit_price, pnl_pct, status, win_loss, score)
                VALUES (?, ?, ?, 100, 101, 1.0, ?, ?, ?)
                """,
                (ts, f"2024-02-{i+1:02d}T01:00:00Z", symbol, labels[i % len(labels)], labels[i % len(labels)], 50 + i),
            )
            connection.execute(
                """
                INSERT INTO signals
                (timestamp, symbol, score, volume_spike, breakout, liquidity_sweep, regime_name, regime_score)
                VALUES (?, ?, ?, 1, 1, 0, 'TREND', 0.7)
                """,
                (ts, symbol, 50 + i),
            )
            connection.execute(
                """
                INSERT INTO flow_logs
                (timestamp, symbol, funding_zscore, oi_expansion_rate, taker_delta, pressure_score, squeeze_probability, whale_activity, funding_warning)
                VALUES (?, ?, 0.1, 0.2, 0.3, 0.4, 0.5, 'LOW', 'NO')
                """,
                (ts, symbol),
            )
        connection.commit()


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


def test_ledger_write_is_idempotent_by_prediction_id(tmp_path):
    cohort = tmp_path / "reports" / "ml_prediction_cohort.csv"
    ledger = tmp_path / "reports" / "ml_prediction_ledger.jsonl"
    first = materialize_prediction_cohort(_dataset(), cohort, ledger, train_window=6, test_window=3)
    first_rows = load_prediction_ledger(ledger)
    second = materialize_prediction_cohort(_dataset(), cohort, ledger, train_window=6, test_window=3)
    second_rows = load_prediction_ledger(ledger)
    assert len(first_rows) == 6
    assert len(second_rows) == 6
    assert first["ledger_rows_appended"] == 6
    assert first["ledger_duplicates_skipped"] == 0
    assert second["ledger_rows_appended"] == 0
    assert second["ledger_duplicates_skipped"] == 6


def test_tiny_invalid_paper_trades_falls_back_to_historical_outcomes(tmp_path):
    paper = tmp_path / "paper_trades.csv"
    paper.write_text("timestamp,symbol,status\n2024-01-01T00:00:00Z,BTCUSDT,OPEN\n", encoding="utf-8")
    signals = tmp_path / "signals_log.csv"
    signals.write_text("timestamp,symbol,score\n", encoding="utf-8")
    db = tmp_path / "fixture.db"
    _historical_db(db, rows=18)

    result = run_prediction_cohort_export(
        paper_trades_path=str(paper),
        signals_log_path=str(signals),
        database_path=str(db),
        cohort_path=tmp_path / "reports" / "ml_prediction_cohort.csv",
        ledger_path=tmp_path / "reports" / "ml_prediction_ledger.jsonl",
        train_window=6,
        test_window=3,
    )

    assert result["selected_source"] == "historical_outcomes"
    assert result["prepared_rows"] == 18
    assert result["rows"] > 0
    assert "paper_trades" in result["source_reject_reasons"]


def test_historical_outcomes_win_loss_tp1_hit_creates_nonzero_rows_and_counts(tmp_path):
    db = tmp_path / "fixture.db"
    _historical_db(db, rows=18)
    result = run_prediction_cohort_export(
        paper_trades_path=str(tmp_path / "missing_paper.csv"),
        signals_log_path=str(tmp_path / "missing_signals.csv"),
        database_path=str(db),
        cohort_path=tmp_path / "cohort.csv",
        ledger_path=tmp_path / "ledger.jsonl",
        train_window=6,
        test_window=3,
    )

    assert result["selected_source"] == "historical_outcomes"
    assert result["rows"] > 0
    assert result["target_counts"]["WIN"] == 12
    assert result["target_counts"]["LOSS"] == 6


def test_source_diagnostics_report_rejected_paper_reason_and_target_counts(tmp_path):
    paper = tmp_path / "paper_trades.csv"
    paper.write_text("timestamp,symbol,status\n2024-01-01T00:00:00Z,BTCUSDT,OPEN\n", encoding="utf-8")
    db = tmp_path / "fixture.db"
    _historical_db(db, rows=18)

    result = run_prediction_cohort_export(
        paper_trades_path=str(paper),
        signals_log_path=str(tmp_path / "missing_signals.csv"),
        database_path=str(db),
        cohort_path=tmp_path / "cohort.csv",
        ledger_path=tmp_path / "ledger.jsonl",
        train_window=6,
        test_window=3,
    )

    assert result["source_candidates"] == ["paper_trades", "historical_outcomes", "internal_paper_trades"]
    assert result["source_candidate_rows"]["paper_trades"] == 1
    assert any("prepared_rows_below_required_window" in reason for reason in result["source_reject_reasons"]["paper_trades"])
    assert result["target_counts"] == {"LOSS": 6, "WIN": 12}


def test_export_ledger_idempotent_after_historical_fallback(tmp_path):
    paper = tmp_path / "paper_trades.csv"
    paper.write_text("timestamp,symbol,status\n", encoding="utf-8")
    db = tmp_path / "fixture.db"
    _historical_db(db, rows=18)
    kwargs = dict(
        paper_trades_path=str(paper),
        signals_log_path=str(tmp_path / "missing_signals.csv"),
        database_path=str(db),
        cohort_path=tmp_path / "cohort.csv",
        ledger_path=tmp_path / "ledger.jsonl",
        train_window=6,
        test_window=3,
    )
    first = run_prediction_cohort_export(**kwargs)
    second = run_prediction_cohort_export(**kwargs)

    assert first["ledger_rows_appended"] == first["rows"]
    assert second["ledger_rows_appended"] == 0
    assert second["ledger_duplicates_skipped"] == first["rows"]


def test_governance_invariants_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["governance"]["model_promotion_allowed"] is False
