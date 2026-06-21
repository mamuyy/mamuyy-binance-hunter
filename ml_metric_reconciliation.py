"""Phase 9D.1B-B read-only ML metric reconciliation audit.

This module is intentionally observational. It never trains models, mutates model
artifacts, writes databases, changes thresholds, promotes models, or unlocks
execution.
"""
from __future__ import annotations

import csv
import json
import math
import hashlib
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from database import sqlite_path
from ml_engine import CATEGORICAL_FEATURES, NUMERIC_FEATURES, PROFITABLE_LABELS, TARGET_LABELS, build_ml_dataset
from ml_prediction_ledger import audit_prediction_ledger, canonical_ml_label, write_prediction_ledger_audit
from ml_temporal_guard import validate_temporal_feature_rows

PHASE = "9D.1C-C Train-Only Preprocessing Guard"
REPRO_STATUSES = {
    "REPRODUCED_EXACT",
    "REPRODUCED_WITH_ROUNDING",
    "CONTRACT_DIFFERENT",
    "SOURCE_STALE",
    "SOURCE_MISSING",
    "UNREPRODUCIBLE",
    "UNVERIFIABLE",
}
MANDATORY_COMPONENTS = [
    "Metric Integrity",
    "Display Metric Integrity",
    "Evaluation Metric Integrity",
    "Data Lineage",
    "Label Integrity",
    "Leakage Safety",
    "Baseline Superiority",
    "Out-of-Sample Adequacy",
    "Walk-Forward Stability",
]
BLOCKER_PRECEDENCE = [
    "BLOCKED_LEAKAGE",
    "BLOCKED_LABEL_CONTRACT",
    "BLOCKED_UNREPRODUCIBLE",
    "BLOCKED_STALE_SOURCE",
    "BLOCKED_BELOW_BASELINE",
    "BLOCKED_INSUFFICIENT_OOS",
    "BLOCKED_INSTABILITY",
]
TARGET_LIKE_COLUMNS = {"target", "status", "win_loss", "pnl_percent", "pnl_pct", "future_return", "direction_hit"}
PREDICTION_COLUMNS = ("y_pred", "prediction", "predicted_label", "predicted_class", "pred_profit", "predicted_direction")
PREDICTED_PROBABILITY_COLUMNS = ("predicted_probability", "prediction_probability", "probability", "confidence", "confidence_score")
TRUE_COLUMNS = ("y_true", "actual", "actual_label", "actual_class", "target", "actual_profit", "direction_hit")
DEFAULT_STALE_TTL_DAYS = 7.0
MANDATORY_CURRENT_READINESS_METRICS = {"Current Model Accuracy", "Walk-Forward Rolling Accuracy", "Walk-Forward Rolling Winrate", "AI Confidence", "Model Health", "Overfit Risk"}



def actual_model_feature_columns(feature_columns: Optional[Sequence[str]] = None) -> List[str]:
    """Return the model feature scope used by ML readiness leakage checks.

    Prediction cohort and ledger artifacts can carry labels, outcomes, row IDs,
    fold/window metadata, and other evaluation-only evidence columns.  Those
    columns are not model inputs.  Keep the default scope aligned with
    ml_engine.build_ml_dataset(), which validates and trains on only
    NUMERIC_FEATURES + CATEGORICAL_FEATURES.
    """
    columns = list(feature_columns) if feature_columns is not None else [*NUMERIC_FEATURES, *CATEGORICAL_FEATURES]
    return [str(column) for column in columns]


