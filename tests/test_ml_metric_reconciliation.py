import json
import os
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from ml_metric_reconciliation import (
    atomic_write_json,
    baseline_status,
    candidate_evidence_bridge,
    classification_metrics,
    connect_readonly,
    discover_artifacts,
    label_contract_audit,
    leakage_status,
    load_prediction_cohort,
    normalize_sqlite_readonly_uri,
    producer_inventory,
    readiness,
    reconstruct_walkforward,
    run_ml_metric_reconciliation,
    segment_performance,
    write_csv,
)


def write_prediction_csv(path: Path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)


def test_target_cannot_be_reused_as_prediction(tmp_path):
    path = tmp_path / "pred.csv"
    write_prediction_csv(path, [{"target": "WIN", "prediction_timestamp": "2024-01-01", "target_maturity_timestamp": "2024-01-02", "model_version": "m1", "evaluation_contract": "c"}])
    result = load_prediction_cohort(str(path))
    assert result["status"] == "UNREPRODUCIBLE"
    assert "y_pred" in result["reason"]


def test_real_y_true_y_pred_metrics_wrong_predictions_not_100(tmp_path):
    path = tmp_path / "pred.csv"
    write_prediction_csv(path, [
        {"y_true": "WIN", "y_pred": "LOSS", "prediction_timestamp": "2024-01-01", "target_maturity_timestamp": "2024-01-02", "model_version": "m1", "evaluation_contract": "c"},
        {"y_true": "LOSS", "y_pred": "WIN", "prediction_timestamp": "2024-01-02", "target_maturity_timestamp": "2024-01-03", "model_version": "m1", "evaluation_contract": "c"},
    ])
    result = load_prediction_cohort(str(path))
    metrics = classification_metrics(result["frame"]["__y_true"].tolist(), result["frame"]["__y_pred"].tolist(), ["WIN", "LOSS"])
    assert result["status"] == "AVAILABLE"
    assert metrics["accuracy"] == 0.0


def test_missing_predictions_yield_null_metrics_and_zero_samples():
    m = classification_metrics([], [], ["WIN", "LOSS"])
    assert m["accuracy"] is None
    assert m["balanced_accuracy"] is None
    assert m["confusion_matrix"][0]["actual_class"] == "NO_EVALUATION_SAMPLE"
    assert baseline_status(None)["status"] == "BLOCKED_INSUFFICIENT_SAMPLE"


def test_displayed_value_reproduction_and_stale_discovery(tmp_path):
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65, "rows": 100}), encoding="utf-8")
    artifacts = discover_artifacts(db_path=str(tmp_path / "missing.db"), model_output_path=str(model), walkforward_path=str(tmp_path / "missing.csv"))
    inv = producer_inventory(artifacts)
    assert any(row["source_verified"] for row in inv if row["metric_name"] == "Current Model Accuracy")
    assert any(row["reproducibility_status"] == "SOURCE_MISSING" for row in inv if row["metric_name"] == "Walk-Forward Rolling Accuracy")


def test_model_health_and_overfit_not_reproduced_without_walkforward_source(tmp_path, monkeypatch):
    model = tmp_path / "model_output.json"
    model.write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(db_path="missing.db", model_output_path=str(model), walkforward_path="missing.csv")
    ids = {row["metric_name"]: row for row in report["metric_identity"]}
    assert ids["Model Health"]["reproducibility_status"] == "SOURCE_MISSING"
    assert ids["Overfit Risk"]["reproducibility_status"] == "SOURCE_MISSING"


def test_strict_readonly_sqlite_uri_missing_db_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    uri = normalize_sqlite_readonly_uri(str(missing))
    assert uri.startswith("file:") and uri.endswith("?mode=ro")
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(str(missing))
    assert not missing.exists()


