import json
import math
import os
import pickle
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score
from sklearn.model_selection import train_test_split

from ml_engine import PROFITABLE_LABELS, _encode, build_ml_dataset
from walkforward import run_walkforward_validation


DEFAULT_REGISTRY = {
    "production": None,
    "candidate": None,
    "history": [],
    "warnings": [],
    "rollback_available": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return dict(DEFAULT_REGISTRY)
    try:
        with open(path, encoding="utf-8") as registry_file:
            payload = json.load(registry_file)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_REGISTRY)
    registry = dict(DEFAULT_REGISTRY)
    registry.update(payload if isinstance(payload, dict) else {})
    registry["history"] = registry.get("history") or []
    registry["warnings"] = registry.get("warnings") or []
    return registry


def _save_registry(path: str, registry: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as registry_file:
        json.dump(registry, registry_file, indent=2, default=str)


def _profit_factor(pnls: pd.Series) -> float:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_drawdown(pnls: pd.Series) -> float:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    if equity.empty:
        return 0.0
    return float((equity - equity.cummax()).min())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _model_age_days(production: Dict[str, Any] | None) -> float:
    if not production or not production.get("train_timestamp"):
        return 9999.0
    try:
        timestamp = datetime.fromisoformat(str(production["train_timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds() / 86400)
    except ValueError:
        return 9999.0


def _drift_warnings(candidate: Dict[str, Any], production: Dict[str, Any] | None, history: List[Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []
    if production:
        if candidate["accuracy"] < _safe_float(production.get("accuracy")) - 0.05:
            warnings.append("DRIFT WARNING: ML accuracy deteriorated versus production.")
        if candidate["walkforward_score"] < _safe_float(production.get("walkforward_score")) - 10:
            warnings.append("DRIFT WARNING: walkforward stability deteriorated.")
        if _safe_float(production.get("profit_factor")) > 0 and candidate["profit_factor"] < _safe_float(production.get("profit_factor")) * 0.70:
            warnings.append("DRIFT WARNING: PF collapsed versus production.")
        if _model_age_days(production) > 45:
            warnings.append("MODEL AGING: production model is older than 45 days.")

    recent = [row for row in history[-3:] if row.get("accuracy") is not None]
    if len(recent) >= 3:
        accuracies = [_safe_float(row.get("accuracy")) for row in recent] + [candidate["accuracy"]]
        if all(left > right for left, right in zip(accuracies, accuracies[1:])):
            warnings.append("RETRAIN RECOMMENDED: accuracy has continuously degraded.")
    if candidate["walkforward_score"] < 45:
        warnings.append("RETRAIN RECOMMENDED: walkforward stability is weak.")
    if candidate["profit_factor"] < 0.80:
        warnings.append("DRIFT WARNING: profit factor is below 0.80.")
    return warnings


def _replacement_allowed(candidate: Dict[str, Any], production: Dict[str, Any] | None) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    if candidate["dataset_rows"] < 8:
        return False, ["Rejected: not enough labeled rows."]
    if candidate["walkforward_score"] < 45:
        return False, ["Rejected: walkforward stability below 45."]
    if not production:
        return True, ["Accepted: no production model exists."]

    production_pf = _safe_float(production.get("profit_factor"))
    production_dd = abs(_safe_float(production.get("max_drawdown")))
    candidate_pf = candidate["profit_factor"]
    candidate_dd = abs(candidate["max_drawdown"])
    pf_ok = candidate_pf >= production_pf * 0.95 or candidate_pf >= production_pf
    dd_limit = production_dd * 1.15 if production_dd > 0 else 5.0
    dd_ok = candidate_dd <= dd_limit
    if not pf_ok:
        reasons.append(f"Rejected: PF {candidate_pf:.4f} is below stable threshold from production {production_pf:.4f}.")
    if not dd_ok:
        reasons.append(f"Rejected: DD {candidate_dd:.4f} worsened beyond allowed limit {dd_limit:.4f}.")
    if pf_ok and dd_ok:
        reasons.append("Accepted: PF stable/improved and DD controlled.")
    return pf_ok and dd_ok, reasons


def _save_candidate_model(path: str, model: RandomForestClassifier, feature_names: List[str], metadata: Dict[str, Any]) -> None:
    with open(path, "wb") as model_file:
        pickle.dump({"model": model, "feature_names": feature_names, "metadata": metadata}, model_file)


def retrain_model(
    database_path: str = "mamuyy_hunter.db",
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    flow_log_path: str = "flow_log.csv",
    registry_path: str = "model_registry.json",
    production_model_path: str = "model_weights.pkl",
    candidate_model_path: str = "model_weights_candidate.pkl",
    previous_model_path: str = "model_weights_previous.pkl",
    walkforward_output_path: str = "logs/retrain_walkforward.csv",
    chart_dir: str = "charts",
) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(walkforward_output_path) or ".", exist_ok=True)
    dataset = build_ml_dataset(
        paper_trades_path,
        signals_log_path,
        flow_log_path,
        database_path=database_path,
    )
    registry = _load_registry(registry_path)
    production = registry.get("production")

    base_result = {
        "ok": False,
        "accepted": False,
        "dataset_rows": int(len(dataset)),
        "registry_path": registry_path,
        "candidate_model_path": candidate_model_path,
        "production_model_path": production_model_path,
        "warnings": [],
        "reasons": [],
    }
    if len(dataset) < 8 or dataset["target"].nunique() < 2:
        candidate = {
            "version": f"candidate-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "train_timestamp": _now(),
            "dataset_rows": int(len(dataset)),
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "walkforward_score": 0.0,
            "model_ready": False,
            "status": "rejected",
        }
        registry["candidate"] = candidate
        registry["warnings"] = ["RETRAIN RECOMMENDED: not enough labeled rows for retraining."]
        registry["rollback_available"] = os.path.exists(previous_model_path)
        _save_registry(registry_path, registry)
        return {
            **base_result,
            "ok": True,
            "candidate": candidate,
            "warnings": registry["warnings"],
            "reasons": ["Not enough data."],
            "rollback_available": registry["rollback_available"],
        }

    X, feature_names = _encode(dataset)
    y = dataset["target"]
    stratify = y if y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.30 if len(dataset) >= 20 else 0.40,
        random_state=42,
        stratify=stratify,
    )
    model = RandomForestClassifier(
        n_estimators=250,
        max_depth=6,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    pnl = pd.to_numeric(dataset.get("pnl_percent", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    walkforward = run_walkforward_validation(
        paper_trades_path=paper_trades_path,
        signals_log_path=signals_log_path,
        output_path=walkforward_output_path,
        chart_dir=chart_dir,
        database_path=database_path,
    )

    candidate = {
        "version": f"model-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "train_timestamp": _now(),
        "dataset_rows": int(len(dataset)),
        "accuracy": float(accuracy_score(y_test, predictions)),
        "precision": float(precision_score(y_test, predictions, average="weighted", zero_division=0)),
        "recall": float(recall_score(y_test, predictions, average="weighted", zero_division=0)),
        "profit_factor": float(_profit_factor(pnl)),
        "max_drawdown": float(_max_drawdown(pnl)),
        "walkforward_score": float(walkforward.get("model_stability_score", 0.0)),
        "walkforward_profit_factor": float(walkforward.get("average_profit_factor", 0.0)),
        "walkforward_health": walkforward.get("model_health", "UNSTABLE"),
        "model_ready": True,
        "status": "candidate",
    }
    _save_candidate_model(candidate_model_path, model, feature_names, candidate)

    accepted, reasons = _replacement_allowed(candidate, production)
    warnings = _drift_warnings(candidate, production, registry.get("history", []))
    if accepted:
        if os.path.exists(production_model_path):
            shutil.copy2(production_model_path, previous_model_path)
        shutil.copy2(candidate_model_path, production_model_path)
        candidate["status"] = "production"
        registry["production"] = candidate
    else:
        candidate["status"] = "rejected"

    registry["candidate"] = candidate
    history = registry.get("history", [])
    history.append({**candidate, "accepted": accepted, "reasons": reasons})
    registry["history"] = history[-25:]
    registry["warnings"] = warnings
    registry["rollback_available"] = os.path.exists(previous_model_path)
    _save_registry(registry_path, registry)

    return {
        **base_result,
        "ok": True,
        "accepted": accepted,
        "candidate": candidate,
        "production": registry.get("production"),
        "warnings": warnings,
        "reasons": reasons,
        "rollback_available": registry["rollback_available"],
    }


def format_retrain_summary(result: Dict[str, Any]) -> str:
    candidate = result.get("candidate", {}) or {}
    production = result.get("production", {}) or {}
    warnings = result.get("warnings", []) or ["none"]
    reasons = result.get("reasons", []) or ["none"]
    return "\n".join(
        [
            "MODEL RETRAINING ENGINE",
            f"OK: {result.get('ok')}",
            f"Accepted: {result.get('accepted')}",
            f"Rows: {result.get('dataset_rows')}",
            f"Candidate Version: {candidate.get('version', '-')}",
            f"Candidate Accuracy: {candidate.get('accuracy', 0):.4f}",
            f"Candidate PF: {candidate.get('profit_factor', 0):.4f}",
            f"Candidate DD: {candidate.get('max_drawdown', 0):.4f}",
            f"Candidate Walkforward: {candidate.get('walkforward_score', 0):.2f}",
            f"Production Version: {production.get('version', '-') if production else '-'}",
            f"Rollback Available: {result.get('rollback_available')}",
            f"Warnings: {' | '.join(warnings)}",
            f"Reasons: {' | '.join(reasons)}",
        ]
    )
