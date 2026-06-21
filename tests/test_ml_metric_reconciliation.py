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
    closed_outcome_to_ml_cohort_coverage_audit,
    classification_metrics,
    connect_readonly,
    discover_artifacts,
    label_contract_audit,
    label_integrity_component_status,
    larger_fold_baseline_diagnostic,
    ml_class_imbalance_diagnostic,
    ml_high_confidence_threshold_candidate_diagnostic,
    ml_model_repair_upgrade_diagnostic_plan,
    paper_filter_candidate_registry,
    paper_filter_shadow_review_scorecard,
    prediction_outcome_linkage_contract_audit,
    prediction_outcome_linkage_producer_contract_plan,
    threshold_candidate_stability_audit,
    threshold_sample_sufficiency_audit,
    filtered_cohort_walkforward_comparison,
    leakage_status,
    load_prediction_cohort,
    normalize_sqlite_readonly_uri,
    producer_inventory,
    readiness,
    readiness_temporal_feature_guard,
    reconstruct_walkforward,
    raw_closed_outcome_source_discovery_audit,
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



def test_temporal_guard_ignores_cohort_metadata_outside_model_features():
    cohort = pd.DataFrame([
        {
            "prediction_timestamp": "2024-01-01T00:00:00Z",
            "feature_timestamp_max": "2023-12-31T23:59:00Z",
            "target_timestamp": "2024-01-02T00:00:00Z",
            "target_horizon": "24h",
            "target_label": "WIN",
            "label_status": "MATURED",
            "label_source": "prediction_ledger",
            "evaluation_status": "EVALUATED",
            "prediction_id": "p1",
            "model_version": "m1",
            "fold_id": 1,
            "train_start": "2023-12-01T00:00:00Z",
            "train_end": "2023-12-31T00:00:00Z",
            "test_start": "2024-01-01T00:00:00Z",
            "test_end": "2024-01-02T00:00:00Z",
            "y_true": "WIN",
            "y_pred": "WIN",
        }
    ])

    result = readiness_temporal_feature_guard(cohort)

    assert result["status"] == "PASS"
    assert result["target_leakage_column_count"] == 0
    assert not any(f.get("reason") == "label_or_outcome_columns_in_model_features" for f in result["temporal_guard_findings"])


def test_temporal_guard_blocks_when_actual_model_feature_scope_contains_label_columns():
    cohort = pd.DataFrame([
        {
            "prediction_timestamp": "2024-01-01T00:00:00Z",
            "feature_timestamp_max": "2023-12-31T23:59:00Z",
            "target_timestamp": "2024-01-02T00:00:00Z",
            "y_true": "WIN",
        }
    ])

    result = readiness_temporal_feature_guard(cohort, feature_columns=["score", "y_true", "target_timestamp"])

    assert result["status"] == "BLOCKED"
    assert result["target_leakage_column_count"] == 2
    finding = next(f for f in result["temporal_guard_findings"] if f.get("reason") == "label_or_outcome_columns_in_model_features")
    assert finding["columns"] == ["target_timestamp", "y_true"]


def test_readiness_leakage_safety_not_blocked_by_metadata_only_cohort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = tmp_path / "model_output.json"
    model.write_text(json.dumps({"accuracy": 0.3281, "ai_confidence_score": 65}), encoding="utf-8")
    cohort = tmp_path / "prediction_cohort.csv"
    rows = []
    for idx in range(12):
        day = idx + 1
        rows.append({
            "prediction_timestamp": f"2024-01-{day:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-01-{day:02d}T00:00:00Z",
            "target_timestamp": f"2024-01-{day + 1:02d}T00:00:00Z",
            "target_horizon": "24h",
            "label_status": "MATURED",
            "label_source": "prediction_ledger",
            "evaluation_status": "EVALUATED",
            "prediction_id": f"p{idx}",
            "model_version": "m1",
            "evaluation_contract": "contract-v1",
            "fold_id": idx // 3,
            "y_true": "WIN" if idx % 2 == 0 else "LOSS",
            "y_pred": "WIN",
        })
    pd.DataFrame(rows).to_csv(cohort, index=False)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "".join(
            json.dumps({
                "prediction_id": row["prediction_id"],
                "candidate_id": f"c{idx}",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "prediction_timestamp": row["prediction_timestamp"],
                "feature_timestamp_max": row["feature_timestamp_max"],
                "target_horizon": row["target_horizon"],
                "target_timestamp": row["target_timestamp"],
                "target_label": row["y_true"],
                "y_pred": row["y_pred"],
                "y_true": row["y_true"],
                "predicted_probability": 0.75,
                "model_version": row["model_version"],
                "feature_schema_version": "features-v1",
                "fold_id": row["fold_id"],
                "train_window_start": "2023-12-01T00:00:00Z",
                "train_window_end": "2023-12-31T00:00:00Z",
                "test_window_start": row["prediction_timestamp"],
                "test_window_end": row["target_timestamp"],
                "label_source": row["label_source"],
                "label_status": row["label_status"],
                "evaluation_status": row["evaluation_status"],
                "temporal_guard_status": "PASS",
                "created_at": "2024-01-15T00:00:00Z",
                "updated_at": "2024-01-15T00:00:00Z",
            }) + "\n"
            for idx, row in enumerate(rows)
        ),
        encoding="utf-8",
    )

    report = run_ml_metric_reconciliation(
        output_dir="reports",
        db_path="missing.db",
        model_output_path=str(model),
        walkforward_path="missing.csv",
        prediction_artifact_path=str(cohort),
        prediction_ledger_path=str(ledger),
    )

    components = report["model_readiness"]["components"]
    assert components["Leakage Safety"] == "REVIEW"
    assert components["Metric Integrity"] == "REVIEW"
    assert components["Evaluation Metric Integrity"] == "REVIEW"
    assert components["Baseline Superiority"].startswith("BLOCKED")
    assert components["Walk-Forward Stability"].startswith("BLOCKED")
    assert report["temporal_feature_guard_status"] == "PASS"
    assert not any(f.get("reason") == "label_or_outcome_columns_in_model_features" for f in report["temporal_guard_findings"])


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


def test_label_integrity_reviews_legacy_caveats_after_ledger_contract_pass():
    status = label_integrity_component_status(
        {"status": "REVIEW"},
        {
            "prediction_ledger_available": True,
            "label_contract_status": "PASS",
            "evaluation_reproducibility_status": "PASS",
            "invalid_labels": [],
        },
    )
    assert status == "REVIEW"


def test_label_integrity_blocks_when_ledger_label_contract_blocked():
    status = label_integrity_component_status(
        {"status": "REVIEW"},
        {
            "prediction_ledger_available": True,
            "label_contract_status": "BLOCKED",
            "evaluation_reproducibility_status": "BLOCKED",
            "invalid_labels": [],
        },
    )
    assert status == "BLOCKED_LABEL_CONTRACT"


def test_label_integrity_blocks_when_ledger_missing():
    status = label_integrity_component_status(
        {"status": "PASS"},
        {
            "prediction_ledger_available": False,
            "label_contract_status": "BLOCKED",
            "evaluation_reproducibility_status": "BLOCKED",
            "invalid_labels": [],
        },
    )
    assert status == "BLOCKED_LABEL_CONTRACT"


def test_overall_readiness_remains_blocked_when_non_label_components_block():
    r = readiness({
        "Label Integrity": "REVIEW",
        "Leakage Safety": "BLOCKED_TEMPORAL_INTEGRITY",
        "Metric Integrity": "BLOCKED_UNREPRODUCIBLE",
        "Baseline Superiority": "BLOCKED_BELOW_BASELINE",
        "Walk-Forward Stability": "BLOCKED_INSTABILITY",
    })
    assert r["overall_status"].startswith("BLOCKED")
    assert r["primary_blocker"] != "BLOCKED_LABEL_CONTRACT"
    assert "Label Integrity" not in r["all_blockers"]


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


