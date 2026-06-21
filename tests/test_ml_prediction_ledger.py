import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ml_metric_reconciliation import run_ml_metric_reconciliation
from ml_prediction_ledger import (
    LEDGER_FIELDS,
    append_prediction,
    audit_prediction_ledger,
    canonical_ml_label,
    create_ledger_row,
    ensure_prediction_ledger,
    load_prediction_ledger,
    normalize_label,
)


def base_row(**overrides):
    row = {
        "candidate_id": "c1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "prediction_timestamp": "2026-01-01T00:00:00Z",
        "feature_timestamp_max": "2025-12-31T23:59:00Z",
        "target_horizon": "24h",
        "target_timestamp": "2026-01-02T00:00:00Z",
        "y_pred": "WIN",
        "predicted_probability": 0.61,
        "model_version": "m1",
        "feature_schema_version": "fs1",
        "fold_id": "f1",
        "train_window_start": "2025-01-01T00:00:00Z",
        "train_window_end": "2025-12-01T00:00:00Z",
        "test_window_start": "2025-12-02T00:00:00Z",
        "test_window_end": "2025-12-31T00:00:00Z",
        "label_source": "paper_outcome",
    }
    row.update(overrides)
    return row


def test_prediction_ledger_schema_creation(tmp_path):
    path = ensure_prediction_ledger(tmp_path / "ledger.jsonl")
    assert path.exists()
    row = create_ledger_row(**base_row())
    assert list(row.keys()) == LEDGER_FIELDS
    assert row["label_status"] == "PENDING"
    assert row["evaluation_status"] == "PENDING"


def test_ledger_append_behavior(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(candidate_id="c1"))
    append_prediction(path, base_row(candidate_id="c2", symbol="ETHUSDT"))
    rows = load_prediction_ledger(path)
    assert len(rows) == 2
    assert rows[0]["candidate_id"] == "c1"
    assert rows[1]["candidate_id"] == "c2"


def test_pending_label_remains_pending_until_horizon(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(raw_label_status="OPEN"))
    audit = audit_prediction_ledger(path, as_of="2026-01-01T12:00:00Z")
    assert audit["pending_prediction_rows"] == 1
    assert audit["matured_prediction_rows"] == 0
    assert load_prediction_ledger(path)[0]["y_true"] is None


def test_matured_label_can_be_attached_after_target_timestamp(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(raw_label_status="TP2_HIT", as_of="2026-01-03T00:00:00Z"))
    row = load_prediction_ledger(path)[0]
    assert row["y_true"] == "WIN"
    assert row["label_status"] == "MATURED"
    audit = audit_prediction_ledger(path, as_of="2026-01-03T00:00:00Z")
    assert audit["temporal_guard_status"] == "PASS"
    assert audit["evaluation_reproducibility_status"] == "PASS"


def test_future_feature_timestamp_blocks_readiness(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(feature_timestamp_max="2026-01-01T00:01:00Z"))
    audit = audit_prediction_ledger(path)
    assert audit["temporal_guard_status"] == "BLOCKED"
    assert audit["model_readiness_blocker"] == "BLOCKED_TEMPORAL_INTEGRITY"


def test_feature_timestamp_after_prediction_timestamp_blocks_readiness(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(feature_timestamp_max="2026-01-01T01:00:00Z"))
    audit = audit_prediction_ledger(path)
    assert "feature_timestamp_after_prediction_timestamp" in audit["temporal_findings"][0]["reasons"]


def test_training_window_overlapping_prediction_timestamp_blocks_readiness(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_prediction(path, base_row(train_window_end="2026-01-01T00:00:00Z"))
    audit = audit_prediction_ledger(path)
    assert "train_window_end_not_before_prediction_timestamp" in audit["temporal_findings"][0]["reasons"]


def test_label_mapping_is_deterministic():
    statuses = ["TP1", "TP2_HIT", "SL", "expired", "open", "flat", "mystery"]
    first = [canonical_ml_label(status) for status in statuses]
    second = [canonical_ml_label(status) for status in statuses]
    assert first == second
    assert first == ["WIN", "WIN", "LOSS", "NEUTRAL", "PENDING", "BREAKEVEN", "UNKNOWN"]


def test_unknown_status_maps_to_unknown_or_pending_not_win_loss():
    assert canonical_ml_label("mystery-status") == "UNKNOWN"
    assert normalize_label("OPEN")["canonical_label"] == "PENDING"
    assert canonical_ml_label("mystery-status") not in {"WIN", "LOSS"}


def test_audit_report_detects_ledger_presence(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    append_prediction(ledger, base_row())
    report = run_ml_metric_reconciliation(output_dir=str(tmp_path / "reports"), db_path=str(tmp_path / "missing.db"), model_output_path=str(tmp_path / "missing.json"), walkforward_path=str(tmp_path / "missing.csv"), prediction_ledger_path=str(ledger))
    assert report["prediction_ledger_available"] is True
    assert report["prediction_ledger_rows"] == 1
    assert Path(report["artifact_paths"]["prediction_ledger_audit_json"]).exists()


def test_audit_report_remains_fail_closed_when_ledger_missing(tmp_path):
    report = run_ml_metric_reconciliation(output_dir=str(tmp_path / "reports"), db_path=str(tmp_path / "missing.db"), model_output_path=str(tmp_path / "missing.json"), walkforward_path=str(tmp_path / "missing.csv"), prediction_ledger_path=str(tmp_path / "missing_ledger.jsonl"))
    assert report["prediction_ledger_available"] is False
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert report["evaluation_reproducibility_status"] == "BLOCKED"


def test_no_live_execution_flags_are_changed(tmp_path):
    report = run_ml_metric_reconciliation(output_dir=str(tmp_path / "reports"), db_path=str(tmp_path / "missing.db"), model_output_path=str(tmp_path / "missing.json"), walkforward_path=str(tmp_path / "missing.csv"), prediction_ledger_path=str(tmp_path / "missing_ledger.jsonl"))
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["model_readiness"]["execution_allowed"] is False


def test_paper_only_remains_true(tmp_path):
    report = run_ml_metric_reconciliation(output_dir=str(tmp_path / "reports"), db_path=str(tmp_path / "missing.db"), model_output_path=str(tmp_path / "missing.json"), walkforward_path=str(tmp_path / "missing.csv"), prediction_ledger_path=str(tmp_path / "missing_ledger.jsonl"))
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["paper_only"] is True