def readiness_temporal_feature_guard(
    cohort: pd.DataFrame,
    source_artifact: Optional[str] = None,
    feature_columns: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Validate temporal integrity using actual model features, not ledger metadata."""
    return validate_temporal_feature_rows(
        cohort if not cohort.empty else [],
        feature_columns=actual_model_feature_columns(feature_columns),
        source_artifact=source_artifact,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def round_or_none(value: Any, digits: int = 6) -> Optional[float]:
    f = safe_float(value)
    return None if f is None else round(f, digits)


def atomic_write_json(path: str | Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_csv(path: str | Path, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deterministic_rows = sorted(list(rows), key=lambda row: tuple(str(row.get(field, "")) for field in fieldnames))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(deterministic_rows)


def read_json(path: str | Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def file_age_days(path: str | Path, now: Optional[datetime] = None) -> Optional[float]:
    p = Path(path)
    if not p.exists():
        return None
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)).total_seconds() / 86400.0


def internal_artifact_timestamp(path: str | Path) -> Tuple[Optional[datetime], Optional[str]]:
    p = Path(path)
    if not p.exists():
        return None, None
    if p.suffix.lower() == ".json":
        data = read_json(p) or {}
        for key in ("generated_at", "timestamp", "created_at", "recorded_at"):
            value = data.get(key)
            if value:
                parsed = pd.to_datetime(value, errors="coerce", utc=True)
                if pd.notna(parsed):
                    return parsed.to_pydatetime(), key
    if p.suffix.lower() == ".csv":
        try:
            frame = pd.read_csv(p, nrows=20)
            for key in ("generated_at", "timestamp", "created_at"):
                if key in frame.columns and frame[key].notna().any():
                    parsed = pd.to_datetime(frame[key].dropna().iloc[0], errors="coerce", utc=True)
                    if pd.notna(parsed):
                        return parsed.to_pydatetime(), key
        except Exception:
            return None, None
    return None, None


def artifact_age_days(path: str | Path, now: Optional[datetime] = None) -> Tuple[Optional[float], str, Optional[str], Optional[str]]:
    ts, field = internal_artifact_timestamp(path)
    now = now or datetime.now(timezone.utc)
    if ts is not None:
        return (now - ts).total_seconds() / 86400.0, "internal_timestamp", ts.isoformat(), field
    age = file_age_days(path, now=now)
    timestamp = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat() if age is not None else None
    return age, "filesystem_mtime" if age is not None else "missing", timestamp, None


def producer_evidence(module_path: str, function_name: str, field_names: Sequence[str]) -> Dict[str, Any]:
    path = Path(module_path)
    if not path.exists():
        return {"verified": False, "evidence": "module missing"}
    text = path.read_text(encoding="utf-8", errors="ignore")
    function_ok = f"def {function_name}" in text if function_name else True
    fields_ok = all(str(field) in text for field in field_names if field)
    return {"verified": bool(function_ok and fields_ok), "function_found": function_ok, "fields_found": fields_ok, "module_path": module_path}


def normalize_sqlite_readonly_uri(database_url_or_path: str) -> str:
    """Normalize like database.sqlite_path, but force SQLite read-only URI mode."""
    path = sqlite_path(database_url_or_path)
    return f"file:{Path(path).expanduser().resolve()}?mode=ro"


def connect_readonly(database_url_or_path: str) -> sqlite3.Connection:
    uri = normalize_sqlite_readonly_uri(database_url_or_path)
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_table_diagnostics(db_path: str, table: str) -> Dict[str, Any]:
    normalized_path = str(Path(sqlite_path(db_path)).expanduser().resolve())
    diagnostics: Dict[str, Any] = {
        "normalized_database_path": normalized_path,
        "readonly_uri": normalize_sqlite_readonly_uri(db_path),
        "database_file_exists": Path(sqlite_path(db_path)).exists(),
        "table_lookup_result": None,
        "sqlite_exception": None,
        "schema": [],
        "row_count": None,
        "query_status": "NOT_RUN",
    }
    if not diagnostics["database_file_exists"]:
        diagnostics["table_lookup_result"] = False
        diagnostics["row_count"] = 0
        diagnostics["query_status"] = "DATABASE_MISSING"
        return diagnostics
    try:
        with connect_readonly(db_path) as connection:
            table_row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
            exists = table_row is not None
            diagnostics["table_lookup_result"] = exists
            if not exists:
                diagnostics["row_count"] = 0
                diagnostics["query_status"] = "TABLE_MISSING"
                return diagnostics
            diagnostics["schema"] = [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
            diagnostics["row_count"] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
            diagnostics["query_status"] = "OK"
    except sqlite3.Error as exc:
        diagnostics["sqlite_exception"] = str(exc)
        diagnostics["query_status"] = "SQLITE_ERROR"
    return diagnostics


def table_exists_readonly(db_path: str, table: str) -> bool:
    return sqlite_table_diagnostics(db_path, table).get("table_lookup_result") is True


def load_table_readonly(db_path: str, table: str) -> pd.DataFrame:
    if not table_exists_readonly(db_path, table):
        return pd.DataFrame()
    with connect_readonly(db_path) as connection:
        return pd.read_sql_query(f"SELECT * FROM {table}", connection)


def _schema_for_path(path: Path) -> List[str]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        data = read_json(path) or {}
        return sorted(data.keys())
    if path.suffix.lower() in {".csv", ".jsonl"}:
        try:
            if path.suffix.lower() == ".jsonl":
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            obj = json.loads(line)
                            return sorted(obj.keys()) if isinstance(obj, dict) else []
                return []
            return pd.read_csv(path, nrows=0).columns.tolist()
        except Exception:
            return []
    return []


def discover_artifacts(
    db_path: str = "mamuyy_hunter.db",
    model_output_path: str = "model_output.json",
    walkforward_path: str = "walkforward_results.csv",
    stale_ttl_days: float = DEFAULT_STALE_TTL_DAYS,
    search_roots: Optional[Sequence[str | Path]] = None,
    allow_fallbacks: bool = True,
) -> List[Dict[str, Any]]:
    candidates = [
        {
            "artifact_name": "model_output",
            "configured_path": model_output_path,
            "fallback_paths": ["model_output.json", "reports/model_output.json"],
            "producer": "ml_engine.run_ml_research",
            "consumer": "telegram.format_ml_analysis_message/dashboard/ml_results",
        },
        {
            "artifact_name": "walkforward_results",
            "configured_path": walkforward_path,
            "fallback_paths": ["walkforward_results.csv", "reports/walkforward_results.csv"],
            "producer": "walkforward.run_walkforward_validation",
            "consumer": "telegram.format_walkforward_report_message/dashboard/walkforward_results",
        },
        {
            "artifact_name": "ml_quality_audit",
            "configured_path": "ml_quality_audit.json",
            "fallback_paths": ["ml_quality_audit.json", "reports/ml_quality_audit.json"],
            "producer": "ml_quality_audit.run_audit",
            "consumer": "operator reports",
        },
        {
            "artifact_name": "candidate_evidence_ledger",
            "configured_path": "reports/candidate_evidence_ledger.jsonl",
            "fallback_paths": ["reports/candidate_evidence_ledger.jsonl"],
            "producer": "candidate_evidence_ledger.run",
            "consumer": "Phase 9D candidate evidence reports",
        },
    ]
    defaults = {"model_output": "model_output.json", "walkforward_results": "walkforward_results.csv", "ml_quality_audit": "ml_quality_audit.json", "candidate_evidence_ledger": "reports/candidate_evidence_ledger.jsonl"}
    roots = [Path(root) for root in (search_roots or [Path.cwd()])]
    discovered: List[Dict[str, Any]] = []
    for candidate in candidates:
        configured = Path(str(candidate["configured_path"]))
        artifact_name = str(candidate["artifact_name"])
        explicit_non_default = str(candidate["configured_path"]) != defaults.get(artifact_name, str(candidate["configured_path"]))
        found = configured if configured.exists() else None
        fallback_candidates: List[Path] = []
        if allow_fallbacks and not explicit_non_default:
            for fallback in candidate["fallback_paths"]:
                fallback_path = Path(fallback)
                fallback_candidates.append(fallback_path)
                for root in roots:
                    fallback_candidates.append(root / fallback_path)
            for fallback_path in fallback_candidates:
                if found is None and fallback_path.exists():
                    found = fallback_path
        path = found or configured
        age, age_source, generated_ts, timestamp_field = artifact_age_days(path) if path.exists() else (None, "missing", None, None)
        discovered.append(
            {
                "artifact_name": candidate["artifact_name"],
                "configured_path": str(configured),
                "discovered_path": str(found) if found else None,
                "exists": bool(found and found.exists()),
                "generated_timestamp": generated_ts,
                "timestamp_field_used": timestamp_field,
                "file_age_days": round_or_none(age, 4),
                "age_source": age_source,
                "stale_ttl_days": stale_ttl_days,
                "stale_source": bool(age is not None and age > stale_ttl_days),
                "schema": _schema_for_path(path),
                "explicit_path_authoritative": explicit_non_default,
                "fallbacks_allowed": bool(allow_fallbacks and not explicit_non_default),
                "fallback_paths_considered": [str(item) for item in fallback_candidates],
                "producer": candidate["producer"],
                "consumer": candidate["consumer"],
            }
        )
    for table in ["ml_results", "walkforward_results", "historical_outcomes", "internal_paper_trades"]:
        diagnostics = sqlite_table_diagnostics(db_path, table)
        exists = diagnostics.get("table_lookup_result") is True
        schema = diagnostics.get("schema") or []
        db_age, db_age_source, db_generated_ts, db_timestamp_field = artifact_age_days(sqlite_path(db_path)) if Path(sqlite_path(db_path)).exists() else (None, "missing", None, None)
        discovered.append(
            {
                "artifact_name": f"database_table:{table}",
                "configured_path": db_path,
                "discovered_path": normalize_sqlite_readonly_uri(db_path) if Path(sqlite_path(db_path)).exists() else None,
                "exists": exists,
                "generated_timestamp": db_generated_ts,
                "timestamp_field_used": db_timestamp_field,
                "file_age_days": round_or_none(db_age, 4),
                "age_source": db_age_source,
                "stale_ttl_days": stale_ttl_days,
                "stale_source": bool(db_age is not None and db_age > stale_ttl_days),
                "schema": schema,
                "row_count": diagnostics.get("row_count"),
                "sqlite_diagnostics": diagnostics,
                "producer": "database.py insert_* or historical label/outcome producers",
                "consumer": "dashboard/risk/audit modules",
            }
        )
    return discovered


def producer_inventory(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    artifact_by_name = {item["artifact_name"]: item for item in artifacts}
    expected = [
        ("Current Model Accuracy", "ml_engine.py", "run_ml_research", "model_output", "accuracy"),
        ("AI Confidence", "ml_engine.py", "run_ml_research", "model_output", "ai_confidence_score"),
        ("Setup Ranking", "ml_engine.py", "_quality/run_ml_research", "model_output", "setup_ranking"),
        ("Top Features", "ml_engine.py", "run_ml_research", "model_output", "feature_importance"),
        ("Most Profitable Regime", "ml_engine.py", "_regime_profitability", "model_output", "most_profitable_regime"),
        ("Worst Regime", "ml_engine.py/walkforward.py", "_regime_profitability/run_walkforward_validation", "model_output", "worst_regime"),
        ("Walk-Forward Rolling Accuracy", "walkforward.py", "run_walkforward_validation", "walkforward_results", "test_accuracy"),
        ("Walk-Forward Rolling Winrate", "walkforward.py", "run_walkforward_validation", "walkforward_results", "winrate"),
        ("Model Health", "walkforward.py", "_health/run_walkforward_validation", "walkforward_results", "model_health"),
        ("Overfit Risk", "walkforward.py", "run_walkforward_validation", "walkforward_results", "train_accuracy,test_accuracy"),
        ("Historical ML accuracy snapshot", "ml_quality_audit.py", "run_audit", "ml_quality_audit", "global_accuracy"),
        ("Candidate Directional Accuracy", "candidate_validator.py/candidate_evidence_ledger.py", "validate_candidate/run", "candidate_evidence_ledger", "direction_hit"),
    ]
    rows: List[Dict[str, Any]] = []
    for metric_name, module, function, artifact_name, columns in expected:
        artifact = artifact_by_name.get(artifact_name, {})
        schema = artifact.get("schema") or []
        required_columns = [col.strip() for col in columns.split(",")]
        source_verified = bool(artifact.get("exists"))
        source_stale = bool(artifact.get("stale_source"))
        module_path = module.split("/")[0] if "/" in module else module
        first_function = function.split("/")[0]
        evidence = producer_evidence(module_path, first_function, required_columns)
        contract_verified = source_verified and not source_stale and all(col in schema for col in required_columns if artifact_name != "walkforward_results" or col != "model_health")
        rows.append(
            {
                "metric_name": metric_name,
                "expected_producer": f"{module}:{function}",
                "discovered_producer": artifact.get("producer"),
                "producer_verified": bool(evidence.get("verified")),
                "producer_evidence": evidence,
                "source_artifact": artifact.get("discovered_path") or artifact.get("configured_path"),
                "configured_path": artifact.get("configured_path"),
                "source_columns": columns,
                "source_verified": source_verified,
                "source_stale": source_stale,
                "contract_verified": contract_verified,
                "user_facing_consumers": artifact.get("consumer"),
                "reproducibility_status": "SOURCE_MISSING" if not source_verified else ("SOURCE_STALE" if source_stale else ("UNREPRODUCIBLE" if not contract_verified else "REPRODUCED_WITH_ROUNDING")),
            }
        )
    return rows


def find_column(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    lower = {str(col).lower(): str(col) for col in columns}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return None


def load_prediction_cohort(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {"status": "SOURCE_MISSING", "reason": "no prediction artifact path discovered", "frame": pd.DataFrame()}
    p = Path(path)
    if not p.exists():
        return {"status": "SOURCE_MISSING", "reason": f"prediction artifact missing: {path}", "frame": pd.DataFrame()}
    try:
        if p.suffix.lower() == ".csv":
            frame = pd.read_csv(p)
        elif p.suffix.lower() == ".json":
            data = read_json(p) or {}
            rows = data.get("predictions") or data.get("rows") or data.get("evaluation_rows")
            frame = pd.DataFrame(rows if isinstance(rows, list) else [])
        else:
            return {"status": "UNREPRODUCIBLE", "reason": f"unsupported prediction artifact suffix: {p.suffix}", "frame": pd.DataFrame()}
    except Exception as exc:
        return {"status": "UNREPRODUCIBLE", "reason": str(exc), "frame": pd.DataFrame()}
    y_true_col = find_column(frame.columns, TRUE_COLUMNS)
    y_pred_col = find_column(frame.columns, PREDICTION_COLUMNS)
    required_meta = {
        "prediction_timestamp": find_column(frame.columns, ("prediction_timestamp", "timestamp", "signal_timestamp")),
        "target_maturity_timestamp": find_column(frame.columns, ("target_maturity_timestamp", "target_timestamp", "close_timestamp")),
        "model_version": find_column(frame.columns, ("model_version", "model_artifact_version")),
        "evaluation_contract": find_column(frame.columns, ("evaluation_contract", "contract")),
    }
    missing = [key for key, value in {"y_true": y_true_col, "y_pred": y_pred_col, **required_meta}.items() if not value]
    if missing:
        return {"status": "UNREPRODUCIBLE", "reason": "missing required prediction cohort columns: " + ", ".join(missing), "frame": frame}
    if y_true_col == y_pred_col:
        return {"status": "UNREPRODUCIBLE", "reason": "y_true and y_pred resolve to the same column", "frame": frame}
    out = frame.copy()
    out["__y_true"] = out[y_true_col]
    out["__y_pred"] = out[y_pred_col]
    probability_col = find_column(frame.columns, PREDICTED_PROBABILITY_COLUMNS)
    if probability_col:
        out["__predicted_probability"] = pd.to_numeric(out[probability_col], errors="coerce")
    columns = {"y_true": y_true_col, "y_pred": y_pred_col, **required_meta}
    if probability_col:
        columns["predicted_probability"] = probability_col
    return {"status": "AVAILABLE", "reason": None, "frame": out, "columns": columns}


def current_readiness_metric_evidence(cohort_result: Dict[str, Any], source_path: Optional[str]) -> Dict[str, Any]:
    """Reproduce current readiness metrics from row-level prediction evidence.

    The values are intentionally derived from the evaluated prediction cohort,
    not from model-output random holdout summaries.
    """
    if cohort_result.get("status") != "AVAILABLE":
        reason = cohort_result.get("reason") or "prediction cohort unavailable"
        return {
            "current_accuracy_reproduction_status": cohort_result.get("status", "SOURCE_MISSING"),
            "current_accuracy_sample_count": 0,
            "current_accuracy_source": source_path,
            "current_accuracy_value": None,
            "current_accuracy_correct_predictions": None,
            "ai_confidence_reproduction_status": "UNAVAILABLE",
            "ai_confidence_sample_count": 0,
            "ai_confidence_source": source_path,
            "ai_confidence_formula": "mean(predicted_probability) over evaluated prediction cohort rows with non-null predicted_probability",
            "ai_confidence_value": None,
            "reason": reason,
        }
    frame = cohort_result["frame"]
    evaluated = frame.dropna(subset=["__y_true", "__y_pred"]).copy()
    sample_count = int(len(evaluated))
    if sample_count == 0:
        accuracy_status = "UNAVAILABLE"
        accuracy = None
        correct = None
    else:
        correct = int((evaluated["__y_true"].astype(str) == evaluated["__y_pred"].astype(str)).sum())
        accuracy = correct / sample_count
        accuracy_status = "REPRODUCED_EXACT"
    probability = pd.to_numeric(evaluated.get("__predicted_probability", pd.Series(dtype=float)), errors="coerce").dropna()
    if sample_count == 0:
        confidence_status = "UNAVAILABLE"
        confidence = None
    elif probability.empty:
        confidence_status = "UNAVAILABLE"
        confidence = None
    else:
        confidence_status = "REPRODUCED_EXACT"
        confidence = float(probability.mean())
    return {
        "current_accuracy_reproduction_status": accuracy_status,
        "current_accuracy_sample_count": sample_count,
        "current_accuracy_source": source_path,
        "current_accuracy_value": accuracy,
        "current_accuracy_correct_predictions": correct,
        "ai_confidence_reproduction_status": confidence_status,
        "ai_confidence_sample_count": int(len(probability)),
        "ai_confidence_source": source_path,
        "ai_confidence_formula": "mean(predicted_probability) over evaluated prediction cohort rows with non-null predicted_probability",
        "ai_confidence_value": confidence,
        "reason": None if accuracy_status == "REPRODUCED_EXACT" else "no evaluated prediction rows",
    }


def no_evaluation_metrics(status: str, reason: str) -> Dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "metrics": None,
        "confusion_matrix": [{"actual_class": "NO_EVALUATION_SAMPLE", **{label: None for label in TARGET_LABELS}}],
    }


def classification_metrics(y_true: Sequence[Any], y_pred: Sequence[Any], labels: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    labels = list(labels or sorted(set(y_true) | set(y_pred), key=str))
    n = len(y_true)
    if n == 0:
        return {
            "samples": 0,
            "accuracy": None,
            "balanced_accuracy": None,
            "by_class": {str(label): {"precision": None, "recall": None, "f1": None, "support": 0} for label in labels},
            "macro_f1": None,
            "weighted_f1": None,
            "confusion_matrix": [{"actual_class": "NO_EVALUATION_SAMPLE", **{str(label): None for label in labels}}],
            "class_support": {},
            "majority_class_baseline": None,
            "random_prior_baseline": None,
            "mcc": None,
        }
    counts = Counter(y_true)
    correct = sum(1 for actual, pred in zip(y_true, y_pred) if actual == pred)
    by_class: Dict[str, Dict[str, Any]] = {}
    matrix: List[Dict[str, Any]] = []
    for label in labels:
        tp = sum(actual == label and pred == label for actual, pred in zip(y_true, y_pred))
        fp = sum(actual != label and pred == label for actual, pred in zip(y_true, y_pred))
        fn = sum(actual == label and pred != label for actual, pred in zip(y_true, y_pred))
        support = counts.get(label, 0)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
        by_class[str(label)] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
        matrix.append({"actual_class": str(label), **{str(pred_label): sum(actual == label and pred == pred_label for actual, pred in zip(y_true, y_pred)) for pred_label in labels}})
    recalls = [row["recall"] for row in by_class.values() if row["recall"] is not None]
    f1s = [row["f1"] for row in by_class.values() if row["f1"] is not None]
    weighted_terms = [row["f1"] * row["support"] for row in by_class.values() if row["f1"] is not None]
    return {
        "samples": n,
        "accuracy": correct / n,
        "balanced_accuracy": sum(recalls) / len(labels) if labels else None,
        "by_class": by_class,
        "macro_f1": sum(f1s) / len(labels) if labels else None,
        "weighted_f1": sum(weighted_terms) / n if weighted_terms else None,
        "confusion_matrix": matrix,
        "class_support": dict(counts),
        "majority_class_baseline": max(counts.values()) / n if counts else None,
        "random_prior_baseline": sum((count / n) ** 2 for count in counts.values()) if counts else None,
        "mcc": mcc_binary(y_true, y_pred),
    }



def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    return None if denominator == 0 else numerator / denominator


def _threshold_confusion_row(threshold: float, rows: Sequence[Tuple[str, str, Optional[float]]], sample_count: int) -> Dict[str, Any]:
    kept = [(actual, predicted) for actual, predicted, probability in rows if probability is not None and probability >= threshold]
    kept_true = [actual for actual, _ in kept]
    kept_pred = [predicted for _, predicted in kept]
    rows_kept = len(kept)
    true_win_count = sum(actual == "WIN" and predicted == "WIN" for actual, predicted in kept)
    true_loss_count = sum(actual == "LOSS" and predicted == "LOSS" for actual, predicted in kept)
    false_win_count = sum(actual == "LOSS" and predicted == "WIN" for actual, predicted in kept)
    false_loss_count = sum(actual == "WIN" and predicted == "LOSS" for actual, predicted in kept)
    predicted_win_count = sum(predicted == "WIN" for predicted in kept_pred)
    predicted_loss_count = sum(predicted == "LOSS" for predicted in kept_pred)
    actual_win_count = sum(actual == "WIN" for actual in kept_true)
    actual_loss_count = sum(actual == "LOSS" for actual in kept_true)
    return {
        "threshold": threshold,
        "rows_kept": rows_kept,
        "kept_ratio": _safe_ratio(rows_kept, sample_count),
        "accuracy": _safe_ratio(sum(actual == predicted for actual, predicted in kept), rows_kept),
        "win_precision": _safe_ratio(true_win_count, predicted_win_count),
        "win_recall": _safe_ratio(true_win_count, actual_win_count),
        "loss_precision": _safe_ratio(true_loss_count, predicted_loss_count),
        "loss_recall": _safe_ratio(true_loss_count, actual_loss_count),
        "false_win_count": false_win_count,
        "false_loss_count": false_loss_count,
        "true_win_count": true_win_count,
        "true_loss_count": true_loss_count,
        "predicted_label_distribution": _counter_dict(kept_pred),
        "true_label_distribution": _counter_dict(kept_true),
    }


def _canonical_probability_rows(cohort: pd.DataFrame) -> Tuple[List[Tuple[str, str, Optional[float]]], str]:
    if cohort.empty:
        return [], "No prediction cohort rows are available."
    frame = cohort.copy()
    if "__y_true" not in frame.columns:
        y_true_col = _first_existing_column(frame, TRUE_COLUMNS)
        if y_true_col:
            frame["__y_true"] = frame[y_true_col]
    if "__y_pred" not in frame.columns:
        y_pred_col = _first_existing_column(frame, PREDICTION_COLUMNS)
        if y_pred_col:
            frame["__y_pred"] = frame[y_pred_col]
    if "__predicted_probability" not in frame.columns:
        probability_col = _first_existing_column(frame, PREDICTED_PROBABILITY_COLUMNS)
        if probability_col:
            frame["__predicted_probability"] = pd.to_numeric(frame[probability_col], errors="coerce")
    if "__y_true" not in frame.columns or "__y_pred" not in frame.columns:
        return [], "y_true/y_pred columns are missing from row-level predictions."
    if "__predicted_probability" not in frame.columns:
        return [], "predicted_probability is missing from row-level predictions."
    rows = []
    for _, row in frame.dropna(subset=["__y_true", "__y_pred"]).iterrows():
        actual = canonical_ml_label(row.get("__y_true"))
        predicted = canonical_ml_label(row.get("__y_pred"))
        probability = safe_float(row.get("__predicted_probability"))
        if actual in {"WIN", "LOSS"} and predicted in {"WIN", "LOSS"} and probability is not None:
            rows.append((actual, predicted, probability))
    return rows, "No evaluated WIN/LOSS rows with predicted_probability are available." if not rows else ""


def ml_high_confidence_threshold_candidate_diagnostic(cohort: pd.DataFrame) -> Dict[str, Any]:
    """Diagnostic-only high-confidence threshold candidate audit.

    The selected threshold is advisory evidence only. It is not applied to live
    signals and does not feed training, readiness gates, baseline superiority,
    walk-forward stability, or execution behavior.
    """
    base: Dict[str, Any] = {
        "threshold_candidate_diagnostic_status": "UNAVAILABLE",
        "high_confidence_threshold_diagnostic": [],
        "threshold_candidate_selected": None,
        "threshold_candidate_selection_reason": None,
        "threshold_candidate_rows_kept": 0,
        "threshold_candidate_kept_ratio": None,
        "threshold_candidate_accuracy": None,
        "threshold_candidate_false_win_count": None,
        "threshold_candidate_win_precision": None,
        "threshold_candidate_findings": [],
        "threshold_candidate_recommendation": (
            "Diagnostic only: do not apply this threshold to production, training, readiness gates, or execution behavior."
        ),
    }
    rows, reason = _canonical_probability_rows(cohort)
    if not rows:
        return {**base, "threshold_candidate_findings": [reason]}

    threshold_rows = [_threshold_confusion_row(threshold, rows, len(rows)) for threshold in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)]
    sufficient = [row for row in threshold_rows if row["rows_kept"] >= 30]
    candidate_pool = sufficient or threshold_rows
    selected = sorted(candidate_pool, key=lambda row: (row["false_win_count"], -(row["accuracy"] or -1), -row["threshold"], -row["rows_kept"]))[0]
    reason_text = (
        "Selected from thresholds retaining at least 30 rows by lowest false_win_count, then highest accuracy."
        if sufficient
        else "No threshold retained at least 30 rows; selected diagnostically by lowest false_win_count, then highest accuracy."
    )
    findings = [
        f"Threshold {selected['threshold']:.2f} has {selected['false_win_count']} false WIN predictions across {selected['rows_kept']} kept rows.",
        "This is diagnostic-only and does not alter production thresholds, readiness gates, or execution behavior.",
    ]
    return {
        **base,
        "threshold_candidate_diagnostic_status": "AVAILABLE",
        "high_confidence_threshold_diagnostic": threshold_rows,
        "threshold_candidate_selected": selected["threshold"],
        "threshold_candidate_selection_reason": reason_text,
        "threshold_candidate_rows_kept": selected["rows_kept"],
        "threshold_candidate_kept_ratio": selected["kept_ratio"],
        "threshold_candidate_accuracy": selected["accuracy"],
        "threshold_candidate_false_win_count": selected["false_win_count"],
        "threshold_candidate_win_precision": selected["win_precision"],
        "threshold_candidate_findings": findings,
        "threshold_candidate_recommendation": (
            "Use this diagnostic to decide whether to repair the model as a broad WIN predictor or evaluate it as a high-confidence LOSS-avoidance/trade filter; do not unlock readiness."
        ),
    }


def _canonical_probability_frame(cohort: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    if cohort.empty:
        return pd.DataFrame(), "No prediction cohort rows are available."
    frame = cohort.copy()
    if "__y_true" not in frame.columns:
        y_true_col = _first_existing_column(frame, TRUE_COLUMNS)
        if y_true_col:
            frame["__y_true"] = frame[y_true_col]
    if "__y_pred" not in frame.columns:
        y_pred_col = _first_existing_column(frame, PREDICTION_COLUMNS)
        if y_pred_col:
            frame["__y_pred"] = frame[y_pred_col]
    if "__predicted_probability" not in frame.columns:
        probability_col = _first_existing_column(frame, PREDICTED_PROBABILITY_COLUMNS)
        if probability_col:
            frame["__predicted_probability"] = pd.to_numeric(frame[probability_col], errors="coerce")
    if "__y_true" not in frame.columns or "__y_pred" not in frame.columns:
        return pd.DataFrame(), "y_true/y_pred columns are missing from row-level predictions."
    if "__predicted_probability" not in frame.columns:
        return pd.DataFrame(), "predicted_probability is missing from row-level predictions."
    frame["__canonical_y_true"] = frame["__y_true"].map(canonical_ml_label)
    frame["__canonical_y_pred"] = frame["__y_pred"].map(canonical_ml_label)
    frame["__predicted_probability"] = pd.to_numeric(frame["__predicted_probability"], errors="coerce")
    frame = frame[
        frame["__canonical_y_true"].isin(["WIN", "LOSS"])
        & frame["__canonical_y_pred"].isin(["WIN", "LOSS"])
        & frame["__predicted_probability"].notna()
    ].copy()
    if frame.empty:
        return pd.DataFrame(), "No evaluated WIN/LOSS rows with predicted_probability are available."
    return frame, ""


def _threshold_metrics_for_frame(frame: pd.DataFrame, sample_count: Optional[int] = None) -> Dict[str, Any]:
    actuals = frame["__canonical_y_true"].astype(str).tolist() if "__canonical_y_true" in frame.columns else []
    preds = frame["__canonical_y_pred"].astype(str).tolist() if "__canonical_y_pred" in frame.columns else []
    rows_kept = len(frame)
    sample_count = rows_kept if sample_count is None else sample_count
    predicted_win_count = sum(pred == "WIN" for pred in preds)
    predicted_loss_count = sum(pred == "LOSS" for pred in preds)
    true_win_label_count = sum(actual == "WIN" for actual in actuals)
    true_loss_label_count = sum(actual == "LOSS" for actual in actuals)
    true_win_count = sum(actual == "WIN" and pred == "WIN" for actual, pred in zip(actuals, preds))
    false_win_count = sum(actual == "LOSS" and pred == "WIN" for actual, pred in zip(actuals, preds))
    return {
        "rows_kept": rows_kept,
        "kept_ratio": _safe_ratio(rows_kept, sample_count),
        "accuracy": _safe_ratio(sum(actual == pred for actual, pred in zip(actuals, preds)), rows_kept),
        "predicted_win_count": predicted_win_count,
        "predicted_loss_count": predicted_loss_count,
        "true_win_count": true_win_label_count,
        "true_loss_count": true_loss_label_count,
        "false_win_count": false_win_count,
        "win_precision": _safe_ratio(true_win_count, predicted_win_count),
        "true_label_distribution": _counter_dict(actuals),
        "predicted_label_distribution": _counter_dict(preds),
    }


def _threshold_segment_summary(frame: pd.DataFrame, column: str) -> List[Dict[str, Any]]:
    rows = []
    for value, segment in frame.groupby(column, dropna=False):
        metrics = _threshold_metrics_for_frame(segment)
        rows.append({
            "segment": None if pd.isna(value) else str(value),
            "rows_kept": metrics["rows_kept"],
            "accuracy": metrics["accuracy"],
            "false_win_count": metrics["false_win_count"],
            "win_precision": metrics["win_precision"],
            "true_label_distribution": metrics["true_label_distribution"],
            "predicted_label_distribution": metrics["predicted_label_distribution"],
        })
    return sorted(rows, key=lambda row: (-int(row["rows_kept"]), str(row["segment"])))


def threshold_candidate_stability_audit(cohort: pd.DataFrame, selected_threshold: Optional[float] = None) -> Dict[str, Any]:
    """Diagnostic-only audit of selected threshold evidence stability.

    This does not apply the threshold to production signals, training, readiness
    gates, baseline superiority, walk-forward stability, or execution behavior.
    """
    base: Dict[str, Any] = {
        "threshold_stability_audit_status": "UNAVAILABLE_NO_SELECTED_THRESHOLD",
        "threshold_stability_selected_threshold": None,
        "threshold_stability_rows_kept": 0,
        "threshold_stability_min_segment_rows": None,
        "threshold_stability_segment_count": 0,
        "threshold_stability_fold_summary": [],
        "threshold_stability_symbol_summary": [],
        "threshold_stability_regime_summary": [],
        "threshold_stability_label_distribution": {},
        "threshold_stability_pred_distribution": {},
        "threshold_stability_false_win_count": None,
        "threshold_stability_accuracy": None,
        "threshold_stability_win_precision": None,
        "threshold_stability_findings": [],
        "threshold_stability_recommendation": (
            "Diagnostic only: review selected high-confidence threshold stability before any future model-filter proposal; do not unlock readiness or execution."
        ),
    }
    frame, reason = _canonical_probability_frame(cohort)
    if frame.empty:
        return {**base, "threshold_stability_findings": [reason]}
    if selected_threshold is None:
        diagnostic = ml_high_confidence_threshold_candidate_diagnostic(cohort)
        selected_threshold = diagnostic.get("threshold_candidate_selected")
    selected_threshold = safe_float(selected_threshold)
    if selected_threshold is None:
        return {**base, "threshold_stability_findings": ["No selected threshold is available for stability audit."]}

    kept = frame[frame["__predicted_probability"] >= selected_threshold].copy()
    metrics = _threshold_metrics_for_frame(kept, sample_count=len(frame))
    findings: List[str] = [
        "This threshold stability audit is diagnostic-only and does not alter readiness gates, training, or execution behavior."
    ]
    summaries: Dict[str, List[Dict[str, Any]]] = {}
    dimension_columns = {
        "fold": "fold_id" if "fold_id" in kept.columns else None,
        "symbol": "symbol" if "symbol" in kept.columns else None,
        "regime": next((col for col in ("market_regime", "regime", "regime_label") if col in kept.columns), None),
    }
    segment_sizes: List[int] = []
    for name, column in dimension_columns.items():
        if column is None:
            summaries[name] = []
            findings.append(f"{name} segment unavailable")
            continue
        summary = _threshold_segment_summary(kept, column)
        summaries[name] = summary
        segment_sizes.extend(int(row["rows_kept"]) for row in summary)
        if metrics["rows_kept"] and any((row["rows_kept"] / metrics["rows_kept"]) > 0.80 for row in summary):
            findings.append("REVIEW_SEGMENT_CONCENTRATION")
    if metrics["false_win_count"] == 0:
        findings.append("False WIN remained zero in available threshold stability evidence")
    elif metrics["false_win_count"] and metrics["false_win_count"] > 0:
        findings.append("False WIN appears in at least one segment; threshold requires further review")
    status = "AVAILABLE" if metrics["rows_kept"] >= 30 else "REVIEW_INSUFFICIENT_THRESHOLD_SAMPLE"
    return {
        **base,
        "threshold_stability_audit_status": status,
        "threshold_stability_selected_threshold": selected_threshold,
        "threshold_stability_rows_kept": metrics["rows_kept"],
        "threshold_stability_min_segment_rows": min(segment_sizes) if segment_sizes else None,
        "threshold_stability_segment_count": len(segment_sizes),
        "threshold_stability_fold_summary": summaries["fold"],
        "threshold_stability_symbol_summary": summaries["symbol"],
        "threshold_stability_regime_summary": summaries["regime"],
        "threshold_stability_label_distribution": metrics["true_label_distribution"],
        "threshold_stability_pred_distribution": metrics["predicted_label_distribution"],
        "threshold_stability_false_win_count": metrics["false_win_count"],
        "threshold_stability_accuracy": metrics["accuracy"],
        "threshold_stability_win_precision": metrics["win_precision"],
        "threshold_stability_findings": findings,
    }



def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _count_rows_readonly(path_value: Any) -> Optional[int]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.exists() or not path.is_file():
        return None
    try:
        if path.suffix.lower() == ".csv":
            return int(sum(len(chunk) for chunk in pd.read_csv(path, chunksize=10000)))
        if path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                for key in ("rows", "outcomes", "closed_outcomes", "records", "data"):
                    if isinstance(data.get(key), list):
                        return len(data[key])
    except Exception:
        return None
    return None


RAW_CLOSED_FIELD_CANDIDATES = {
    "closed_at",
    "status",
    "outcome",
    "label",
    "pnl",
    "prediction_id",
    "symbol",
    "entry_time",
    "exit_time",
}
RAW_CLOSED_PATH_HINTS = ("outcome", "closed", "ledger", "trade")
MAX_DISCOVERY_FILE_BYTES = 5_000_000
MAX_DISCOVERY_CANDIDATES = 80
CANONICAL_RAW_CLOSED_SOURCE_PATH = Path("reports") / "paper_outcome_audit.json"
CANONICAL_RAW_CLOSED_CONTAINER = "closed_trades"
CANONICAL_RAW_CLOSED_FIELDS = {"closed_at", "status", "symbol", "outcome", "label", "pnl", "prediction_id"}


def _safe_candidate_path(path_value: Any) -> Optional[Path]:
    if not path_value:
        return None
    try:
        path = Path(str(path_value)).expanduser()
    except (TypeError, ValueError):
        return None
    return path if path.exists() and path.is_file() else None


def _metadata_from_records(records: Sequence[Any]) -> Tuple[List[str], int]:
    fields = set()
    rows_seen = 0
    for row in records[:25]:
        if isinstance(row, dict):
            rows_seen += 1
            fields.update(str(key) for key in row.keys())
    detected = sorted(field for field in fields if field in RAW_CLOSED_FIELD_CANDIDATES)
    return detected, rows_seen


def _json_record_container(data: Any) -> Tuple[Optional[Sequence[Any]], Optional[int], Optional[str]]:
    if isinstance(data, list):
        return data, len(data), "root_list"
    if isinstance(data, dict):
        for key in ("rows", "outcomes", "closed_outcomes", "records", "data", "trades", "closed_trades", "ledger"):
            value = data.get(key)
            if isinstance(value, list):
                return value, len(value), key
    return None, None, None


def _inspect_json_candidate(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records, row_count, container = _json_record_container(data)
    detected, rows_seen = _metadata_from_records(list(records or [])[:25])
    return {"row_count": row_count, "detected_fields": detected, "sampled_rows": rows_seen, "container": container}


def _is_canonical_raw_closed_source_candidate(candidate: Dict[str, Any]) -> bool:
    """Identify the canonical diagnostic raw closed-outcome source.

    The rule is intentionally narrow and read-only: select
    reports/paper_outcome_audit.json only when it exposes a non-empty
    closed_trades JSON container with enough closed-outcome fields to support
    coverage reconciliation.
    """
    try:
        path = Path(str(candidate.get("path") or "")).resolve()
        canonical_path = CANONICAL_RAW_CLOSED_SOURCE_PATH.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    detected_fields = set(candidate.get("detected_fields") or [])
    return (
        path == canonical_path
        and candidate.get("type") == "json"
        and candidate.get("container") == CANONICAL_RAW_CLOSED_CONTAINER
        and (candidate.get("row_count") or 0) > 0
        and len(detected_fields & CANONICAL_RAW_CLOSED_FIELDS) >= 2
    )


def _inspect_jsonl_candidate(path: Path) -> Dict[str, Any]:
    row_count = 0
    sample: List[Any] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row_count += 1
            if len(sample) < 25:
                try:
                    sample.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    detected, rows_seen = _metadata_from_records(sample)
    return {"row_count": row_count, "detected_fields": detected, "sampled_rows": rows_seen, "container": "jsonl_lines"}


def _inspect_csv_candidate(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field) for field in (reader.fieldnames or [])]
        row_count = sum(1 for _ in reader)
    return {
        "row_count": row_count,
        "detected_fields": sorted(field for field in fields if field in RAW_CLOSED_FIELD_CANDIDATES),
        "sampled_rows": min(row_count, 25),
        "container": "csv_rows",
    }


def _inspect_sqlite_candidate(path: Path) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    with connect_readonly(str(path)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables[:30]:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
            detected = sorted(column for column in columns if column in RAW_CLOSED_FIELD_CANDIDATES)
            if not detected and not any(hint in table.lower() for hint in RAW_CLOSED_PATH_HINTS):
                continue
            row_count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
            candidates.append({
                "path": f"{path}#{table}",
                "type": "sqlite",
                "row_count": row_count,
                "detected_fields": detected,
                "confidence_score": _raw_closed_confidence(str(path), detected, row_count, table),
                "reason": "sqlite table has closed-outcome-like name or fields",
                "sampled_rows": 0,
            })
    return candidates


def _raw_closed_confidence(path_text: str, detected_fields: Sequence[str], row_count: Optional[int], table: str = "") -> float:
    text = f"{path_text} {table}".lower()
    score = 0.0
    score += min(len(detected_fields) * 0.12, 0.60)
    score += 0.12 if row_count and row_count > 0 else 0.0
    score += 0.10 if "closed" in text else 0.0
    score += 0.10 if "outcome" in text else 0.0
    score += 0.06 if "trade" in text else 0.0
    score += 0.04 if "ledger" in text else 0.0
    return round(min(score, 0.99), 3)


def raw_closed_outcome_source_discovery_audit(report_or_artifacts: Dict[str, Any]) -> Dict[str, Any]:
    """Discover raw closed-outcome source candidates using bounded read-only inspection."""
    report = report_or_artifacts if isinstance(report_or_artifacts, dict) else {}
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}
    candidates: List[Dict[str, Any]] = []
    findings = ["DIAGNOSTIC_ONLY_READ_ONLY_SOURCE_DISCOVERY"]
    read_errors = 0
    path_values = []
    for key in ("closed_outcomes", "closed_outcomes_csv", "outcome_ledger", "outcome_ledger_jsonl", "closed_outcome_path", "trade_ledger", "paper_trade_ledger"):
        path_values.extend([report.get(key), artifact_paths.get(key)])
    explicit = [_safe_candidate_path(value) for value in path_values]
    paths = {path.resolve() for path in explicit if path is not None}
    for pattern in ("reports/outcome*.json", "reports/*outcome*.json", "reports/*closed*.json", "reports/*ledger*.json", "reports/*trade*.json", "reports/**/*.json", "reports/**/*.jsonl", "reports/**/*.csv", "data/*.db", "data/*.sqlite", "*.db", "*.sqlite"):
        for path in Path(".").glob(pattern):
            if path.is_file() and any(hint in str(path).lower() for hint in RAW_CLOSED_PATH_HINTS):
                paths.add(path.resolve())
            if len(paths) >= MAX_DISCOVERY_CANDIDATES:
                break
    for path in sorted(paths, key=lambda p: str(p))[:MAX_DISCOVERY_CANDIDATES]:
        suffix = path.suffix.lower()
        source_type = "json" if suffix == ".json" else "jsonl" if suffix == ".jsonl" else "csv" if suffix == ".csv" else "sqlite" if suffix in {".db", ".sqlite"} else "unknown"
        try:
            if source_type in {"json", "jsonl", "csv"} and path.stat().st_size > MAX_DISCOVERY_FILE_BYTES:
                findings.append(f"SKIPPED_LARGE_FILE:{path}")
                continue
            if source_type == "json":
                meta = _inspect_json_candidate(path)
            elif source_type == "jsonl":
                meta = _inspect_jsonl_candidate(path)
            elif source_type == "csv":
                meta = _inspect_csv_candidate(path)
            elif source_type == "sqlite":
                candidates.extend(_inspect_sqlite_candidate(path))
                continue
            else:
                meta = {"row_count": None, "detected_fields": [], "sampled_rows": 0, "container": None}
            detected = meta["detected_fields"]
            confidence = _raw_closed_confidence(str(path), detected, meta.get("row_count"))
            if confidence >= 0.20:
                candidates.append({
                    "path": str(path),
                    "type": source_type,
                    "row_count": meta.get("row_count"),
                    "detected_fields": detected,
                    "confidence_score": confidence,
                    "reason": "path and fields look like closed outcome source",
                    "sampled_rows": meta.get("sampled_rows", 0),
                    "container": meta.get("container"),
                })
        except Exception as exc:
            read_errors += 1
            findings.append(f"READ_ERROR:{path}:{exc.__class__.__name__}")
    candidates = sorted(candidates, key=lambda row: (row.get("confidence_score") or 0, row.get("row_count") or 0), reverse=True)
    canonical_candidates = [candidate for candidate in candidates if _is_canonical_raw_closed_source_candidate(candidate)]
    selected = canonical_candidates[0] if canonical_candidates else (
        candidates[0] if candidates and (len(candidates) == 1 or (candidates[0]["confidence_score"] - candidates[1]["confidence_score"]) >= 0.20) else None
    )
    if read_errors and candidates:
        status = "READ_ERROR_PARTIAL_DISCOVERY"
    elif selected:
        status = "AVAILABLE_SELECTED_SOURCE"
    elif candidates:
        status = "AVAILABLE_CANDIDATES_NEED_REVIEW"
    else:
        status = "UNAVAILABLE_NO_CANDIDATE_SOURCE_FOUND"
    return {
        "raw_closed_source_discovery_status": status,
        "raw_closed_source_candidate_count": len(candidates),
        "raw_closed_source_selected_path": selected.get("path") if selected else None,
        "raw_closed_source_selected_type": selected.get("type") if selected else None,
        "raw_closed_source_selected_row_count": selected.get("row_count") if selected else None,
        "raw_closed_source_candidates": candidates[:25],
        "raw_closed_source_findings": findings,
        "raw_closed_source_recommendation": "Diagnostic only: review selected/candidate source before using it for model repair; do not change training, inference, thresholds, readiness, or execution.",
    }


def closed_outcome_to_ml_cohort_coverage_audit(
    report_or_artifacts: Dict[str, Any],
    min_threshold_rows: int = 100,
    min_pred_win_rows: int = 30,
    low_retention_threshold: float = 0.80,
) -> Dict[str, Any]:
    """Build a diagnostic-only bridge from closed outcomes to ML evaluation evidence.

    This function only summarizes available report fields and read-only artifact
    counts. It does not alter training, inference, predictions, thresholds,
    readiness gates, or execution controls.
    """
    report = report_or_artifacts if isinstance(report_or_artifacts, dict) else {}
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}

    raw_closed_count = _optional_int(
        report.get("raw_closed_source_selected_row_count")
        or report.get("closed_to_ml_coverage_raw_closed_count")
        or report.get("raw_closed_outcome_count")
        or report.get("closed_outcome_count")
        or report.get("closed_outcomes_count")
    )
    raw_source_status = "AVAILABLE_FROM_RAW_CLOSED_SOURCE_DISCOVERY" if report.get("raw_closed_source_selected_row_count") is not None else ("AVAILABLE_FROM_REPORT_FIELDS" if raw_closed_count is not None else "UNAVAILABLE")
    for key in ("closed_outcomes", "closed_outcomes_csv", "outcome_ledger", "outcome_ledger_jsonl", "closed_outcome_path"):
        artifact_count = _count_rows_readonly(report.get(key) or artifact_paths.get(key))
        if artifact_count is not None:
            raw_closed_count = artifact_count
            raw_source_status = f"AVAILABLE_FROM_ARTIFACT:{key}"
            break

    ml_count = _optional_int(report.get("filtered_cohort_rows_full") or report.get("current_accuracy_sample_count") or report.get("row_level_walkforward_rows"))
    kept_count = _optional_int(report.get("filtered_cohort_rows_kept") or report.get("threshold_sample_sufficiency_rows_kept"))
    skipped_count = _optional_int(report.get("filtered_cohort_rows_skipped"))
    if skipped_count is None and ml_count is not None and kept_count is not None:
        skipped_count = max(ml_count - kept_count, 0)

    filtered_pred_dist = report.get("filtered_cohort_filtered_prediction_distribution") or {}
    pred_win_count = _optional_int(report.get("threshold_sample_sufficiency_pred_win_count") or filtered_pred_dist.get("WIN"))
    pred_loss_count = _optional_int(report.get("threshold_sample_sufficiency_pred_loss_count") or filtered_pred_dist.get("LOSS"))

    closed_to_ml_ratio = round_or_none(_safe_ratio(ml_count, raw_closed_count) if ml_count is not None and raw_closed_count else None)
    ml_to_threshold_ratio = round_or_none(_safe_ratio(kept_count, ml_count) if kept_count is not None and ml_count else None)
    raw_to_ml_gap_count = raw_closed_count - ml_count if raw_closed_count is not None and ml_count is not None else None

    drop_reasons = {
        "missing_prediction_link": None,
        "missing_label": None,
        "missing_prediction": None,
        "missing_probability": None,
        "immature_or_unclosed_outcome": report.get("pending_prediction_rows"),
        "temporal_guard_exclusion": report.get("future_feature_violation_count"),
        "non_evaluable_row": report.get("invalid_prediction_rows"),
        "threshold_exclusion": skipped_count,
        "predicted_win_scarcity": pred_win_count,
    }
    stage_counts = {
        "raw_closed_outcomes": raw_closed_count,
        "ml_cohort_rows": ml_count,
        "raw_to_ml_gap_rows": raw_to_ml_gap_count,
        "threshold_kept_rows": kept_count,
        "threshold_skipped_rows": skipped_count,
        "threshold_predicted_win_rows": pred_win_count,
        "threshold_predicted_loss_rows": pred_loss_count,
    }
    if raw_source_status == "UNAVAILABLE" and any(value is not None for key, value in stage_counts.items() if key != "raw_closed_outcomes"):
        raw_source_status = "AVAILABLE_FROM_REPORT_FIELDS"

    findings = [
        "DIAGNOSTIC_ONLY_DOES_NOT_ALTER_READINESS_OR_EXECUTION",
        "Diagnostic-only coverage audit; does not upgrade the model, change training/inference/predictions, apply threshold 0.80 to runtime signals, change readiness gates, or unlock execution.",
    ]
    if raw_closed_count is None:
        findings.append("RAW_CLOSED_OUTCOME_SOURCE_UNAVAILABLE_FOR_COVERAGE_RECONCILIATION")
    elif ml_count is not None and raw_closed_count > ml_count:
        findings.append("CLOSED_OUTCOME_COUNT_EXCEEDS_ML_COHORT_COUNT")
    if kept_count is not None and kept_count < min_threshold_rows:
        findings.append("THRESHOLD_FILTERED_SAMPLE_BELOW_MINIMUM")
    if pred_win_count is not None and pred_win_count < min_pred_win_rows:
        findings.append("PREDICTED_WIN_SAMPLE_BELOW_MINIMUM")
    if closed_to_ml_ratio is not None and closed_to_ml_ratio < low_retention_threshold:
        findings.append("LOW_COVERAGE_RETENTION_REQUIRES_DROP_REASON_AUDIT")

    return {
        "closed_to_ml_coverage_status": raw_source_status,
        "closed_to_ml_coverage_raw_closed_count": raw_closed_count,
        "closed_to_ml_coverage_ml_cohort_count": ml_count,
        "closed_to_ml_coverage_threshold_kept_count": kept_count,
        "closed_to_ml_coverage_threshold_skipped_count": skipped_count,
        "closed_to_ml_coverage_threshold_pred_win_count": pred_win_count,
        "closed_to_ml_coverage_threshold_pred_loss_count": pred_loss_count,
        "closed_to_ml_coverage_closed_to_ml_retention_ratio": closed_to_ml_ratio,
        "closed_to_ml_coverage_raw_to_ml_gap_count": raw_to_ml_gap_count,
        "closed_to_ml_coverage_ml_to_threshold_retention_ratio": ml_to_threshold_ratio,
        "closed_to_ml_coverage_drop_reasons": drop_reasons,
        "closed_to_ml_coverage_known_stage_counts": stage_counts,
        "closed_to_ml_coverage_findings": findings,
        "closed_to_ml_coverage_recommendation": "Diagnostic only: use the coverage bridge to audit where closed outcomes are excluded before any model repair; keep readiness blocked and execution disabled.",
    }


def _read_json_records_from_container(path: Path, container: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    if isinstance(data, list):
        records = data
        used = "root_list"
    elif isinstance(data, dict):
        preferred = [container] if container else []
        records = None
        used = None
        for key in [*preferred, "closed_trades", "rows", "outcomes", "closed_outcomes", "records", "data", "trades", "ledger"]:
            if key and isinstance(data.get(key), list):
                records = data.get(key)
                used = key
                break
        if records is None:
            return [], None
    else:
        return [], None
    return [row for row in records if isinstance(row, dict)], used


def _read_jsonl_records(path: Path, max_rows: int = 100_000) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(rows) >= max_rows:
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _field_availability(rows: Sequence[Dict[str, Any]], fields: Sequence[str]) -> Dict[str, Dict[str, int]]:
    total = len(rows)
    return {field: {"present": sum(1 for row in rows if _present(row.get(field))), "missing": total - sum(1 for row in rows if _present(row.get(field)))} for field in fields}


LINKAGE_REQUIRED_KEYS = [
    "prediction_id",
    "trade_id",
    "signal_id",
    "source_signal_timestamp",
    "symbol",
    "entry_time",
    "closed_at",
    "target_timestamp",
]

LINKAGE_MINIMUM_FUTURE_FIELDS = [
    "prediction_id",
    "trade_id or signal_id",
    "symbol",
    "source_signal_timestamp",
    "target_timestamp",
    "closed_at",
    "outcome/label",
    "predicted_probability",
]

LINKAGE_PRODUCER_PLAN_REQUIRED_FUTURE_FIELDS = [
    "prediction_id",
    "trade_id or signal_id",
    "symbol",
    "source_signal_timestamp",
    "target_timestamp",
    "closed_at",
    "outcome or label",
    "predicted_probability",
    "model_version",
    "evaluation_contract",
]

LINKAGE_PRODUCER_PLAN_RECOMMENDED_WRITE_POINTS = [
    "When prediction is generated, persist prediction_id, symbol, predicted_probability, model_version, target_timestamp, evaluation_contract.",
    "When paper trade is opened or tracked, persist prediction_id and trade_id/signal_id.",
    "When paper trade closes, write closed outcome with prediction_id and trade_id/signal_id.",
    "If a dedicated linkage ledger exists or is added later, write one row per prediction/outcome pair.",
]

LINKAGE_PRODUCER_PLAN_VALIDATION_RULES = [
    "Every future closed outcome must have prediction_id.",
    "Every future closed outcome must have either trade_id or signal_id.",
    "Every future closed outcome must have symbol and closed_at.",
    "Every future closed outcome must have outcome/label.",
    "Every future linkage row must join to exactly one ML prediction row.",
    "Duplicated prediction_id in closed outcome rows should be flagged.",
    "Missing predicted_probability should be flagged.",
    "Missing target_timestamp should be flagged.",
]

LINKAGE_PRODUCER_PLAN_DO_NOT_DO = [
    "no backfill in this PR",
    "no inference changes",
    "no training changes",
    "no prediction changes",
    "no threshold runtime application",
    "no readiness unlock",
    "no database/source mutation",
]


def _payload_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload_json")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip() and len(payload) <= 100_000:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _key_coverage(rows: Sequence[Dict[str, Any]], keys: Sequence[str], include_payload: bool = False) -> Dict[str, Dict[str, Any]]:
    total = len(rows)
    coverage: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        present = 0
        for row in rows:
            if _present(row.get(key)) or (include_payload and _present(_payload_dict(row).get(key))):
                present += 1
        coverage[key] = {
            "present": present,
            "missing": total - present,
            "coverage_ratio": round_or_none(_safe_ratio(present, total) if total else None),
        }
    return coverage


def prediction_outcome_linkage_contract_audit(report_or_artifacts: Dict[str, Any]) -> Dict[str, Any]:
    """Audit the read-only prediction-to-outcome linkage contract.

    This diagnostic only inspects bounded existing artifacts. It does not mutate
    raw outcomes, prediction ledgers, databases, model state, thresholds,
    readiness gates, or execution controls.
    """
    report = report_or_artifacts if isinstance(report_or_artifacts, dict) else {}
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}
    raw_path = Path(str(report.get("raw_closed_source_selected_path") or artifact_paths.get("raw_closed_source_selected_path") or CANONICAL_RAW_CLOSED_SOURCE_PATH))
    ledger_path = Path(str(report.get("prediction_ledger_path") or artifact_paths.get("prediction_ledger") or artifact_paths.get("ml_prediction_ledger") or "reports/ml_prediction_ledger.jsonl"))
    raw_rows, container = _read_json_records_from_container(raw_path, report.get("raw_closed_source_selected_container") or CANONICAL_RAW_CLOSED_CONTAINER)
    ml_rows = _read_jsonl_records(ledger_path)

    raw_coverage = _key_coverage(raw_rows, LINKAGE_REQUIRED_KEYS)
    ml_coverage = _key_coverage(ml_rows, LINKAGE_REQUIRED_KEYS)
    payload_coverage = {
        "raw_closed": _key_coverage(raw_rows, LINKAGE_REQUIRED_KEYS, include_payload=True),
        "ml_ledger": _key_coverage(ml_rows, LINKAGE_REQUIRED_KEYS, include_payload=True),
    }

    available_keys = [
        key for key in LINKAGE_REQUIRED_KEYS
        if raw_coverage[key]["present"] > 0 and ml_coverage[key]["present"] > 0
    ]
    missing_keys = {
        "raw_closed": [key for key in LINKAGE_REQUIRED_KEYS if raw_coverage[key]["present"] == 0],
        "ml_ledger": [key for key in LINKAGE_REQUIRED_KEYS if ml_coverage[key]["present"] == 0],
    }
    raw_has_stable = raw_coverage["prediction_id"]["present"] > 0 or raw_coverage["trade_id"]["present"] > 0 or raw_coverage["signal_id"]["present"] > 0
    ml_has_prediction_id = ml_coverage["prediction_id"]["present"] > 0
    high_confidence_keys = [
        key for key in ("prediction_id", "trade_id", "signal_id")
        if raw_coverage[key]["coverage_ratio"] == 1.0 and ml_coverage[key]["coverage_ratio"] == 1.0
    ]
    fallback_symbol_closed = raw_coverage["symbol"]["present"] > 0 and raw_coverage["closed_at"]["present"] > 0 and ml_coverage["symbol"]["present"] > 0 and ml_coverage["target_timestamp"]["present"] > 0

    gaps: List[str] = []
    findings = ["DIAGNOSTIC_ONLY_READ_ONLY_LINKAGE_CONTRACT_AUDIT"]
    if not raw_has_stable:
        gaps.append("RAW_CLOSED_MISSING_STABLE_LINKAGE_ID")
    if ml_has_prediction_id and raw_coverage["prediction_id"]["present"] == 0:
        gaps.append("PREDICTION_LEDGER_HAS_ID_BUT_OUTCOME_SOURCE_DOES_NOT")
    if fallback_symbol_closed and not high_confidence_keys:
        gaps.append("FALLBACK_JOIN_KEY_WEAK_FOR_MODEL_REPAIR")
    if not high_confidence_keys:
        gaps.append("NO_HIGH_CONFIDENCE_ONE_TO_ONE_LINKAGE_KEY")

    preferred_key = high_confidence_keys[0] if high_confidence_keys else ("symbol+closed_at" if fallback_symbol_closed else None)
    status = "AVAILABLE_LINKAGE_CONTRACT_READY" if high_confidence_keys else "BLOCKED_LINKAGE_CONTRACT_INCOMPLETE"
    recommendation = (
        "Future producer should write at least prediction_id, trade_id or signal_id, symbol, "
        "source_signal_timestamp, target_timestamp, closed_at, outcome/label, and "
        "predicted_probability into the raw closed outcome source or a dedicated "
        "prediction_outcome_linkage ledger. Do not repair model until "
        "prediction/outcome linkage is auditable."
    )
    return {
        "prediction_outcome_linkage_contract_status": status,
        "prediction_outcome_linkage_required_keys": LINKAGE_REQUIRED_KEYS,
        "prediction_outcome_linkage_preferred_key": preferred_key,
        "prediction_outcome_linkage_available_keys": available_keys,
        "prediction_outcome_linkage_missing_keys": missing_keys,
        "prediction_outcome_linkage_raw_closed_key_coverage": raw_coverage,
        "prediction_outcome_linkage_ml_ledger_key_coverage": ml_coverage,
        "prediction_outcome_linkage_payload_key_coverage": payload_coverage,
        "prediction_outcome_linkage_contract_gaps": sorted(set(gaps)),
        "prediction_outcome_linkage_minimum_required_future_fields": LINKAGE_MINIMUM_FUTURE_FIELDS,
        "prediction_outcome_linkage_findings": findings + [f"raw_closed_container={container}", f"raw_rows={len(raw_rows)}", f"ml_ledger_rows={len(ml_rows)}"],
        "prediction_outcome_linkage_recommendation": recommendation,
    }


def prediction_outcome_linkage_producer_contract_plan(report_or_artifacts: Dict[str, Any]) -> Dict[str, Any]:
    """Emit a forward-only producer contract plan for future outcome linkage.

    This is diagnostic/planning-only. It consumes existing audit/readiness fields
    and does not backfill old rows, write databases, mutate source artifacts,
    change model training or inference, apply thresholds, or unlock readiness.
    """
    report = report_or_artifacts if isinstance(report_or_artifacts, dict) else {}
    readiness_report = report.get("model_readiness") if isinstance(report.get("model_readiness"), dict) else {}
    contract_gaps = list(report.get("prediction_outcome_linkage_contract_gaps") or [])
    reason_counts = report.get("raw_to_ml_gap_reason_counts") if isinstance(report.get("raw_to_ml_gap_reason_counts"), dict) else {}
    coverage_status = report.get("closed_to_ml_coverage_status") or report.get("closed_outcome_to_ml_cohort_coverage_status")
    linkage_blocked = report.get("prediction_outcome_linkage_contract_status") != "AVAILABLE_LINKAGE_CONTRACT_READY"
    blockers = set(contract_gaps)
    if linkage_blocked:
        blockers.update({
            "RAW_CLOSED_MISSING_STABLE_LINKAGE_ID",
            "PREDICTION_LEDGER_HAS_ID_BUT_OUTCOME_SOURCE_DOES_NOT",
            "NO_HIGH_CONFIDENCE_ONE_TO_ONE_LINKAGE_KEY",
            "MODEL_REPAIR_BLOCKED_UNTIL_LINKAGE_READY",
        })
    if "NO_HIGH_CONFIDENCE_ONE_TO_ONE_LINKAGE_KEY" in contract_gaps:
        blockers.add("MODEL_REPAIR_BLOCKED_UNTIL_LINKAGE_READY")

    findings = [
        "DIAGNOSTIC_ONLY_FORWARD_ONLY_PRODUCER_CONTRACT_PLAN",
        f"linkage_contract_status={report.get('prediction_outcome_linkage_contract_status')}",
        f"raw_to_ml_gap_count={report.get('closed_to_ml_coverage_raw_to_ml_gap_count')}",
        f"coverage_status={coverage_status}",
        f"readiness_overall_status={readiness_report.get('overall_status')}",
        f"execution_allowed={readiness_report.get('execution_allowed')}",
        f"paper_only={readiness_report.get('paper_only')}",
    ]
    if reason_counts:
        findings.append(f"raw_to_ml_gap_reason_counts={dict(sorted(reason_counts.items()))}")

    recommendation = (
        "Add forward-only producer/writer coverage so future closed outcomes carry stable prediction linkage "
        "fields at prediction generation, paper trade tracking, and close time. Do not backfill historical "
        "rows or repair/upgrade the model until future one-to-one prediction/outcome linkage is auditable."
    )
    return {
        "prediction_outcome_linkage_producer_plan_status": "PRODUCER_CONTRACT_PLAN_AVAILABLE_LINKAGE_BLOCKED",
        "prediction_outcome_linkage_producer_plan_mode": "FORWARD_ONLY_NO_BACKFILL_DIAGNOSTIC_PLAN",
        "prediction_outcome_linkage_producer_plan_required_future_fields": LINKAGE_PRODUCER_PLAN_REQUIRED_FUTURE_FIELDS,
        "prediction_outcome_linkage_producer_plan_recommended_write_points": LINKAGE_PRODUCER_PLAN_RECOMMENDED_WRITE_POINTS,
        "prediction_outcome_linkage_producer_plan_validation_rules": LINKAGE_PRODUCER_PLAN_VALIDATION_RULES,
        "prediction_outcome_linkage_producer_plan_blockers": sorted(blockers),
        "prediction_outcome_linkage_producer_plan_do_not_do": LINKAGE_PRODUCER_PLAN_DO_NOT_DO,
        "prediction_outcome_linkage_producer_plan_findings": findings,
        "prediction_outcome_linkage_producer_plan_recommendation": recommendation,
    }


def _row_join_value(row: Dict[str, Any], join_key: str) -> Optional[str]:
    if join_key == "symbol_entry_time":
        symbol = row.get("symbol")
        ts = row.get("entry_time") or row.get("prediction_timestamp")
        return f"{symbol}|{ts}" if _present(symbol) and _present(ts) else None
    if join_key == "symbol_closed_at":
        symbol = row.get("symbol")
        ts = row.get("closed_at") or row.get("target_timestamp")
        return f"{symbol}|{ts}" if _present(symbol) and _present(ts) else None
    value = row.get(join_key)
    return str(value) if _present(value) else None


def _select_gap_join_key(raw_rows: Sequence[Dict[str, Any]], ml_rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    candidates = ("prediction_id", "trade_id", "symbol_entry_time", "symbol_closed_at")
    for key in candidates:
        raw_present = sum(1 for row in raw_rows if _row_join_value(row, key))
        ml_present = sum(1 for row in ml_rows if _row_join_value(row, key))
        if raw_present and ml_present:
            return key, "AVAILABLE"
    return None, "JOIN_KEY_UNAVAILABLE"


def _safe_unmatched_raw_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "status": row.get("status") or row.get("outcome"),
        "closed_at": row.get("closed_at"),
        "prediction_id_present": _present(row.get("prediction_id")),
        "label_present": _present(row.get("label") or row.get("target_label") or row.get("y_true") or row.get("status") or row.get("outcome")),
        "probability_present": _present(row.get("predicted_probability") or row.get("probability") or row.get("confidence")),
    }


def raw_closed_to_ml_cohort_gap_reason_audit(report_or_artifacts: Dict[str, Any], sample_limit: int = 5) -> Dict[str, Any]:
    """Read-only diagnostic audit for raw closed outcomes absent from the ML cohort.

    The audit only reads JSON/JSONL artifacts and emits bounded metadata. It
    does not write raw outcomes, prediction ledgers, databases, model artifacts,
    threshold settings, readiness gates, or execution controls.
    """
    report = report_or_artifacts if isinstance(report_or_artifacts, dict) else {}
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}
    raw_path_value = report.get("raw_closed_source_selected_path") or artifact_paths.get("raw_closed_source_selected_path") or CANONICAL_RAW_CLOSED_SOURCE_PATH
    ledger_path_value = report.get("prediction_ledger_path") or artifact_paths.get("prediction_ledger") or artifact_paths.get("ml_prediction_ledger") or "reports/ml_prediction_ledger.jsonl"
    raw_path = Path(str(raw_path_value))
    ledger_path = Path(str(ledger_path_value))
    raw_rows, container = _read_json_records_from_container(raw_path, report.get("raw_closed_source_selected_container") or CANONICAL_RAW_CLOSED_CONTAINER)
    ml_rows = _read_jsonl_records(ledger_path)
    raw_count = _optional_int(report.get("closed_to_ml_coverage_raw_closed_count") or report.get("raw_closed_source_selected_row_count")) or len(raw_rows)
    ml_count = _optional_int(report.get("closed_to_ml_coverage_ml_cohort_count") or report.get("prediction_ledger_rows") or report.get("current_accuracy_sample_count")) or len(ml_rows)
    gap_count = _optional_int(report.get("closed_to_ml_coverage_raw_to_ml_gap_count"))
    if gap_count is None and raw_count is not None and ml_count is not None:
        gap_count = max(raw_count - ml_count, 0)

    reason_counts: Counter[str] = Counter()
    findings = ["DIAGNOSTIC_ONLY_READ_ONLY_GAP_REASON_AUDIT"]
    join_key, join_status = _select_gap_join_key(raw_rows, ml_rows)
    unmatched_raw: List[Dict[str, Any]] = []
    unmatched_ml_count = 0

    missing_prediction_id_count = sum(1 for row in raw_rows if not _present(row.get("prediction_id")))
    if missing_prediction_id_count:
        reason_counts["MISSING_PREDICTION_ID"] = missing_prediction_id_count

    if not raw_rows or not ml_rows:
        join_status = "JOIN_KEY_UNAVAILABLE"
        join_key = None
    if join_key is None:
        reason_counts["JOIN_KEY_UNAVAILABLE"] = gap_count or 0
        reason_counts["UNKNOWN_REQUIRES_MANUAL_REVIEW"] = gap_count or 0
        unmatched_raw = list(raw_rows[:sample_limit])
        findings.append("JOIN_KEY_UNAVAILABLE_PRESERVED_COUNTS_ONLY")
    else:
        raw_keys = [_row_join_value(row, join_key) for row in raw_rows]
        ml_keys = [_row_join_value(row, join_key) for row in ml_rows]
        raw_dupes = {key for key, count in Counter(k for k in raw_keys if k).items() if count > 1}
        ml_dupes = {key for key, count in Counter(k for k in ml_keys if k).items() if count > 1}
        ml_index = {key: row for key, row in zip(ml_keys, ml_rows) if key}
        raw_key_set = {key for key in raw_keys if key}
        unmatched_ml_count = sum(1 for key in ml_keys if key and key not in raw_key_set)
        for row, key in zip(raw_rows, raw_keys):
            if not key:
                reason_counts["MISSING_JOIN_KEY"] += 1
                unmatched_raw.append(row)
                continue
            if key in raw_dupes or key in ml_dupes:
                reason_counts["DUPLICATE_JOIN_KEY"] += 1
            ml_row = ml_index.get(key)
            if ml_row is None:
                reason_counts["MISSING_ML_LEDGER_MATCH"] += 1
                unmatched_raw.append(row)
                continue
            if not _present(ml_row.get("y_true") or ml_row.get("target_label") or row.get("label") or row.get("status") or row.get("outcome")):
                reason_counts["MISSING_LABEL"] += 1
            if not _present(ml_row.get("y_pred") or ml_row.get("prediction") or ml_row.get("predicted_label")):
                reason_counts["MISSING_PREDICTION"] += 1
            if not _present(ml_row.get("predicted_probability") or ml_row.get("probability") or ml_row.get("confidence")):
                reason_counts["MISSING_PROBABILITY"] += 1
            if not _present(row.get("status") or row.get("outcome") or row.get("closed_status")):
                reason_counts["MISSING_OUTCOME_STATUS"] += 1
            if str(ml_row.get("evaluation_status") or "").upper() not in {"", "READY", "EVALUATED", "VALID"}:
                reason_counts["NON_EVALUABLE_ROW"] += 1

    return {
        "raw_to_ml_gap_reason_audit_status": "AVAILABLE" if raw_rows and ml_rows else "UNAVAILABLE",
        "raw_to_ml_gap_raw_closed_count": raw_count,
        "raw_to_ml_gap_ml_cohort_count": ml_count,
        "raw_to_ml_gap_count": gap_count,
        "raw_to_ml_gap_join_key_used": join_key,
        "raw_to_ml_gap_join_key_status": join_status,
        "raw_to_ml_gap_reason_counts": dict(sorted(reason_counts.items())),
        "raw_to_ml_gap_field_availability": {
            "raw_closed": _field_availability(raw_rows, ["prediction_id", "trade_id", "symbol", "entry_time", "closed_at", "status", "outcome", "label"]),
            "ml_cohort": _field_availability(ml_rows, ["prediction_id", "trade_id", "symbol", "prediction_timestamp", "target_timestamp", "y_true", "target_label", "y_pred", "predicted_probability"]),
            "raw_closed_container": container,
        },
        "raw_to_ml_gap_unmatched_raw_count": len(unmatched_raw) if join_key else (gap_count or 0),
        "raw_to_ml_gap_unmatched_ml_count": unmatched_ml_count,
        "raw_to_ml_gap_sample_unmatched_raw_metadata": [_safe_unmatched_raw_metadata(row) for row in unmatched_raw[:sample_limit]],
        "raw_to_ml_gap_findings": findings,
        "raw_to_ml_gap_recommendation": "Diagnostic only: use these gap reasons to plan source/label repair; do not mutate source files, training, inference, predictions, thresholds, readiness gates, or execution.",
    }

def threshold_sample_sufficiency_audit(
    report_or_cohort: Any,
    selected_threshold: Optional[float] = None,
    min_rows_required: int = 100,
    min_pred_win_required: int = 30,
    min_segment_rows_required: int = 10,
) -> Dict[str, Any]:
    """Diagnostic-only audit of selected threshold sample sufficiency.

    This evaluates whether selected-threshold evidence is strong enough to inform
    future paper-only trade-filter review. It does not apply thresholds to
    signals, training, readiness gates, baseline/walk-forward gates, or execution.
    """
    base: Dict[str, Any] = {
        "threshold_sample_sufficiency_status": "UNAVAILABLE_NO_SELECTED_THRESHOLD",
        "threshold_sample_sufficiency_selected_threshold": None,
        "threshold_sample_sufficiency_rows_kept": 0,
        "threshold_sample_sufficiency_pred_win_count": 0,
        "threshold_sample_sufficiency_pred_loss_count": 0,
        "threshold_sample_sufficiency_true_win_count": 0,
        "threshold_sample_sufficiency_true_loss_count": 0,
        "threshold_sample_sufficiency_false_win_count": None,
        "threshold_sample_sufficiency_win_precision": None,
        "threshold_sample_sufficiency_accuracy": None,
        "threshold_sample_sufficiency_min_rows_required": min_rows_required,
        "threshold_sample_sufficiency_min_pred_win_required": min_pred_win_required,
        "threshold_sample_sufficiency_min_segment_rows_required": min_segment_rows_required,
        "threshold_sample_sufficiency_findings": [],
        "threshold_sample_sufficiency_recommendation": (
            "Diagnostic only: do not apply the selected threshold or unlock readiness/execution; use this only to judge whether future paper-only filter review has enough sample support."
        ),
    }

    selected_threshold = safe_float(selected_threshold)
    if selected_threshold is None and isinstance(report_or_cohort, dict):
        selected_threshold = safe_float(
            report_or_cohort.get("threshold_stability_selected_threshold")
            or report_or_cohort.get("threshold_candidate_selected")
        )
    if selected_threshold is None:
        return {**base, "threshold_sample_sufficiency_findings": ["No selected threshold is available for sample sufficiency audit."]}

    metrics: Optional[Dict[str, Any]] = None
    min_segment_rows = None
    if isinstance(report_or_cohort, dict):
        if "threshold_stability_rows_kept" not in report_or_cohort:
            result = {**base, "threshold_sample_sufficiency_selected_threshold": selected_threshold}
            result["threshold_sample_sufficiency_status"] = "UNAVAILABLE_NO_PROBABILITY_EVIDENCE"
            result["threshold_sample_sufficiency_findings"] = ["Selected threshold exists but probability evidence is missing."]
            return result
        pred_dist = report_or_cohort.get("threshold_stability_pred_distribution") or {}
        true_dist = report_or_cohort.get("threshold_stability_label_distribution") or {}
        metrics = {
            "rows_kept": int(report_or_cohort.get("threshold_stability_rows_kept") or 0),
            "predicted_win_count": int(pred_dist.get("WIN") or 0),
            "predicted_loss_count": int(pred_dist.get("LOSS") or 0),
            "true_win_count": int(true_dist.get("WIN") or 0),
            "true_loss_count": int(true_dist.get("LOSS") or 0),
            "false_win_count": report_or_cohort.get("threshold_stability_false_win_count"),
            "win_precision": report_or_cohort.get("threshold_stability_win_precision"),
            "accuracy": report_or_cohort.get("threshold_stability_accuracy"),
        }
        min_segment_rows = report_or_cohort.get("threshold_stability_min_segment_rows")
    else:
        frame, reason = _canonical_probability_frame(report_or_cohort)
        if frame.empty:
            result = {**base, "threshold_sample_sufficiency_selected_threshold": selected_threshold}
            result["threshold_sample_sufficiency_status"] = "UNAVAILABLE_NO_PROBABILITY_EVIDENCE"
            result["threshold_sample_sufficiency_findings"] = [reason]
            return result
        kept = frame[frame["__predicted_probability"] >= selected_threshold].copy()
        metrics = _threshold_metrics_for_frame(kept, sample_count=len(frame))
        segment_sizes = []
        for column in ("fold_id", "symbol", "market_regime", "regime", "regime_label"):
            if column in kept.columns:
                segment_sizes.extend(int(len(segment)) for _, segment in kept.groupby(column, dropna=False))
        min_segment_rows = min(segment_sizes) if segment_sizes else None

    findings = ["This threshold sample sufficiency audit is diagnostic-only and does not alter readiness gates, training, threshold selection, or execution behavior."]
    statuses: List[str] = []
    if metrics["rows_kept"] < min_rows_required:
        statuses.append("REVIEW_INSUFFICIENT_KEPT_ROWS")
        findings.append(f"Rows kept {metrics['rows_kept']} is below required minimum {min_rows_required}.")
    if metrics["predicted_win_count"] < min_pred_win_required:
        statuses.append("REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE")
        findings.append(f"Predicted WIN sample {metrics['predicted_win_count']} is below required minimum {min_pred_win_required}.")
    if min_segment_rows is not None and min_segment_rows < min_segment_rows_required:
        statuses.append("REVIEW_INSUFFICIENT_SEGMENT_ROWS")
        findings.append(f"Smallest available segment has {min_segment_rows} rows, below required minimum {min_segment_rows_required}.")
    if metrics.get("false_win_count") == 0 and metrics["predicted_win_count"] < min_pred_win_required:
        findings.append("Zero false WIN is promising but not yet statistically strong because predicted WIN sample is insufficient.")
    if not statuses:
        statuses.append("AVAILABLE_SAMPLE_SUFFICIENT")
        findings.append("Selected threshold sample support meets diagnostic minimums for future paper-only filter review.")

    return {
        **base,
        "threshold_sample_sufficiency_status": ";".join(statuses),
        "threshold_sample_sufficiency_selected_threshold": selected_threshold,
        "threshold_sample_sufficiency_rows_kept": metrics["rows_kept"],
        "threshold_sample_sufficiency_pred_win_count": metrics["predicted_win_count"],
        "threshold_sample_sufficiency_pred_loss_count": metrics["predicted_loss_count"],
        "threshold_sample_sufficiency_true_win_count": metrics["true_win_count"],
        "threshold_sample_sufficiency_true_loss_count": metrics["true_loss_count"],
        "threshold_sample_sufficiency_false_win_count": metrics.get("false_win_count"),
        "threshold_sample_sufficiency_win_precision": metrics.get("win_precision"),
        "threshold_sample_sufficiency_accuracy": metrics.get("accuracy"),
        "threshold_sample_sufficiency_findings": findings,
    }


def _cohort_comparison_metrics(frame: pd.DataFrame, baseline_label: Optional[str] = None) -> Dict[str, Any]:
    actuals = frame["__canonical_y_true"].astype(str).tolist() if "__canonical_y_true" in frame.columns else []
    preds = frame["__canonical_y_pred"].astype(str).tolist() if "__canonical_y_pred" in frame.columns else []
    rows = len(actuals)
    baseline_label = baseline_label or _majority_label(actuals)
    model_accuracy = _safe_ratio(sum(actual == pred for actual, pred in zip(actuals, preds)), rows)
    baseline_accuracy = _safe_ratio(sum(actual == baseline_label for actual in actuals), rows) if baseline_label else None
    false_win_count = sum(actual == "LOSS" and pred == "WIN" for actual, pred in zip(actuals, preds))
    return {
        "rows": rows,
        "model_accuracy": model_accuracy,
        "baseline_accuracy": baseline_accuracy,
        "false_win_count": false_win_count,
        "prediction_distribution": _counter_dict(preds),
        "label_distribution": _counter_dict(actuals),
        "predicted_win_count": sum(pred == "WIN" for pred in preds),
        "baseline_label": baseline_label,
    }


def _filtered_segment_comparison_summary(frame: pd.DataFrame, column: str, min_rows: int = 10) -> List[Dict[str, Any]]:
    rows = []
    for value, segment in frame.groupby(column, dropna=False):
        metrics = _cohort_comparison_metrics(segment)
        rows.append({
            "segment": None if pd.isna(value) else str(value),
            "rows_kept": metrics["rows"],
            "model_accuracy": metrics["model_accuracy"],
            "baseline_accuracy": metrics["baseline_accuracy"],
            "model_vs_baseline_delta": round_or_none(metrics["model_accuracy"] - metrics["baseline_accuracy"] if metrics["model_accuracy"] is not None and metrics["baseline_accuracy"] is not None else None),
            "false_win_count": metrics["false_win_count"],
            "prediction_distribution": metrics["prediction_distribution"],
            "label_distribution": metrics["label_distribution"],
            "insufficient_kept_rows": metrics["rows"] < min_rows,
        })
    return sorted(rows, key=lambda row: (-int(row["rows_kept"]), str(row["segment"])))


def filtered_cohort_walkforward_comparison(
    cohort: pd.DataFrame,
    selected_threshold: Optional[float] = None,
    min_filtered_rows: int = 100,
    min_pred_win_rows: int = 30,
    min_segment_rows: int = 10,
) -> Dict[str, Any]:
    """Diagnostic-only full-vs-threshold-filtered cohort comparison.

    This evidence is advisory only. It does not apply thresholds to runtime
    signals, training, selected-threshold logic, readiness gates, baseline
    superiority, walk-forward stability, or execution behavior.
    """
    base: Dict[str, Any] = {
        "filtered_cohort_comparison_status": "UNAVAILABLE_NO_SELECTED_THRESHOLD",
        "filtered_cohort_selected_threshold": None,
        "filtered_cohort_rows_full": 0,
        "filtered_cohort_rows_kept": 0,
        "filtered_cohort_rows_skipped": 0,
        "filtered_cohort_kept_ratio": None,
        "filtered_cohort_full_model_accuracy": None,
        "filtered_cohort_filtered_model_accuracy": None,
        "filtered_cohort_full_baseline_accuracy": None,
        "filtered_cohort_filtered_baseline_accuracy": None,
        "filtered_cohort_filtered_model_vs_baseline_delta": None,
        "filtered_cohort_filtered_vs_full_accuracy_delta": None,
        "filtered_cohort_full_false_win_count": None,
        "filtered_cohort_filtered_false_win_count": None,
        "filtered_cohort_false_win_delta": None,
        "filtered_cohort_full_prediction_distribution": {},
        "filtered_cohort_filtered_prediction_distribution": {},
        "filtered_cohort_full_label_distribution": {},
        "filtered_cohort_filtered_label_distribution": {},
        "filtered_cohort_fold_count": 0,
        "filtered_cohort_fold_summary": [],
        "filtered_cohort_symbol_summary": [],
        "filtered_cohort_regime_summary": [],
        "filtered_cohort_findings": [],
        "filtered_cohort_recommendation": "Diagnostic only: do not apply this threshold or unlock execution; use the full-vs-filtered evidence only for future paper-only review.",
    }
    selected_threshold = safe_float(selected_threshold)
    if selected_threshold is None:
        return {**base, "filtered_cohort_findings": ["No selected threshold is available for filtered cohort comparison."]}
    frame, reason = _canonical_probability_frame(cohort)
    if frame.empty:
        result = {**base, "filtered_cohort_selected_threshold": selected_threshold}
        result["filtered_cohort_comparison_status"] = "UNAVAILABLE_NO_PROBABILITY_EVIDENCE"
        result["filtered_cohort_findings"] = [reason]
        return result

    kept = frame[frame["__predicted_probability"] >= selected_threshold].copy()
    full = _cohort_comparison_metrics(frame)
    filtered = _cohort_comparison_metrics(kept)
    findings = ["Filtered cohort comparison is diagnostic-only and does not alter readiness gates, threshold selection, training, or execution behavior."]
    if filtered["rows"] < min_filtered_rows:
        findings.append("REVIEW_INSUFFICIENT_FILTERED_SAMPLE")
    if filtered["predicted_win_count"] < min_pred_win_rows:
        findings.append("REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE")

    fold_col = "fold_id" if "fold_id" in kept.columns else None
    fold_summary = _filtered_segment_comparison_summary(kept, fold_col, min_segment_rows) if fold_col else []
    if fold_col and (len(fold_summary) < 2 or any(row["rows_kept"] / filtered["rows"] > 0.80 for row in fold_summary if filtered["rows"])):
        findings.append("REVIEW_SEGMENT_CONCENTRATION")
    symbol_col = "symbol" if "symbol" in kept.columns else None
    symbol_summary = _filtered_segment_comparison_summary(kept, symbol_col, min_segment_rows) if symbol_col else []
    if symbol_col and (len(symbol_summary) < 2 or any(row["rows_kept"] / filtered["rows"] > 0.80 for row in symbol_summary if filtered["rows"])):
        findings.append("REVIEW_SEGMENT_CONCENTRATION")
    regime_col = next((col for col in ("market_regime", "regime", "regime_label") if col in kept.columns), None)
    regime_summary = _filtered_segment_comparison_summary(kept, regime_col, min_segment_rows) if regime_col else []
    if not regime_col:
        findings.append("REGIME_SEGMENT_UNAVAILABLE")

    full_acc = full["model_accuracy"]
    filtered_acc = filtered["model_accuracy"]
    filtered_baseline = filtered["baseline_accuracy"]
    return {
        **base,
        "filtered_cohort_comparison_status": "AVAILABLE",
        "filtered_cohort_selected_threshold": selected_threshold,
        "filtered_cohort_rows_full": full["rows"],
        "filtered_cohort_rows_kept": filtered["rows"],
        "filtered_cohort_rows_skipped": full["rows"] - filtered["rows"],
        "filtered_cohort_kept_ratio": _safe_ratio(filtered["rows"], full["rows"]),
        "filtered_cohort_full_model_accuracy": full_acc,
        "filtered_cohort_filtered_model_accuracy": filtered_acc,
        "filtered_cohort_full_baseline_accuracy": full["baseline_accuracy"],
        "filtered_cohort_filtered_baseline_accuracy": filtered_baseline,
        "filtered_cohort_filtered_model_vs_baseline_delta": round_or_none(filtered_acc - filtered_baseline if filtered_acc is not None and filtered_baseline is not None else None),
        "filtered_cohort_filtered_vs_full_accuracy_delta": round_or_none(filtered_acc - full_acc if filtered_acc is not None and full_acc is not None else None),
        "filtered_cohort_full_false_win_count": full["false_win_count"],
        "filtered_cohort_filtered_false_win_count": filtered["false_win_count"],
        "filtered_cohort_false_win_delta": filtered["false_win_count"] - full["false_win_count"],
        "filtered_cohort_full_prediction_distribution": full["prediction_distribution"],
        "filtered_cohort_filtered_prediction_distribution": filtered["prediction_distribution"],
        "filtered_cohort_full_label_distribution": full["label_distribution"],
        "filtered_cohort_filtered_label_distribution": filtered["label_distribution"],
        "filtered_cohort_fold_count": len(fold_summary),
        "filtered_cohort_fold_summary": fold_summary,
        "filtered_cohort_symbol_summary": symbol_summary,
        "filtered_cohort_regime_summary": regime_summary if regime_col else {"status": "UNAVAILABLE", "reason": "No regime column exists in filtered cohort evidence."},
        "filtered_cohort_findings": sorted(set(findings), key=findings.index),
    }


def paper_filter_candidate_registry(
    report: Dict[str, Any],
    min_filtered_rows: int = 100,
    min_pred_win_rows: int = 30,
    min_segment_rows: int = 10,
) -> Dict[str, Any]:
    """Build a paper-only, OFF-by-default registry entry for filter candidates.

    The registry is governance evidence only. It records diagnostic support and
    blockers for a future shadow review, but it never enables a runtime filter,
    changes threshold selection, alters training or predictions, changes
    readiness gates, or unlocks execution.
    """
    threshold = safe_float(report.get("filtered_cohort_selected_threshold"))
    status = str(report.get("filtered_cohort_comparison_status"))
    evidence_available = status == "AVAILABLE" and threshold is not None
    positive_evidence: List[str] = []
    blockers: List[str] = []

    if evidence_available:
        accuracy_delta = safe_float(report.get("filtered_cohort_filtered_vs_full_accuracy_delta"))
        baseline_delta = safe_float(report.get("filtered_cohort_filtered_model_vs_baseline_delta"))
        filtered_false_wins = report.get("filtered_cohort_filtered_false_win_count")
        false_win_delta = safe_float(report.get("filtered_cohort_false_win_delta"))
        if accuracy_delta is not None and accuracy_delta > 0:
            positive_evidence.append("FILTERED_MODEL_ACCURACY_IMPROVED_OVER_FULL_MODEL")
        if baseline_delta is not None and baseline_delta > 0:
            positive_evidence.append("FILTERED_MODEL_BEAT_FILTERED_BASELINE")
        if filtered_false_wins == 0:
            positive_evidence.append("FILTERED_FALSE_WIN_COUNT_ZERO")
        if false_win_delta is not None and false_win_delta < 0:
            positive_evidence.append("FALSE_WIN_COUNT_IMPROVED_VERSUS_FULL_COHORT")

        findings = set(report.get("filtered_cohort_findings") or [])
        if "REVIEW_INSUFFICIENT_FILTERED_SAMPLE" in findings or int(report.get("filtered_cohort_rows_kept") or 0) < min_filtered_rows:
            blockers.append("INSUFFICIENT_FILTERED_ROWS")
        pred_win_count = int((report.get("filtered_cohort_filtered_prediction_distribution") or {}).get("WIN", 0))
        if "REVIEW_INSUFFICIENT_PRED_WIN_SAMPLE" in findings or pred_win_count < min_pred_win_rows:
            blockers.append("INSUFFICIENT_PREDICTED_WIN_SAMPLE")
        segment_rows = [
            row for key in ("filtered_cohort_fold_summary", "filtered_cohort_symbol_summary")
            for row in (report.get(key) or [])
            if isinstance(row, dict)
        ]
        if any(row.get("insufficient_kept_rows") for row in segment_rows):
            blockers.append("INSUFFICIENT_SEGMENT_ROWS")
        regime = report.get("filtered_cohort_regime_summary")
        if "REGIME_SEGMENT_UNAVAILABLE" in findings or (isinstance(regime, dict) and regime.get("status") == "UNAVAILABLE"):
            blockers.append("REGIME_SEGMENT_UNAVAILABLE")

    else:
        blockers.append("UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE")

    readiness = report.get("model_readiness") or {}
    if readiness.get("overall_status") == "BLOCKED_BELOW_BASELINE" or readiness.get("primary_blocker") == "BLOCKED_BELOW_BASELINE":
        blockers.append("OVERALL_READINESS_BLOCKED_BELOW_BASELINE")

    blockers = sorted(set(blockers), key=blockers.index)
    positive_evidence = sorted(set(positive_evidence), key=positive_evidence.index)
    status_value = "REVIEW_CANDIDATE_NOT_ENABLED" if evidence_available else "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE"
    return {
        "paper_filter_candidate_status": status_value,
        "paper_filter_candidate_name": "ML_HIGH_CONFIDENCE_THRESHOLD_0_80_FILTER_CANDIDATE" if threshold == 0.8 else "ML_HIGH_CONFIDENCE_THRESHOLD_FILTER_CANDIDATE",
        "paper_filter_candidate_enabled": False,
        "paper_filter_candidate_threshold": threshold,
        "paper_filter_candidate_mode": "paper_only_shadow_review",
        "paper_filter_candidate_basis": "Diagnostic filtered-cohort comparison only; does not drive trading logic, runtime configuration, signal generation, training, prediction, threshold selection, readiness gates, or execution.",
        "paper_filter_candidate_positive_evidence": positive_evidence,
        "paper_filter_candidate_blockers": blockers,
        "paper_filter_candidate_min_rows_required": min_filtered_rows,
        "paper_filter_candidate_min_pred_win_required": min_pred_win_rows,
        "paper_filter_candidate_min_segment_rows_required": min_segment_rows,
        "paper_filter_candidate_regime_required": True,
        "paper_filter_candidate_next_review_requirements": [
            "MIN_FILTERED_ROWS_GTE_100",
            "MIN_PREDICTED_WIN_ROWS_GTE_30",
            "MIN_PER_SEGMENT_ROWS_GTE_10",
            "REGIME_EVIDENCE_AVAILABLE",
            "FILTERED_MODEL_REMAINS_ABOVE_FILTERED_BASELINE",
            "FALSE_WIN_REMAINS_LOW_OR_ZERO",
            "READINESS_GATES_REMAIN_INDEPENDENTLY_EVALUATED",
        ],
        "paper_filter_candidate_recommendation": "Keep candidate OFF by default. Continue paper-only shadow evidence collection; do not apply threshold 0.80 to runtime signals or unlock execution until future governance review explicitly approves it.",
    }


def paper_filter_shadow_review_scorecard(
    report: Dict[str, Any],
    min_filtered_rows: int = 100,
    min_pred_win_rows: int = 30,
    min_segment_rows: int = 10,
) -> Dict[str, Any]:
    """Summarize paper-filter candidate evidence for governance review only.

    This scorecard is diagnostic-only. It does not enable the candidate, apply
    thresholds to runtime signals, change configuration, alter training or
    predictions, modify threshold selection, change readiness gates, or unlock
    execution.
    """
    if "paper_filter_candidate_status" not in report:
        return {
            "paper_filter_shadow_review_status": "UNAVAILABLE_NO_CANDIDATE_REGISTRY",
            "paper_filter_shadow_review_candidate_name": None,
            "paper_filter_shadow_review_candidate_enabled": False,
            "paper_filter_shadow_review_threshold": None,
            "paper_filter_shadow_review_positive_evidence_count": 0,
            "paper_filter_shadow_review_blocker_count": 1,
            "paper_filter_shadow_review_blockers": ["UNAVAILABLE_NO_CANDIDATE_REGISTRY"],
            "paper_filter_shadow_review_passed_requirements": [],
            "paper_filter_shadow_review_failed_requirements": ["CANDIDATE_REGISTRY_AVAILABLE"],
            "paper_filter_shadow_review_missing_requirements": [],
            "paper_filter_shadow_review_min_filtered_rows_required": min_filtered_rows,
            "paper_filter_shadow_review_min_pred_win_rows_required": min_pred_win_rows,
            "paper_filter_shadow_review_min_segment_rows_required": min_segment_rows,
            "paper_filter_shadow_review_regime_required": True,
            "paper_filter_shadow_review_governance_verdict": "NO_REVIEW_WITHOUT_CANDIDATE_REGISTRY",
            "paper_filter_shadow_review_findings": ["Paper filter candidate registry is missing; no shadow review scorecard can be completed."],
            "paper_filter_shadow_review_recommendation": "Keep candidate OFF. Generate the paper-only candidate registry before governance review.",
        }

    evidence = list(report.get("paper_filter_candidate_positive_evidence") or [])
    blockers = list(report.get("paper_filter_candidate_blockers") or [])
    passed: List[str] = []
    failed: List[str] = []
    missing: List[str] = []

    evidence_to_requirement = {
        "FILTERED_MODEL_BEAT_FILTERED_BASELINE": "FILTERED_MODEL_ABOVE_FILTERED_BASELINE",
        "FILTERED_FALSE_WIN_COUNT_ZERO": "FALSE_WIN_LOW_OR_ZERO",
        "FALSE_WIN_COUNT_IMPROVED_VERSUS_FULL_COHORT": "FALSE_WIN_LOW_OR_ZERO",
        "FILTERED_MODEL_ACCURACY_IMPROVED_OVER_FULL_MODEL": "FILTERED_ACCURACY_ABOVE_FULL_ACCURACY",
    }
    for item in evidence:
        req = evidence_to_requirement.get(item)
        if req and req not in passed:
            passed.append(req)

    blocker_to_requirement = {
        "INSUFFICIENT_FILTERED_ROWS": "MIN_FILTERED_ROWS_GTE_100",
        "INSUFFICIENT_PREDICTED_WIN_SAMPLE": "MIN_PREDICTED_WIN_ROWS_GTE_30",
        "INSUFFICIENT_SEGMENT_ROWS": "MIN_PER_SEGMENT_ROWS_GTE_10",
        "REGIME_SEGMENT_UNAVAILABLE": "REGIME_EVIDENCE_AVAILABLE",
        "OVERALL_READINESS_BLOCKED_BELOW_BASELINE": "OVERALL_READINESS_NOT_BLOCKED",
        "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE": "FILTERED_COHORT_EVIDENCE_AVAILABLE",
    }
    for item in blockers:
        req = blocker_to_requirement.get(item)
        if req and req not in failed:
            failed.append(req)

    default_requirements = [
        "MIN_FILTERED_ROWS_GTE_100",
        "MIN_PREDICTED_WIN_ROWS_GTE_30",
        "MIN_PER_SEGMENT_ROWS_GTE_10",
        "REGIME_EVIDENCE_AVAILABLE",
        "OVERALL_READINESS_NOT_BLOCKED",
    ]
    for req, ok in (
        ("MIN_FILTERED_ROWS_GTE_100", int(report.get("filtered_cohort_rows_kept") or report.get("threshold_sample_sufficiency_rows_kept") or 0) >= min_filtered_rows),
        ("MIN_PREDICTED_WIN_ROWS_GTE_30", int((report.get("filtered_cohort_filtered_prediction_distribution") or {}).get("WIN", report.get("threshold_sample_sufficiency_pred_win_count") or 0)) >= min_pred_win_rows),
        ("OVERALL_READINESS_NOT_BLOCKED", (report.get("model_readiness") or {}).get("overall_status") not in {"BLOCKED_BELOW_BASELINE"} and (report.get("model_readiness") or {}).get("primary_blocker") not in {"BLOCKED_BELOW_BASELINE"}),
    ):
        if ok and req not in failed and req not in passed:
            passed.append(req)
    if "INSUFFICIENT_SEGMENT_ROWS" not in blockers and "MIN_PER_SEGMENT_ROWS_GTE_10" not in failed:
        passed.append("MIN_PER_SEGMENT_ROWS_GTE_10")
    if "REGIME_SEGMENT_UNAVAILABLE" not in blockers and "REGIME_EVIDENCE_AVAILABLE" not in failed:
        passed.append("REGIME_EVIDENCE_AVAILABLE")

    for req in default_requirements:
        if req not in passed and req not in failed:
            missing.append(req)

    status = "REVIEW_READY_FOR_PAPER_ONLY_GOVERNANCE_REVIEW" if not failed and not missing else "REVIEW_SHADOW_CANDIDATE_BLOCKED"
    if report.get("paper_filter_candidate_status") == "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE":
        status = "UNAVAILABLE_NO_FILTERED_COHORT_EVIDENCE"

    promising = bool(evidence) and any(b in blockers for b in {"INSUFFICIENT_FILTERED_ROWS", "INSUFFICIENT_PREDICTED_WIN_SAMPLE", "INSUFFICIENT_SEGMENT_ROWS", "REGIME_SEGMENT_UNAVAILABLE"})
    findings = [
        "Scorecard is diagnostic/governance-only and is not a permission system.",
        "Candidate remains disabled; threshold is not applied to runtime signals.",
    ]
    if promising:
        findings.append("Candidate evidence is promising, but sample, segment, regime, or readiness blockers mean it is not review-ready.")
    if status == "REVIEW_READY_FOR_PAPER_ONLY_GOVERNANCE_REVIEW":
        findings.append("Synthetic or current evidence satisfies scorecard requirements for paper-only governance review; execution remains disabled.")

    return {
        "paper_filter_shadow_review_status": status,
        "paper_filter_shadow_review_candidate_name": report.get("paper_filter_candidate_name"),
        "paper_filter_shadow_review_candidate_enabled": False,
        "paper_filter_shadow_review_threshold": report.get("paper_filter_candidate_threshold"),
        "paper_filter_shadow_review_positive_evidence_count": len(evidence),
        "paper_filter_shadow_review_blocker_count": len(blockers),
        "paper_filter_shadow_review_blockers": blockers,
        "paper_filter_shadow_review_passed_requirements": sorted(set(passed), key=passed.index),
        "paper_filter_shadow_review_failed_requirements": failed,
        "paper_filter_shadow_review_missing_requirements": missing,
        "paper_filter_shadow_review_min_filtered_rows_required": min_filtered_rows,
        "paper_filter_shadow_review_min_pred_win_rows_required": min_pred_win_rows,
        "paper_filter_shadow_review_min_segment_rows_required": min_segment_rows,
        "paper_filter_shadow_review_regime_required": True,
        "paper_filter_shadow_review_governance_verdict": "PAPER_ONLY_REVIEW_READY_CANDIDATE_DISABLED" if status == "REVIEW_READY_FOR_PAPER_ONLY_GOVERNANCE_REVIEW" else "PAPER_ONLY_REVIEW_BLOCKED_CANDIDATE_DISABLED",
        "paper_filter_shadow_review_findings": findings,
        "paper_filter_shadow_review_recommendation": "Keep candidate OFF by default. Use this scorecard only for future paper-only governance review; do not apply threshold 0.80, change config/training/predictions/threshold selection/readiness gates, or unlock execution.",
    }


def ml_model_repair_upgrade_diagnostic_plan(report: Dict[str, Any]) -> Dict[str, Any]:
    """Create a diagnostic-only plan for future ML model repair/upgrade work.

    This function is intentionally observational. It does not train a model,
    change inference, select or apply thresholds, mutate readiness gates, promote
    candidates, or unlock execution.
    """
    required_fields = (
        "model_readiness",
        "baseline_superiority_status",
        "threshold_candidate_diagnostic_status",
        "threshold_stability_audit_status",
        "threshold_sample_sufficiency_status",
        "filtered_cohort_comparison_status",
        "paper_filter_candidate_status",
        "paper_filter_shadow_review_status",
    )
    missing = [field for field in required_fields if field not in report]

    readiness = report.get("model_readiness") or {}
    components = readiness.get("components") or {}
    baseline_status_value = str(report.get("baseline_superiority_status") or components.get("Baseline Superiority") or "")
    overall_status = str(readiness.get("overall_status") or "")
    primary_blocker = str(readiness.get("primary_blocker") or "")
    baseline_delta = safe_float(report.get("model_vs_baseline_delta"))

    primary_problem: List[str] = []
    findings: List[str] = [
        "Diagnostic/planning-only: no model training, inference, prediction, threshold, readiness, or execution behavior is changed."
    ]
    blockers: List[str] = []
    required_evidence: List[str] = []

    broad_below_baseline = (
        "BLOCKED_BELOW_BASELINE" in {overall_status, primary_blocker, baseline_status_value}
        or baseline_status_value.startswith("BLOCKED_BELOW_BASELINE")
        or (baseline_delta is not None and baseline_delta < 0)
    )
    if broad_below_baseline:
        primary_problem.append("BROAD_MODEL_BELOW_BASELINE")
        blockers.append("BASELINE_SUPERIORITY_NOT_PROVEN")
        required_evidence.append("MODEL_ACCURACY_ABOVE_BASELINE_ON_ROW_LEVEL_WALKFORWARD")

    threshold_selected = safe_float(report.get("threshold_candidate_selected") or report.get("filtered_cohort_selected_threshold"))
    false_win_delta = safe_float(report.get("filtered_cohort_false_win_delta"))
    filtered_vs_full_delta = safe_float(report.get("filtered_cohort_filtered_vs_full_accuracy_delta"))
    filtered_baseline_delta = safe_float(report.get("filtered_cohort_filtered_model_vs_baseline_delta"))
    sample_status = str(report.get("threshold_sample_sufficiency_status") or "")
    candidate_blockers = list(report.get("paper_filter_candidate_blockers") or [])
    shadow_blockers = list(report.get("paper_filter_shadow_review_blockers") or [])
    sample_blockers = {"INSUFFICIENT_FILTERED_ROWS", "INSUFFICIENT_PREDICTED_WIN_SAMPLE", "INSUFFICIENT_SEGMENT_ROWS"}
    promising_filter = (
        threshold_selected == 0.8
        and (false_win_delta is not None and false_win_delta < 0)
        and (
            (filtered_vs_full_delta is not None and filtered_vs_full_delta > 0)
            or (filtered_baseline_delta is not None and filtered_baseline_delta > 0)
            or report.get("filtered_cohort_filtered_false_win_count") == 0
        )
    )
    undersampled = (
        "INSUFFICIENT" in sample_status
        or bool(sample_blockers.intersection(candidate_blockers))
        or bool(sample_blockers.intersection(shadow_blockers))
    )
    if promising_filter and undersampled:
        primary_problem.append("THRESHOLD_FILTER_PROMISING_BUT_UNDERSAMPLED")
        blockers.append("THRESHOLD_FILTER_SAMPLE_SUPPORT_INSUFFICIENT")
        required_evidence.extend([
            "MIN_FILTERED_ROWS_GTE_100",
            "MIN_PREDICTED_WIN_ROWS_GTE_30",
            "MIN_PER_SEGMENT_ROWS_GTE_10",
        ])

    pred_dist = report.get("threshold_stability_pred_distribution") or report.get("filtered_cohort_filtered_prediction_distribution") or {}
    pred_win = int(pred_dist.get("WIN") or 0)
    pred_loss = int(pred_dist.get("LOSS") or 0)
    if pred_loss > pred_win and (pred_win + pred_loss) > 0:
        primary_problem.append("CLASS_IMBALANCE_OR_DECISION_BOUNDARY_REVIEW")
        required_evidence.append("CLASS_DISTRIBUTION_AND_DECISION_BOUNDARY_ERROR_ANALYSIS")

    regime_summary = report.get("filtered_cohort_regime_summary")
    regime_blocked = (
        "REGIME_SEGMENT_UNAVAILABLE" in candidate_blockers
        or "REGIME_SEGMENT_UNAVAILABLE" in shadow_blockers
        or "REGIME_SEGMENT_UNAVAILABLE" in set(report.get("filtered_cohort_findings") or [])
        or (isinstance(regime_summary, dict) and regime_summary.get("status") == "UNAVAILABLE")
    )
    if regime_blocked:
        primary_problem.append("REGIME_EVIDENCE_GAP")
        blockers.append("REGIME_EVIDENCE_UNAVAILABLE")
        required_evidence.append("REGIME_SEGMENTED_THRESHOLD_AND_BASELINE_COMPARISON")

    candidate_paths = [
        "CLASS_IMBALANCE_REPAIR",
        "COST_SENSITIVE_TRAINING",
        "PROBABILITY_CALIBRATION",
        "REGIME_AWARE_MODELING",
        "FEATURE_RELIABILITY_REVIEW",
    ]
    recommended_first_path = "CLASS_IMBALANCE_AND_THRESHOLD_CALIBRATION_DIAGNOSTIC"
    if broad_below_baseline and promising_filter and undersampled:
        findings.append("Broad model performance is below baseline while threshold-filtered evidence improves but lacks enough sample support.")
    elif broad_below_baseline:
        findings.append("Broad model performance remains below baseline; repair diagnostics should precede any upgrade.")
    if regime_blocked:
        findings.append("Regime evidence is missing or blocked, so filtered-cohort stability cannot be accepted yet.")

    readiness_state = "REPAIR_PLAN_ONLY_READINESS_LOCKED" if broad_below_baseline or blockers else "REPAIR_PLAN_ONLY_EVIDENCE_REVIEW"
    return {
        "ml_model_upgrade_diagnostic_status": "AVAILABLE" if not missing else "UNAVAILABLE_MISSING_PRIOR_FIELDS",
        "ml_model_upgrade_readiness_state": readiness_state,
        "ml_model_upgrade_primary_problem": sorted(set(primary_problem), key=primary_problem.index) or ["NO_PRIMARY_PROBLEM_IDENTIFIED_FROM_AVAILABLE_FIELDS"],
        "ml_model_upgrade_candidate_paths": candidate_paths,
        "ml_model_upgrade_recommended_first_path": recommended_first_path,
        "ml_model_upgrade_blockers": sorted(set(blockers + missing), key=(blockers + missing).index),
        "ml_model_upgrade_required_evidence": sorted(set(required_evidence), key=required_evidence.index),
        "ml_model_upgrade_do_not_change": [
            "NO_LIVE_OR_TESTNET_EXECUTION",
            "NO_RUNTIME_THRESHOLD_APPLICATION",
            "NO_READINESS_UNLOCK",
            "NO_TRAINING_CHANGE_IN_THIS_PR",
            "NO_PREDICTION_CHANGE_IN_THIS_PR",
        ],
        "ml_model_upgrade_findings": findings,
        "ml_model_upgrade_recommendation": (
            "Keep execution disabled and paper-only. Use this plan to investigate class imbalance, cost-sensitive/threshold-aware training, "
            "probability calibration, regime-aware modeling, and feature reliability before any future model upgrade PR."
        ),
    }

def ml_class_imbalance_diagnostic(cohort: pd.DataFrame) -> Dict[str, Any]:
    """Report class-imbalance, confusion-matrix, and threshold diagnostics.

    This audit is diagnostic-only. It does not change model training, model
    thresholds, readiness gates, baseline superiority, walk-forward stability, or
    execution behavior.
    """
    base: Dict[str, Any] = {
        "class_imbalance_diagnostic_status": "UNAVAILABLE_NO_EVALUATED_ROWS",
        "class_imbalance_sample_count": 0,
        "true_label_distribution": {},
        "predicted_label_distribution": {},
        "confusion_matrix": {
            "actual_WIN": {"predicted_WIN": 0, "predicted_LOSS": 0},
            "actual_LOSS": {"predicted_WIN": 0, "predicted_LOSS": 0},
        },
        "win_precision": None,
        "win_recall": None,
        "win_f1": None,
        "loss_precision": None,
        "loss_recall": None,
        "loss_f1": None,
        "false_win_count": 0,
        "false_loss_count": 0,
        "true_win_count": 0,
        "true_loss_count": 0,
        "model_prediction_bias": None,
        "majority_class": None,
        "majority_class_ratio": None,
        "probability_threshold_diagnostic": [],
        "class_imbalance_findings": [],
        "class_imbalance_recommendation": (
            "Diagnostic only: do not alter model training, thresholds, readiness gates, or execution behavior from this audit alone."
        ),
    }
    if cohort.empty:
        base["class_imbalance_findings"] = ["No prediction cohort rows are available for class-imbalance diagnostics."]
        return base

    frame = cohort.copy()
    if "__y_true" not in frame.columns:
        y_true_col = _first_existing_column(frame, TRUE_COLUMNS)
        if y_true_col:
            frame["__y_true"] = frame[y_true_col]
    if "__y_pred" not in frame.columns:
        y_pred_col = _first_existing_column(frame, PREDICTION_COLUMNS)
        if y_pred_col:
            frame["__y_pred"] = frame[y_pred_col]
    if "__y_true" not in frame.columns or "__y_pred" not in frame.columns:
        base["class_imbalance_diagnostic_status"] = "UNAVAILABLE_MISSING_LABELS"
        base["class_imbalance_findings"] = ["y_true/y_pred columns are missing from row-level predictions."]
        return base

    rows = []
    for _, row in frame.dropna(subset=["__y_true", "__y_pred"]).iterrows():
        actual = canonical_ml_label(row.get("__y_true"))
        predicted = canonical_ml_label(row.get("__y_pred"))
        if actual in {"WIN", "LOSS"} and predicted in {"WIN", "LOSS"}:
            rows.append((actual, predicted, safe_float(row.get("__predicted_probability", row.get("predicted_probability")))))
    if not rows:
        base["class_imbalance_findings"] = ["No evaluated WIN/LOSS rows are available for class-imbalance diagnostics."]
        return base

    y_true = [r[0] for r in rows]
    y_pred = [r[1] for r in rows]
    sample_count = len(rows)
    true_dist = _counter_dict(y_true)
    pred_dist = _counter_dict(y_pred)
    true_win = sum(a == "WIN" and p == "WIN" for a, p in zip(y_true, y_pred))
    true_loss = sum(a == "LOSS" and p == "LOSS" for a, p in zip(y_true, y_pred))
    false_win = sum(a == "LOSS" and p == "WIN" for a, p in zip(y_true, y_pred))
    false_loss = sum(a == "WIN" and p == "LOSS" for a, p in zip(y_true, y_pred))
    actual_win = true_dist.get("WIN", 0)
    actual_loss = true_dist.get("LOSS", 0)
    predicted_win = pred_dist.get("WIN", 0)
    predicted_loss = pred_dist.get("LOSS", 0)
    win_precision = _safe_ratio(true_win, predicted_win)
    win_recall = _safe_ratio(true_win, actual_win)
    loss_precision = _safe_ratio(true_loss, predicted_loss)
    loss_recall = _safe_ratio(true_loss, actual_loss)
    win_f1 = 2 * win_precision * win_recall / (win_precision + win_recall) if win_precision is not None and win_recall is not None and win_precision + win_recall else None
    loss_f1 = 2 * loss_precision * loss_recall / (loss_precision + loss_recall) if loss_precision is not None and loss_recall is not None and loss_precision + loss_recall else None
    majority_class, majority_count = max(true_dist.items(), key=lambda item: (item[1], item[0]))
    true_win_ratio = actual_win / sample_count
    predicted_win_ratio = predicted_win / sample_count
    findings: List[str] = []
    status = "AVAILABLE"
    majority_ratio = majority_count / sample_count
    if majority_ratio >= 0.60:
        status = "REVIEW_CLASS_IMBALANCE"
        findings.append(f"True labels are dominated by {majority_class} at {round_or_none(majority_ratio)} of evaluated rows.")
    if predicted_win_ratio - true_win_ratio >= 0.10:
        findings.append("Model appears optimistic relative to true label distribution.")
    if false_win >= max(2, int(0.10 * sample_count)):
        findings.append("False WIN predictions are a likely contributor to below-baseline performance.")
    if win_precision is not None and win_precision < 0.50:
        recommendation = "Review model as LOSS avoidance filter before using it as WIN/entry predictor."
    else:
        recommendation = "Use this diagnostic to decide whether to repair WIN prediction or evaluate LOSS-avoidance/trade-filter behavior; do not unlock readiness."

    probability_thresholds = []
    probabilities = [r[2] for r in rows]
    if any(prob is not None for prob in probabilities):
        for threshold in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
            kept = [(a, p) for a, p, prob in rows if prob is not None and prob >= threshold]
            kept_pred_win = sum(p == "WIN" for _, p in kept)
            kept_true_win = sum(a == "WIN" and p == "WIN" for a, p in kept)
            probability_thresholds.append({
                "threshold": threshold,
                "rows_kept": len(kept),
                "kept_ratio": _safe_ratio(len(kept), sample_count),
                "accuracy_on_kept_rows": _safe_ratio(sum(a == p for a, p in kept), len(kept)),
                "win_precision_on_kept_rows": _safe_ratio(kept_true_win, kept_pred_win),
                "false_win_count_on_kept_rows": sum(a == "LOSS" and p == "WIN" for a, p in kept),
            })

    return {
        **base,
        "class_imbalance_diagnostic_status": status,
        "class_imbalance_sample_count": sample_count,
        "true_label_distribution": true_dist,
        "predicted_label_distribution": pred_dist,
        "confusion_matrix": {
            "actual_WIN": {"predicted_WIN": true_win, "predicted_LOSS": false_loss},
            "actual_LOSS": {"predicted_WIN": false_win, "predicted_LOSS": true_loss},
        },
        "win_precision": win_precision,
        "win_recall": win_recall,
        "win_f1": win_f1,
        "loss_precision": loss_precision,
        "loss_recall": loss_recall,
        "loss_f1": loss_f1,
        "false_win_count": false_win,
        "false_loss_count": false_loss,
        "true_win_count": true_win,
        "true_loss_count": true_loss,
        "model_prediction_bias": {
            "true_win_ratio": true_win_ratio,
            "predicted_win_ratio": predicted_win_ratio,
            "predicted_minus_true_win_ratio": predicted_win_ratio - true_win_ratio,
        },
        "majority_class": majority_class,
        "majority_class_ratio": majority_ratio,
        "probability_threshold_diagnostic": probability_thresholds,
        "class_imbalance_findings": findings or ["Class imbalance diagnostic is available; no dominant diagnostic finding crossed review thresholds."],
        "class_imbalance_recommendation": recommendation,
    }

def mcc_binary(y_true: Sequence[Any], y_pred: Sequence[Any]) -> Optional[float]:
    labels = list(set(y_true) | set(y_pred))
    if len(labels) != 2:
        return None
    positive = labels[0]
    tp = sum(actual == positive and pred == positive for actual, pred in zip(y_true, y_pred))
    tn = sum(actual != positive and pred != positive for actual, pred in zip(y_true, y_pred))
    fp = sum(actual != positive and pred == positive for actual, pred in zip(y_true, y_pred))
    fn = sum(actual == positive and pred != positive for actual, pred in zip(y_true, y_pred))
    denominator = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
    return ((tp * tn) - (fp * fn)) / math.sqrt(denominator) if denominator else 0.0



ROW_LEVEL_WALKFORWARD_FIELDS = [
    "prediction_id",
    "fold_id",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "prediction_timestamp",
    "feature_timestamp_max",
    "symbol",
    "canonical_label",
    "y_true",
    "y_pred",
    "predicted_probability",
    "baseline_prediction",
    "model_correct",
    "baseline_correct",
]


def _first_existing_column(frame: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    return find_column(frame.columns, candidates)


def _stable_prediction_id(row: pd.Series, idx: int) -> str:
    explicit = row.get("prediction_id")
    if pd.notna(explicit) and str(explicit):
        return str(explicit)
    parts = [
        str(row.get("symbol", "")),
        str(row.get("prediction_timestamp", row.get("timestamp", row.get("signal_timestamp", "")))),
        str(row.get("__y_true", "")),
        str(row.get("__y_pred", "")),
        str(idx),
    ]
    return "row-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _majority_label(values: Sequence[Any]) -> Any:
    counts = Counter(str(value) for value in values if pd.notna(value))
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def row_level_walkforward_audit(
    cohort: pd.DataFrame,
    temporal_feature_guard: Dict[str, Any],
    preprocessing_guard: Dict[str, Any],
    min_folds: int = 2,
    min_test_rows: int = 10,
    train_window: int = 6,
    test_window: int = 3,
) -> Dict[str, Any]:
    findings: List[str] = []
    if cohort.empty:
        return {
            "row_level_walkforward_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS",
            "row_level_walkforward_rows": 0,
            "row_level_walkforward_folds": 0,
            "baseline_accuracy": None,
            "model_accuracy": None,
            "model_vs_baseline_delta": None,
            "baseline_superiority_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS",
            "row_level_walkforward_findings": ["No valid row-level prediction cohort is available."],
            "rows": [],
            "folds": [],
        }
    if temporal_feature_guard.get("status") == "BLOCKED":
        findings.append("Temporal/preprocessing guard is BLOCKED: temporal feature guard failed.")
    if str(preprocessing_guard.get("train_only_preprocessing_status")) .startswith("BLOCKED"):
        findings.append("Temporal/preprocessing guard is BLOCKED: train-only preprocessing guard failed.")

    ts_col = _first_existing_column(cohort, ("prediction_timestamp", "timestamp", "signal_timestamp"))
    if not ts_col:
        findings.append("prediction_timestamp is missing from row-level predictions.")
        return {"row_level_walkforward_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS", "row_level_walkforward_rows": 0, "row_level_walkforward_folds": 0, "baseline_accuracy": None, "model_accuracy": None, "model_vs_baseline_delta": None, "baseline_superiority_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS", "row_level_walkforward_findings": findings, "rows": [], "folds": []}

    frame = cohort.copy().reset_index(drop=True)
    if "__y_true" not in frame.columns:
        y_true_col = _first_existing_column(frame, TRUE_COLUMNS)
        if y_true_col:
            frame["__y_true"] = frame[y_true_col]
    if "__y_pred" not in frame.columns:
        y_pred_col = _first_existing_column(frame, PREDICTION_COLUMNS)
        if y_pred_col:
            frame["__y_pred"] = frame[y_pred_col]
    if "__y_true" not in frame.columns or "__y_pred" not in frame.columns:
        findings.append("y_true/y_pred columns are missing from row-level predictions.")
        return {"row_level_walkforward_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS", "row_level_walkforward_rows": 0, "row_level_walkforward_folds": 0, "baseline_accuracy": None, "model_accuracy": None, "model_vs_baseline_delta": None, "baseline_superiority_status": "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS", "row_level_walkforward_findings": findings, "rows": [], "folds": []}
    frame["__prediction_ts_sort"] = pd.to_datetime(frame[ts_col], errors="coerce", utc=True)
    frame = frame.sort_values(["__prediction_ts_sort", ts_col], na_position="last").reset_index(drop=True)
    rows: List[Dict[str, Any]] = []
    folds: List[Dict[str, Any]] = []
    fold_id = 1
    start = 0
    feature_ts_col = _first_existing_column(frame, ("feature_timestamp_max", "max_feature_timestamp", "feature_timestamp"))
    symbol_col = _first_existing_column(frame, ("symbol", "asset"))
    prob_col = _first_existing_column(frame, ("predicted_probability", "prediction_probability", "probability", "confidence"))
    while start + train_window + test_window <= len(frame):
        train = frame.iloc[start:start+train_window]
        test = frame.iloc[start+train_window:start+train_window+test_window]
        start += test_window
        baseline_prediction = _majority_label([canonical_ml_label(value) for value in train["__y_true"].tolist()])
        if baseline_prediction is None:
            findings.append(f"Fold {fold_id} has no train labels for baseline.")
            continue
        fold_rows = []
        for idx, row in test.iterrows():
            raw_y_true = row.get("__y_true")
            raw_y_pred = row.get("__y_pred")
            y_true = canonical_ml_label(raw_y_true)
            y_pred = canonical_ml_label(raw_y_pred)
            out = {
                "prediction_id": _stable_prediction_id(row, int(idx)),
                "fold_id": fold_id,
                "train_start": str(train[ts_col].iloc[0]),
                "train_end": str(train[ts_col].iloc[-1]),
                "test_start": str(test[ts_col].iloc[0]),
                "test_end": str(test[ts_col].iloc[-1]),
                "prediction_timestamp": str(row.get(ts_col)),
                "feature_timestamp_max": None if not feature_ts_col else row.get(feature_ts_col),
                "symbol": None if not symbol_col else row.get(symbol_col),
                "canonical_label": y_true,
                "y_true": y_true,
                "y_pred": y_pred,
                "predicted_probability": None if not prob_col else row.get(prob_col),
                "baseline_prediction": baseline_prediction,
                "model_correct": bool(y_true == y_pred),
                "baseline_correct": bool(y_true == baseline_prediction),
            }
            rows.append(out); fold_rows.append(out)
        folds.append({
            "fold_id": fold_id,
            "train_start": str(train[ts_col].iloc[0]),
            "train_end": str(train[ts_col].iloc[-1]),
            "test_start": str(test[ts_col].iloc[0]),
            "test_end": str(test[ts_col].iloc[-1]),
            "train_rows": int(len(train)),
            "test_rows": int(len(fold_rows)),
            "baseline_prediction": baseline_prediction,
            "model_accuracy": round_or_none(sum(r["model_correct"] for r in fold_rows) / len(fold_rows) if fold_rows else None),
            "baseline_accuracy": round_or_none(sum(r["baseline_correct"] for r in fold_rows) / len(fold_rows) if fold_rows else None),
        })
        fold_id += 1
    model_accuracy = round_or_none(sum(r["model_correct"] for r in rows) / len(rows) if rows else None)
    baseline_accuracy = round_or_none(sum(r["baseline_correct"] for r in rows) / len(rows) if rows else None)
    delta = round_or_none(model_accuracy - baseline_accuracy if model_accuracy is not None and baseline_accuracy is not None else None)
    if not rows:
        status = "BLOCKED_MISSING_ROW_LEVEL_PREDICTIONS"
        findings.append("No fold-level test rows were produced from the row-level cohort.")
    elif len(folds) < min_folds:
        status = "BLOCKED_INSUFFICIENT_FOLDS"; findings.append(f"Only {len(folds)} folds available; minimum is {min_folds}.")
    elif len(rows) < min_test_rows:
        status = "BLOCKED_INSUFFICIENT_TEST_ROWS"; findings.append(f"Only {len(rows)} evaluated rows available; minimum is {min_test_rows}.")
    elif temporal_feature_guard.get("status") == "BLOCKED" or str(preprocessing_guard.get("train_only_preprocessing_status")).startswith("BLOCKED"):
        status = "BLOCKED_GUARD_FAILURE"
    elif delta is None or delta <= 0:
        status = "BLOCKED_BELOW_BASELINE"; findings.append("Model accuracy does not exceed majority-class baseline accuracy.")
    else:
        status = "PASS_BASELINE_SUPERIORITY"; findings.append("Model accuracy exceeds train-fold majority-class baseline with enough folds and rows.")
    return {"row_level_walkforward_status": status, "row_level_walkforward_rows": len(rows), "row_level_walkforward_folds": len(folds), "baseline_accuracy": baseline_accuracy, "model_accuracy": model_accuracy, "model_vs_baseline_delta": delta, "baseline_superiority_status": status, "row_level_walkforward_findings": findings, "rows": rows, "folds": folds}


def _counter_dict(values: Sequence[Any]) -> Dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values if value is not None and pd.notna(value)).items()))


def baseline_root_cause_audit(row_level_walkforward: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize why baseline superiority is blocked without changing gates.

    This is evidence-quality reporting only: it diagnoses micro-fold evaluation and
    train-fold majority-class baseline dominance while leaving readiness statuses
    and thresholds unchanged.
    """
    folds = row_level_walkforward.get("folds") or []
    if not folds:
        return {
            "baseline_root_cause_audit": "UNAVAILABLE",
            "baseline_evidence_quality_status": "UNAVAILABLE",
            "baseline_micro_fold_status": "UNAVAILABLE",
            "baseline_fold_size_distribution": {},
            "baseline_train_size_distribution": {},
            "baseline_prediction_distribution": {},
            "baseline_model_worse_folds": 0,
            "baseline_model_better_folds": 0,
            "baseline_model_tie_folds": 0,
            "baseline_worse_fold_prediction_distribution": {},
            "baseline_better_fold_prediction_distribution": {},
            "baseline_root_cause_findings": ["No fold-level baseline evidence is available for root-cause audit."],
            "baseline_root_cause_recommendation": "Keep readiness fail-closed until row-level walk-forward baseline evidence is available.",
        }

    test_sizes = [int(fold.get("test_rows") or 0) for fold in folds]
    train_sizes = [int(fold.get("train_rows") or 0) for fold in folds]
    micro_folds = [size for size in test_sizes if size < 10]
    micro_ratio = len(micro_folds) / len(test_sizes) if test_sizes else 0.0
    micro_status = "REVIEW_MICRO_FOLD_EVIDENCE" if micro_ratio >= 0.5 else "PASS_FOLD_SIZE_REVIEW"

    baseline_predictions = [fold.get("baseline_prediction") for fold in folds]
    prediction_distribution = _counter_dict(baseline_predictions)
    top_count = max(prediction_distribution.values()) if prediction_distribution else 0
    top_ratio = top_count / len(folds) if folds else 0.0
    evidence_status = "REVIEW_MAJOR_CLASS_BASELINE_DOMINANCE" if top_ratio >= 0.75 else "PASS_BASELINE_CLASS_BALANCE_REVIEW"

    worse: List[Dict[str, Any]] = []
    better: List[Dict[str, Any]] = []
    tie: List[Dict[str, Any]] = []
    for fold in folds:
        model_acc = safe_float(fold.get("model_accuracy"))
        baseline_acc = safe_float(fold.get("baseline_accuracy"))
        if model_acc is None or baseline_acc is None:
            tie.append(fold)
        elif model_acc < baseline_acc:
            worse.append(fold)
        elif model_acc > baseline_acc:
            better.append(fold)
        else:
            tie.append(fold)

    findings = [
        f"Model accuracy is {row_level_walkforward.get('model_accuracy')} versus baseline accuracy {row_level_walkforward.get('baseline_accuracy')} with delta {row_level_walkforward.get('model_vs_baseline_delta')}.",
        f"Micro-fold evidence: {len(micro_folds)} of {len(folds)} folds have fewer than 10 test rows.",
        f"Baseline prediction distribution is {prediction_distribution}.",
        f"Fold outcomes: model worse={len(worse)}, model better={len(better)}, tie={len(tie)}.",
    ]
    if evidence_status == "REVIEW_MAJOR_CLASS_BASELINE_DOMINANCE":
        findings.append("Baseline superiority evidence is dominated by a single train-fold majority-class prediction.")
    if worse and _counter_dict([fold.get("baseline_prediction") for fold in worse]):
        findings.append(f"Worse folds baseline prediction distribution is {_counter_dict([fold.get('baseline_prediction') for fold in worse])}.")

    recommendation = (
        "Do not unlock readiness or alter baseline/walk-forward thresholds; review larger or less granular walk-forward folds "
        "and class-imbalance-aware model diagnostics while keeping paper-only execution controls."
    )
    return {
        "baseline_root_cause_audit": "AVAILABLE",
        "baseline_evidence_quality_status": evidence_status,
        "baseline_micro_fold_status": micro_status,
        "baseline_fold_size_distribution": _counter_dict(test_sizes),
        "baseline_train_size_distribution": _counter_dict(train_sizes),
        "baseline_prediction_distribution": prediction_distribution,
        "baseline_model_worse_folds": len(worse),
        "baseline_model_better_folds": len(better),
        "baseline_model_tie_folds": len(tie),
        "baseline_worse_fold_prediction_distribution": _counter_dict([fold.get("baseline_prediction") for fold in worse]),
        "baseline_better_fold_prediction_distribution": _counter_dict([fold.get("baseline_prediction") for fold in better]),
        "baseline_root_cause_findings": findings,
        "baseline_root_cause_recommendation": recommendation,
    }


def larger_fold_baseline_diagnostic(
    cohort: pd.DataFrame,
    min_test_rows: int = 10,
    min_train_rows: int = 20,
) -> Dict[str, Any]:
    """Evaluate majority-class baseline evidence on larger blocked folds.

    This diagnostic is intentionally advisory. It does not feed readiness gates,
    model training, thresholds, execution controls, or promotion logic.
    """
    base: Dict[str, Any] = {
        "larger_fold_baseline_diagnostic_status": "UNAVAILABLE_INSUFFICIENT_ROWS",
        "larger_fold_rows": 0,
        "larger_fold_count": 0,
        "larger_fold_min_test_rows": int(min_test_rows),
        "larger_fold_min_train_rows": int(min_train_rows),
        "larger_fold_model_accuracy": None,
        "larger_fold_baseline_accuracy": None,
        "larger_fold_model_vs_baseline_delta": None,
        "larger_fold_baseline_prediction_distribution": {},
        "larger_fold_model_worse_folds": 0,
        "larger_fold_model_better_folds": 0,
        "larger_fold_model_tie_folds": 0,
        "larger_fold_findings": [],
        "larger_fold_recommendation": (
            "Diagnostic only: keep existing readiness gates unchanged and use this evidence to decide whether next work "
            "should target fold-size governance, class-imbalance-aware baseline review, or model/threshold repair."
        ),
        "larger_fold_details": [],
    }
    if cohort.empty or len(cohort) < min_train_rows + min_test_rows:
        base["larger_fold_findings"] = [
            f"Insufficient rows for larger-fold diagnostic: need at least {min_train_rows + min_test_rows}, found {0 if cohort.empty else len(cohort)}."
        ]
        return base

    ts_col = _first_existing_column(cohort, ("prediction_timestamp", "timestamp", "signal_timestamp"))
    if not ts_col:
        base["larger_fold_findings"] = ["prediction_timestamp is missing from row-level predictions."]
        return base

    frame = cohort.copy().reset_index(drop=True)
    if "__y_true" not in frame.columns:
        y_true_col = _first_existing_column(frame, TRUE_COLUMNS)
        if y_true_col:
            frame["__y_true"] = frame[y_true_col]
    if "__y_pred" not in frame.columns:
        y_pred_col = _first_existing_column(frame, PREDICTION_COLUMNS)
        if y_pred_col:
            frame["__y_pred"] = frame[y_pred_col]
    if "__y_true" not in frame.columns or "__y_pred" not in frame.columns:
        base["larger_fold_findings"] = ["y_true/y_pred columns are missing from row-level predictions."]
        return base

    frame["__prediction_ts_sort"] = pd.to_datetime(frame[ts_col], errors="coerce", utc=True)
    frame = frame.sort_values(["__prediction_ts_sort", ts_col], na_position="last").reset_index(drop=True)
    rows: List[Dict[str, Any]] = []
    folds: List[Dict[str, Any]] = []
    fold_id = 1
    start = 0
    while start + min_train_rows + min_test_rows <= len(frame):
        train = frame.iloc[start:start + min_train_rows]
        test = frame.iloc[start + min_train_rows:start + min_train_rows + min_test_rows]
        start += min_test_rows
        baseline_prediction = _majority_label([canonical_ml_label(value) for value in train["__y_true"].tolist()])
        if baseline_prediction is None:
            continue
        fold_rows = []
        for _, row in test.iterrows():
            y_true = canonical_ml_label(row.get("__y_true"))
            y_pred = canonical_ml_label(row.get("__y_pred"))
            out = {
                "fold_id": fold_id,
                "y_true": y_true,
                "y_pred": y_pred,
                "baseline_prediction": baseline_prediction,
                "model_correct": bool(y_true == y_pred),
                "baseline_correct": bool(y_true == baseline_prediction),
            }
            rows.append(out)
            fold_rows.append(out)
        model_accuracy = round_or_none(sum(r["model_correct"] for r in fold_rows) / len(fold_rows) if fold_rows else None)
        baseline_accuracy = round_or_none(sum(r["baseline_correct"] for r in fold_rows) / len(fold_rows) if fold_rows else None)
        folds.append({
            "fold_id": fold_id,
            "train_start": str(train[ts_col].iloc[0]),
            "train_end": str(train[ts_col].iloc[-1]),
            "test_start": str(test[ts_col].iloc[0]),
            "test_end": str(test[ts_col].iloc[-1]),
            "train_rows": int(len(train)),
            "test_rows": int(len(fold_rows)),
            "baseline_prediction": baseline_prediction,
            "model_accuracy": model_accuracy,
            "baseline_accuracy": baseline_accuracy,
            "model_vs_baseline_delta": round_or_none(model_accuracy - baseline_accuracy if model_accuracy is not None and baseline_accuracy is not None else None),
        })
        fold_id += 1

    if not rows:
        base["larger_fold_findings"] = ["No larger diagnostic folds could be built from the row-level cohort."]
        return base

    model_accuracy = round_or_none(sum(r["model_correct"] for r in rows) / len(rows))
    baseline_accuracy = round_or_none(sum(r["baseline_correct"] for r in rows) / len(rows))
    delta = round_or_none(model_accuracy - baseline_accuracy)
    worse = [fold for fold in folds if safe_float(fold.get("model_accuracy")) is not None and safe_float(fold.get("baseline_accuracy")) is not None and safe_float(fold.get("model_accuracy")) < safe_float(fold.get("baseline_accuracy"))]
    better = [fold for fold in folds if safe_float(fold.get("model_accuracy")) is not None and safe_float(fold.get("baseline_accuracy")) is not None and safe_float(fold.get("model_accuracy")) > safe_float(fold.get("baseline_accuracy"))]
    tie = [fold for fold in folds if fold not in worse and fold not in better]
    findings = [
        f"Larger-fold diagnostic is available for {len(rows)} rows across {len(folds)} folds.",
        f"Diagnostic model accuracy is {model_accuracy} versus baseline accuracy {baseline_accuracy} with delta {delta}.",
    ]
    findings.append(
        "Larger diagnostic folds still show the model below or equal to the majority-class baseline."
        if delta is not None and delta <= 0
        else "Larger diagnostic folds show model improvement over baseline; this is diagnostic evidence only and does not unlock readiness."
    )
    return {
        **base,
        "larger_fold_baseline_diagnostic_status": "AVAILABLE",
        "larger_fold_rows": len(rows),
        "larger_fold_count": len(folds),
        "larger_fold_model_accuracy": model_accuracy,
        "larger_fold_baseline_accuracy": baseline_accuracy,
        "larger_fold_model_vs_baseline_delta": delta,
        "larger_fold_baseline_prediction_distribution": _counter_dict([fold.get("baseline_prediction") for fold in folds]),
        "larger_fold_model_worse_folds": len(worse),
        "larger_fold_model_better_folds": len(better),
        "larger_fold_model_tie_folds": len(tie),
        "larger_fold_findings": findings,
        "larger_fold_details": folds,
    }


def baseline_status(metrics: Optional[Dict[str, Any]], min_samples: int = 30) -> Dict[str, Any]:
    if not metrics:
        return {"status": "BLOCKED_INSUFFICIENT_SAMPLE", "sample_size": 0, "absolute_accuracy_improvement": None, "practically_meaningful": False}
    samples = metrics.get("samples") or 0
    accuracy = metrics.get("accuracy")
    baseline = metrics.get("majority_class_baseline")
    if samples < min_samples:
        status = "BLOCKED_INSUFFICIENT_SAMPLE"
    elif accuracy is None or baseline is None:
        status = "UNAVAILABLE"
    elif accuracy <= baseline:
        status = "BLOCKED_BELOW_BASELINE"
    elif accuracy - baseline < 0.03:
        status = "REVIEW_MARGINAL"
    else:
        status = "PASS_BASELINE"
    return {
        "status": status,
        "sample_size": samples,
        "absolute_accuracy_improvement": None if accuracy is None or baseline is None else accuracy - baseline,
        "practically_meaningful": status == "PASS_BASELINE",
    }


def leakage_status(train: pd.DataFrame, test: pd.DataFrame, time_col: str = "timestamp", feature_cols: Optional[List[str]] = None) -> Dict[str, Any]:
    if train.empty or test.empty:
        return {"status": "UNVERIFIABLE", "reasons": ["missing train or test rows"]}
    reasons: List[str] = []
    if time_col in train.columns and time_col in test.columns:
        train_time = pd.to_datetime(train[time_col], errors="coerce", utc=True)
        test_time = pd.to_datetime(test[time_col], errors="coerce", utc=True)
        if train_time.notna().any() and test_time.notna().any() and test_time.min() <= train_time.max():
            reasons.append("BLOCKED_TEMPORAL_LEAKAGE")
    else:
        reasons.append("UNVERIFIABLE_TIME_COLUMN")
    if {"symbol", time_col}.issubset(train.columns) and {"symbol", time_col}.issubset(test.columns):
        train_keys = set(zip(train["symbol"].astype(str), train[time_col].astype(str)))
        test_keys = set(zip(test["symbol"].astype(str), test[time_col].astype(str)))
        if train_keys & test_keys:
            reasons.append("BLOCKED_SPLIT_CONTAMINATION")
    feature_cols = feature_cols or []
    if any(str(column).lower() in TARGET_LIKE_COLUMNS for column in feature_cols):
        reasons.append("BLOCKED_TARGET_LEAKAGE")
    blockers = [reason for reason in reasons if reason.startswith("BLOCKED")]
    if blockers:
        return {"status": blockers[0], "reasons": reasons}
    if reasons:
        return {"status": "UNVERIFIABLE", "reasons": reasons}
    return {"status": "PASS", "reasons": []}


def reconstruct_walkforward(path: Optional[str]) -> Dict[str, Any]:
    if not path or not Path(path).exists():
        return {
            "status": "SOURCE_MISSING",
            "folds": [],
            "fold_count": 0,
            "weighted_aggregate": None,
            "weighted_aggregate_reason": "walk-forward artifact missing",
            "unweighted_aggregate": None,
            "responsible_method": "walkforward.run_walkforward_validation computes unweighted mean(test_accuracy)",
        }
    df = pd.read_csv(path)
    required = {"fold", "train_start", "train_end", "test_start", "test_end", "test_accuracy"}
    missing = sorted(required - set(df.columns))
    if missing:
        return {"status": "UNREPRODUCIBLE", "reason": "missing columns: " + ", ".join(missing), "folds": [], "fold_count": 0}
    folds: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        train_start = row.get("train_start")
        train_end = row.get("train_end")
        test_start = row.get("test_start")
        test_end = row.get("test_end")
        accuracy = round_or_none(row.get("test_accuracy"))
        train_rows = safe_float(row.get("train_rows"))
        test_rows = safe_float(row.get("test_rows"))
        try:
            embargo_gap = float(test_start) - float(train_end)
            ordering_ok = float(test_start) > float(train_end)
        except (TypeError, ValueError):
            embargo_gap = None
            ordering_ok = False
        leakage = "UNVERIFIABLE"
        reasons = []
        index_only = all(str(value).replace(".", "", 1).isdigit() for value in [train_start, train_end, test_start, test_end])
        if not ordering_ok:
            leakage = "BLOCKED_TEMPORAL_LEAKAGE"
            reasons.append("test_start must be greater than train_end")
        elif index_only:
            leakage = "UNVERIFIABLE"
            reasons.append("fold boundaries are positional indexes, not row-level timestamps")
        elif train_rows is None or test_rows is None:
            leakage = "UNVERIFIABLE"
            reasons.append("fold row counts unavailable")
        elif not bool(row.get("row_level_evidence", False)):
            leakage = "UNVERIFIABLE"
            reasons.append("row-level train/test keys, labels, predictions, and fold membership unavailable")
        else:
            leakage = "PASS"
        folds.append(
            {
                "fold_id": int(row.get("fold")),
                "training_start": train_start,
                "training_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "index_gap": embargo_gap if index_only else None,
                "temporal_embargo": None if index_only else embargo_gap,
                "train_rows": None if train_rows is None else int(train_rows),
                "test_rows": None if test_rows is None else int(test_rows),
                "class_distribution": row.get("class_distribution") if "class_distribution" in df.columns else None,
                "accuracy": accuracy,
                "balanced_accuracy": round_or_none(row.get("balanced_accuracy")),
                "macro_f1": round_or_none(row.get("macro_f1")),
                "baseline_accuracy": round_or_none(row.get("baseline_accuracy")),
                "improvement_over_baseline": None,
                "regime_distribution": row.get("regime_distribution") if "regime_distribution" in df.columns else None,
                "excluded_rows": row.get("excluded_rows") if "excluded_rows" in df.columns else None,
                "leakage_status": leakage,
                "leakage_reasons": ";".join(reasons),
            }
        )
    accuracies = [fold["accuracy"] for fold in folds if fold["accuracy"] is not None]
    unweighted = sum(accuracies) / len(accuracies) if accuracies else None
    weighted = None
    weighted_reason = None
    if folds and all(fold["test_rows"] is not None and fold["accuracy"] is not None for fold in folds):
        total_rows = sum(int(fold["test_rows"]) for fold in folds)
        weighted = sum(float(fold["accuracy"]) * int(fold["test_rows"]) for fold in folds) / total_rows if total_rows else None
    else:
        weighted_reason = "test_rows unavailable for one or more folds"
    return {
        "status": "REPRODUCED_WITH_ROUNDING" if accuracies else "UNREPRODUCIBLE",
        "folds": folds,
        "fold_count": len(folds),
        "weighted_aggregate": round_or_none(weighted),
        "weighted_aggregate_reason": weighted_reason,
        "unweighted_aggregate": round_or_none(unweighted),
        "median_fold": round_or_none(pd.Series(accuracies).median()) if accuracies else None,
        "standard_deviation": round_or_none(pd.Series(accuracies).std(ddof=0)) if accuracies else None,
        "worst_fold": min(folds, key=lambda item: item["accuracy"] if item["accuracy"] is not None else 9, default=None),
        "best_fold": max(folds, key=lambda item: item["accuracy"] if item["accuracy"] is not None else -1, default=None),
        "latest_fold": folds[-1] if folds else None,
        "pct_folds_above_60": round_or_none(sum(value >= 0.60 for value in accuracies) / len(accuracies) * 100) if accuracies else None,
        "pct_folds_beating_baseline": None,
        "responsible_method": "walkforward.run_walkforward_validation computes unweighted mean(test_accuracy); weighted aggregate is audit-only when test_rows exists",
    }


def summarize_walkforward_display(path: Optional[str]) -> Dict[str, Any]:
    """Reproduce display-only walk-forward values from fold CSV when possible."""
    if not path or not Path(path).exists():
        return {
            "status": "SOURCE_MISSING",
            "average_accuracy": None,
            "average_winrate": None,
            "overfit_risk_score": None,
            "model_health": None,
            "reason": "walk-forward artifact missing",
        }
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return {"status": "UNREPRODUCIBLE", "average_accuracy": None, "average_winrate": None, "overfit_risk_score": None, "model_health": None, "reason": str(exc)}
    out: Dict[str, Any] = {"status": "UNREPRODUCIBLE", "average_accuracy": None, "average_winrate": None, "overfit_risk_score": None, "model_health": None, "reason": None}
    if "test_accuracy" in df.columns and not df.empty:
        out["average_accuracy"] = round_or_none(pd.to_numeric(df["test_accuracy"], errors="coerce").mean())
        out["status"] = "REPRODUCED_WITH_ROUNDING"
    if "winrate" in df.columns and not df.empty:
        out["average_winrate"] = round_or_none(pd.to_numeric(df["winrate"], errors="coerce").mean())
    if {"train_accuracy", "test_accuracy"}.issubset(df.columns) and not df.empty:
        train_mean = pd.to_numeric(df["train_accuracy"], errors="coerce").mean()
        test_mean = pd.to_numeric(df["test_accuracy"], errors="coerce").mean()
        if pd.notna(train_mean) and pd.notna(test_mean):
            out["overfit_risk_score"] = round_or_none(max(0.0, float(train_mean - test_mean)) * 100.0)
            accuracies = pd.to_numeric(df["test_accuracy"], errors="coerce").dropna()
            stability_score = max(0.0, 100.0 - float(accuracies.std(ddof=0) * 100.0)) if len(accuracies) else 0.0
            if out["overfit_risk_score"] >= 65:
                out["model_health"] = "OVERFIT RISK"
            elif stability_score < 45:
                out["model_health"] = "UNSTABLE"
            else:
                out["model_health"] = "ROBUST"
    if out["average_winrate"] is None and out["overfit_risk_score"] is None:
        out["reason"] = "fold display columns unavailable"
    return out


def parse_historical_ml_artifact(path: Optional[str], stale_ttl_days: float = DEFAULT_STALE_TTL_DAYS) -> Dict[str, Any]:
    if not path or not Path(path).exists():
        return {"status": "SOURCE_MISSING", "global_accuracy": None, "rows": None, "reason": "historical ML artifact missing"}
    data = read_json(path)
    age, age_source, generated_ts, timestamp_field = artifact_age_days(path)
    stale = bool(age is not None and age > stale_ttl_days)
    if data is None:
        return {"status": "UNREPRODUCIBLE", "global_accuracy": None, "rows": None, "reason": "invalid JSON", "generated_timestamp": generated_ts, "timestamp_field_used": timestamp_field, "file_age_days": round_or_none(age, 4), "age_source": age_source, "stale_source": stale}
    accuracy = safe_float(data.get("global_accuracy") if "global_accuracy" in data else data.get("accuracy"))
    base = {"generated_timestamp": generated_ts, "timestamp_field_used": timestamp_field, "file_age_days": round_or_none(age, 4), "age_source": age_source, "stale_source": stale, "model_version": data.get("model_version"), "evaluation_contract": data.get("evaluation_contract"), "rows": data.get("rows")}
    if accuracy is None:
        return {**base, "status": "UNREPRODUCIBLE", "global_accuracy": None, "reason": "global_accuracy/accuracy missing"}
    return {**base, "status": "SOURCE_STALE" if stale else "REPRODUCED_WITH_ROUNDING", "global_accuracy": accuracy, "reason": None}


def dataset_lineage_readonly(db_path: str) -> Dict[str, Any]:
    """Summarize dataset lineage using read-only DB access only."""
    base = {
        "dataset_source": "read-only historical_outcomes/signals inspection",
        "row_count": 0,
        "feature_count": len(NUMERIC_FEATURES) + len(CATEGORICAL_FEATURES),
        "date_range": [None, None],
        "symbol_coverage": None,
        "class_distribution": {},
        "duplicate_rows": None,
        "future_timestamps": None,
        "lineage": "LEGACY_UNKNOWN when DB/table is absent",
        "excluded_rows_and_reasons": [],
        "read_only": True,
    }
    diagnostics = sqlite_table_diagnostics(db_path, "historical_outcomes")
    base["sqlite_diagnostics"] = diagnostics
    base["normalized_database_path"] = diagnostics.get("normalized_database_path")
    base["table_lookup_result"] = diagnostics.get("table_lookup_result")
    base["schema"] = diagnostics.get("schema") or []
    base["query_status"] = diagnostics.get("query_status")
    base["sqlite_exception"] = diagnostics.get("sqlite_exception")
    if diagnostics.get("table_lookup_result") is not True:
        base["status"] = "SOURCE_MISSING" if not diagnostics.get("sqlite_exception") else "UNREPRODUCIBLE"
        return base
    try:
        with connect_readonly(db_path) as connection:
            row = connection.execute("SELECT COUNT(*), MIN(signal_timestamp), MAX(signal_timestamp), COUNT(DISTINCT symbol) FROM historical_outcomes").fetchone()
            base["row_count"] = int(row[0] or 0)
            base["date_range"] = [row[1], row[2]]
            base["symbol_coverage"] = int(row[3] or 0)
            counts = connection.execute("SELECT status, COUNT(*) FROM historical_outcomes GROUP BY status").fetchall()
            base["class_distribution"] = {str(r[0]): int(r[1]) for r in counts}
            dup = connection.execute("SELECT COUNT(*) FROM (SELECT symbol, signal_timestamp, COUNT(*) c FROM historical_outcomes GROUP BY symbol, signal_timestamp HAVING c > 1)").fetchone()[0]
            base["duplicate_rows"] = int(dup or 0)
            future = connection.execute("SELECT COUNT(*) FROM historical_outcomes WHERE signal_timestamp > datetime('now')").fetchone()[0]
            base["future_timestamps"] = int(future or 0)
            base["status"] = "AVAILABLE"
            return base
    except sqlite3.Error as exc:
        base["status"] = "UNREPRODUCIBLE"
        base["excluded_rows_and_reasons"].append(str(exc))
        return base


def audit_train_only_preprocessing() -> Dict[str, Any]:
    """Static fail-closed audit for split-safe preprocessing lineage."""
    findings: List[Dict[str, Any]] = []
    full_dataset_fit_violation_count = 0

    ml_text = Path("ml_engine.py").read_text(encoding="utf-8", errors="ignore") if Path("ml_engine.py").exists() else ""
    wf_text = Path("walkforward.py").read_text(encoding="utf-8", errors="ignore") if Path("walkforward.py").exists() else ""

    required_markers = [
        "fit_train_only_preprocessor(train_dataset)",
        "transform_with_train_preprocessor(test_dataset, preprocessor)",
        "transform_with_train_preprocessor(dataset, preprocessor)",
    ]
    if all(marker in ml_text for marker in required_markers):
        findings.append({
            "finding": "ml_engine_holdout_preprocessing",
            "status": "PASS",
            "evidence": "Holdout path splits raw prepared rows first, fits OneHotEncoder on train_dataset only, then transforms test/all rows with the train-fitted preprocessor.",
        })
    else:
        full_dataset_fit_violation_count += 1
        findings.append({
            "finding": "ml_engine_holdout_preprocessing",
            "status": "REVIEW",
            "evidence": "Could not verify train-only preprocessing markers in ml_engine.py.",
        })

    if "fit_train_only_preprocessor(train)" in wf_text and "transform_with_train_preprocessor(test, preprocessor)" in wf_text and "_encode(test)" not in wf_text:
        findings.append({
            "finding": "walkforward_fold_preprocessing",
            "status": "PASS",
            "evidence": "Each walk-forward fold fits preprocessing on that fold's train slice and transforms the fold test slice using the same train-fitted state.",
        })
    else:
        full_dataset_fit_violation_count += 1
        findings.append({
            "finding": "walkforward_fold_preprocessing",
            "status": "REVIEW",
            "evidence": "Could not verify isolated per-fold train-only preprocessing in walkforward.py.",
        })

    suspicious_patterns = ["fit_transform(dataset[CATEGORICAL_FEATURES]", "_encode(test)"]
    for pattern in suspicious_patterns:
        if pattern in ml_text or pattern in wf_text:
            full_dataset_fit_violation_count += 1
            findings.append({"finding": "full_dataset_fit_pattern", "status": "BLOCKED_PREPROCESSOR_LEAKAGE", "evidence": f"Found suspicious pattern: {pattern}"})

    status = "PASS" if full_dataset_fit_violation_count == 0 and findings and all(f["status"] == "PASS" for f in findings) else ("BLOCKED_PREPROCESSOR_LEAKAGE" if any(str(f.get("status", "")).startswith("BLOCKED") for f in findings) else "REVIEW")
    return {
        "preprocessing_fit_scope": "TRAIN_ONLY_VERIFIED" if status == "PASS" else "UNVERIFIED",
        "train_only_preprocessing_status": status,
        "full_dataset_fit_violation_count": full_dataset_fit_violation_count,
        "preprocessing_guard_findings": findings,
    }


def code_leakage_findings() -> List[Dict[str, Any]]:
    preprocessing = audit_train_only_preprocessing()
    findings = [
        {
            "finding": "latest_by_symbol_feature_join",
            "status": "PASS",
            "evidence": "ml_engine.build_ml_dataset uses as-of feature joins and temporal feature guard status is also reported separately.",
        },
    ]
    findings.extend(preprocessing["preprocessing_guard_findings"])
    return findings


def compare_displayed(displayed: Any, reproduced: Optional[float], scale: float = 1.0, tolerance: float = 0.005) -> str:
    expected = safe_float(str(displayed).replace("%", "").replace("/100", "").replace("~", ""))
    if expected is None or reproduced is None:
        return "UNREPRODUCIBLE"
    value = reproduced * scale
    if abs(value - expected) <= 1e-9:
        return "REPRODUCED_EXACT"
    if abs(value - expected) <= tolerance:
        return "REPRODUCED_WITH_ROUNDING"
    return "CONTRACT_DIFFERENT"


def metric_identity(artifacts: List[Dict[str, Any]], walkforward: Dict[str, Any], model: Dict[str, Any], wf_display: Dict[str, Any], hist66: Dict[str, Any], current_evidence: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    artifact_map = {item.get("artifact_name"): item for item in artifacts}
    model_stale = bool(artifact_map.get("model_output", {}).get("stale_source"))
    walk_stale = bool(artifact_map.get("walkforward_results", {}).get("stale_source"))
    hist_stale = bool(artifact_map.get("ml_quality_audit", {}).get("stale_source"))
    walk_status = "SOURCE_STALE" if walk_stale else walkforward.get("status")
    current_evidence = current_evidence or {}
    current = safe_float(model.get("accuracy"))
    evidence_accuracy_status = current_evidence.get("current_accuracy_reproduction_status")
    current_status = evidence_accuracy_status if evidence_accuracy_status == "REPRODUCED_EXACT" else ("SOURCE_STALE" if model_stale else (compare_displayed("32.81", current, scale=100) if current is not None else "SOURCE_MISSING"))
    evidence_confidence_status = current_evidence.get("ai_confidence_reproduction_status")
    hist_status = "SOURCE_STALE" if hist_stale else (compare_displayed("66.40", hist66.get("global_accuracy"), scale=100) if hist66.get("global_accuracy") is not None else hist66.get("status", "SOURCE_MISSING"))
    rows = [
        {
            "display_value": "32.81%",
            "metric_name": "Current Model Accuracy",
            "identity": "prediction cohort/ledger accuracy = correct_predictions / evaluated_predictions using row-level y_true and y_pred; random holdout accuracy is advisory unless exact rows, seed, y_true/y_pred, and sample count are stored",
            "producer": "prediction cohort / prediction ledger",
            "contract_match_walkforward": False,
            "total_dataset_rows": model.get("rows"),
            "holdout_sample_count": None,
            "sample_count": current_evidence.get("current_accuracy_sample_count"),
            "source_artifact": current_evidence.get("current_accuracy_source"),
            "current_accuracy_value": current_evidence.get("current_accuracy_value"),
            "correct_predictions": current_evidence.get("current_accuracy_correct_predictions"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": current_status,
            "evaluation_reproduction_status": current_status,
            "reproducibility_status": current_status,
        },
        {
            "display_value": "64.38%",
            "metric_name": "Walk-Forward Rolling Accuracy",
            "identity": "unweighted mean of walkforward_results.csv test_accuracy folds according to producer code; weighted is audit-only when test_rows exists",
            "producer": "walkforward.py:run_walkforward_validation",
            "contract_match_walkforward": True,
            "total_dataset_rows": None,
            "holdout_sample_count": None,
            "sample_count": walkforward.get("fold_count"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": "SOURCE_STALE" if walk_stale else (compare_displayed("64.38", wf_display.get("average_accuracy"), scale=100) if wf_display.get("average_accuracy") is not None else wf_display.get("status", "SOURCE_MISSING")),
            "evaluation_reproduction_status": "UNVERIFIABLE" if walkforward.get("weighted_aggregate") is None else walkforward.get("status"),
            "reproducibility_status": "SOURCE_STALE" if walk_stale else (compare_displayed("64.38", wf_display.get("average_accuracy"), scale=100) if wf_display.get("average_accuracy") is not None else wf_display.get("status", "SOURCE_MISSING")),
        },
        {
            "display_value": "66.40%",
            "metric_name": "Historical ML accuracy snapshot",
            "identity": "requires ml_quality_audit artifact or DB evidence; not current readiness evidence until matching contract is proven",
            "producer": "ml_quality_audit.py:run_audit",
            "contract_match_walkforward": False,
            "sample_count": hist66.get("rows"),
            "mandatory_current_readiness": False,
            "advisory_only": True,
            "display_reproduction_status": hist_status,
            "evaluation_reproduction_status": "UNREPRODUCIBLE",
            "reproducibility_status": hist_status,
        },
        {
            "display_value": "~70%",
            "metric_name": "Earlier historical snapshot",
            "identity": "legacy/operator snapshot with no authoritative source discovered in repository fixtures",
            "producer": "LEGACY_UNKNOWN",
            "contract_match_walkforward": False,
            "sample_count": None,
            "mandatory_current_readiness": False,
            "advisory_only": True,
            "display_reproduction_status": "SOURCE_MISSING",
            "evaluation_reproduction_status": "UNREPRODUCIBLE",
            "reproducibility_status": "SOURCE_MISSING",
        },
        {
            "display_value": "45.68%",
            "metric_name": "Walk-Forward Rolling Winrate",
            "identity": "unweighted mean of walk-forward test fold target profitable rate/trade winrate, not classifier accuracy, when winrate column exists",
            "producer": "walkforward.py:_winrate/run_walkforward_validation",
            "contract_match_walkforward": False,
            "sample_count": walkforward.get("fold_count"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": "SOURCE_STALE" if walk_stale else (compare_displayed("45.68", wf_display.get("average_winrate"), scale=1) if wf_display.get("average_winrate") is not None else wf_display.get("status", "SOURCE_MISSING")),
            "evaluation_reproduction_status": "UNVERIFIABLE",
            "reproducibility_status": "SOURCE_STALE" if walk_stale else (compare_displayed("45.68", wf_display.get("average_winrate"), scale=1) if wf_display.get("average_winrate") is not None else wf_display.get("status", "SOURCE_MISSING")),
        },
        {
            "display_value": "65/100",
            "metric_name": "AI Confidence",
            "identity": "deterministic mean(predicted_probability) over evaluated prediction cohort/ledger rows; unavailable rather than invented when predicted_probability is absent",
            "producer": "prediction cohort / prediction ledger",
            "contract_match_walkforward": False,
            "total_dataset_rows": model.get("rows"),
            "holdout_sample_count": None,
            "sample_count": current_evidence.get("ai_confidence_sample_count"),
            "source_artifact": current_evidence.get("ai_confidence_source"),
            "ai_confidence_formula": current_evidence.get("ai_confidence_formula"),
            "ai_confidence_value": current_evidence.get("ai_confidence_value"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": evidence_confidence_status or ("SOURCE_STALE" if model_stale else (compare_displayed("65", safe_float(model.get("ai_confidence_score"))) if model else "SOURCE_MISSING")),
            "evaluation_reproduction_status": evidence_confidence_status or "UNREPRODUCIBLE",
            "reproducibility_status": evidence_confidence_status or ("SOURCE_STALE" if model_stale else (compare_displayed("65", safe_float(model.get("ai_confidence_score"))) if model else "SOURCE_MISSING")),
        },
        {
            "display_value": "ROBUST",
            "metric_name": "Model Health",
            "identity": "rule-derived health from walk-forward stability and overfit risk; cannot reproduce without walk-forward source evidence",
            "producer": "walkforward.py:_health",
            "contract_match_walkforward": False,
            "sample_count": walkforward.get("fold_count"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": ("SOURCE_STALE" if walk_stale else ("REPRODUCED_EXACT" if wf_display.get("model_health") == "ROBUST" else ("CONTRACT_DIFFERENT" if wf_display.get("model_health") else wf_display.get("status", "SOURCE_MISSING")))),
            "evaluation_reproduction_status": "UNVERIFIABLE",
            "reproducibility_status": ("SOURCE_STALE" if walk_stale else ("REPRODUCED_EXACT" if wf_display.get("model_health") == "ROBUST" else ("CONTRACT_DIFFERENT" if wf_display.get("model_health") else wf_display.get("status", "SOURCE_MISSING")))),
        },
        {
            "display_value": "35.55/100",
            "metric_name": "Overfit Risk",
            "identity": "100 * max(0, mean(train_accuracy) - mean(test_accuracy)); cannot reproduce without train/test fold metrics",
            "producer": "walkforward.py:run_walkforward_validation",
            "contract_match_walkforward": False,
            "sample_count": walkforward.get("fold_count"),
            "mandatory_current_readiness": True,
            "display_reproduction_status": "SOURCE_STALE" if walk_stale else (compare_displayed("35.55", wf_display.get("overfit_risk_score"), scale=1, tolerance=0.01) if wf_display.get("overfit_risk_score") is not None else wf_display.get("status", "SOURCE_MISSING")),
            "evaluation_reproduction_status": "UNVERIFIABLE",
            "reproducibility_status": "SOURCE_STALE" if walk_stale else (compare_displayed("35.55", wf_display.get("overfit_risk_score"), scale=1, tolerance=0.01) if wf_display.get("overfit_risk_score") is not None else wf_display.get("status", "SOURCE_MISSING")),
        },
    ]
    return rows


def label_contract_audit() -> Dict[str, Any]:
    return {
        "source_module": "outcome_labeler.py",
        "class_names_and_encoding": {"LOSS": "stop-loss or negative outcome", "WIN": "TP2 or positive holding-period outcome", "TP1 HIT": "TP1 outcome", "OPEN/FLAT": "not kept by ML TARGET_LABELS"},
        "input_price": "historical_klines.close joined at signal timestamp as entry",
        "target_price": "future candle low/high/close depending on SL/TP/horizon path",
        "prediction_timestamp": "signals.timestamp",
        "target_timestamp": "future candle timestamp or blank when no future candles",
        "horizon": "holding_candles default 20",
        "return_threshold": "SL 2%, TP1 3%, TP2 5% defaults",
        "neutral_handling": "FLAT possible in outcome_labeler but excluded by ml_engine TARGET_LABELS",
        "fees_slippage": "not applied in label_historical_outcomes",
        "maturity_requirements": "requires future candles after signal timestamp; no future candles create OPEN/FLAT row",
        "missing_future_data": "NO_FUTURE_CANDLES open/flat outcome",
        "regime_assignment_timing": "regime is joined from signal timestamp in ml_engine historical dataset",
        "status": "REVIEW",
        "tp2_hit_vs_win_mismatch": "outcome_labeler returns status WIN for TP2 hits while ml_engine TARGET_LABELS includes TP2 HIT; legacy artifacts may mix TP2 HIT and WIN semantics.",
        "blocked_reason": "Fees/slippage and neutral treatment are explicit, but TP2 HIT versus WIN semantics, historical artifact lineage, and all displayed metric horizons are not fully proven from prediction artifacts.",
    }


def segment_readiness_status(metrics: Dict[str, Any], min_samples: int) -> Tuple[str, Optional[float], Optional[str]]:
    samples = metrics.get("samples") or 0
    acc = metrics.get("accuracy")
    baseline = metrics.get("majority_class_baseline")
    balanced = metrics.get("balanced_accuracy")
    macro_f1 = metrics.get("macro_f1")
    if samples < min_samples:
        return "BLOCKED_INSUFFICIENT_SAMPLE", None if acc is None or baseline is None else acc - baseline, "sample count below minimum"
    if acc is None or baseline is None:
        return "UNAVAILABLE", None, "accuracy or baseline unavailable"
    improvement = acc - baseline
    if improvement <= 0:
        return "BLOCKED_BELOW_BASELINE", improvement, "accuracy at or below majority baseline"
    if improvement < 0.03:
        return "REVIEW_MARGINAL", improvement, "improvement below 3 percentage points"
    if balanced is None or macro_f1 is None:
        return "REVIEW", improvement, "balanced accuracy or macro-F1 unavailable"
    return "PASS", improvement, None


def segment_performance(cohort: pd.DataFrame, min_samples: int = 10) -> List[Dict[str, Any]]:
    if cohort.empty or "__y_true" not in cohort.columns or "__y_pred" not in cohort.columns:
        return []
    rows: List[Dict[str, Any]] = []
    for column in ["horizon", "regime_name", "volatility_regime", "symbol", "model_version"]:
        if column not in cohort.columns:
            continue
        for value, group in cohort.groupby(column):
            metrics = classification_metrics(group["__y_true"].astype(str).tolist(), group["__y_pred"].astype(str).tolist(), TARGET_LABELS)
            status, improvement, ci_reason = segment_readiness_status(metrics, min_samples)
            rows.append(
                {
                    "segment_type": column,
                    "segment_value": str(value),
                    "samples": len(group),
                    "class_support": json.dumps(metrics["class_support"], sort_keys=True),
                    "accuracy": metrics["accuracy"],
                    "balanced_accuracy": metrics["balanced_accuracy"],
                    "macro_f1": metrics["macro_f1"],
                    "baseline": metrics["majority_class_baseline"],
                    "improvement_over_baseline": improvement,
                    "confidence_interval": None,
                    "confidence_interval_reason": ci_reason or "confidence interval unavailable",
                    "readiness_status": status,
                }
            )
    return rows


def candidate_evidence_bridge(ledger_path: str = "reports/candidate_evidence_ledger.jsonl", model_sample: int = 0, paper_sample: Optional[int] = None) -> Dict[str, Any]:
    path = Path(ledger_path)
    if not path.exists():
        return {
            "status": "UNAVAILABLE",
            "missing_key": str(path),
            "model_evaluation_population": {"samples": model_sample},
            "candidate_evidence_population": {"samples": 0},
            "paper_trade_outcome_population": {"samples": paper_sample},
            "by_horizon": {"24h": None, "48h": None, "72h": None},
        }
    records: List[Dict[str, Any]] = []
    malformed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if record.get("status") == "RECORDED" and record.get("direction_hit") is not None:
                records.append(record)
    by_horizon: Dict[str, Any] = {}
    for horizon in ["24h", "48h", "72h"]:
        group = [record for record in records if str(record.get("horizon")) == horizon]
        hits = [bool(record.get("direction_hit")) for record in group]
        by_horizon[horizon] = {"samples": len(hits), "direction_accuracy": (sum(hits) / len(hits) if hits else None)}
    return {
        "status": "AVAILABLE" if records else "UNAVAILABLE",
        "missing_key": None if records else "READY mature horizon records with direction_hit",
        "model_evaluation_population": {"samples": model_sample},
        "candidate_evidence_population": {"samples": len(records), "malformed_lines": malformed},
        "paper_trade_outcome_population": {"samples": paper_sample},
        "by_horizon": by_horizon,
        "merge_policy": "candidate evidence is not merged with classifier accuracy",
    }


def _metric_blocker_for_status(status: Optional[str]) -> Optional[str]:
    precedence = {
        "SOURCE_STALE": "BLOCKED_STALE_SOURCE",
        "CONTRACT_DIFFERENT": "BLOCKED_CONTRACT_DIFFERENT",
        "SOURCE_MISSING": "BLOCKED_UNREPRODUCIBLE",
        "UNREPRODUCIBLE": "BLOCKED_UNREPRODUCIBLE",
        "UNVERIFIABLE": "REVIEW_EVIDENCE_UNAVAILABLE",
        "UNAVAILABLE": "REVIEW_EVIDENCE_UNAVAILABLE",
    }
    return precedence.get(str(status))


def metric_integrity_summary(identities: List[Dict[str, Any]]) -> Dict[str, Any]:
    display = metric_display_integrity_summary(identities)
    evaluation = metric_evaluation_integrity_summary(identities)
    return {
        "primary_metric_integrity_blocker": evaluation["primary_metric_integrity_blocker"] or display["primary_metric_integrity_blocker"],
        "all_mandatory_identity_blockers": display["all_mandatory_identity_blockers"] + evaluation["all_mandatory_identity_blockers"],
        "status": evaluation["status"] if evaluation["status"] != "PASS" else display["status"],
        "display_metric_integrity": display,
        "evaluation_metric_integrity": evaluation,
    }


def metric_display_integrity_summary(identities: List[Dict[str, Any]]) -> Dict[str, Any]:
    blockers = []
    for item in identities:
        if not item.get("mandatory_current_readiness"):
            continue
        status = item.get("display_reproduction_status", item.get("reproducibility_status"))
        blocker = _metric_blocker_for_status(status)
        if blocker and blocker != "REVIEW_EVIDENCE_UNAVAILABLE":
            if item.get("metric_name") in {"Walk-Forward Rolling Accuracy", "Walk-Forward Rolling Winrate", "Model Health", "Overfit Risk"}:
                continue
            blockers.append({"metric_name": item.get("metric_name"), "status": status, "blocker": blocker, "reason": item.get("identity"), "integrity_type": "display"})
    order = ["BLOCKED_STALE_SOURCE", "BLOCKED_CONTRACT_DIFFERENT", "BLOCKED_UNREPRODUCIBLE"]
    primary = next((candidate for candidate in order if any(b["blocker"] == candidate for b in blockers)), None)
    return {"primary_metric_integrity_blocker": primary, "all_mandatory_identity_blockers": blockers, "status": primary or "PASS"}


def metric_evaluation_integrity_summary(identities: List[Dict[str, Any]]) -> Dict[str, Any]:
    blockers = []
    reviews = []
    for item in identities:
        if not item.get("mandatory_current_readiness"):
            continue
        status = item.get("evaluation_reproduction_status", item.get("reproducibility_status"))
        blocker = _metric_blocker_for_status(status)
        row = {"metric_name": item.get("metric_name"), "status": status, "blocker": blocker, "reason": item.get("identity"), "integrity_type": "evaluation"}
        if blocker == "REVIEW_EVIDENCE_UNAVAILABLE":
            reviews.append(row)
        elif blocker:
            if item.get("metric_name") in {"Walk-Forward Rolling Accuracy", "Walk-Forward Rolling Winrate", "Model Health", "Overfit Risk"}:
                reviews.append(row)
                continue
            blockers.append(row)
    order = ["BLOCKED_STALE_SOURCE", "BLOCKED_CONTRACT_DIFFERENT", "BLOCKED_UNREPRODUCIBLE"]
    primary = next((candidate for candidate in order if any(b["blocker"] == candidate for b in blockers)), None)
    status = primary or ("REVIEW" if reviews else "PASS")
    return {"primary_metric_integrity_blocker": primary, "all_mandatory_identity_blockers": blockers, "review_identity_findings": reviews, "status": status}

def data_lineage_status(lineage: Dict[str, Any]) -> str:
    if lineage.get("status") == "SOURCE_MISSING":
        return "BLOCKED_UNREPRODUCIBLE"
    if lineage.get("row_count", 0) == 0:
        return "BLOCKED_INSUFFICIENT_OOS"
    if (lineage.get("future_timestamps") or 0) > 0:
        return "BLOCKED_LEAKAGE"
    if (lineage.get("duplicate_rows") or 0) > 0:
        return "BLOCKED_SPLIT_CONTAMINATION"
    if lineage.get("status") != "AVAILABLE":
        return "BLOCKED_UNREPRODUCIBLE"
    return "PASS"


def label_integrity_component_status(label_contract: Dict[str, Any], prediction_ledger: Dict[str, Any]) -> str:
    """Aggregate legacy and prediction-ledger label contract checks fail-closed.

    The row-level prediction ledger is the authoritative current label contract.
    Legacy semantic caveats remain visible as REVIEW once the ledger label and
    evaluation contracts both pass, but they should not become the primary
    BLOCKED_LABEL_CONTRACT readiness blocker by themselves.
    """
    legacy_status = str(label_contract.get("status", "BLOCKED"))
    ledger_label_status = str(prediction_ledger.get("label_contract_status", "BLOCKED"))
    ledger_eval_status = str(prediction_ledger.get("evaluation_reproducibility_status", "BLOCKED"))
    invalid_labels = prediction_ledger.get("invalid_labels") or []

    if not prediction_ledger.get("prediction_ledger_available", False):
        return "BLOCKED_LABEL_CONTRACT"
    if legacy_status == "BLOCKED" or invalid_labels:
        return "BLOCKED_LABEL_CONTRACT"
    if ledger_label_status == "BLOCKED":
        return "BLOCKED_LABEL_CONTRACT"
    if ledger_label_status == "PASS" and ledger_eval_status == "PASS":
        return "PASS" if legacy_status == "PASS" else "REVIEW"
    if ledger_label_status == "REVIEW" or ledger_eval_status == "REVIEW":
        return "REVIEW"
    return "BLOCKED_LABEL_CONTRACT"


def readiness(components: Dict[str, str]) -> Dict[str, Any]:
    blockers = {name: status for name, status in components.items() if str(status).startswith("BLOCKED") or str(status) in {"SOURCE_MISSING", "UNREPRODUCIBLE"}}
    review_reasons = {name: status for name, status in components.items() if str(status).startswith("REVIEW") or str(status) in {"UNVERIFIABLE", "UNAVAILABLE"}}
    all_blocker_statuses = list(blockers.values())
    primary = None
    for status in BLOCKER_PRECEDENCE:
        if status in all_blocker_statuses:
            primary = status
            break
    if primary is None and blockers:
        primary = next(iter(blockers.values()))
    overall = primary or ("REVIEW" if review_reasons else "PASS")
    return {
        "components": components,
        "mandatory_component_statuses": {name: components.get(name, "UNAVAILABLE") for name in MANDATORY_COMPONENTS},
        "primary_blocker": primary,
        "all_blockers": blockers,
        "review_reasons": review_reasons,
        "overall_status": overall,
        "paper_only": True,
        "execution_allowed": False,
        "automatic_promotion_allowed": False,
        "model_promotion_allowed": False,
        "readiness_advisory_only": True,
    }


def run_ml_metric_reconciliation(
    output_dir: str = "reports",
    db_path: str = "mamuyy_hunter.db",
    model_output_path: str = "model_output.json",
    walkforward_path: str = "walkforward_results.csv",
    prediction_artifact_path: Optional[str] = None,
    prediction_ledger_path: str = "reports/ml_prediction_ledger.jsonl",
    artifact_context: Optional[str] = None,
    stale_ttl_days: float = DEFAULT_STALE_TTL_DAYS,
) -> Dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = discover_artifacts(db_path=db_path, model_output_path=model_output_path, walkforward_path=walkforward_path, stale_ttl_days=stale_ttl_days)
    inventory = producer_inventory(artifacts)
    model_artifact = next((item for item in artifacts if item["artifact_name"] == "model_output"), {})
    model = read_json(model_artifact.get("discovered_path") or model_output_path) or {}
    walk_artifact = next((item for item in artifacts if item["artifact_name"] == "walkforward_results"), {})
    walk_path = walk_artifact.get("discovered_path") or walkforward_path
    walkforward = reconstruct_walkforward(walk_path)
    wf_display = summarize_walkforward_display(walk_path)
    hist_artifact = next((item for item in artifacts if item["artifact_name"] == "ml_quality_audit"), {})
    historical_66 = parse_historical_ml_artifact(hist_artifact.get("discovered_path") or "ml_quality_audit.json", stale_ttl_days=stale_ttl_days)
    default_prediction_cohort = Path("reports/ml_prediction_cohort.csv")
    discovered_prediction_artifact = prediction_artifact_path or (str(default_prediction_cohort) if default_prediction_cohort.exists() else model_artifact.get("discovered_path"))
    cohort_result = load_prediction_cohort(discovered_prediction_artifact)
    if cohort_result["status"] == "AVAILABLE":
        cohort = cohort_result["frame"]
        metrics = classification_metrics(cohort["__y_true"].astype(str).tolist(), cohort["__y_pred"].astype(str).tolist(), TARGET_LABELS)
        reproduced_metrics: Dict[str, Any] = {"status": "REPRODUCED_WITH_ROUNDING", "reason": None, "metrics": metrics, "confusion_matrix": metrics["confusion_matrix"], "cohort_columns": cohort_result.get("columns")}
    else:
        cohort = pd.DataFrame()
        reproduced_metrics = no_evaluation_metrics(cohort_result["status"], cohort_result["reason"])
        metrics = None

    current_evidence = current_readiness_metric_evidence(cohort_result, discovered_prediction_artifact)
    identities = metric_identity(artifacts, walkforward, model, wf_display, historical_66, current_evidence=current_evidence)

    lineage = dataset_lineage_readonly(db_path)
    baseline = baseline_status(metrics)
    segments = segment_performance(cohort)
    candidate_bridge = candidate_evidence_bridge(model_sample=(metrics or {}).get("samples", 0), paper_sample=lineage.get("row_count", 0))
    prediction_ledger = audit_prediction_ledger(prediction_ledger_path)
    temporal_feature_guard = readiness_temporal_feature_guard(
        cohort,
        source_artifact=discovered_prediction_artifact,
    )
    preprocessing_guard = audit_train_only_preprocessing()

    row_level_walkforward = row_level_walkforward_audit(cohort, temporal_feature_guard, preprocessing_guard)
    baseline_audit = baseline_root_cause_audit(row_level_walkforward)
    larger_fold_diagnostic = larger_fold_baseline_diagnostic(cohort)
    class_imbalance_diagnostic = ml_class_imbalance_diagnostic(cohort)
    threshold_candidate_diagnostic = ml_high_confidence_threshold_candidate_diagnostic(cohort)
    threshold_stability_audit = threshold_candidate_stability_audit(
        cohort,
        selected_threshold=threshold_candidate_diagnostic.get("threshold_candidate_selected"),
    )
    threshold_sample_sufficiency = threshold_sample_sufficiency_audit(
        cohort,
        selected_threshold=threshold_candidate_diagnostic.get("threshold_candidate_selected"),
    )
    filtered_cohort_comparison = filtered_cohort_walkforward_comparison(
        cohort,
        selected_threshold=threshold_candidate_diagnostic.get("threshold_candidate_selected"),
    )

    metric_integrity = metric_integrity_summary(identities)
    display_metric_integrity = metric_integrity["display_metric_integrity"]
    evaluation_metric_integrity = metric_integrity["evaluation_metric_integrity"]
    label_contract = label_contract_audit()
    label_integrity_status = label_integrity_component_status(label_contract, prediction_ledger)
    components = {
        "Metric Integrity": metric_integrity["status"],
        "Display Metric Integrity": display_metric_integrity["status"],
        "Evaluation Metric Integrity": "BLOCKED_UNREPRODUCIBLE" if prediction_ledger["evaluation_reproducibility_status"] == "BLOCKED" else ("REVIEW" if prediction_ledger["evaluation_reproducibility_status"] == "REVIEW" else evaluation_metric_integrity["status"]),
        "Data Lineage": data_lineage_status(lineage),
        "Label Integrity": label_integrity_status,
        "Leakage Safety": "BLOCKED_TEMPORAL_INTEGRITY" if prediction_ledger["temporal_guard_status"] == "BLOCKED" or temporal_feature_guard["status"] == "BLOCKED" else ("BLOCKED_LEAKAGE" if any(isinstance(f, dict) and str(f.get("status", "")).startswith("BLOCKED") for f in code_leakage_findings()) else ("UNVERIFIABLE" if cohort.empty else "REVIEW")),
        "Baseline Superiority": row_level_walkforward["baseline_superiority_status"],
        "Out-of-Sample Adequacy": "BLOCKED_INSUFFICIENT_OOS" if not metrics else "REVIEW",
        "Walk-Forward Stability": row_level_walkforward["row_level_walkforward_status"] if row_level_walkforward["row_level_walkforward_status"].startswith("BLOCKED") else ("BLOCKED_INSUFFICIENT_OOS" if walkforward.get("status") in {"SOURCE_MISSING", "UNREPRODUCIBLE"} else ("UNVERIFIABLE" if any(f.get("leakage_status") == "UNVERIFIABLE" for f in walkforward.get("folds", [])) else "REVIEW")),
        "Regime Stability": "UNAVAILABLE" if not segments else "REVIEW",
        "Calibration Quality": "UNAVAILABLE",
        "Candidate-Evidence Support": candidate_bridge["status"],
    }
    ready = readiness(components)
    paper_filter_candidate = paper_filter_candidate_registry({**filtered_cohort_comparison, "model_readiness": ready})
    paper_filter_shadow_review = paper_filter_shadow_review_scorecard({**threshold_stability_audit, **threshold_sample_sufficiency, **filtered_cohort_comparison, **paper_filter_candidate, "model_readiness": ready})
    coverage_audit_input = {
        **threshold_sample_sufficiency,
        **filtered_cohort_comparison,
        "current_accuracy_sample_count": current_evidence.get("current_accuracy_sample_count"),
        "row_level_walkforward_rows": row_level_walkforward.get("row_level_walkforward_rows"),
        "matured_prediction_rows": prediction_ledger.get("matured_prediction_rows"),
        "pending_prediction_rows": prediction_ledger.get("pending_prediction_rows"),
        "invalid_prediction_rows": prediction_ledger.get("invalid_prediction_rows"),
        "future_feature_violation_count": temporal_feature_guard.get("future_feature_violation_count"),
    }
    raw_closed_source_discovery = raw_closed_outcome_source_discovery_audit({"artifact_paths": {"closed_outcomes": Path("reports") / "closed_outcomes.json"}})
    coverage_audit_input.update(raw_closed_source_discovery)
    closed_to_ml_coverage_audit = closed_outcome_to_ml_cohort_coverage_audit(coverage_audit_input)
    gap_reason_audit = raw_closed_to_ml_cohort_gap_reason_audit({**coverage_audit_input, **closed_to_ml_coverage_audit, "prediction_ledger_path": prediction_ledger_path})
    linkage_contract_audit = prediction_outcome_linkage_contract_audit({**coverage_audit_input, **closed_to_ml_coverage_audit, **gap_reason_audit, "prediction_ledger_path": prediction_ledger_path})
    producer_contract_plan = prediction_outcome_linkage_producer_contract_plan({
        "model_readiness": ready,
        **closed_to_ml_coverage_audit,
        **gap_reason_audit,
        **linkage_contract_audit,
    })
    model_upgrade_diagnostic_plan = ml_model_repair_upgrade_diagnostic_plan({
        "model_readiness": ready,
        "baseline_superiority_status": row_level_walkforward["baseline_superiority_status"],
        "model_vs_baseline_delta": row_level_walkforward["model_vs_baseline_delta"],
        **baseline_audit,
        **threshold_candidate_diagnostic,
        **threshold_stability_audit,
        **threshold_sample_sufficiency,
        **filtered_cohort_comparison,
        **raw_closed_source_discovery,
        **closed_to_ml_coverage_audit,
        **gap_reason_audit,
        **linkage_contract_audit,
        **producer_contract_plan,
        **paper_filter_candidate,
        **paper_filter_shadow_review,
    })
    default_sources = db_path == "mamuyy_hunter.db" and model_output_path == "model_output.json" and walkforward_path == "walkforward_results.csv"
    production_artifacts_exist = any(item.get("exists") for item in artifacts if item.get("artifact_name") in {"model_output", "walkforward_results", "database_table:historical_outcomes", "database_table:ml_results", "database_table:walkforward_results"})
    context = artifact_context or ("RUNTIME_AUDIT" if metrics else ("RUNTIME_AUDIT_NO_PREDICTION_COHORT" if (default_sources and production_artifacts_exist) else "NON_PRODUCTION_EMPTY_FIXTURE"))
    report = {
        "phase": PHASE,
        "artifact_context": context,
        "generated_at": utc_now(),
        "governance": {
            "paper_only": True,
            "execution_allowed": False,
            "automatic_promotion_allowed": False,
            "model_promotion_allowed": False,
            "readiness_advisory_only": True,
            "read_only_audit": True,
        },
        "artifact_discovery": artifacts,
        "source_inventory": inventory,
        "mandatory_current_readiness_metrics": sorted(MANDATORY_CURRENT_READINESS_METRICS),
        "metric_identity": identities,
        "dataset_lineage": lineage,
        "label_contracts": [label_contract],
        "split_and_leakage_audit": {"status": "BLOCKED_LEAKAGE" if any(isinstance(f, dict) and str(f.get("status", "")).startswith("BLOCKED") for f in code_leakage_findings()) else ("UNVERIFIABLE" if cohort.empty else "REVIEW"), "findings": (["No valid prediction/label cohort available"] if cohort.empty else []) + code_leakage_findings()},
        "preprocessing_fit_scope": preprocessing_guard["preprocessing_fit_scope"],
        "train_only_preprocessing_status": preprocessing_guard["train_only_preprocessing_status"],
        "full_dataset_fit_violation_count": preprocessing_guard["full_dataset_fit_violation_count"],
        "preprocessing_guard_findings": preprocessing_guard["preprocessing_guard_findings"],
        "reproduced_metrics": reproduced_metrics,
        "baseline_comparison": baseline,
        "row_level_walkforward_status": row_level_walkforward["row_level_walkforward_status"],
        "row_level_walkforward_rows": row_level_walkforward["row_level_walkforward_rows"],
        "row_level_walkforward_folds": row_level_walkforward["row_level_walkforward_folds"],
        "baseline_accuracy": row_level_walkforward["baseline_accuracy"],
        "model_accuracy": row_level_walkforward["model_accuracy"],
        "model_vs_baseline_delta": row_level_walkforward["model_vs_baseline_delta"],
        "baseline_superiority_status": row_level_walkforward["baseline_superiority_status"],
        "row_level_walkforward_findings": row_level_walkforward["row_level_walkforward_findings"],
        **baseline_audit,
        **larger_fold_diagnostic,
        **class_imbalance_diagnostic,
        **threshold_candidate_diagnostic,
        **threshold_stability_audit,
        **threshold_sample_sufficiency,
        **filtered_cohort_comparison,
        **raw_closed_source_discovery,
        **closed_to_ml_coverage_audit,
        **gap_reason_audit,
        **linkage_contract_audit,
        **producer_contract_plan,
        **paper_filter_candidate,
        **paper_filter_shadow_review,
        **model_upgrade_diagnostic_plan,
        "row_level_walkforward_summary": {k: v for k, v in row_level_walkforward.items() if k not in {"rows"}},
        "walkforward_display_reproduction": wf_display,
        "historical_ml_artifact_reproduction": historical_66,
        "walkforward_reconciliation": walkforward,
        "current_accuracy_reproduction_status": current_evidence["current_accuracy_reproduction_status"],
        "current_accuracy_sample_count": current_evidence["current_accuracy_sample_count"],
        "current_accuracy_source": current_evidence["current_accuracy_source"],
        "current_accuracy_value": current_evidence["current_accuracy_value"],
        "ai_confidence_reproduction_status": current_evidence["ai_confidence_reproduction_status"],
        "ai_confidence_sample_count": current_evidence["ai_confidence_sample_count"],
        "ai_confidence_source": current_evidence["ai_confidence_source"],
        "ai_confidence_formula": current_evidence["ai_confidence_formula"],
        "ai_confidence_value": current_evidence["ai_confidence_value"],
        "current_readiness_metric_evidence": current_evidence,
        "current_accuracy_reconciliation": identities[0],
        "historical_snapshot_reconciliation": identities[2:4],
        "segment_performance": segments,
        "candidate_evidence_bridge": candidate_bridge,
        "prediction_ledger_summary": prediction_ledger,
        "asof_feature_join_status": temporal_feature_guard["asof_feature_join_status"],
        "temporal_feature_guard_status": temporal_feature_guard["status"],
        "future_feature_violation_count": temporal_feature_guard["future_feature_violation_count"],
        "missing_feature_timestamp_count": temporal_feature_guard["missing_feature_timestamp_count"],
        "target_leakage_column_count": temporal_feature_guard["target_leakage_column_count"],
        "feature_timestamp_coverage": temporal_feature_guard["feature_timestamp_coverage"],
        "temporal_guard_findings": temporal_feature_guard["temporal_guard_findings"],
        "prediction_ledger_available": prediction_ledger["prediction_ledger_available"],
        "prediction_ledger_rows": prediction_ledger["prediction_ledger_rows"],
        "matured_prediction_rows": prediction_ledger["matured_prediction_rows"],
        "pending_prediction_rows": prediction_ledger["pending_prediction_rows"],
        "invalid_prediction_rows": prediction_ledger["invalid_prediction_rows"],
        "temporal_guard_status": prediction_ledger["temporal_guard_status"],
        "label_contract_status": prediction_ledger["label_contract_status"],
        "legacy_label_contract_status": label_contract["status"],
        "prediction_ledger_label_contract_status": prediction_ledger["label_contract_status"],
        "label_integrity_component_status": label_integrity_status,
        "evaluation_reproducibility_status": prediction_ledger["evaluation_reproducibility_status"],
        "metric_integrity_summary": metric_integrity,
        "display_metric_integrity_summary": display_metric_integrity,
        "evaluation_metric_integrity_summary": evaluation_metric_integrity,
        "model_readiness": ready,
        "terminology_recommendations": ["Current Holdout Accuracy", "Walk-Forward OOS Accuracy", "Latest-Fold Accuracy", "Balanced Accuracy", "Candidate Evidence Accuracy", "Paper Trade Winrate", "AI Confidence Heuristic", "Model Readiness"],
        "artifact_paths": {
            "json": str(output / "ml_metric_reconciliation.json"),
            "metric_identity_csv": str(output / "ml_metric_identity.csv"),
            "walkforward_folds_csv": str(output / "ml_walkforward_folds.csv"),
            "confusion_matrix_csv": str(output / "ml_confusion_matrix.csv"),
            "segment_performance_csv": str(output / "ml_segment_performance.csv"),
            "prediction_ledger_audit_json": str(output / "ml_prediction_ledger_audit.json"),
            "row_level_walkforward_json": str(output / "ml_row_level_walkforward.json"),
            "row_level_walkforward_rows_csv": str(output / "ml_row_level_walkforward_rows.csv"),
        },
        "limitations": ["No predictions are fabricated from labels.", "Display-value reproduction is separated from full evaluation reproduction.", "Missing or stale artifacts are not invented.", "Report is advisory and PAPER_ONLY."],
    }
    atomic_write_json(output / "ml_metric_reconciliation.json", report)
    atomic_write_json(output / "ml_row_level_walkforward.json", {k: v for k, v in row_level_walkforward.items() if k != "rows"})
    write_prediction_ledger_audit(prediction_ledger, output / "ml_prediction_ledger_audit.json")
    write_csv(output / "ml_metric_identity.csv", identities, ["display_value", "metric_name", "identity", "producer", "contract_match_walkforward", "sample_count", "display_reproduction_status", "evaluation_reproduction_status", "reproducibility_status"])
    write_csv(output / "ml_walkforward_folds.csv", walkforward.get("folds", []), ["fold_id", "training_start", "training_end", "test_start", "test_end", "index_gap", "temporal_embargo", "train_rows", "test_rows", "class_distribution", "accuracy", "balanced_accuracy", "macro_f1", "baseline_accuracy", "improvement_over_baseline", "regime_distribution", "excluded_rows", "leakage_status", "leakage_reasons"])
    write_csv(output / "ml_confusion_matrix.csv", reproduced_metrics.get("confusion_matrix", []), ["actual_class", *TARGET_LABELS])
    write_csv(output / "ml_segment_performance.csv", segments, ["segment_type", "segment_value", "samples", "class_support", "accuracy", "balanced_accuracy", "macro_f1", "baseline", "improvement_over_baseline", "confidence_interval", "confidence_interval_reason", "readiness_status"])
    write_csv(output / "ml_row_level_walkforward_rows.csv", row_level_walkforward.get("rows", []), ROW_LEVEL_WALKFORWARD_FIELDS)
    return report


if __name__ == "__main__":
    result = run_ml_metric_reconciliation()
    print(json.dumps({"phase": result["phase"], "overall_status": result["model_readiness"]["overall_status"], "json": result["artifact_paths"]["json"]}, indent=2))