def _write_valid_ledger(path: Path, rows):
    path.write_text(
        "".join(
            json.dumps({
                "prediction_id": row.get("prediction_id", f"p{idx}"),
                "candidate_id": f"c{idx}",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "prediction_timestamp": row["prediction_timestamp"],
                "feature_timestamp_max": row.get("feature_timestamp_max", row["prediction_timestamp"]),
                "target_horizon": "24h",
                "target_timestamp": row.get("target_timestamp", row.get("target_maturity_timestamp")),
                "target_label": row["y_true"],
                "y_pred": row["y_pred"],
                "y_true": row["y_true"],
                **({"predicted_probability": row["predicted_probability"]} if "predicted_probability" in row else {}),
                "model_version": row.get("model_version", "m1"),
                "feature_schema_version": "features-v1",
                "fold_id": row.get("fold_id", idx // 2),
                "train_window_start": "2023-12-01T00:00:00Z",
                "train_window_end": "2023-12-31T00:00:00Z",
                "test_window_start": row["prediction_timestamp"],
                "test_window_end": row.get("target_timestamp", row.get("target_maturity_timestamp")),
                "label_source": "prediction_ledger",
                "label_status": "MATURED",
                "evaluation_status": "EVALUATED",
                "temporal_guard_status": "PASS",
                "created_at": "2024-01-15T00:00:00Z",
                "updated_at": "2024-01-15T00:00:00Z",
            }) + "\n"
            for idx, row in enumerate(rows)
        ),
        encoding="utf-8",
    )


def _run_current_metric_report(tmp_path, rows):
    model = tmp_path / "model_output.json"
    model.write_text(json.dumps({"accuracy": 0.01, "ai_confidence_score": 1, "rows": 999}), encoding="utf-8")
    cohort = tmp_path / "prediction_cohort.csv"
    pd.DataFrame(rows).to_csv(cohort, index=False)
    ledger = tmp_path / "ledger.jsonl"
    _write_valid_ledger(ledger, rows)
    return run_ml_metric_reconciliation(
        output_dir=str(tmp_path / "reports"),
        db_path=str(tmp_path / "missing.db"),
        model_output_path=str(model),
        walkforward_path=str(tmp_path / "missing_walkforward.csv"),
        prediction_artifact_path=str(cohort),
        prediction_ledger_path=str(ledger),
    )


def test_current_accuracy_reproduced_from_cohort_rows(tmp_path):
    rows = [
        {"prediction_id": "p1", "prediction_timestamp": "2024-01-01T00:00:00Z", "target_maturity_timestamp": "2024-01-02T00:00:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.9},
        {"prediction_id": "p2", "prediction_timestamp": "2024-01-02T00:00:00Z", "target_maturity_timestamp": "2024-01-03T00:00:00Z", "target_timestamp": "2024-01-03T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.8},
        {"prediction_id": "p3", "prediction_timestamp": "2024-01-03T00:00:00Z", "target_maturity_timestamp": "2024-01-04T00:00:00Z", "target_timestamp": "2024-01-04T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 1, "y_true": "LOSS", "y_pred": "LOSS", "predicted_probability": 0.7},
    ]
    report = _run_current_metric_report(tmp_path, rows)
    assert report["current_accuracy_reproduction_status"] == "REPRODUCED_EXACT"
    assert report["current_accuracy_sample_count"] == 3
    assert report["current_accuracy_value"] == pytest.approx(2 / 3)


def test_ai_confidence_reproduced_from_mean_predicted_probability(tmp_path):
    rows = [
        {"prediction_id": "p1", "prediction_timestamp": "2024-01-01T00:00:00Z", "target_maturity_timestamp": "2024-01-02T00:00:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.2},
        {"prediction_id": "p2", "prediction_timestamp": "2024-01-02T00:00:00Z", "target_maturity_timestamp": "2024-01-03T00:00:00Z", "target_timestamp": "2024-01-03T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 1, "y_true": "LOSS", "y_pred": "LOSS", "predicted_probability": 0.6},
    ]
    report = _run_current_metric_report(tmp_path, rows)
    assert report["ai_confidence_reproduction_status"] == "REPRODUCED_EXACT"
    assert report["ai_confidence_sample_count"] == 2
    assert report["ai_confidence_value"] == pytest.approx(0.4)
    assert report["ai_confidence_formula"] == "mean(predicted_probability) over evaluated prediction cohort rows with non-null predicted_probability"


def test_missing_predicted_probability_makes_ai_confidence_unavailable(tmp_path):
    rows = [
        {"prediction_id": "p1", "prediction_timestamp": "2024-01-01T00:00:00Z", "target_maturity_timestamp": "2024-01-02T00:00:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "WIN", "y_pred": "WIN"},
    ]
    report = _run_current_metric_report(tmp_path, rows)
    assert report["ai_confidence_reproduction_status"] in {"REVIEW", "UNAVAILABLE"}
    assert report["ai_confidence_value"] is None


def test_random_holdout_not_mandatory_when_cohort_evidence_exists(tmp_path):
    rows = [
        {"prediction_id": "p1", "prediction_timestamp": "2024-01-01T00:00:00Z", "target_maturity_timestamp": "2024-01-02T00:00:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.9},
        {"prediction_id": "p2", "prediction_timestamp": "2024-01-02T00:00:00Z", "target_maturity_timestamp": "2024-01-03T00:00:00Z", "target_timestamp": "2024-01-03T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 1, "y_true": "LOSS", "y_pred": "LOSS", "predicted_probability": 0.8},
    ]
    report = _run_current_metric_report(tmp_path, rows)
    current = {row["metric_name"]: row for row in report["metric_identity"]}
    assert current["Current Model Accuracy"]["producer"] == "prediction cohort / prediction ledger"
    assert current["Current Model Accuracy"]["evaluation_reproduction_status"] == "REPRODUCED_EXACT"
    assert report["model_readiness"]["components"]["Metric Integrity"] != "BLOCKED_UNREPRODUCIBLE"
    assert report["model_readiness"]["components"]["Evaluation Metric Integrity"] != "BLOCKED_UNREPRODUCIBLE"


def test_baseline_and_walkforward_blockers_remain_unchanged(tmp_path):
    rows = [
        {"prediction_id": "p1", "prediction_timestamp": "2024-01-01T00:00:00Z", "target_maturity_timestamp": "2024-01-02T00:00:00Z", "target_timestamp": "2024-01-02T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 0, "y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.9},
        {"prediction_id": "p2", "prediction_timestamp": "2024-01-02T00:00:00Z", "target_maturity_timestamp": "2024-01-03T00:00:00Z", "target_timestamp": "2024-01-03T00:00:00Z", "model_version": "m1", "evaluation_contract": "c", "fold_id": 1, "y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.8},
    ]
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert components["Baseline Superiority"].startswith("BLOCKED")
    assert components["Walk-Forward Stability"].startswith("BLOCKED")


def _baseline_audit_fixture():
    from ml_metric_reconciliation import baseline_root_cause_audit, row_level_walkforward_audit

    labels = ["LOSS"] * 9 + ["WIN"] * 3 + ["LOSS"] * 3
    preds = ["LOSS"] * 6 + ["WIN"] * 6 + ["LOSS"] * 3
    cohort = pd.DataFrame([
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-01-{idx + 1:02d}T00:00:00Z",
            "y_true": labels[idx],
            "y_pred": preds[idx],
        }
        for idx in range(len(labels))
    ])
    row_level = row_level_walkforward_audit(
        cohort,
        {"status": "PASS"},
        {"train_only_preprocessing_status": "PASS"},
        min_test_rows=1,
    )
    return baseline_root_cause_audit(row_level), row_level


def test_baseline_root_cause_audit_reports_micro_fold_evidence():
    audit, _row_level = _baseline_audit_fixture()
    assert audit["baseline_micro_fold_status"] == "REVIEW_MICRO_FOLD_EVIDENCE"
    assert audit["baseline_fold_size_distribution"] == {"3": 3}


def test_baseline_root_cause_audit_reports_loss_majority_baseline_dominance():
    audit, _row_level = _baseline_audit_fixture()
    assert audit["baseline_evidence_quality_status"] == "REVIEW_MAJOR_CLASS_BASELINE_DOMINANCE"
    assert audit["baseline_prediction_distribution"] == {"LOSS": 3}


def test_baseline_root_cause_audit_reports_exact_fold_outcome_counts():
    audit, _row_level = _baseline_audit_fixture()
    assert audit["baseline_model_worse_folds"] == 1
    assert audit["baseline_model_better_folds"] == 1
    assert audit["baseline_model_tie_folds"] == 1
    assert audit["baseline_worse_fold_prediction_distribution"] == {"LOSS": 1}
    assert audit["baseline_better_fold_prediction_distribution"] == {"LOSS": 1}


def test_baseline_superiority_remains_blocked_below_baseline():
    from ml_metric_reconciliation import row_level_walkforward_audit

    cohort = pd.DataFrame([
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-02-{idx + 1:02d}T00:00:00Z",
            "y_true": "LOSS",
            "y_pred": "WIN",
        }
        for idx in range(12)
    ])
    row_level = row_level_walkforward_audit(
        cohort,
        {"status": "PASS"},
        {"train_only_preprocessing_status": "PASS"},
        min_test_rows=1,
    )
    assert row_level["model_accuracy"] < row_level["baseline_accuracy"]
    assert row_level["baseline_superiority_status"] == "BLOCKED_BELOW_BASELINE"


def _larger_fold_rows(labels, preds):
    return pd.DataFrame([
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-03-{idx + 1:02d}T00:00:00Z",
            "y_true": labels[idx],
            "y_pred": preds[idx],
        }
        for idx in range(len(labels))
    ])


def test_larger_fold_baseline_diagnostic_available_with_enough_rows():
    labels = ["LOSS"] * 20 + ["LOSS"] * 6 + ["WIN"] * 4
    preds = ["LOSS"] * 20 + ["WIN"] * 10
    diagnostic = larger_fold_baseline_diagnostic(_larger_fold_rows(labels, preds), min_train_rows=20, min_test_rows=10)
    assert diagnostic["larger_fold_baseline_diagnostic_status"] == "AVAILABLE"
    assert diagnostic["larger_fold_rows"] == 10
    assert diagnostic["larger_fold_count"] == 1


def test_larger_fold_baseline_diagnostic_unavailable_with_insufficient_rows():
    labels = ["LOSS"] * 10
    preds = ["LOSS"] * 10
    diagnostic = larger_fold_baseline_diagnostic(_larger_fold_rows(labels, preds), min_train_rows=20, min_test_rows=10)
    assert diagnostic["larger_fold_baseline_diagnostic_status"] == "UNAVAILABLE_INSUFFICIENT_ROWS"
    assert diagnostic["larger_fold_rows"] == 0
    assert diagnostic["larger_fold_count"] == 0


def test_larger_fold_baseline_diagnostic_computes_accuracy_and_delta():
    labels = ["LOSS"] * 20 + ["LOSS"] * 6 + ["WIN"] * 4
    preds = ["LOSS"] * 20 + ["LOSS"] * 5 + ["WIN"] * 5
    diagnostic = larger_fold_baseline_diagnostic(_larger_fold_rows(labels, preds), min_train_rows=20, min_test_rows=10)
    assert diagnostic["larger_fold_model_accuracy"] == pytest.approx(0.9)
    assert diagnostic["larger_fold_baseline_accuracy"] == pytest.approx(0.6)
    assert diagnostic["larger_fold_model_vs_baseline_delta"] == pytest.approx(0.3)


def test_larger_fold_baseline_diagnostic_reports_baseline_prediction_distribution():
    labels = ["LOSS"] * 20 + ["LOSS"] * 6 + ["WIN"] * 4
    preds = ["LOSS"] * 20 + ["LOSS"] * 10
    diagnostic = larger_fold_baseline_diagnostic(_larger_fold_rows(labels, preds), min_train_rows=20, min_test_rows=10)
    assert diagnostic["larger_fold_baseline_prediction_distribution"] == {"LOSS": 1}


def test_larger_fold_diagnostic_does_not_change_existing_readiness_components(tmp_path):
    rows = [
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-04-{idx + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-03-{idx + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-05-{idx + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-05-{idx + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
            "fold_id": idx // 3,
            "y_true": "LOSS",
            "y_pred": "WIN",
            "predicted_probability": 0.9,
        }
        for idx in range(30)
    ]
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["larger_fold_baseline_diagnostic_status"] == "AVAILABLE"
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"


def test_walkforward_stability_remains_blocked_when_row_level_below_baseline():
    from ml_metric_reconciliation import row_level_walkforward_audit

    cohort = pd.DataFrame([
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-03-{idx + 1:02d}T00:00:00Z",
            "y_true": "LOSS",
            "y_pred": "WIN",
        }
        for idx in range(18)
    ])
    row_level = row_level_walkforward_audit(
        cohort,
        {"status": "PASS"},
        {"train_only_preprocessing_status": "PASS"},
    )
    walk_forward_stability = row_level["row_level_walkforward_status"] if row_level["row_level_walkforward_status"].startswith("BLOCKED") else "REVIEW"
    assert row_level["row_level_walkforward_status"] == "BLOCKED_BELOW_BASELINE"
    assert walk_forward_stability == "BLOCKED_BELOW_BASELINE"


def _class_imbalance_fixture():
    return pd.DataFrame([
        {"y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.90},
        {"y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.40},
        {"y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.80},
        {"y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.70},
        {"y_true": "LOSS", "y_pred": "LOSS", "predicted_probability": 0.30},
        {"y_true": "LOSS", "y_pred": "LOSS", "predicted_probability": 0.20},
    ])


def test_class_imbalance_diagnostic_reports_label_distributions():
    diagnostic = ml_class_imbalance_diagnostic(_class_imbalance_fixture())
    assert diagnostic["class_imbalance_diagnostic_status"] == "REVIEW_CLASS_IMBALANCE"
    assert diagnostic["class_imbalance_sample_count"] == 6
    assert diagnostic["true_label_distribution"] == {"LOSS": 4, "WIN": 2}
    assert diagnostic["predicted_label_distribution"] == {"LOSS": 3, "WIN": 3}
    assert diagnostic["majority_class"] == "LOSS"
    assert diagnostic["majority_class_ratio"] == pytest.approx(4 / 6)


def test_class_imbalance_diagnostic_computes_confusion_matrix():
    diagnostic = ml_class_imbalance_diagnostic(_class_imbalance_fixture())
    assert diagnostic["confusion_matrix"] == {
        "actual_WIN": {"predicted_WIN": 1, "predicted_LOSS": 1},
        "actual_LOSS": {"predicted_WIN": 2, "predicted_LOSS": 2},
    }
    assert diagnostic["true_win_count"] == 1
    assert diagnostic["true_loss_count"] == 2
    assert diagnostic["false_win_count"] == 2
    assert diagnostic["false_loss_count"] == 1


def test_class_imbalance_diagnostic_computes_precision_recall_f1():
    diagnostic = ml_class_imbalance_diagnostic(_class_imbalance_fixture())
    assert diagnostic["win_precision"] == pytest.approx(1 / 3)
    assert diagnostic["win_recall"] == pytest.approx(1 / 2)
    assert diagnostic["win_f1"] == pytest.approx(0.4)
    assert diagnostic["loss_precision"] == pytest.approx(2 / 3)
    assert diagnostic["loss_recall"] == pytest.approx(1 / 2)
    assert diagnostic["loss_f1"] == pytest.approx(4 / 7)


def test_class_imbalance_diagnostic_detects_false_win_predictions():
    diagnostic = ml_class_imbalance_diagnostic(_class_imbalance_fixture())
    assert diagnostic["false_win_count"] == 2
    assert any("False WIN predictions" in finding for finding in diagnostic["class_imbalance_findings"])
    assert diagnostic["class_imbalance_recommendation"] == "Review model as LOSS avoidance filter before using it as WIN/entry predictor."


def test_class_imbalance_diagnostic_returns_probability_thresholds():
    diagnostic = ml_class_imbalance_diagnostic(_class_imbalance_fixture())
    thresholds = diagnostic["probability_threshold_diagnostic"]
    assert [row["threshold"] for row in thresholds] == [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    t80 = next(row for row in thresholds if row["threshold"] == 0.80)
    assert t80["rows_kept"] == 2
    assert t80["kept_ratio"] == pytest.approx(2 / 6)
    assert t80["accuracy_on_kept_rows"] == pytest.approx(0.5)
    assert t80["win_precision_on_kept_rows"] == pytest.approx(0.5)
    assert t80["false_win_count_on_kept_rows"] == 1


def test_class_imbalance_diagnostic_does_not_alter_readiness_gates(tmp_path):
    rows = [
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-06-{idx + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{idx + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
            "fold_id": idx // 3,
            "y_true": "LOSS",
            "y_pred": "WIN",
            "predicted_probability": 0.9,
        }
        for idx in range(30)
    ]
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["class_imbalance_diagnostic_status"] == "REVIEW_CLASS_IMBALANCE"
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True


def test_threshold_candidate_diagnostic_available_with_predicted_probability():
    diagnostic = ml_high_confidence_threshold_candidate_diagnostic(_class_imbalance_fixture())
    assert diagnostic["threshold_candidate_diagnostic_status"] == "AVAILABLE"
    assert [row["threshold"] for row in diagnostic["high_confidence_threshold_diagnostic"]] == [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    assert diagnostic["threshold_candidate_selected"] is not None


def test_threshold_candidate_diagnostic_unavailable_without_predicted_probability():
    cohort = _class_imbalance_fixture().drop(columns=["predicted_probability"])
    diagnostic = ml_high_confidence_threshold_candidate_diagnostic(cohort)
    assert diagnostic["threshold_candidate_diagnostic_status"] == "UNAVAILABLE"
    assert diagnostic["threshold_candidate_selected"] is None
    assert "predicted_probability" in diagnostic["threshold_candidate_findings"][0]


def test_threshold_candidate_selector_prefers_lower_false_win_when_rows_sufficient():
    rows = []
    # 0.80 keeps 30 rows and has no false WINs, while lower thresholds include false WINs.
    rows.extend({"y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.90} for _ in range(30))
    rows.extend({"y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.75} for _ in range(10))
    diagnostic = ml_high_confidence_threshold_candidate_diagnostic(pd.DataFrame(rows))
    assert diagnostic["threshold_candidate_selected"] == 0.80
    assert diagnostic["threshold_candidate_rows_kept"] == 30
    assert diagnostic["threshold_candidate_false_win_count"] == 0


def test_threshold_candidate_selector_uses_accuracy_as_tie_breaker():
    rows = []
    # 0.70 and 0.80 both have zero false WINs and sufficient rows; 0.80 has higher accuracy.
    rows.extend({"y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.90} for _ in range(30))
    rows.extend({"y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.70} for _ in range(10))
    rows.extend({"y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.60} for _ in range(10))
    diagnostic = ml_high_confidence_threshold_candidate_diagnostic(pd.DataFrame(rows))
    assert diagnostic["threshold_candidate_selected"] == 0.80
    assert diagnostic["threshold_candidate_accuracy"] == pytest.approx(1.0)


def test_threshold_candidate_diagnostic_does_not_alter_readiness_gates(tmp_path):
    rows = [
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-06-{idx + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{idx + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
            "fold_id": idx // 3,
            "y_true": "LOSS",
            "y_pred": "WIN",
            "predicted_probability": 0.9,
        }
        for idx in range(30)
    ]
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["threshold_candidate_diagnostic_status"] == "AVAILABLE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"


def _threshold_stability_rows(count=36, include_segments=True, false_win_count=0):
    rows = []
    for idx in range(count):
        is_false_win = idx < false_win_count
        row = {
            "y_true": "LOSS" if is_false_win else ("WIN" if idx % 3 else "LOSS"),
            "y_pred": "WIN" if is_false_win else ("WIN" if idx % 3 else "LOSS"),
            "predicted_probability": 0.85,
        }
        if include_segments:
            row.update({
                "fold_id": idx % 3,
                "symbol": "BTCUSDT" if idx % 2 == 0 else "ETHUSDT",
                "market_regime": "trend" if idx % 4 else "range",
            })
        rows.append(row)
    return rows


def test_threshold_stability_audit_available_with_selected_threshold_and_enough_rows():
    audit = threshold_candidate_stability_audit(pd.DataFrame(_threshold_stability_rows(36)), selected_threshold=0.80)
    assert audit["threshold_stability_audit_status"] == "AVAILABLE"
    assert audit["threshold_stability_selected_threshold"] == 0.80
    assert audit["threshold_stability_rows_kept"] == 36


def test_threshold_stability_audit_unavailable_without_selected_threshold():
    cohort = pd.DataFrame(_threshold_stability_rows(36)).drop(columns=["predicted_probability"])
    audit = threshold_candidate_stability_audit(cohort)
    assert audit["threshold_stability_audit_status"] == "UNAVAILABLE_NO_SELECTED_THRESHOLD"
    assert audit["threshold_stability_selected_threshold"] is None


def test_threshold_stability_audit_review_when_threshold_sample_too_small():
    audit = threshold_candidate_stability_audit(pd.DataFrame(_threshold_stability_rows(12)), selected_threshold=0.80)
    assert audit["threshold_stability_audit_status"] == "REVIEW_INSUFFICIENT_THRESHOLD_SAMPLE"
    assert audit["threshold_stability_rows_kept"] == 12


def test_threshold_stability_audit_metrics_for_selected_threshold():
    rows = _threshold_stability_rows(30, false_win_count=2)
    audit = threshold_candidate_stability_audit(pd.DataFrame(rows), selected_threshold=0.80)
    assert audit["threshold_stability_false_win_count"] == 2
    assert audit["threshold_stability_accuracy"] == pytest.approx(28 / 30)
    assert audit["threshold_stability_win_precision"] == pytest.approx(19 / 21)
    assert "False WIN appears in at least one segment; threshold requires further review" in audit["threshold_stability_findings"]


def test_threshold_stability_audit_segment_summaries_when_columns_exist():
    audit = threshold_candidate_stability_audit(pd.DataFrame(_threshold_stability_rows(36)), selected_threshold=0.80)
    assert audit["threshold_stability_fold_summary"]
    assert audit["threshold_stability_symbol_summary"]
    assert audit["threshold_stability_regime_summary"]
    assert audit["threshold_stability_min_segment_rows"] is not None
    assert "False WIN remained zero in available threshold stability evidence" in audit["threshold_stability_findings"]


def test_threshold_stability_audit_does_not_alter_readiness_components(tmp_path):
    rows = [
        {
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-06-{idx + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{idx + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{idx + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
            "fold_id": idx // 10,
            "symbol": "BTCUSDT" if idx % 2 == 0 else "ETHUSDT",
            "regime": "trend",
            "y_true": "LOSS",
            "y_pred": "WIN",
            "predicted_probability": 0.9,
        }
        for idx in range(30)
    ]
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["threshold_stability_audit_status"] == "AVAILABLE"
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True



def _threshold_sufficiency_rows(total=120, pred_wins=40, false_win_count=0):
    rows = []
    for idx in range(total):
        predicts_win = idx < pred_wins
        is_false_win = predicts_win and idx < false_win_count
        rows.append({
            "y_true": "LOSS" if is_false_win else ("WIN" if predicts_win or idx % 4 == 0 else "LOSS"),
            "y_pred": "WIN" if predicts_win else "LOSS",
            "predicted_probability": 0.85,
            "fold_id": idx // 40,
            "symbol": "BTCUSDT" if idx % 2 == 0 else "ETHUSDT",
            "market_regime": "trend" if idx % 3 else "range",
        })
    return rows



def _coverage_base_report(**overrides):
    report = {
        "filtered_cohort_rows_full": 250,
        "filtered_cohort_rows_kept": 81,
        "filtered_cohort_rows_skipped": 169,
        "threshold_sample_sufficiency_pred_win_count": 2,
        "threshold_sample_sufficiency_pred_loss_count": 79,
    }
    report.update(overrides)
    return report


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_raw_closed_source_discovery_finds_json_selected_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "reports" / "closed_outcomes.json"
    _write_json(path, [{"closed_at": "2024-01-02", "outcome": "WIN", "pnl": 1.2, "prediction_id": "p1", "symbol": "BTCUSDT"}])

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_discovery_status"] == "AVAILABLE_SELECTED_SOURCE"
    assert audit["raw_closed_source_selected_type"] == "json"
    assert audit["raw_closed_source_selected_row_count"] == 1


def test_raw_closed_source_discovery_counts_json_list_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "outcome_rows.json", [
        {"closed_at": "t1", "outcome": "WIN", "pnl": 1},
        {"closed_at": "t2", "outcome": "LOSS", "pnl": -1},
    ])

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_selected_row_count"] == 2


def test_raw_closed_source_discovery_counts_jsonl_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "reports" / "closed_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            json.dumps({"closed_at": "t1", "status": "CLOSED", "outcome": "WIN"}),
            json.dumps({"closed_at": "t2", "status": "CLOSED", "outcome": "LOSS"}),
            json.dumps({"closed_at": "t3", "status": "CLOSED", "outcome": "WIN"}),
        ]) + "\n",
        encoding="utf-8",
    )

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_selected_type"] == "jsonl"
    assert audit["raw_closed_source_selected_row_count"] == 3


def test_raw_closed_source_discovery_counts_csv_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "reports" / "closed_trades.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"closed_at": "t1", "status": "CLOSED", "pnl": 1.0},
        {"closed_at": "t2", "status": "CLOSED", "pnl": -1.0},
    ]).to_csv(path, index=False)

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_selected_type"] == "csv"
    assert audit["raw_closed_source_selected_row_count"] == 2


def test_raw_closed_source_discovery_emits_field_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "closed_outcome_metadata.json", [{"closed_at": "t1", "status": "CLOSED", "label": "WIN", "symbol": "ETHUSDT", "entry_time": "e", "exit_time": "x"}])

    audit = raw_closed_outcome_source_discovery_audit({})
    candidate = audit["raw_closed_source_candidates"][0]

    assert {"closed_at", "status", "label", "symbol", "entry_time", "exit_time"}.issubset(set(candidate["detected_fields"]))
    assert "sampled_rows" in candidate


def test_raw_closed_source_discovery_multiple_sources_need_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "closed_outcomes_a.json", [{"closed_at": "t1", "outcome": "WIN", "pnl": 1}])
    _write_json(tmp_path / "reports" / "closed_outcomes_b.json", [{"closed_at": "t2", "outcome": "LOSS", "pnl": -1}])

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_discovery_status"] == "AVAILABLE_CANDIDATES_NEED_REVIEW"
    assert audit["raw_closed_source_selected_path"] is None


def test_raw_closed_source_discovery_selects_paper_outcome_audit_canonical_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "paper_outcome_audit.json", {
        "closed_trades": [
            {"closed_at": f"2024-01-{(idx % 28) + 1:02d}", "status": "CLOSED", "symbol": "BTCUSDT"}
            for idx in range(403)
        ]
    })
    ledger_path = tmp_path / "reports" / "ml_prediction_ledger.jsonl"
    ledger_path.write_text(
        json.dumps({"closed_at": "2024-01-01", "status": "CLOSED", "symbol": "BTCUSDT"}) + "\n",
        encoding="utf-8",
    )

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_discovery_status"] == "AVAILABLE_SELECTED_SOURCE"
    assert audit["raw_closed_source_selected_path"] == str((tmp_path / "reports" / "paper_outcome_audit.json").resolve())
    assert audit["raw_closed_source_selected_type"] == "json"
    assert audit["raw_closed_source_selected_row_count"] == 403
    assert audit["raw_closed_source_candidate_count"] >= 2


def test_raw_closed_source_discovery_selected_status_for_canonical_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "paper_outcome_audit.json", {
        "closed_trades": [{"closed_at": "t1", "status": "CLOSED", "symbol": "ETHUSDT"}]
    })

    audit = raw_closed_outcome_source_discovery_audit({})

    assert audit["raw_closed_source_discovery_status"] == "AVAILABLE_SELECTED_SOURCE"


def test_closed_to_ml_coverage_uses_selected_raw_closed_source_count():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=500))

    assert audit["closed_to_ml_coverage_status"] == "AVAILABLE_FROM_RAW_CLOSED_SOURCE_DISCOVERY"
    assert audit["closed_to_ml_coverage_raw_closed_count"] == 500
    assert audit["closed_to_ml_coverage_closed_to_ml_retention_ratio"] == 0.5
    assert "CLOSED_OUTCOME_COUNT_EXCEEDS_ML_COHORT_COUNT" in audit["closed_to_ml_coverage_findings"]
    assert "RAW_CLOSED_OUTCOME_SOURCE_UNAVAILABLE_FOR_COVERAGE_RECONCILIATION" not in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_propagates_canonical_selected_row_count():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=403))

    assert audit["closed_to_ml_coverage_raw_closed_count"] == 403


def test_closed_to_ml_coverage_computes_canonical_retention_ratio():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=403))

    assert audit["closed_to_ml_coverage_closed_to_ml_retention_ratio"] == round(250 / 403, 6)


def test_closed_to_ml_coverage_computes_raw_to_ml_gap_count():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=403))

    assert audit["closed_to_ml_coverage_raw_to_ml_gap_count"] == 153
    assert audit["closed_to_ml_coverage_known_stage_counts"]["raw_to_ml_gap_rows"] == 153


def test_closed_to_ml_coverage_flags_canonical_raw_count_exceeds_ml_cohort():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=403))

    assert "CLOSED_OUTCOME_COUNT_EXCEEDS_ML_COHORT_COUNT" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_canonical_source_removes_unavailable_finding():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_source_selected_row_count=403))

    assert "RAW_CLOSED_OUTCOME_SOURCE_UNAVAILABLE_FOR_COVERAGE_RECONCILIATION" not in audit["closed_to_ml_coverage_findings"]


def test_raw_closed_source_discovery_no_candidate_and_coverage_report_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    discovery = raw_closed_outcome_source_discovery_audit({})
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(**discovery))

    assert discovery["raw_closed_source_discovery_status"] == "UNAVAILABLE_NO_CANDIDATE_SOURCE_FOUND"
    assert audit["closed_to_ml_coverage_status"] == "AVAILABLE_FROM_REPORT_FIELDS"
    assert audit["closed_to_ml_coverage_raw_closed_count"] is None


def test_closed_to_ml_coverage_available_from_report_fields_without_raw_source():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report())
    assert audit["closed_to_ml_coverage_status"] == "AVAILABLE_FROM_REPORT_FIELDS"
    assert "RAW_CLOSED_OUTCOME_SOURCE_UNAVAILABLE_FOR_COVERAGE_RECONCILIATION" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_maps_known_stage_counts():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report())
    assert audit["closed_to_ml_coverage_ml_cohort_count"] == 250
    assert audit["closed_to_ml_coverage_threshold_kept_count"] == 81
    assert audit["closed_to_ml_coverage_threshold_skipped_count"] == 169
    assert audit["closed_to_ml_coverage_threshold_pred_win_count"] == 2
    assert audit["closed_to_ml_coverage_threshold_pred_loss_count"] == 79
    assert audit["closed_to_ml_coverage_known_stage_counts"] == {
        "raw_closed_outcomes": None,
        "ml_cohort_rows": 250,
        "raw_to_ml_gap_rows": None,
        "threshold_kept_rows": 81,
        "threshold_skipped_rows": 169,
        "threshold_predicted_win_rows": 2,
        "threshold_predicted_loss_rows": 79,
    }


def test_closed_to_ml_coverage_flags_predicted_win_sample_below_minimum():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(threshold_sample_sufficiency_pred_win_count=29))
    assert "PREDICTED_WIN_SAMPLE_BELOW_MINIMUM" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_flags_threshold_filtered_sample_below_minimum():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(filtered_cohort_rows_kept=99))
    assert "THRESHOLD_FILTERED_SAMPLE_BELOW_MINIMUM" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_raw_closed_exceeds_ml_cohort_and_retention_ratio():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report(raw_closed_outcome_count=500))
    assert audit["closed_to_ml_coverage_raw_closed_count"] == 500
    assert audit["closed_to_ml_coverage_closed_to_ml_retention_ratio"] == 0.5
    assert "CLOSED_OUTCOME_COUNT_EXCEEDS_ML_COHORT_COUNT" in audit["closed_to_ml_coverage_findings"]
    assert "LOW_COVERAGE_RETENTION_REQUIRES_DROP_REASON_AUDIT" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_missing_raw_source_does_not_fail():
    audit = closed_outcome_to_ml_cohort_coverage_audit(_coverage_base_report())
    assert audit["closed_to_ml_coverage_raw_closed_count"] is None
    assert "RAW_CLOSED_OUTCOME_SOURCE_UNAVAILABLE_FOR_COVERAGE_RECONCILIATION" in audit["closed_to_ml_coverage_findings"]


def test_closed_to_ml_coverage_does_not_change_readiness_or_governance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "reports" / "paper_outcome_audit.json", {
        "closed_trades": [
            {"closed_at": f"2024-01-{(idx % 28) + 1:02d}", "status": "CLOSED", "symbol": "BTCUSDT"}
            for idx in range(403)
        ]
    })
    rows = _threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)
    for idx, row in enumerate(rows):
        row["y_true"] = "WIN" if idx < 80 else "LOSS"
        row["y_pred"] = "WIN" if idx < 2 else "LOSS"
        row.update({
            "prediction_id": f"p-coverage-{idx}",
            "prediction_timestamp": f"2024-06-{(idx % 28) + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
        })
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["closed_to_ml_coverage_status"] in {
        "AVAILABLE_FROM_REPORT_FIELDS",
        "AVAILABLE_FROM_ARTIFACT:closed_outcomes",
        "AVAILABLE_FROM_RAW_CLOSED_SOURCE_DISCOVERY",
    }
    assert report["raw_closed_source_discovery_status"] == "AVAILABLE_SELECTED_SOURCE"
    assert report["closed_to_ml_coverage_status"] == "AVAILABLE_FROM_RAW_CLOSED_SOURCE_DISCOVERY"
    assert report["closed_to_ml_coverage_raw_closed_count"] == 403
    assert report["closed_to_ml_coverage_raw_to_ml_gap_count"] == 283
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True