def test_readonly_db_cannot_write_and_metadata_unchanged(tmp_path):
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table t(id integer)")
        connection.execute("insert into t values (1)")
    size = db.stat().st_size
    mtime = db.stat().st_mtime_ns
    with connect_readonly(str(db)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("insert into t values (2)")
        rows = connection.execute("select count(*) from t").fetchone()[0]
    assert rows == 1
    assert db.stat().st_size == size
    assert db.stat().st_mtime_ns == mtime


def test_leakage_detection_modes():
    train = pd.DataFrame({"timestamp": ["2024-01-02"], "symbol": ["BTC"]})
    test = pd.DataFrame({"timestamp": ["2024-01-01"], "symbol": ["BTC"]})
    assert leakage_status(train, test)["status"] == "BLOCKED_TEMPORAL_LEAKAGE"
    assert leakage_status(train, train.copy())["status"] in {"BLOCKED_TEMPORAL_LEAKAGE", "BLOCKED_SPLIT_CONTAMINATION"}
    assert "BLOCKED_TARGET_LEAKAGE" in leakage_status(train, test, feature_cols=["pnl_percent"])["reasons"]
    assert leakage_status(pd.DataFrame(), test)["status"] == "UNVERIFIABLE"


def test_walkforward_aggregates_latest_worst_weighted_and_unverifiable(tmp_path):
    p = tmp_path / "walk.csv"
    pd.DataFrame([
        {"fold": 1, "train_start": 0, "train_end": 10, "test_start": 11, "test_end": 20, "test_accuracy": 0.4, "train_rows": 10, "test_rows": 10},
        {"fold": 2, "train_start": 10, "train_end": 20, "test_start": 21, "test_end": 40, "test_accuracy": 0.8, "train_rows": 10, "test_rows": 30},
    ]).to_csv(p, index=False)
    r = reconstruct_walkforward(str(p))
    assert r["fold_count"] == 2
    assert r["unweighted_aggregate"] == 0.6
    assert r["weighted_aggregate"] == 0.7
    assert r["latest_fold"]["fold_id"] == 2
    assert r["worst_fold"]["accuracy"] == 0.4
    assert all(f["leakage_status"] == "UNVERIFIABLE" for f in r["folds"])


def test_walkforward_weighted_null_without_test_rows_and_overlap_block(tmp_path):
    p = tmp_path / "walk.csv"
    pd.DataFrame([{"fold": 1, "train_start": 0, "train_end": 10, "test_start": 10, "test_end": 20, "test_accuracy": 0.4}]).to_csv(p, index=False)
    r = reconstruct_walkforward(str(p))
    assert r["weighted_aggregate"] is None
    assert r["weighted_aggregate_reason"]
    assert r["folds"][0]["leakage_status"] == "BLOCKED_TEMPORAL_LEAKAGE"


def test_atomic_json_and_deterministic_csv(tmp_path):
    j = tmp_path / "x.json"
    atomic_write_json(j, {"b": 1, "a": 2})
    assert json.loads(j.read_text()) == {"a": 2, "b": 1}
    c = tmp_path / "x.csv"
    write_csv(c, [{"a": "2", "b": "b"}, {"a": "1", "b": "a"}], ["a", "b"])
    assert c.read_text().splitlines() == ["a,b", "1,a", "2,b"]


def test_segments_use_valid_cohort_only_and_model_version_separation():
    cohort = pd.DataFrame({"__y_true": ["WIN", "LOSS"], "__y_pred": ["WIN", "WIN"], "regime_name": ["A", "A"], "model_version": ["m1", "m2"]})
    seg = segment_performance(cohort, min_samples=3)
    assert seg
    assert all(row["readiness_status"] == "BLOCKED_INSUFFICIENT_SAMPLE" for row in seg)
    assert segment_performance(pd.DataFrame()) == []


def test_label_contract_discovery():
    label = label_contract_audit()
    assert label["source_module"] == "outcome_labeler.py"
    assert label["horizon"] == "holding_candles default 20"
    assert label["fees_slippage"] == "not applied in label_historical_outcomes"


def test_candidate_pending_excluded_and_ready_horizons_separated(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"status": "RECORDED", "horizon": "24h", "direction_hit": True},
        {"status": "RECORDED", "horizon": "48h", "direction_hit": False},
        {"status": "PENDING", "horizon": "72h", "direction_hit": True},
        {"status": "BLOCKED", "horizon": "24h", "direction_hit": True},
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    bridge = candidate_evidence_bridge(str(ledger), model_sample=2, paper_sample=3)
    assert bridge["candidate_evidence_population"]["samples"] == 2
    assert bridge["by_horizon"]["24h"]["samples"] == 1
    assert bridge["by_horizon"]["48h"]["direction_accuracy"] == 0.0
    assert bridge["by_horizon"]["72h"]["samples"] == 0


def test_readiness_preserves_all_blockers():
    r = readiness({"Metric Integrity": "BLOCKED_UNREPRODUCIBLE", "Label Integrity": "BLOCKED_LABEL_CONTRACT", "Leakage Safety": "REVIEW"})
    assert r["primary_blocker"] in {"BLOCKED_LABEL_CONTRACT", "BLOCKED_UNREPRODUCIBLE"}
    assert set(r["all_blockers"]) == {"Metric Integrity", "Label Integrity"}
    assert "Leakage Safety" in r["review_reasons"]


def test_full_audit_empty_data_governance_no_mutation_and_context(tmp_path, monkeypatch):
    model = tmp_path / "model_output.json"
    model.write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65}), encoding="utf-8")
    mt = model.stat().st_mtime_ns
    monkeypatch.chdir(tmp_path)
    report = run_ml_metric_reconciliation(output_dir="reports", db_path="missing.db", model_output_path=str(model), walkforward_path="missing.csv")
    assert report["artifact_context"] == "NON_PRODUCTION_EMPTY_FIXTURE"
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
    assert report["governance"]["model_promotion_allowed"] is False
    assert report["reproduced_metrics"]["metrics"] is None
    assert report["baseline_comparison"]["status"] == "BLOCKED_INSUFFICIENT_SAMPLE"
    assert report["segment_performance"] == []
    assert not Path("missing.db").exists()
    assert model.stat().st_mtime_ns == mt


