"""Phase 9D.1C-A ML prediction ledger and label-contract foundation.

Append-only JSONL utilities for row-level prediction evidence. This module is
fail-closed and observational: it does not train models, tune thresholds, score
candidates, calculate PnL, or enable execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

LEDGER_FIELDS = [
    "prediction_id", "candidate_id", "symbol", "side", "prediction_timestamp",
    "feature_timestamp_max", "target_horizon", "target_timestamp", "target_label",
    "y_pred", "y_true", "predicted_probability", "model_version", "feature_schema_version",
    "fold_id", "train_window_start", "train_window_end", "test_window_start",
    "test_window_end", "label_source", "label_status", "evaluation_status",
    "temporal_guard_status", "created_at", "updated_at",
]

FINAL_TARGET_LABELS = {"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"}
CANONICAL_LABELS = FINAL_TARGET_LABELS | {"UNKNOWN", "PENDING"}
MATURED_LABEL_STATUSES = {"MATURED"}
PENDING_LABEL_STATUSES = {"PENDING"}
INVALID_LABEL_STATUSES = {"MISSING", "INVALID"}

WIN_STATUSES = {"WIN", "TP", "TP1", "TP1_HIT", "TP1 HIT", "TP2", "TP2_HIT", "TP2 HIT", "TAKE_PROFIT", "PROFIT", "CLOSED_WIN"}
LOSS_STATUSES = {"LOSS", "SL", "STOP_LOSS", "STOP LOSS", "STOPPED", "CLOSED_LOSS", "LIQUIDATED"}
BREAKEVEN_STATUSES = {"BREAKEVEN", "BREAK_EVEN", "BE", "FLAT"}
PENDING_STATUSES = {"PENDING", "OPEN", "ACTIVE", "UNMATURED", "NOT_MATURED", ""}
NEUTRAL_STATUSES = {"NEUTRAL", "EXPIRED", "TIMEOUT", "NO_HIT", "CLOSED_FLAT"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: Any) -> Optional[pd.Timestamp]:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(parsed) else parsed


def canonical_ml_label(status: Any) -> str:
    """Map paper/outcome status into the canonical ML label contract."""
    normalized = str(status or "").strip().upper().replace("-", "_")
    spaced = normalized.replace("_", " ")
    candidates = {normalized, spaced}
    if candidates & WIN_STATUSES:
        return "WIN"
    if candidates & LOSS_STATUSES:
        return "LOSS"
    if candidates & BREAKEVEN_STATUSES:
        return "BREAKEVEN"
    if candidates & NEUTRAL_STATUSES:
        return "NEUTRAL"
    if candidates & PENDING_STATUSES:
        return "PENDING"
    return "UNKNOWN"


def label_status_for_label(label: Optional[str], target_timestamp: Any = None, as_of: Any = None) -> str:
    if label in {"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"}:
        target = parse_ts(target_timestamp)
        now = parse_ts(as_of) or pd.Timestamp.now(tz="UTC")
        return "PENDING" if target is not None and now < target else "MATURED"
    if label == "PENDING":
        return "PENDING"
    if label == "UNKNOWN":
        return "MISSING"
    return "INVALID"


def normalize_label(status: Any, target_timestamp: Any = None, as_of: Any = None) -> Dict[str, str]:
    label = canonical_ml_label(status)
    return {"canonical_label": label, "label_status": label_status_for_label(label, target_timestamp, as_of)}


def _prediction_id(row: Dict[str, Any]) -> str:
    basis = "|".join(str(row.get(k, "")) for k in ("candidate_id", "symbol", "side", "prediction_timestamp", "target_timestamp", "model_version", "fold_id"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _canonical_final_label(value: Any) -> Optional[str]:
    label = canonical_ml_label(value)
    return label if label in FINAL_TARGET_LABELS else None


def create_ledger_row(**kwargs: Any) -> Dict[str, Any]:
    now = kwargs.get("created_at") or utc_now()
    row = {field: kwargs.get(field) for field in LEDGER_FIELDS}
    row["created_at"] = row.get("created_at") or now
    row["updated_at"] = row.get("updated_at") or now
    raw_label = kwargs.get("raw_label_status", kwargs.get("paper_status", row.get("target_label", row.get("y_true"))))
    if row.get("target_label") is None and raw_label is not None:
        mapped = normalize_label(raw_label, row.get("target_timestamp"), kwargs.get("as_of"))
        row["target_label"] = _canonical_final_label(mapped["canonical_label"])
        if row.get("y_true") is None and mapped["canonical_label"] != "PENDING":
            row["y_true"] = mapped["canonical_label"]
        row["label_status"] = row.get("label_status") or mapped["label_status"]
    else:
        row["target_label"] = _canonical_final_label(row.get("target_label"))
    if row.get("y_true") is None:
        row["y_true"] = row.get("target_label")
    elif row.get("target_label") is not None and _canonical_final_label(row.get("y_true")) != row.get("target_label"):
        row["label_status"] = "INVALID"
    else:
        row["y_true"] = _canonical_final_label(row.get("y_true")) or row.get("y_true")
        row["target_label"] = row.get("target_label") or _canonical_final_label(row.get("y_true"))
    row["label_status"] = row.get("label_status") or ("PENDING" if row.get("target_label") is None else "MATURED")
    if row.get("label_status") == "MATURED" and row.get("target_label") is None:
        row["label_status"] = "MISSING"
    row["evaluation_status"] = row.get("evaluation_status") or ("READY" if row["label_status"] == "MATURED" else "PENDING")
    if row.get("evaluation_status") == "READY" and row.get("label_status") != "MATURED":
        row["evaluation_status"] = "BLOCKED_MISSING_LABEL"
    row["prediction_id"] = row.get("prediction_id") or _prediction_id(row)
    row["temporal_guard_status"] = row.get("temporal_guard_status") or ("BLOCKED" if validate_temporal_row(row, kwargs.get("as_of")) else "PASS")
    return {field: row.get(field) for field in LEDGER_FIELDS}


def ensure_prediction_ledger(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    return path


def append_prediction(path: str | Path, row: Dict[str, Any]) -> Dict[str, Any]:
    path = ensure_prediction_ledger(path)
    normalized = create_ledger_row(**row)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(normalized, sort_keys=True, ensure_ascii=False) + "\n")
    return normalized


def load_prediction_ledger(path: str | Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def validate_temporal_row(row: Dict[str, Any], as_of: Any = None) -> List[str]:
    reasons: List[str] = []
    pred = parse_ts(row.get("prediction_timestamp"))
    feature = parse_ts(row.get("feature_timestamp_max"))
    target = parse_ts(row.get("target_timestamp"))
    train_end = parse_ts(row.get("train_window_end"))
    as_ts = parse_ts(as_of) or pd.Timestamp.now(tz="UTC")
    if pred is not None and target is not None and pred > target:
        reasons.append("prediction_timestamp_after_target_timestamp")
    if feature is not None and pred is not None and feature > pred:
        reasons.append("feature_timestamp_after_prediction_timestamp")
    if train_end is not None and pred is not None and train_end >= pred:
        reasons.append("train_window_end_not_before_prediction_timestamp")
    if row.get("label_status") == "MATURED" and row.get("target_label") not in FINAL_TARGET_LABELS:
        reasons.append("matured_prediction_missing_target_label")
    if row.get("label_status") == "MATURED" and target is not None and as_ts < target:
        reasons.append("matured_label_before_target_timestamp")
    if row.get("label_status") == "PENDING" and target is not None and as_ts < target and row.get("y_true") in {"WIN", "LOSS", "BREAKEVEN", "NEUTRAL"}:
        reasons.append("pending_prediction_has_mature_label_before_horizon")
    return reasons


def audit_prediction_ledger(path: str | Path = "reports/ml_prediction_ledger.jsonl", as_of: Any = None) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"prediction_ledger_available": False, "prediction_ledger_path": str(path), "prediction_ledger_rows": 0, "matured_prediction_rows": 0, "pending_prediction_rows": 0, "invalid_prediction_rows": 0, "temporal_guard_status": "BLOCKED", "label_contract_status": "BLOCKED", "evaluation_reproducibility_status": "BLOCKED", "model_readiness_blocker": "BLOCKED_INSUFFICIENT_PREDICTION_COHORT", "findings": ["prediction ledger missing"]}
    rows = load_prediction_ledger(path)
    missing_fields = [field for field in LEDGER_FIELDS if any(field not in row for row in rows)] if rows else []
    temporal_findings = [{"prediction_id": row.get("prediction_id"), "reasons": validate_temporal_row(row, as_of)} for row in rows]
    temporal_findings = [f for f in temporal_findings if f["reasons"]]
    label_values = {row.get("target_label", row.get("y_true")) for row in rows if row.get("target_label", row.get("y_true")) is not None}
    invalid_labels = sorted(str(v) for v in label_values if v not in CANONICAL_LABELS)
    prediction_ids = [str(row.get("prediction_id")) for row in rows if row.get("prediction_id")]
    duplicate_prediction_id_count = len(prediction_ids) - len(set(prediction_ids))
    duplicate_prediction_ids = sorted([pid for pid in set(prediction_ids) if prediction_ids.count(pid) > 1])
    matured = sum(1 for row in rows if row.get("label_status") == "MATURED" and row.get("target_label") in FINAL_TARGET_LABELS)
    pending = sum(1 for row in rows if row.get("label_status") == "PENDING")
    missing_matured_labels = sum(1 for row in rows if row.get("label_status") == "MATURED" and row.get("target_label") not in FINAL_TARGET_LABELS)
    invalid = sum(1 for row in rows if row.get("label_status") in INVALID_LABEL_STATUSES) + missing_matured_labels + len(temporal_findings)
    findings = []
    if duplicate_prediction_id_count > 0:
        findings.append(f"duplicate prediction_id values detected: {duplicate_prediction_id_count}")
    temporal_status = "BLOCKED" if temporal_findings else ("PASS" if rows else "REVIEW")
    label_status = "BLOCKED" if missing_fields or invalid_labels else ("PASS" if rows else "REVIEW")
    eval_status = "PASS" if rows and matured > 0 and not temporal_findings and not invalid_labels and duplicate_prediction_id_count == 0 else ("REVIEW" if rows and duplicate_prediction_id_count == 0 else ("BLOCKED" if rows else "BLOCKED"))
    return {"prediction_ledger_available": True, "prediction_ledger_path": str(path), "prediction_ledger_rows": len(rows), "matured_prediction_rows": matured, "pending_prediction_rows": pending, "invalid_prediction_rows": invalid, "temporal_guard_status": temporal_status, "label_contract_status": label_status, "evaluation_reproducibility_status": eval_status, "model_readiness_blocker": "BLOCKED_TEMPORAL_INTEGRITY" if temporal_findings else (None if eval_status == "PASS" else "BLOCKED_INSUFFICIENT_PREDICTION_COHORT"), "missing_schema_fields": missing_fields, "invalid_labels": invalid_labels, "temporal_findings": temporal_findings, "duplicate_prediction_id_count": duplicate_prediction_id_count, "duplicate_prediction_ids": duplicate_prediction_ids, "findings": findings}


def write_prediction_ledger_audit(report: Dict[str, Any], output_path: str | Path = "reports/ml_prediction_ledger_audit.json") -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=output_path.name, suffix=".tmp", dir=str(output_path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, output_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    report = audit_prediction_ledger()
    write_prediction_ledger_audit(report)
    print(json.dumps(report, indent=2, sort_keys=True))