def test_threshold_sample_sufficiency_available_with_enough_kept_and_predicted_wins():
    audit = threshold_sample_sufficiency_audit(pd.DataFrame(_threshold_sufficiency_rows()), selected_threshold=0.80)
    assert "AVAILABLE_SAMPLE_SUFFICIENT" in audit["threshold_sample_sufficiency_status"]
    assert audit["threshold_sample_sufficiency_rows_kept"] == 120
    assert audit["threshold_sample_sufficiency_pred_win_count"] == 40


def test_threshold_sample_sufficiency_reviews_insufficient_kept_rows():
    audit = threshold_sample_sufficiency_audit(pd.DataFrame(_threshold_sufficiency_rows(total=80, pred_wins=40)), selected_threshold=0.80)
    assert "REVIEW_INSUFFICIENT_KEPT_ROWS" in audit["threshold_sample_sufficiency_status"]


def test_threshold_sample_sufficiency_reviews_insufficient_pred_win_sample():
    audit = threshold_sample_sufficiency_audit(pd.DataFrame(_threshold_sufficiency_rows(total=120, pred_wins=2)), selected_threshold=0.80)
    assert "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE" in audit["threshold_sample_sufficiency_status"]


def test_threshold_sample_sufficiency_warns_zero_false_win_is_promising_but_weak_when_pred_win_small():
    audit = threshold_sample_sufficiency_audit(pd.DataFrame(_threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)), selected_threshold=0.80)
    assert "Zero false WIN is promising but not yet statistically strong because predicted WIN sample is insufficient." in audit["threshold_sample_sufficiency_findings"]