def test_walkforward_display_reproduces_winrate_overfit_and_robust(tmp_path):
    from ml_metric_reconciliation import summarize_walkforward_display, metric_identity
    wf = tmp_path / "walk.csv"
    pd.DataFrame([
        {"fold": 1, "train_start": 0, "train_end": 10, "test_start": 11, "test_end": 20, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68, "train_rows": 10, "test_rows": 10},
        {"fold": 2, "train_start": 20, "train_end": 30, "test_start": 31, "test_end": 40, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68, "train_rows": 10, "test_rows": 10},
    ]).to_csv(wf, index=False)
    summary = summarize_walkforward_display(str(wf))
    assert summary["average_winrate"] == 45.68
    assert summary["overfit_risk_score"] == 35.55
    assert summary["model_health"] == "ROBUST"
    ids = {row["metric_name"]: row for row in metric_identity([], reconstruct_walkforward(str(wf)), {}, summary, {"status": "SOURCE_MISSING"})}
    assert ids["Walk-Forward Rolling Winrate"]["display_reproduction_status"] == "REPRODUCED_EXACT"
    assert ids["Overfit Risk"]["display_reproduction_status"] == "REPRODUCED_EXACT"
    assert ids["Model Health"]["display_reproduction_status"] == "REPRODUCED_EXACT"


