import json
from pathlib import Path

import pandas as pd

from ml_metric_reconciliation import row_level_walkforward_audit, run_ml_metric_reconciliation


def _rows(labels, preds):
    return [
        {
            "prediction_id": f"p{i}",
            "prediction_timestamp": f"2024-01-{i+1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-01-{i+2:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-01-{i+1:02d}T00:00:00Z",
            "symbol": "BTCUSDT",
            "y_true": y,
            "y_pred": p,
            "predicted_probability": 0.8,
            "model_version": "fixture-v1",
            "evaluation_contract": "canonical-direction-v1",
        }
        for i, (y, p) in enumerate(zip(labels, preds))
    ]


def _guards():
    return {"status": "PASS"}, {"train_only_preprocessing_status": "PASS"}


def test_row_level_walkforward_report_generated_from_fixture(tmp_path, monkeypatch):
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    preds = ["WIN"] * 6 + ["LOSS", "LOSS", "LOSS"] + ["LOSS", "WIN", "WIN"]
    pred = tmp_path / "predictions.csv"
    pd.DataFrame(_rows(labels, preds)).to_csv(pred, index=False)
    monkeypatch.chdir(tmp_path)

    report = run_ml_metric_reconciliation(output_dir="reports", prediction_artifact_path=str(pred), model_output_path="missing.json", walkforward_path="missing.csv")

    wf_json = Path("reports/ml_row_level_walkforward.json")
    wf_rows = Path("reports/ml_row_level_walkforward_rows.csv")
    assert wf_json.exists()
    assert wf_rows.exists()
    assert report["row_level_walkforward_rows"] == 6
    assert report["row_level_walkforward_folds"] == 2
    assert report["baseline_accuracy"] == 0.333333
    assert report["model_accuracy"] == 0.666667
    assert report["baseline_superiority_status"] == "BLOCKED_INSUFFICIENT_TEST_ROWS"
    assert json.loads(wf_json.read_text())["row_level_walkforward_folds"] == 2


def test_each_prediction_row_has_fold_metadata_and_prediction_timestamp():
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    audit = row_level_walkforward_audit(pd.DataFrame(_rows(labels, labels)), *_guards(), min_test_rows=6)
    assert audit["rows"]
    for row in audit["rows"]:
        assert row["fold_id"]
        assert row["train_start"]
        assert row["train_end"]
        assert row["test_start"]
        assert row["test_end"]
        assert row["prediction_timestamp"]


def test_majority_baseline_computed_from_train_fold():
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"]
    preds = ["WIN"] * 9
    audit = row_level_walkforward_audit(pd.DataFrame(_rows(labels, preds)), *_guards(), min_folds=1, min_test_rows=3)
    assert audit["row_level_walkforward_folds"] == 1
    assert {row["baseline_prediction"] for row in audit["rows"]} == {"WIN"}
    assert audit["baseline_accuracy"] == 0.333333


def test_model_superiority_pass_only_when_beats_baseline_with_enough_rows_and_folds():
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    preds = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    audit = row_level_walkforward_audit(pd.DataFrame(_rows(labels, preds)), *_guards(), min_folds=2, min_test_rows=6)
    assert audit["baseline_accuracy"] == 0.333333
    assert audit["model_accuracy"] == 1.0
    assert audit["baseline_superiority_status"] == "PASS_BASELINE_SUPERIORITY"


def test_model_superiority_blocked_when_rows_or_folds_insufficient():
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"]
    audit = row_level_walkforward_audit(pd.DataFrame(_rows(labels, labels)), *_guards(), min_folds=2, min_test_rows=3)
    assert audit["baseline_superiority_status"] == "BLOCKED_INSUFFICIENT_FOLDS"
    audit = row_level_walkforward_audit(pd.DataFrame(_rows(labels, labels)), *_guards(), min_folds=1, min_test_rows=4)
    assert audit["baseline_superiority_status"] == "BLOCKED_INSUFFICIENT_TEST_ROWS"


def test_temporal_and_preprocessing_guards_remain_integrated():
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    audit = row_level_walkforward_audit(
        pd.DataFrame(_rows(labels, labels)),
        {"status": "BLOCKED"},
        {"train_only_preprocessing_status": "PASS"},
        min_folds=2,
        min_test_rows=6,
    )
    assert audit["baseline_superiority_status"] == "BLOCKED_GUARD_FAILURE"


def test_no_execution_governance_changes(tmp_path, monkeypatch):
    pred = tmp_path / "predictions.csv"
    labels = ["WIN"] * 6 + ["LOSS", "LOSS", "WIN"] + ["LOSS", "WIN", "LOSS"]
    pd.DataFrame(_rows(labels, labels)).to_csv(pred, index=False)
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", prediction_artifact_path=str(pred), model_output_path="missing.json", walkforward_path="missing.csv")
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["governance"]["model_promotion_allowed"] is False