def test_threshold_sample_sufficiency_unavailable_without_selected_threshold():
    audit = threshold_sample_sufficiency_audit(pd.DataFrame(_threshold_sufficiency_rows(total=120, pred_wins=40)))
    assert audit["threshold_sample_sufficiency_status"] == "UNAVAILABLE_NO_SELECTED_THRESHOLD"


def test_threshold_sample_sufficiency_does_not_change_readiness_or_governance(tmp_path):
    rows = _threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)
    for idx, row in enumerate(rows):
        row["y_true"] = "WIN" if idx < 80 else "LOSS"
        row["y_pred"] = "WIN" if idx < 2 else "LOSS"
        row.update({
            "prediction_id": f"p{idx}",
            "prediction_timestamp": f"2024-06-{(idx % 28) + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
        })
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE" in report["threshold_sample_sufficiency_status"]
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True


def _filtered_comparison_rows():
    rows = []
    specs = [
        ("WIN", "WIN", 0.90, 1, "BTCUSDT"),
        ("LOSS", "WIN", 0.88, 1, "BTCUSDT"),
        ("LOSS", "LOSS", 0.87, 2, "ETHUSDT"),
        ("WIN", "LOSS", 0.70, 2, "ETHUSDT"),
        ("LOSS", "WIN", 0.60, 3, "SOLUSDT"),
    ]
    for idx, (true, pred, prob, fold, symbol) in enumerate(specs):
        rows.append({"y_true": true, "y_pred": pred, "predicted_probability": prob, "fold_id": fold, "symbol": symbol})
    return rows


