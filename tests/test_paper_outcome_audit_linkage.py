import sqlite3

from paper_outcome_audit import (
    build_prediction_outcome_linkage_fields,
    generate_paper_outcome_audit,
    validate_prediction_outcome_linkage_fields,
)
from ml_metric_reconciliation import readiness
from internal_paper_engine import (
    _insert_trade,
    _update_open_trades,
    extract_prediction_linkage_metadata,
)


def test_build_linkage_fields_copies_prediction_id_from_prediction_context():
    fields = build_prediction_outcome_linkage_fields(prediction={"prediction_id": "pred-1"})

    assert fields["prediction_id"] == "pred-1"


def test_build_linkage_fields_copies_prediction_metadata_when_present():
    fields = build_prediction_outcome_linkage_fields(
        prediction={
            "predicted_probability": 0.82,
            "model_version": "model-v1",
            "target_timestamp": "2026-06-21T01:00:00Z",
            "evaluation_contract": "win_loss_v1",
        }
    )

    assert fields["predicted_probability"] == 0.82
    assert fields["model_version"] == "model-v1"
    assert fields["target_timestamp"] == "2026-06-21T01:00:00Z"
    assert fields["evaluation_contract"] == "win_loss_v1"


def test_build_linkage_fields_copies_trade_or_signal_id_from_trade_context():
    fields = build_prediction_outcome_linkage_fields(trade={"trade_id": "trade-1", "signal_id": "signal-1"})

    assert fields["trade_id"] == "trade-1"
    assert fields["signal_id"] == "signal-1"


def test_build_linkage_fields_copies_closed_at_and_outcome_label_from_outcome_context():
    fields = build_prediction_outcome_linkage_fields(
        outcome={"closed_at": "2026-06-21T02:00:00Z", "outcome": "WIN", "label": "WIN"}
    )

    assert fields["closed_at"] == "2026-06-21T02:00:00Z"
    assert fields["outcome"] == "WIN"
    assert fields["label"] == "WIN"


def test_build_linkage_fields_does_not_synthesize_prediction_id_when_missing():
    fields = build_prediction_outcome_linkage_fields(
        trade={"symbol": "BTCUSDT", "signal_timestamp": "2026-06-21T00:00:00Z"},
        outcome={"closed_at": "2026-06-21T02:00:00Z"},
    )

    assert fields["prediction_id"] is None
    assert fields["symbol"] == "BTCUSDT"
    assert fields["closed_at"] == "2026-06-21T02:00:00Z"


