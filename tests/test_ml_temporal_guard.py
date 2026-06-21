import pandas as pd

from ml_temporal_guard import asof_feature_join, target_like_feature_columns, validate_temporal_feature_rows
from ml_metric_reconciliation import run_ml_metric_reconciliation


def test_feature_timestamp_at_or_before_prediction_passes():
    result = validate_temporal_feature_rows([
        {"prediction_timestamp": "2024-01-01T01:00:00Z", "feature_timestamp_max": "2024-01-01T00:59:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "score": 1.0}
    ], feature_columns=["score"])
    assert result["status"] == "PASS"


def test_feature_timestamp_after_prediction_is_blocked():
    result = validate_temporal_feature_rows([
        {"prediction_timestamp": "2024-01-01T01:00:00Z", "feature_timestamp_max": "2024-01-01T01:01:00Z", "score": 1.0}
    ], feature_columns=["score"])
    assert result["status"] == "BLOCKED"
    assert result["future_feature_violation_count"] == 1


def test_missing_timestamp_metadata_is_not_pass():
    result = validate_temporal_feature_rows([{"prediction_timestamp": "2024-01-01T01:00:00Z", "score": 1.0}], feature_columns=["score"])
    assert result["status"] in {"REVIEW", "BLOCKED"}
    assert result["status"] != "PASS"


def test_label_outcome_columns_rejected_from_model_features():
    leaked = target_like_feature_columns(["score", "outcome_status", "future_return", "regime_name"])
    assert "outcome_status" in leaked
    assert "future_return" in leaked
    result = validate_temporal_feature_rows([
        {"prediction_timestamp": "2024-01-01T01:00:00Z", "feature_timestamp_max": "2024-01-01T00:59:00Z", "outcome_status": "WIN"}
    ], feature_columns=["score", "outcome_status"])
    assert result["status"] == "BLOCKED"
    assert result["target_leakage_column_count"] == 1


def test_asof_join_chooses_latest_not_after_prediction():
    preds = pd.DataFrame({"symbol": ["BTC"], "prediction_timestamp": ["2024-01-01T01:00:00Z"]})
    feats = pd.DataFrame({
        "symbol": ["BTC", "BTC", "BTC"],
        "timestamp": ["2024-01-01T00:30:00Z", "2024-01-01T00:59:00Z", "2024-01-01T01:01:00Z"],
        "score": [1, 2, 999],
    })
    joined = asof_feature_join(preds, feats)
    assert int(joined.loc[0, "score"]) == 2
    assert str(joined.loc[0, "feature_timestamp_max"]) == "2024-01-01 00:59:00+00:00"


def test_metric_audit_fail_closed_without_valid_prediction_cohort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["temporal_feature_guard_status"] in {"REVIEW", "BLOCKED"}
    assert report["model_readiness"]["overall_status"] != "PASS"


def test_governance_invariants_remain_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["governance"]["model_promotion_allowed"] is False


def test_asof_join_multi_symbol_is_group_safe_and_ignores_future_rows():
    preds = pd.DataFrame({
        "symbol": ["ETH", "BTC", "ETH", "XRP"],
        "prediction_timestamp": [
            "2024-01-01T01:00:00Z",
            "2024-01-01T00:45:00Z",
            "2024-01-01T02:00:00Z",
            "2024-01-01T01:30:00Z",
        ],
    })
    feats = pd.DataFrame({
        "symbol": ["BTC", "BTC", "ETH", "ETH", "ETH", "XRP"],
        "timestamp": [
            "2024-01-01T00:30:00Z",
            "2024-01-01T00:50:00Z",
            "2024-01-01T00:59:00Z",
            "2024-01-01T01:30:00Z",
            "2024-01-01T02:01:00Z",
            "2024-01-01T01:31:00Z",
        ],
        "score": [10, 999, 20, 21, 999, 999],
    })

    joined = asof_feature_join(preds, feats)

    assert len(joined) == len(preds)
    assert joined["symbol"].tolist() == ["ETH", "BTC", "ETH", "XRP"]
    assert joined["score"].tolist()[:3] == [20, 10, 21]
    assert pd.isna(joined.loc[3, "score"])
    assert pd.isna(joined.loc[3, "feature_timestamp_max"])
    assert all(
        pd.isna(row.feature_timestamp_max) or row.feature_timestamp_max <= row.prediction_timestamp
        for row in joined.itertuples(index=False)
    )