def test_filtered_cohort_comparison_available_with_threshold_and_probability():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_comparison_status"] == "AVAILABLE"
    assert diagnostic["filtered_cohort_selected_threshold"] == 0.80


def test_filtered_cohort_comparison_rows_kept_ratio_and_skipped():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_rows_full"] == 5
    assert diagnostic["filtered_cohort_rows_kept"] == 3
    assert diagnostic["filtered_cohort_rows_skipped"] == 2
    assert diagnostic["filtered_cohort_kept_ratio"] == 0.6


def test_filtered_cohort_comparison_accuracy_baseline_and_delta():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_filtered_model_accuracy"] == pytest.approx(2 / 3)
    assert diagnostic["filtered_cohort_filtered_baseline_accuracy"] == pytest.approx(2 / 3)
    assert diagnostic["filtered_cohort_filtered_model_vs_baseline_delta"] == 0.0


def test_filtered_cohort_comparison_false_win_count_and_delta():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_full_false_win_count"] == 2
    assert diagnostic["filtered_cohort_filtered_false_win_count"] == 1
    assert diagnostic["filtered_cohort_false_win_delta"] == -1


def test_filtered_cohort_comparison_insufficient_sample_findings():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert "REVIEW_INSUFFICIENT_FILTERED_SAMPLE" in diagnostic["filtered_cohort_findings"]
    assert "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE" in diagnostic["filtered_cohort_findings"]


def test_filtered_cohort_comparison_fold_and_symbol_summaries():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_fold_count"] == 2
    assert {row["segment"] for row in diagnostic["filtered_cohort_fold_summary"]} == {"1", "2"}
    assert {row["segment"] for row in diagnostic["filtered_cohort_symbol_summary"]} == {"BTCUSDT", "ETHUSDT"}
    assert all("insufficient_kept_rows" in row for row in diagnostic["filtered_cohort_fold_summary"])


def test_filtered_cohort_comparison_reports_regime_unavailable_without_regime_column():
    diagnostic = filtered_cohort_walkforward_comparison(pd.DataFrame(_filtered_comparison_rows()), selected_threshold=0.80)
    assert diagnostic["filtered_cohort_regime_summary"]["status"] == "UNAVAILABLE"
    assert "REGIME_SEGMENT_UNAVAILABLE" in diagnostic["filtered_cohort_findings"]


def test_filtered_cohort_comparison_does_not_change_readiness_or_governance(tmp_path):
    rows = _threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)
    for idx, row in enumerate(rows):
        row["y_true"] = "WIN" if idx < 80 else "LOSS"
        row["y_pred"] = "WIN" if idx < 2 else "LOSS"
        row.update({
            "prediction_id": f"p-filtered-{idx}",
            "prediction_timestamp": f"2024-06-{(idx % 28) + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
        })
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["filtered_cohort_comparison_status"] == "AVAILABLE"
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True


def _paper_filter_candidate_base_report():
    diagnostic = {
        "filtered_cohort_comparison_status": "AVAILABLE",
        "filtered_cohort_selected_threshold": 0.80,
        "filtered_cohort_rows_full": 250,
        "filtered_cohort_rows_kept": 81,
        "filtered_cohort_filtered_prediction_distribution": {"WIN": 20, "LOSS": 61},
        "filtered_cohort_full_model_accuracy": 0.644,
        "filtered_cohort_filtered_model_accuracy": 0.7407407407407407,
        "filtered_cohort_filtered_baseline_accuracy": 0.7160493827160493,
        "filtered_cohort_filtered_model_vs_baseline_delta": 0.024691,
        "filtered_cohort_filtered_vs_full_accuracy_delta": 0.096741,
        "filtered_cohort_full_false_win_count": 31,
        "filtered_cohort_filtered_false_win_count": 0,
        "filtered_cohort_false_win_delta": -31,
        "filtered_cohort_findings": [
            "REVIEW_INSUFFICIENT_FILTERED_SAMPLE",
            "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE",
            "REGIME_SEGMENT_UNAVAILABLE",
        ],
        "filtered_cohort_fold_summary": [{"segment": "1", "rows_kept": 9, "insufficient_kept_rows": True}],
        "filtered_cohort_symbol_summary": [],
        "filtered_cohort_regime_summary": {"status": "UNAVAILABLE"},
        "model_readiness": {"overall_status": "BLOCKED_BELOW_BASELINE", "primary_blocker": "BLOCKED_BELOW_BASELINE"},
    }
    return diagnostic


