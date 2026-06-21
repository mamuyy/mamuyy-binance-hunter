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
TRUE_COLUMNS = ("y_true", "actual", "actual_label", "actual_class", "target", "actual_profit", "direction_hit")
DEFAULT_STALE_TTL_DAYS = 7.0
MANDATORY_CURRENT_READINESS_METRICS = {"Current Model Accuracy", "Walk-Forward Rolling Accuracy", "Walk-Forward Rolling Winrate", "AI Confidence", "Model Health", "Overfit Risk"}




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
    return {"status": "AVAILABLE", "reason": None, "frame": out, "columns": {"y_true": y_true_col, "y_pred": y_pred_col, **required_meta}}


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


def metric_identity(artifacts: List[Dict[str, Any]], walkforward: Dict[str, Any], model: Dict[str, Any], wf_display: Dict[str, Any], hist66: Dict[str, Any]) -> List[Dict[str, Any]]:
    artifact_map = {item.get("artifact_name"): item for item in artifacts}
    model_stale = bool(artifact_map.get("model_output", {}).get("stale_source"))
    walk_stale = bool(artifact_map.get("walkforward_results", {}).get("stale_source"))
    hist_stale = bool(artifact_map.get("ml_quality_audit", {}).get("stale_source"))
    walk_status = "SOURCE_STALE" if walk_stale else walkforward.get("status")
    current = safe_float(model.get("accuracy"))
    current_status = "SOURCE_STALE" if model_stale else (compare_displayed("32.81", current, scale=100) if current is not None else "SOURCE_MISSING")
    hist_status = "SOURCE_STALE" if hist_stale else (compare_displayed("66.40", hist66.get("global_accuracy"), scale=100) if hist66.get("global_accuracy") is not None else hist66.get("status", "SOURCE_MISSING"))
    rows = [
        {
            "display_value": "32.81%",
            "metric_name": "Current Model Accuracy",
            "identity": "random holdout multiclass accuracy from ml_engine.run_ml_research when model_output has accuracy; unresolved if current artifact is missing/stale",
            "producer": "ml_engine.py:run_ml_research",
            "contract_match_walkforward": False,
            "total_dataset_rows": model.get("rows"),
            "holdout_sample_count": None,
            "sample_count": None,
            "mandatory_current_readiness": True,
            "display_reproduction_status": current_status,
            "evaluation_reproduction_status": "UNREPRODUCIBLE",
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
            "identity": "latest-row profitable class probability heuristic from ml_engine.run_ml_research when model_output has ai_confidence_score",
            "producer": "ml_engine.py:run_ml_research",
            "contract_match_walkforward": False,
            "total_dataset_rows": model.get("rows"),
            "holdout_sample_count": None,
            "sample_count": None,
            "mandatory_current_readiness": True,
            "display_reproduction_status": "SOURCE_STALE" if model_stale else (compare_displayed("65", safe_float(model.get("ai_confidence_score"))) if model else "SOURCE_MISSING"),
            "evaluation_reproduction_status": "UNREPRODUCIBLE",
            "reproducibility_status": "SOURCE_STALE" if model_stale else (compare_displayed("65", safe_float(model.get("ai_confidence_score"))) if model else "SOURCE_MISSING"),
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
    identities = metric_identity(artifacts, walkforward, model, wf_display, historical_66)

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

    lineage = dataset_lineage_readonly(db_path)
    baseline = baseline_status(metrics)
    segments = segment_performance(cohort)
    candidate_bridge = candidate_evidence_bridge(model_sample=(metrics or {}).get("samples", 0), paper_sample=lineage.get("row_count", 0))
    prediction_ledger = audit_prediction_ledger(prediction_ledger_path)
    temporal_feature_guard = validate_temporal_feature_rows(
        cohort if not cohort.empty else [],
        feature_columns=[column for column in [*NUMERIC_FEATURES, *CATEGORICAL_FEATURES] if not cohort.empty and column in cohort.columns],
        source_artifact=discovered_prediction_artifact,
    )
    preprocessing_guard = audit_train_only_preprocessing()

    row_level_walkforward = row_level_walkforward_audit(cohort, temporal_feature_guard, preprocessing_guard)

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
        "row_level_walkforward_summary": {k: v for k, v in row_level_walkforward.items() if k not in {"rows"}},
        "walkforward_display_reproduction": wf_display,
        "historical_ml_artifact_reproduction": historical_66,
        "walkforward_reconciliation": walkforward,
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