def test_historical_6640_artifact_parsed_and_display_separated(tmp_path):
    from ml_metric_reconciliation import parse_historical_ml_artifact, metric_identity
    hist = tmp_path / "ml_quality_audit.json"
    hist.write_text(json.dumps({"global_accuracy": 0.6640, "rows": 50}), encoding="utf-8")
    parsed = parse_historical_ml_artifact(str(hist))
    ids = {row["metric_name"]: row for row in metric_identity([{"artifact_name": "ml_quality_audit", "exists": True}], {"status": "SOURCE_MISSING", "fold_count": 0}, {}, {"status": "SOURCE_MISSING"}, parsed)}
    assert parsed["global_accuracy"] == 0.664
    assert ids["Historical ML accuracy snapshot"]["display_reproduction_status"] == "REPRODUCED_EXACT"
    assert ids["Historical ML accuracy snapshot"]["evaluation_reproduction_status"] == "UNREPRODUCIBLE"


def test_stale_ttl_enforced_in_artifact_discovery(tmp_path):
    model = tmp_path / "model.json"
    model.write_text("{}", encoding="utf-8")
    old = 1_600_000_000
    os.utime(model, (old, old))
    artifacts = discover_artifacts(db_path=str(tmp_path / "missing.db"), model_output_path=str(model), walkforward_path=str(tmp_path / "missing.csv"), stale_ttl_days=0.0001)
    model_artifact = next(item for item in artifacts if item["artifact_name"] == "model_output")
    assert model_artifact["stale_source"] is True
    inv = producer_inventory(artifacts)
    assert next(row for row in inv if row["metric_name"] == "Current Model Accuracy")["reproducibility_status"] == "SOURCE_STALE"


def test_dataset_lineage_readonly_does_not_create_db(tmp_path):
    from ml_metric_reconciliation import dataset_lineage_readonly
    missing = tmp_path / "missing.db"
    lineage = dataset_lineage_readonly(str(missing))
    assert lineage["status"] == "SOURCE_MISSING"
    assert lineage["read_only"] is True
    assert not missing.exists()


def test_model_health_contract_different_when_computed_not_robust(tmp_path):
    from ml_metric_reconciliation import summarize_walkforward_display, metric_identity
    wf = tmp_path / "walk.csv"
    pd.DataFrame([
        {"fold": 1, "train_start": "2024-01-01", "train_end": "2024-01-02", "test_start": "2024-01-03", "test_end": "2024-01-04", "train_accuracy": 0.95, "test_accuracy": 0.10, "winrate": 10, "train_rows": 10, "test_rows": 10}
    ]).to_csv(wf, index=False)
    summary = summarize_walkforward_display(str(wf))
    ids = {row["metric_name"]: row for row in metric_identity([], reconstruct_walkforward(str(wf)), {}, summary, {"status": "SOURCE_MISSING"})}
    assert summary["model_health"] == "OVERFIT RISK"
    assert ids["Model Health"]["display_reproduction_status"] == "CONTRACT_DIFFERENT"


def test_segment_readiness_baseline_gates():
    from ml_metric_reconciliation import segment_readiness_status
    below = {"samples": 50, "accuracy": 0.50, "majority_class_baseline": 0.60, "balanced_accuracy": 0.5, "macro_f1": 0.5}
    marginal = {"samples": 50, "accuracy": 0.621, "majority_class_baseline": 0.60, "balanced_accuracy": 0.62, "macro_f1": 0.62}
    meaningful = {"samples": 50, "accuracy": 0.70, "majority_class_baseline": 0.60, "balanced_accuracy": 0.70, "macro_f1": 0.70}
    assert segment_readiness_status(below, 10)[0] == "BLOCKED_BELOW_BASELINE"
    assert segment_readiness_status(marginal, 10)[0] == "REVIEW_MARGINAL"
    assert segment_readiness_status(meaningful, 10)[0] == "PASS"