def test_paper_filter_candidate_registry_emits_review_candidate_not_enabled_with_sample_blockers():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    assert registry["paper_filter_candidate_status"] == "REVIEW_CANDIDATE_NOT_ENABLED"
    assert registry["paper_filter_candidate_name"] == "ML_HIGH_CONFIDENCE_THRESHOLD_0_80_FILTER_CANDIDATE"
    assert registry["paper_filter_candidate_threshold"] == 0.80
    assert registry["paper_filter_candidate_mode"] == "paper_only_shadow_review"


def test_paper_filter_candidate_enabled_is_always_false():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    assert registry["paper_filter_candidate_enabled"] is False


def test_paper_filter_candidate_positive_evidence_includes_accuracy_baseline_and_false_win_reduction():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    evidence = registry["paper_filter_candidate_positive_evidence"]
    assert "FILTERED_MODEL_ACCURACY_IMPROVED_OVER_FULL_MODEL" in evidence
    assert "FILTERED_MODEL_BEAT_FILTERED_BASELINE" in evidence
    assert "FILTERED_FALSE_WIN_COUNT_ZERO" in evidence
    assert "FALSE_WIN_COUNT_IMPROVED_VERSUS_FULL_COHORT" in evidence


def test_paper_filter_candidate_blockers_include_sample_segment_and_regime_blockers():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    blockers = registry["paper_filter_candidate_blockers"]
    assert "INSUFFICIENT_FILTERED_ROWS" in blockers
    assert "INSUFFICIENT_PREDICTED_WIN_SAMPLE" in blockers
    assert "INSUFFICIENT_SEGMENT_ROWS" in blockers
    assert "REGIME_SEGMENT_UNAVAILABLE" in blockers
    assert "OVERALL_READINESS_BLOCKED_BELOW_BASELINE" in blockers


def test_paper_filter_candidate_missing_filtered_cohort_diagnostic_returns_unavailable():
    registry = paper_filter_candidate_registry({"model_readiness": {"overall_status": "BLOCKED_BELOW_BASELINE"}})
    assert registry["paper_filter_candidate_status"] == "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE"
    assert "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE" in registry["paper_filter_candidate_blockers"]
    assert registry["paper_filter_candidate_enabled"] is False


def test_paper_filter_candidate_registry_does_not_alter_readiness_or_governance(tmp_path):
    rows = _threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)
    for idx, row in enumerate(rows):
        row["y_true"] = "WIN" if idx < 80 else "LOSS"
        row["y_pred"] = "WIN" if idx < 2 else "LOSS"
        row.update({
            "prediction_id": f"p-paper-filter-{idx}",
            "prediction_timestamp": f"2024-06-{(idx % 28) + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
        })
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["paper_filter_candidate_status"] == "REVIEW_CANDIDATE_NOT_ENABLED"
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True


def test_paper_filter_shadow_review_missing_candidate_registry_unavailable():
    scorecard = paper_filter_shadow_review_scorecard({})
    assert scorecard["paper_filter_shadow_review_status"] == "UNAVAILABLE_NO_CANDIDATE_REGISTRY"
    assert scorecard["paper_filter_shadow_review_candidate_enabled"] is False


def test_paper_filter_shadow_review_candidate_with_blockers_blocked():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    scorecard = paper_filter_shadow_review_scorecard({**_paper_filter_candidate_base_report(), **registry})
    assert scorecard["paper_filter_shadow_review_status"] == "REVIEW_SHADOW_CANDIDATE_BLOCKED"
    assert scorecard["paper_filter_shadow_review_blocker_count"] >= 1


def test_paper_filter_shadow_review_candidate_remains_disabled_with_positive_evidence():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    scorecard = paper_filter_shadow_review_scorecard({**_paper_filter_candidate_base_report(), **registry})
    assert scorecard["paper_filter_shadow_review_positive_evidence_count"] > 0
    assert scorecard["paper_filter_shadow_review_candidate_enabled"] is False


def test_paper_filter_shadow_review_passed_requirements_include_baseline_and_false_win():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    scorecard = paper_filter_shadow_review_scorecard({**_paper_filter_candidate_base_report(), **registry})
    passed = scorecard["paper_filter_shadow_review_passed_requirements"]
    assert "FILTERED_MODEL_ABOVE_FILTERED_BASELINE" in passed
    assert "FALSE_WIN_LOW_OR_ZERO" in passed


def test_paper_filter_shadow_review_failed_requirements_include_current_blockers():
    registry = paper_filter_candidate_registry(_paper_filter_candidate_base_report())
    scorecard = paper_filter_shadow_review_scorecard({**_paper_filter_candidate_base_report(), **registry})
    failed = scorecard["paper_filter_shadow_review_failed_requirements"]
    assert "MIN_FILTERED_ROWS_GTE_100" in failed
    assert "MIN_PREDICTED_WIN_ROWS_GTE_30" in failed
    assert "MIN_PER_SEGMENT_ROWS_GTE_10" in failed
    assert "REGIME_EVIDENCE_AVAILABLE" in failed
    assert "OVERALL_READINESS_NOT_BLOCKED" in failed


def test_paper_filter_shadow_review_ready_status_keeps_candidate_disabled():
    report = {
        **_paper_filter_candidate_base_report(),
        "filtered_cohort_rows_kept": 120,
        "filtered_cohort_filtered_prediction_distribution": {"WIN": 35, "LOSS": 85},
        "filtered_cohort_findings": [],
        "filtered_cohort_fold_summary": [{"segment": "1", "rows_kept": 60, "insufficient_kept_rows": False}, {"segment": "2", "rows_kept": 60, "insufficient_kept_rows": False}],
        "filtered_cohort_regime_summary": [{"segment": "bull", "rows_kept": 60}, {"segment": "bear", "rows_kept": 60}],
        "model_readiness": {"overall_status": "REVIEW", "primary_blocker": None},
    }
    registry = paper_filter_candidate_registry(report)
    scorecard = paper_filter_shadow_review_scorecard({**report, **registry})
    assert scorecard["paper_filter_shadow_review_status"] == "REVIEW_READY_FOR_PAPER_ONLY_GOVERNANCE_REVIEW"
    assert scorecard["paper_filter_shadow_review_candidate_enabled"] is False