def test_paper_outcome_row_assembly_includes_linkage_fields_for_new_rows(tmp_path):
    db_path = tmp_path / "paper.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE internal_paper_trades (
            id INTEGER PRIMARY KEY,
            prediction_id TEXT,
            signal_id TEXT,
            symbol TEXT,
            side TEXT,
            status TEXT,
            entry_price REAL,
            exit_price REAL,
            current_price REAL,
            pnl REAL,
            exit_reason TEXT,
            timestamp TEXT,
            updated_at TEXT,
            predicted_probability REAL,
            model_version TEXT,
            target_timestamp TEXT,
            evaluation_contract TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO internal_paper_trades VALUES (
            7, 'pred-7', 'signal-7', 'ETHUSDT', 'LONG', 'CLOSED', 100, 110, 110,
            10, 'target_hit', '2026-06-21T00:00:00Z', '2026-06-21T04:00:00Z',
            0.91, 'model-v2', '2026-06-22T00:00:00Z', 'paper_eval_v1'
        )
        """
    )
    connection.commit()
    connection.close()

    report = generate_paper_outcome_audit(
        db_path=str(db_path),
        output_path=str(tmp_path / "paper_outcome_audit.json"),
        write_report=False,
    )

    row = report["closed_trades"][0]
    assert row["prediction_id"] == "pred-7"
    assert row["trade_id"] == 7
    assert row["signal_id"] == "signal-7"
    assert row["predicted_probability"] == 0.91
    assert row["model_version"] == "model-v2"
    assert row["target_timestamp"] == "2026-06-22T00:00:00Z"
    assert row["evaluation_contract"] == "paper_eval_v1"
    assert row["closed_at"] == "2026-06-21T04:00:00Z"
    assert row["outcome"] == "WIN"
    assert row["prediction_outcome_linkage_flags"] == []


def test_validate_linkage_fields_flags_missing_prediction_id_and_trade_signal_id():
    flags = validate_prediction_outcome_linkage_fields({"symbol": "BTCUSDT"})

    assert "MISSING_PREDICTION_ID" in flags
    assert "MISSING_TRADE_OR_SIGNAL_ID" in flags


def test_readiness_governance_remains_locked_when_components_blocked():
    report = readiness({"baseline_superiority": "BLOCKED_BELOW_BASELINE"})

    assert report["overall_status"] == "BLOCKED_BELOW_BASELINE"
    assert report["primary_blocker"] == "BLOCKED_BELOW_BASELINE"
    assert report["execution_allowed"] is False
    assert report["paper_only"] is True


def test_extract_prediction_linkage_metadata_copies_direct_prediction_dict():
    metadata = extract_prediction_linkage_metadata(
        {
            "prediction_id": "pred-direct",
            "predicted_probability": 0.77,
            "model_version": "model-direct",
            "evaluation_contract": "contract-direct",
            "target_timestamp": "2026-06-23T00:00:00Z",
            "source_signal_timestamp": "2026-06-22T00:00:00Z",
            "symbol": "BTCUSDT",
        }
    )

    assert metadata == {
        "prediction_id": "pred-direct",
        "predicted_probability": 0.77,
        "model_version": "model-direct",
        "evaluation_contract": "contract-direct",
        "target_timestamp": "2026-06-23T00:00:00Z",
        "source_signal_timestamp": "2026-06-22T00:00:00Z",
        "symbol": "BTCUSDT",
    }


def test_extract_prediction_linkage_metadata_supports_nested_prediction_ml_payload():
    metadata = extract_prediction_linkage_metadata(
        {
            "symbol": "ETHUSDT",
            "payload_json": {
                "prediction": {
                    "prediction_id": "pred-nested",
                    "probability": 0.81,
                    "target_timestamp": "2026-06-24T00:00:00Z",
                },
                "ml": {
                    "model_version": "model-nested",
                    "evaluation_contract": "contract-nested",
                },
            },
        }
    )

    assert metadata["prediction_id"] == "pred-nested"
    assert metadata["predicted_probability"] == 0.81
    assert metadata["model_version"] == "model-nested"
    assert metadata["evaluation_contract"] == "contract-nested"
    assert metadata["target_timestamp"] == "2026-06-24T00:00:00Z"
    assert metadata["symbol"] == "ETHUSDT"


def test_extract_prediction_linkage_metadata_does_not_synthesize_prediction_id_when_missing():
    metadata = extract_prediction_linkage_metadata(
        {"symbol": "BTCUSDT", "timestamp": "2026-06-22T00:00:00Z"}
    )

    assert "prediction_id" not in metadata
    assert metadata["symbol"] == "BTCUSDT"
    assert metadata["source_signal_timestamp"] == "2026-06-22T00:00:00Z"


def test_opened_internal_paper_trade_carries_prediction_metadata(tmp_path):
    db_path = tmp_path / "paper_open.db"
    trade = {
        "timestamp": "2026-06-22T00:00:00Z",
        "source_signal_timestamp": "2026-06-22T00:00:00Z",
        "symbol": "BTCUSDT",
        "market_type": "crypto",
        "side": "LONG",
        "entry_price": 100.0,
        "current_price": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "exit_price": None,
        "pnl": 0.0,
        "confidence": 0.8,
        "regime": "NORMAL",
        "macro_state": "NORMAL",
        "allocation_tier": "WATCH",
        "status": "OPEN",
        "exit_reason": "",
        "updated_at": "2026-06-22T00:00:00Z",
        "payload_json": "{}",
        "prediction_id": "pred-open",
        "predicted_probability": 0.84,
        "model_version": "model-open",
        "evaluation_contract": "contract-open",
        "target_timestamp": "2026-06-23T00:00:00Z",
    }

    assert _insert_trade(str(db_path), trade) is True
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT prediction_id, predicted_probability, model_version, "
            "evaluation_contract, target_timestamp FROM internal_paper_trades"
        ).fetchone()

    assert row == (
        "pred-open",
        0.84,
        "model-open",
        "contract-open",
        "2026-06-23T00:00:00Z",
    )


def test_closed_outcome_assembly_emits_carried_prediction_metadata(tmp_path):
    db_path = tmp_path / "paper_close.db"
    trade = {
        "timestamp": "2026-06-22T00:00:00Z",
        "source_signal_timestamp": "2026-06-22T00:00:00Z",
        "symbol": "BTCUSDT",
        "market_type": "crypto",
        "side": "LONG",
        "entry_price": 100.0,
        "current_price": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "exit_price": None,
        "pnl": 0.0,
        "confidence": 0.8,
        "regime": "NORMAL",
        "macro_state": "NORMAL",
        "allocation_tier": "WATCH",
        "status": "OPEN",
        "exit_reason": "",
        "updated_at": "2026-06-22T00:00:00Z",
        "payload_json": "{}",
        "prediction_id": "pred-close",
        "predicted_probability": 0.86,
        "model_version": "model-close",
        "evaluation_contract": "contract-close",
        "target_timestamp": "2026-06-23T00:00:00Z",
    }
    _insert_trade(str(db_path), trade)

    assert _update_open_trades(str(db_path), {"BTCUSDT": 106.0}) == 1
    report = generate_paper_outcome_audit(
        db_path=str(db_path),
        output_path=str(tmp_path / "paper_outcome_audit.json"),
        write_report=False,
    )

    row = report["closed_trades"][0]
    assert row["prediction_id"] == "pred-close"
    assert row["predicted_probability"] == 0.86
    assert row["model_version"] == "model-close"
    assert row["evaluation_contract"] == "contract-close"
    assert row["target_timestamp"] == "2026-06-23T00:00:00Z"
    assert row["prediction_outcome_linkage_flags"] == []


def test_missing_upstream_prediction_metadata_is_non_fatal(tmp_path):
    db_path = tmp_path / "paper_missing.db"
    trade = {
        "timestamp": "2026-06-22T00:00:00Z",
        "source_signal_timestamp": "2026-06-22T00:00:00Z",
        "symbol": "BTCUSDT",
        "market_type": "crypto",
        "side": "LONG",
        "entry_price": 100.0,
        "current_price": 100.0,
        "sl": 98.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "exit_price": None,
        "pnl": 0.0,
        "confidence": 0.8,
        "regime": "NORMAL",
        "macro_state": "NORMAL",
        "allocation_tier": "WATCH",
        "status": "OPEN",
        "exit_reason": "",
        "updated_at": "2026-06-22T00:00:00Z",
        "payload_json": "{}",
    }
    _insert_trade(str(db_path), trade)
    _update_open_trades(str(db_path), {"BTCUSDT": 97.0})

    report = generate_paper_outcome_audit(
        db_path=str(db_path),
        output_path=str(tmp_path / "paper_outcome_audit.json"),
        write_report=False,
    )

    flags = report["closed_trades"][0]["prediction_outcome_linkage_flags"]
    assert "MISSING_PREDICTION_ID" in flags
    assert "MISSING_PREDICTED_PROBABILITY" in flags