def test_metric_integrity_preserves_stale_and_contract_different():
    from ml_metric_reconciliation import metric_integrity_summary
    result = metric_integrity_summary([
        {"metric_name": "A", "mandatory_current_readiness": True, "reproducibility_status": "SOURCE_STALE", "identity": "old"},
        {"metric_name": "B", "mandatory_current_readiness": True, "reproducibility_status": "CONTRACT_DIFFERENT", "identity": "diff"},
        {"metric_name": "Historical", "mandatory_current_readiness": False, "reproducibility_status": "SOURCE_MISSING", "identity": "advisory"},
    ])
    assert result["primary_metric_integrity_blocker"] == "BLOCKED_STALE_SOURCE"
    assert {b["blocker"] for b in result["all_mandatory_identity_blockers"]} == {"BLOCKED_STALE_SOURCE", "BLOCKED_CONTRACT_DIFFERENT"}


def test_data_lineage_status_not_all_stale_and_future_blocks():
    from ml_metric_reconciliation import data_lineage_status
    assert data_lineage_status({"status": "SOURCE_MISSING", "row_count": 0}) == "BLOCKED_UNREPRODUCIBLE"
    assert data_lineage_status({"status": "AVAILABLE", "row_count": 0}) == "BLOCKED_INSUFFICIENT_OOS"
    assert data_lineage_status({"status": "AVAILABLE", "row_count": 1, "future_timestamps": 1}) == "BLOCKED_LEAKAGE"
    assert data_lineage_status({"status": "AVAILABLE", "row_count": 1, "future_timestamps": 0, "duplicate_rows": 0}) == "PASS"


def test_internal_generated_timestamp_overrides_mtime_and_is_persisted(tmp_path):
    from datetime import datetime, timezone
    from ml_metric_reconciliation import discover_artifacts
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"generated_at": "2026-06-20T00:00:00Z"}), encoding="utf-8")
    os.utime(model, (1_600_000_000, 1_600_000_000))
    artifact = next(a for a in discover_artifacts(db_path=str(tmp_path / "missing.db"), model_output_path=str(model), walkforward_path=str(tmp_path / "missing.csv")) if a["artifact_name"] == "model_output")
    assert artifact["age_source"] == "internal_timestamp"
    assert artifact["timestamp_field_used"] == "generated_at"
    assert artifact["generated_timestamp"].startswith("2026-06-20")


def test_index_gap_not_temporal_embargo(tmp_path):
    wf = tmp_path / "walk.csv"
    pd.DataFrame([{"fold": 1, "train_start": 0, "train_end": 10, "test_start": 11, "test_end": 20, "test_accuracy": 0.5, "train_rows": 10, "test_rows": 10}]).to_csv(wf, index=False)
    fold = reconstruct_walkforward(str(wf))["folds"][0]
    assert fold["index_gap"] == 1
    assert fold["temporal_embargo"] is None
    assert fold["leakage_status"] == "UNVERIFIABLE"


def test_explicit_missing_custom_walkforward_does_not_use_repository_fallback(tmp_path, monkeypatch):
    repo_wf = tmp_path / "walkforward_results.csv"
    pd.DataFrame([{"fold": 1, "test_accuracy": 0.6438}]).to_csv(repo_wf, index=False)
    custom = tmp_path / "missing_custom.csv"
    monkeypatch.chdir(tmp_path)
    artifacts = discover_artifacts(db_path=str(tmp_path / "missing.db"), walkforward_path=str(custom))
    wf = next(item for item in artifacts if item["artifact_name"] == "walkforward_results")
    assert wf["exists"] is False
    assert wf["discovered_path"] is None
    assert wf["explicit_path_authoritative"] is True


def test_default_walkforward_path_can_use_documented_fallback(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    fallback = reports / "walkforward_results.csv"
    pd.DataFrame([{"fold": 1, "test_accuracy": 0.6438}]).to_csv(fallback, index=False)
    monkeypatch.chdir(tmp_path)
    artifacts = discover_artifacts(db_path=str(tmp_path / "missing.db"), walkforward_path="walkforward_results.csv")
    wf = next(item for item in artifacts if item["artifact_name"] == "walkforward_results")
    assert wf["exists"] is True
    assert wf["discovered_path"].endswith("reports/walkforward_results.csv")
    assert wf["fallbacks_allowed"] is True


def test_production_default_sources_without_prediction_cohort_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("model_output.json").write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65}), encoding="utf-8")
    report = run_ml_metric_reconciliation(output_dir="reports")
    assert report["artifact_context"] == "RUNTIME_AUDIT_NO_PREDICTION_COHORT"
    assert report["reproduced_metrics"]["metrics"] is None