def test_paper_filter_shadow_review_does_not_change_readiness_or_governance(tmp_path):
    rows = _threshold_sufficiency_rows(total=120, pred_wins=2, false_win_count=0)
    for idx, row in enumerate(rows):
        row["y_true"] = "WIN" if idx < 80 else "LOSS"
        row["y_pred"] = "WIN" if idx < 2 else "LOSS"
        row.update({
            "prediction_id": f"p-shadow-{idx}",
            "prediction_timestamp": f"2024-06-{(idx % 28) + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-05-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_maturity_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-07-{(idx % 28) + 1:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "c",
        })
    report = _run_current_metric_report(tmp_path, rows)
    components = report["model_readiness"]["components"]
    assert report["paper_filter_shadow_review_candidate_enabled"] is False
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert components["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert components["Walk-Forward Stability"] == "BLOCKED_BELOW_BASELINE"
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True


def _upgrade_plan_base_report():
    return {
        "model_readiness": {
            "overall_status": "BLOCKED_BELOW_BASELINE",
            "primary_blocker": "BLOCKED_BELOW_BASELINE",
            "components": {
                "Baseline Superiority": "BLOCKED_BELOW_BASELINE",
                "Walk-Forward Stability": "BLOCKED_INSTABILITY",
            },
        },
        "baseline_superiority_status": "BLOCKED_BELOW_BASELINE",
        "model_vs_baseline_delta": -0.12,
        "threshold_candidate_diagnostic_status": "AVAILABLE",
        "threshold_candidate_selected": 0.80,
        "threshold_stability_audit_status": "REVIEW_INSUFFICIENT_THRESHOLD_SAMPLE",
        "threshold_stability_pred_distribution": {"LOSS": 12, "WIN": 2},
        "threshold_sample_sufficiency_status": "REVIEW_INSUFFICIENT_KEPT_ROWS;REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE",
        "threshold_sample_sufficiency_rows_kept": 14,
        "threshold_sample_sufficiency_pred_win_count": 2,
        "filtered_cohort_comparison_status": "AVAILABLE",
        "filtered_cohort_selected_threshold": 0.80,
        "filtered_cohort_filtered_vs_full_accuracy_delta": 0.20,
        "filtered_cohort_filtered_model_vs_baseline_delta": 0.10,
        "filtered_cohort_false_win_delta": -5,
        "filtered_cohort_filtered_false_win_count": 0,
        "filtered_cohort_filtered_prediction_distribution": {"LOSS": 12, "WIN": 2},
        "filtered_cohort_findings": ["REVIEW_INSUFFICIENT_FILTERED_SAMPLE", "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE"],
        "filtered_cohort_regime_summary": [],
        "paper_filter_candidate_status": "REVIEW_CANDIDATE_NOT_ENABLED",
        "paper_filter_candidate_enabled": False,
        "paper_filter_candidate_blockers": ["INSUFFICIENT_FILTERED_ROWS", "INSUFFICIENT_PREDICTED_WIN_SAMPLE", "OVERALL_READINESS_BLOCKED_BELOW_BASELINE"],
        "paper_filter_shadow_review_status": "REVIEW_SHADOW_CANDIDATE_BLOCKED",
        "paper_filter_shadow_review_blockers": ["INSUFFICIENT_FILTERED_ROWS", "INSUFFICIENT_PREDICTED_WIN_SAMPLE"],
    }


def test_ml_model_upgrade_diagnostic_status_available_with_prior_fields():
    from ml_metric_reconciliation import ml_model_repair_upgrade_diagnostic_plan

    plan = ml_model_repair_upgrade_diagnostic_plan(_upgrade_plan_base_report())

    assert plan["ml_model_upgrade_diagnostic_status"] == "AVAILABLE"
    assert plan["ml_model_upgrade_candidate_paths"] == [
        "CLASS_IMBALANCE_REPAIR",
        "COST_SENSITIVE_TRAINING",
        "PROBABILITY_CALIBRATION",
        "REGIME_AWARE_MODELING",
        "FEATURE_RELIABILITY_REVIEW",
    ]


def test_ml_model_upgrade_detects_broad_model_below_baseline():
    from ml_metric_reconciliation import ml_model_repair_upgrade_diagnostic_plan

    plan = ml_model_repair_upgrade_diagnostic_plan(_upgrade_plan_base_report())

    assert "BROAD_MODEL_BELOW_BASELINE" in plan["ml_model_upgrade_primary_problem"]
    assert "BASELINE_SUPERIORITY_NOT_PROVEN" in plan["ml_model_upgrade_blockers"]


def test_ml_model_upgrade_detects_promising_threshold_filter_but_undersampled():
    from ml_metric_reconciliation import ml_model_repair_upgrade_diagnostic_plan

    plan = ml_model_repair_upgrade_diagnostic_plan(_upgrade_plan_base_report())

    assert "THRESHOLD_FILTER_PROMISING_BUT_UNDERSAMPLED" in plan["ml_model_upgrade_primary_problem"]
    assert "THRESHOLD_FILTER_SAMPLE_SUPPORT_INSUFFICIENT" in plan["ml_model_upgrade_blockers"]


def test_ml_model_upgrade_recommends_first_path_for_weak_broad_model_and_promising_filter():
    from ml_metric_reconciliation import ml_model_repair_upgrade_diagnostic_plan

    plan = ml_model_repair_upgrade_diagnostic_plan(_upgrade_plan_base_report())

    assert plan["ml_model_upgrade_recommended_first_path"] == "CLASS_IMBALANCE_AND_THRESHOLD_CALIBRATION_DIAGNOSTIC"


def test_ml_model_upgrade_includes_regime_evidence_gap_when_regime_blocker_exists():
    from ml_metric_reconciliation import ml_model_repair_upgrade_diagnostic_plan
    report = _upgrade_plan_base_report()
    report["paper_filter_candidate_blockers"].append("REGIME_SEGMENT_UNAVAILABLE")
    report["filtered_cohort_regime_summary"] = {"status": "UNAVAILABLE"}

    plan = ml_model_repair_upgrade_diagnostic_plan(report)

    assert "REGIME_EVIDENCE_GAP" in plan["ml_model_upgrade_primary_problem"]
    assert "REGIME_EVIDENCE_UNAVAILABLE" in plan["ml_model_upgrade_blockers"]


def test_ml_model_upgrade_does_not_alter_readiness_governance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = tmp_path / "prediction_cohort.csv"
    rows = []
    for idx in range(20):
        actual = "WIN" if idx < 8 else "LOSS"
        predicted = "LOSS" if idx < 14 else "WIN"
        rows.append({
            "prediction_timestamp": f"2024-02-{idx + 1:02d}T00:00:00Z",
            "feature_timestamp_max": f"2024-02-{idx + 1:02d}T00:00:00Z",
            "target_timestamp": f"2024-02-{idx + 2:02d}T00:00:00Z",
            "model_version": "m1",
            "evaluation_contract": "contract-v1",
            "fold_id": idx // 5,
            "y_true": actual,
            "y_pred": predicted,
            "predicted_probability": 0.85 if idx < 14 else 0.55,
        })
    pd.DataFrame(rows).to_csv(cohort, index=False)

    report = run_ml_metric_reconciliation(
        output_dir="reports",
        db_path="missing.db",
        model_output_path="missing_model.json",
        walkforward_path="missing_walk.csv",
        prediction_artifact_path=str(cohort),
        prediction_ledger_path="missing_ledger.jsonl",
    )

    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Baseline Superiority"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Walk-Forward Stability"].startswith("BLOCKED")
    assert report["governance"]["execution_allowed"] is False
    assert report["governance"]["paper_only"] is True
    assert report["ml_model_upgrade_diagnostic_status"] == "AVAILABLE"


def _write_gap_fixture(tmp_path, raw_rows, ml_rows):
    reports = tmp_path / "reports"
    reports.mkdir()
    raw = reports / "paper_outcome_audit.json"
    ledger = reports / "ml_prediction_ledger.jsonl"
    raw.write_text(json.dumps({"closed_trades": raw_rows}), encoding="utf-8")
    ledger.write_text("".join(json.dumps(row) + "\n" for row in ml_rows), encoding="utf-8")
    return raw, ledger


def _gap_audit(tmp_path, raw_rows, ml_rows, extra=None):
    from ml_metric_reconciliation import raw_closed_to_ml_cohort_gap_reason_audit

    raw, ledger = _write_gap_fixture(tmp_path, raw_rows, ml_rows)
    report = {
        "raw_closed_source_selected_path": str(raw),
        "raw_closed_source_selected_row_count": len(raw_rows),
        "prediction_ledger_path": str(ledger),
    }
    if extra:
        report.update(extra)
    return raw_closed_to_ml_cohort_gap_reason_audit(report)


def _linkage_audit(tmp_path, raw_rows, ml_rows, extra=None):
    raw, ledger = _write_gap_fixture(tmp_path, raw_rows, ml_rows)
    report = {
        "raw_closed_source_selected_path": str(raw),
        "raw_closed_source_selected_row_count": len(raw_rows),
        "prediction_ledger_path": str(ledger),
    }
    if extra:
        report.update(extra)
    return prediction_outcome_linkage_contract_audit(report)


def test_prediction_outcome_linkage_blocks_when_raw_lacks_ids_but_ledger_has_prediction_id(tmp_path):
    audit = _linkage_audit(
        tmp_path,
        [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}],
        [{"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02", "y_true": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["prediction_outcome_linkage_contract_status"] == "BLOCKED_LINKAGE_CONTRACT_INCOMPLETE"


def test_prediction_outcome_linkage_emits_raw_closed_missing_stable_id(tmp_path):
    audit = _linkage_audit(
        tmp_path,
        [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}],
        [{"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02"}],
    )

    assert "RAW_CLOSED_MISSING_STABLE_LINKAGE_ID" in audit["prediction_outcome_linkage_contract_gaps"]


def test_prediction_outcome_linkage_emits_ledger_has_id_but_outcome_does_not(tmp_path):
    audit = _linkage_audit(
        tmp_path,
        [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}],
        [{"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02"}],
    )

    assert "PREDICTION_LEDGER_HAS_ID_BUT_OUTCOME_SOURCE_DOES_NOT" in audit["prediction_outcome_linkage_contract_gaps"]


def test_prediction_outcome_linkage_emits_weak_fallback_when_only_symbol_closed_available(tmp_path):
    audit = _linkage_audit(
        tmp_path,
        [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}],
        [{"symbol": "BTC", "target_timestamp": "2024-01-02", "y_true": "WIN"}],
    )

    assert audit["prediction_outcome_linkage_preferred_key"] == "symbol+closed_at"
    assert "FALLBACK_JOIN_KEY_WEAK_FOR_MODEL_REPAIR" in audit["prediction_outcome_linkage_contract_gaps"]


def test_prediction_outcome_linkage_reports_raw_and_ml_key_coverage(tmp_path):
    audit = _linkage_audit(
        tmp_path,
        [{"prediction_id": "p1", "symbol": "BTC", "closed_at": "2024-01-02"}],
        [{"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02"}],
    )

    assert audit["prediction_outcome_linkage_raw_closed_key_coverage"]["prediction_id"]["present"] == 1
    assert audit["prediction_outcome_linkage_ml_ledger_key_coverage"]["prediction_id"]["present"] == 1


def test_prediction_outcome_linkage_reports_minimum_required_future_fields(tmp_path):
    audit = _linkage_audit(tmp_path, [], [])

    fields = audit["prediction_outcome_linkage_minimum_required_future_fields"]
    assert "prediction_id" in fields
    assert "trade_id or signal_id" in fields
    assert "predicted_probability" in fields


def test_prediction_outcome_linkage_does_not_mutate_sources_or_backfill(tmp_path):
    raw, ledger = _write_gap_fixture(
        tmp_path,
        [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}],
        [{"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02"}],
    )
    raw_before = raw.read_bytes()
    ledger_before = ledger.read_bytes()

    prediction_outcome_linkage_contract_audit({
        "raw_closed_source_selected_path": str(raw),
        "prediction_ledger_path": str(ledger),
    })

    assert raw.read_bytes() == raw_before
    assert ledger.read_bytes() == ledger_before


def test_prediction_outcome_linkage_does_not_alter_readiness_governance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = tmp_path / "prediction_cohort.csv"
    pd.DataFrame([
        {"prediction_timestamp": "2024-01-01", "target_timestamp": "2024-01-02", "model_version": "m1", "evaluation_contract": "c", "y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.7},
        {"prediction_timestamp": "2024-01-02", "target_timestamp": "2024-01-03", "model_version": "m1", "evaluation_contract": "c", "y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.6},
    ]).to_csv(cohort, index=False)
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "paper_outcome_audit.json").write_text(json.dumps({"closed_trades": [{"symbol": "BTC", "closed_at": "2024-01-02", "status": "WIN"}]}), encoding="utf-8")
    (reports / "ml_prediction_ledger.jsonl").write_text(json.dumps({"prediction_id": "p1", "symbol": "BTC", "target_timestamp": "2024-01-02", "y_true": "WIN", "predicted_probability": 0.9}) + "\n", encoding="utf-8")

    report = run_ml_metric_reconciliation(
        output_dir="reports",
        db_path="missing.db",
        model_output_path="missing_model.json",
        walkforward_path="missing_walk.csv",
        prediction_artifact_path=str(cohort),
        prediction_ledger_path=str(reports / "ml_prediction_ledger.jsonl"),
    )

    assert report["prediction_outcome_linkage_contract_status"] == "BLOCKED_LINKAGE_CONTRACT_INCOMPLETE"
    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Baseline Superiority"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Walk-Forward Stability"].startswith("BLOCKED")
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True


def _producer_plan_base_report():
    return {
        "prediction_outcome_linkage_contract_status": "BLOCKED_LINKAGE_CONTRACT_INCOMPLETE",
        "prediction_outcome_linkage_contract_gaps": [
            "RAW_CLOSED_MISSING_STABLE_LINKAGE_ID",
            "PREDICTION_LEDGER_HAS_ID_BUT_OUTCOME_SOURCE_DOES_NOT",
            "FALLBACK_JOIN_KEY_WEAK_FOR_MODEL_REPAIR",
            "NO_HIGH_CONFIDENCE_ONE_TO_ONE_LINKAGE_KEY",
        ],
        "raw_to_ml_gap_reason_counts": {
            "MISSING_PREDICTION_ID": 403,
            "MISSING_ML_LEDGER_MATCH": 399,
            "MISSING_JOIN_KEY": 4,
        },
        "closed_to_ml_coverage_raw_to_ml_gap_count": 153,
        "model_readiness": {
            "overall_status": "BLOCKED_BELOW_BASELINE",
            "primary_blocker": "BLOCKED_BELOW_BASELINE",
            "components": {
                "Baseline Superiority": "BLOCKED_BELOW_BASELINE",
                "Walk-Forward Stability": "BLOCKED_INSTABILITY",
            },
            "execution_allowed": False,
            "paper_only": True,
        },
    }


def test_producer_plan_status_available_but_linkage_blocked_when_contract_incomplete():
    plan = prediction_outcome_linkage_producer_contract_plan(_producer_plan_base_report())

    assert plan["prediction_outcome_linkage_producer_plan_status"] == "PRODUCER_CONTRACT_PLAN_AVAILABLE_LINKAGE_BLOCKED"
    assert plan["prediction_outcome_linkage_producer_plan_mode"] == "FORWARD_ONLY_NO_BACKFILL_DIAGNOSTIC_PLAN"


def test_producer_plan_required_future_fields_include_linkage_and_prediction_context():
    plan = prediction_outcome_linkage_producer_contract_plan(_producer_plan_base_report())
    fields = plan["prediction_outcome_linkage_producer_plan_required_future_fields"]

    for field in [
        "prediction_id",
        "trade_id or signal_id",
        "symbol",
        "source_signal_timestamp",
        "target_timestamp",
        "closed_at",
        "outcome or label",
        "predicted_probability",
    ]:
        assert field in fields


def test_producer_plan_validation_rules_require_prediction_id_and_trade_or_signal_id():
    plan = prediction_outcome_linkage_producer_contract_plan(_producer_plan_base_report())
    rules = plan["prediction_outcome_linkage_producer_plan_validation_rules"]

    assert "Every future closed outcome must have prediction_id." in rules
    assert "Every future closed outcome must have either trade_id or signal_id." in rules


def test_producer_plan_blocks_model_repair_until_high_confidence_key_exists():
    plan = prediction_outcome_linkage_producer_contract_plan(_producer_plan_base_report())

    assert "MODEL_REPAIR_BLOCKED_UNTIL_LINKAGE_READY" in plan["prediction_outcome_linkage_producer_plan_blockers"]


def test_producer_plan_do_not_do_preserves_diagnostic_boundaries():
    plan = prediction_outcome_linkage_producer_contract_plan(_producer_plan_base_report())
    do_not_do = plan["prediction_outcome_linkage_producer_plan_do_not_do"]

    for item in [
        "no backfill in this PR",
        "no training changes",
        "no inference changes",
        "no prediction changes",
        "no threshold runtime application",
        "no readiness unlock",
    ]:
        assert item in do_not_do


def test_producer_plan_does_not_alter_readiness_governance_values():
    source = _producer_plan_base_report()
    before = json.loads(json.dumps(source["model_readiness"]))
    prediction_outcome_linkage_producer_contract_plan(source)

    assert source["model_readiness"] == before
    assert source["model_readiness"]["overall_status"] == "BLOCKED_BELOW_BASELINE"
    assert source["model_readiness"]["components"]["Baseline Superiority"] == "BLOCKED_BELOW_BASELINE"
    assert source["model_readiness"]["components"]["Walk-Forward Stability"] == "BLOCKED_INSTABILITY"
    assert source["model_readiness"]["execution_allowed"] is False
    assert source["model_readiness"]["paper_only"] is True


def test_raw_to_ml_gap_reason_audit_available_with_raw_source_and_ledger(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"prediction_id": "p1", "symbol": "BTC", "status": "WIN", "closed_at": "2024-01-02"}],
        [{"prediction_id": "p1", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.9}],
    )

    assert audit["raw_to_ml_gap_reason_audit_status"] == "AVAILABLE"


def test_raw_to_ml_gap_reason_audit_reports_counts(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"prediction_id": f"p{i}", "symbol": "BTC", "status": "WIN"} for i in range(4)],
        [{"prediction_id": "p0", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["raw_to_ml_gap_raw_closed_count"] == 4
    assert audit["raw_to_ml_gap_ml_cohort_count"] == 1
    assert audit["raw_to_ml_gap_count"] == 3


def test_raw_to_ml_gap_reason_audit_uses_prediction_id_join_key(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"prediction_id": "p1", "trade_id": "t1", "symbol": "BTC", "status": "WIN"}],
        [{"prediction_id": "p1", "trade_id": "different", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["raw_to_ml_gap_join_key_used"] == "prediction_id"
    assert audit["raw_to_ml_gap_join_key_status"] == "AVAILABLE"


def test_raw_to_ml_gap_reason_audit_counts_missing_prediction_id(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"symbol": "BTC", "entry_time": "2024-01-01", "status": "WIN"}],
        [{"prediction_id": "p1", "symbol": "BTC", "prediction_timestamp": "2024-01-02", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["raw_to_ml_gap_reason_counts"]["MISSING_PREDICTION_ID"] == 1


def test_raw_to_ml_gap_reason_audit_counts_missing_ml_ledger_match(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"prediction_id": "p1", "symbol": "BTC", "status": "WIN"}, {"prediction_id": "p2", "symbol": "ETH", "status": "LOSS"}],
        [{"prediction_id": "p1", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["raw_to_ml_gap_reason_counts"]["MISSING_ML_LEDGER_MATCH"] == 1


def test_raw_to_ml_gap_reason_audit_counts_missing_probability(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"prediction_id": "p1", "symbol": "BTC", "status": "WIN"}],
        [{"prediction_id": "p1", "y_true": "WIN", "y_pred": "WIN"}],
    )

    assert audit["raw_to_ml_gap_reason_counts"]["MISSING_PROBABILITY"] == 1


def test_raw_to_ml_gap_reason_audit_join_key_unavailable_preserves_counts(tmp_path):
    audit = _gap_audit(
        tmp_path,
        [{"symbol": "BTC", "status": "WIN"}, {"symbol": "ETH", "status": "LOSS"}],
        [{"y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}],
    )

    assert audit["raw_to_ml_gap_join_key_status"] == "JOIN_KEY_UNAVAILABLE"
    assert audit["raw_to_ml_gap_reason_counts"]["JOIN_KEY_UNAVAILABLE"] == 1
    assert audit["raw_to_ml_gap_reason_counts"]["UNKNOWN_REQUIRES_MANUAL_REVIEW"] == 1


def test_raw_to_ml_gap_reason_audit_sample_metadata_bounded_and_safe(tmp_path):
    raw_rows = [{"prediction_id": f"p{i}", "symbol": "BTC", "status": "WIN", "closed_at": "2024-01-02", "secret": "do-not-dump"} for i in range(10)]
    audit = _gap_audit(tmp_path, raw_rows, [{"prediction_id": "other", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.8}])

    sample = audit["raw_to_ml_gap_sample_unmatched_raw_metadata"]
    assert len(sample) == 5
    assert all("secret" not in row for row in sample)
    assert set(sample[0]) == {"symbol", "status", "closed_at", "prediction_id_present", "label_present", "probability_present"}


def test_raw_to_ml_gap_reason_audit_does_not_alter_readiness_governance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cohort = tmp_path / "prediction_cohort.csv"
    pd.DataFrame([
        {"prediction_timestamp": "2024-01-01", "target_timestamp": "2024-01-02", "model_version": "m1", "evaluation_contract": "c", "y_true": "WIN", "y_pred": "LOSS", "predicted_probability": 0.7},
        {"prediction_timestamp": "2024-01-02", "target_timestamp": "2024-01-03", "model_version": "m1", "evaluation_contract": "c", "y_true": "LOSS", "y_pred": "WIN", "predicted_probability": 0.6},
    ]).to_csv(cohort, index=False)
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "paper_outcome_audit.json").write_text(json.dumps({"closed_trades": [{"prediction_id": "p1", "symbol": "BTC", "status": "WIN"}]}), encoding="utf-8")
    (reports / "ml_prediction_ledger.jsonl").write_text(json.dumps({"prediction_id": "p1", "y_true": "WIN", "y_pred": "WIN", "predicted_probability": 0.9}) + "\n", encoding="utf-8")

    report = run_ml_metric_reconciliation(
        output_dir="reports",
        db_path="missing.db",
        model_output_path="missing_model.json",
        walkforward_path="missing_walk.csv",
        prediction_artifact_path=str(cohort),
        prediction_ledger_path=str(reports / "ml_prediction_ledger.jsonl"),
    )

    assert report["model_readiness"]["overall_status"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Baseline Superiority"].startswith("BLOCKED")
    assert report["model_readiness"]["components"]["Walk-Forward Stability"].startswith("BLOCKED")
    assert report["model_readiness"]["execution_allowed"] is False
    assert report["model_readiness"]["paper_only"] is True