def test_artifact_discovery_and_lineage_agree_on_historical_outcomes(tmp_path):
    db = tmp_path / "lineage.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table historical_outcomes(symbol text, signal_timestamp text, status text)")
        connection.execute("insert into historical_outcomes values ('BTCUSDT', '2024-01-01 00:00:00', 'WIN')")
    artifacts = discover_artifacts(db_path=str(db), walkforward_path=str(tmp_path / "missing.csv"))
    hist = next(item for item in artifacts if item["artifact_name"] == "database_table:historical_outcomes")
    from ml_metric_reconciliation import dataset_lineage_readonly
    lineage = dataset_lineage_readonly(str(db))
    assert hist["exists"] is True
    assert lineage["status"] == "AVAILABLE"
    assert hist["row_count"] == lineage["row_count"] == 1
    assert hist["sqlite_diagnostics"]["normalized_database_path"] == lineage["normalized_database_path"]
    assert hist["sqlite_diagnostics"]["query_status"] == lineage["query_status"] == "OK"


def test_sqlite_diagnostics_preserved_for_missing_database(tmp_path):
    artifacts = discover_artifacts(db_path=str(tmp_path / "missing.db"), walkforward_path=str(tmp_path / "missing.csv"))
    hist = next(item for item in artifacts if item["artifact_name"] == "database_table:historical_outcomes")
    diag = hist["sqlite_diagnostics"]
    assert diag["database_file_exists"] is False
    assert diag["table_lookup_result"] is False
    assert diag["schema"] == []
    assert diag["row_count"] == 0
    assert diag["query_status"] == "DATABASE_MISSING"
    assert diag["sqlite_exception"] is None


def test_display_integrity_passes_while_evaluation_integrity_reviews_or_blocks(tmp_path):
    from ml_metric_reconciliation import metric_display_integrity_summary, metric_evaluation_integrity_summary, metric_identity, reconstruct_walkforward, summarize_walkforward_display
    wf = tmp_path / "walkforward_results.csv"
    pd.DataFrame([
        {"fold": 1, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68},
        {"fold": 2, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68},
    ]).to_csv(wf, index=False)
    model = {"accuracy": 0.3281, "ai_confidence_score": 65}
    ids = metric_identity([], reconstruct_walkforward(str(wf)), model, summarize_walkforward_display(str(wf)), {"status": "SOURCE_MISSING"})
    display = metric_display_integrity_summary(ids)
    evaluation = metric_evaluation_integrity_summary(ids)
    assert display["status"] == "PASS"
    assert evaluation["status"] in {"REVIEW", "BLOCKED_UNREPRODUCIBLE"}


def test_overall_readiness_remains_fail_closed_with_display_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("model_output.json").write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65}), encoding="utf-8")
    pd.DataFrame([
        {"fold": 1, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68},
        {"fold": 2, "train_accuracy": 0.9993, "test_accuracy": 0.6438, "winrate": 45.68},
    ]).to_csv("walkforward_results.csv", index=False)
    report = run_ml_metric_reconciliation(output_dir="reports")
    assert report["display_metric_integrity_summary"]["status"] == "PASS"
    assert report["evaluation_metric_integrity_summary"]["status"] in {"REVIEW", "BLOCKED_UNREPRODUCIBLE"}
    assert report["model_readiness"]["overall_status"] != "PASS"
    assert report["governance"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["automatic_promotion_allowed"] is False
