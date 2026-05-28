"""PAPER_ONLY governance audit intelligence layer.

This module performs read-only consistency checks across existing governance
artifacts. It never routes broker orders, mutates execution state, deploys
strategies, retrains models, enables live trading, or promotes Phase 3.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REPORT_PATH = "reports/governance_audit.json"
STALE_HOURS_DEFAULT = 24

GOVERNANCE_CONSTRAINTS: Dict[str, Any] = {
    "paper_only": "PAPER_ONLY",
    "read_only_analytics": True,
    "no_broker_order_routing": True,
    "no_execution": True,
    "no_execution_mutation": True,
    "no_live_trading": True,
    "no_deployment": True,
    "no_strategy_deployment": True,
    "no_model_retraining": True,
    "no_phase_3_promotion": True,
}

REQUIRED_REPORTS: Dict[str, str] = {
    "portfolio_risk_budget": "reports/portfolio_risk_budget.json",
    "promotion_scorecard": "reports/promotion_scorecard.json",
    "drift_detection": "reports/drift_detection_report.json",
    "emergency_brake": "reports/emergency_brake_simulation.json",
    "transition_prediction": "reports/transition_prediction_report.json",
}

PROMOTION_POSITIVE = {"PROMOTE_CANDIDATE", "PASS"}
RISK_FREEZE = {"FREEZE", "FREEZE NEW ALLOCATION", "HALT", "BLOCK", "NO_NEW_ALLOCATION"}
GOVERNANCE_PASS = {"PASS", "SAFE", "HEALTHY", "OK", "GREEN"}
DRIFT_HIGH = {"HIGH", "CRITICAL", "RED", "UNSTABLE", "ELEVATED"}
BRAKE_HIGH_TRIGGER_THRESHOLD = 50
RISK_BRAKE_ALIGNED = RISK_FREEZE | {"DEFENSIVE", "REDUCE EXPOSURE"}
GOVERNANCE_BRAKE_ALIGNED_MARKERS = ("DEFENSIVE", "HOLD", "NO TRADE", "NO_TRADE")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as report_file:
            payload = json.load(report_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as report_file:
        json.dump(payload, report_file, indent=2, sort_keys=True)
        report_file.write("\n")


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _report_timestamp(path: str, payload: Dict[str, Any]) -> datetime | None:
    generated_at = _parse_timestamp(payload.get("generated_at"))
    if generated_at is not None:
        return generated_at
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return None


def _upper(value: Any, default: str = "UNKNOWN") -> str:
    text = str(value if value is not None else default).strip().upper()
    return text or default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _nested_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current



def _brake_source(brake: Dict[str, Any]) -> str:
    source = _upper(
        brake.get("brake_source")
        or brake.get("source")
        or _nested_get(brake, "summary", "brake_source")
        or _nested_get(brake, "summary", "source"),
        "",
    )
    if source:
        return source
    return "SIMULATION_RESEARCH" if brake else "NONE"


def _brake_trigger_count(brake: Dict[str, Any]) -> int:
    return int(_number(
        _nested_get(brake, "summary", "brake_trigger_count", default=None)
        or _nested_get(brake, "summary", "trigger_count", default=None)
        or brake.get("high_trigger_count")
        or brake.get("trigger_count"),
        0.0,
    ))


def _governance_brake_aligned(*values: Any) -> bool:
    for value in values:
        text = _upper(value, "")
        if any(marker in text for marker in GOVERNANCE_BRAKE_ALIGNED_MARKERS):
            return True
    return False

def _top_candidate(scorecard: Dict[str, Any]) -> Dict[str, Any]:
    candidates = scorecard.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def _artifact_health(reports: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        name: {
            "path": REQUIRED_REPORTS[name],
            "present": bool(reports.get(name)),
        }
        for name in REQUIRED_REPORTS
    }



def _audit_brake_context(brake: Dict[str, Any]) -> Dict[str, Any]:
    trigger_count = _brake_trigger_count(brake)
    return {
        "trigger_count": trigger_count,
        "brake_risk_level": "HIGH" if trigger_count >= BRAKE_HIGH_TRIGGER_THRESHOLD else "LOW" if trigger_count > 0 else "NONE",
        "brake_source": _brake_source(brake),
    }

def _find_missing_reports(reports: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    missing: List[Dict[str, str]] = []
    for name, path in REQUIRED_REPORTS.items():
        if not reports.get(name):
            missing.append({"report": name, "path": path})
    return missing


def _find_stale_reports(
    reports: Dict[str, Dict[str, Any]],
    *,
    now: datetime,
    stale_hours: int,
) -> List[Dict[str, Any]]:
    stale: List[Dict[str, Any]] = []
    threshold_seconds = stale_hours * 3600
    for name, path in REQUIRED_REPORTS.items():
        payload = reports.get(name, {})
        if not payload:
            continue
        timestamp = _report_timestamp(path, payload)
        if timestamp is None:
            stale.append({
                "report": name,
                "path": path,
                "age_hours": None,
                "reason": "No generated_at timestamp or file mtime available.",
            })
            continue
        age_seconds = max(0.0, (now - timestamp).total_seconds())
        if age_seconds > threshold_seconds:
            stale.append({
                "report": name,
                "path": path,
                "generated_at": timestamp.isoformat(),
                "age_hours": round(age_seconds / 3600, 2),
                "threshold_hours": stale_hours,
            })
    return stale


def _check_policy_violations(reports: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    violations: List[Dict[str, str]] = []
    for name, payload in reports.items():
        if not payload:
            continue
        if payload.get("paper_only") is False:
            violations.append({"report": name, "policy": "PAPER_ONLY", "detail": "paper_only is false"})
        if payload.get("execution_enabled") is True:
            violations.append({"report": name, "policy": "no_execution", "detail": "execution_enabled is true"})
        if payload.get("live_trading_enabled") is True:
            violations.append({"report": name, "policy": "no_live_trading", "detail": "live_trading_enabled is true"})
        constraints = payload.get("governance_constraints")
        if isinstance(constraints, dict):
            expected_true = [
                "read_only_analytics",
                "no_broker_routing",
                "no_order_placement",
                "no_real_execution",
                "no_execution_mutation",
                "no_live_trading",
                "no_auto_deployment",
                "no_strategy_auto_promotion",
                "no_model_retraining",
                "no_phase_3_promotion",
            ]
            for key in expected_true:
                if key in constraints and constraints.get(key) is not True:
                    violations.append({"report": name, "policy": key, "detail": f"{key} is not true"})
            if constraints.get("paper_only") not in (None, "PAPER_ONLY", True):
                violations.append({"report": name, "policy": "PAPER_ONLY", "detail": "constraint paper_only is not PAPER_ONLY"})
    return violations


def _check_conflicts(reports: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    conflicts: List[Dict[str, str]] = []
    risk_budget = reports.get("portfolio_risk_budget", {})
    scorecard = reports.get("promotion_scorecard", {})
    drift = reports.get("drift_detection", {})
    brake = reports.get("emergency_brake", {})
    top = _top_candidate(scorecard)
    summary = scorecard.get("summary", {}) if isinstance(scorecard.get("summary"), dict) else {}

    risk_recommendation = _upper(risk_budget.get("recommendation"), "NORMAL")
    top_recommendation = _upper(top.get("recommendation") or summary.get("top_recommendation"), "HOLD")
    promotion_readiness = _upper(top.get("promotion_readiness"), "UNKNOWN")
    governance_status = _upper(summary.get("governance_status") or top.get("governance_compatibility"), "UNKNOWN")

    if risk_recommendation in RISK_FREEZE and (
        top_recommendation in PROMOTION_POSITIVE or promotion_readiness in PROMOTION_POSITIVE
    ):
        conflicts.append({
            "type": "RISK_BUDGET_PROMOTION_CONFLICT",
            "severity": "CRITICAL",
            "detail": f"Risk Budget says {risk_recommendation} while Promotion Scorecard says {top_recommendation}/{promotion_readiness}.",
        })

    drift_label = _upper(
        drift.get("drift_label")
        or drift.get("drift_risk")
        or drift.get("risk_label")
        or _nested_get(drift, "summary", "drift_label")
        or top.get("drift_risk")
        or summary.get("drift_label"),
        "UNKNOWN",
    )
    drift_score = _number(
        drift.get("drift_score")
        or drift.get("risk_score")
        or _nested_get(drift, "summary", "drift_score")
        or top.get("drift_risk_score"),
        0.0,
    )
    if (drift_label in DRIFT_HIGH or drift_score >= 70.0) and governance_status in GOVERNANCE_PASS:
        conflicts.append({
            "type": "DRIFT_GOVERNANCE_MISMATCH",
            "severity": "HIGH",
            "detail": f"Drift is {drift_label} ({drift_score:.1f}) while governance is {governance_status}.",
        })

    brake_active = bool(brake.get("brake_active") or brake.get("active") or brake.get("emergency_brake_active"))
    trigger_count = _brake_trigger_count(brake)
    brake_source = _brake_source(brake)
    brake_risk_high = brake_active or trigger_count >= BRAKE_HIGH_TRIGGER_THRESHOLD
    governance_aligned = _governance_brake_aligned(
        summary.get("governance_action"),
        summary.get("governance_message"),
        summary.get("message"),
        summary.get("action"),
        top.get("governance_action"),
        top.get("governance_message"),
        top.get("message"),
        top.get("action"),
        top.get("recommendation") or summary.get("top_recommendation"),
    )
    if brake_risk_high and risk_recommendation not in RISK_BRAKE_ALIGNED:
        conflicts.append({
            "type": "EMERGENCY_BRAKE_RISK_BUDGET_MISMATCH",
            "severity": "HIGH",
            "detail": (
                f"High emergency brake risk detected (active={brake_active}, triggers={trigger_count}, "
                f"source={brake_source}) while risk budget is {risk_recommendation}."
            ),
        })
    if brake_risk_high and governance_status in GOVERNANCE_PASS and not governance_aligned:
        conflicts.append({
            "type": "EMERGENCY_BRAKE_GOVERNANCE_MISMATCH",
            "severity": "HIGH",
            "detail": (
                f"High emergency brake risk detected (active={brake_active}, triggers={trigger_count}, "
                f"source={brake_source}) while governance is {governance_status}."
            ),
        })

    if risk_recommendation == "NORMAL" and _number(risk_budget.get("risk_budget_utilization"), 0.0) >= 100.0:
        conflicts.append({
            "type": "RISK_BUDGET_UTILIZATION_CONTRADICTION",
            "severity": "MEDIUM",
            "detail": "Risk budget utilization is at/above 100% while recommendation is NORMAL.",
        })

    return conflicts


def _severity(conflicts: Iterable[Dict[str, str]], stale_reports: List[Dict[str, Any]], missing_reports: List[Dict[str, str]], policy_violations: List[Dict[str, str]]) -> str:
    if policy_violations or any(item.get("severity") == "CRITICAL" for item in conflicts):
        return "CRITICAL"
    if any(item.get("severity") == "HIGH" for item in conflicts):
        return "HIGH"
    if conflicts or len(missing_reports) >= 3:
        return "MEDIUM"
    if stale_reports or missing_reports:
        return "LOW"
    return "NONE"


def _score(conflicts: List[Dict[str, str]], stale_reports: List[Dict[str, Any]], missing_reports: List[Dict[str, str]], policy_violations: List[Dict[str, str]]) -> int:
    score = 100
    score -= len(policy_violations) * 25
    for conflict in conflicts:
        score -= {"CRITICAL": 30, "HIGH": 20, "MEDIUM": 12}.get(conflict.get("severity", "LOW"), 8)
    score -= len(stale_reports) * 6
    score -= len(missing_reports) * 8
    return max(0, min(100, score))


def _health(score: int, severity: str) -> str:
    if severity == "CRITICAL" or score < 50:
        return "CRITICAL"
    if severity in {"HIGH", "MEDIUM"} or score < 75:
        return "WATCH"
    if severity == "LOW" or score < 90:
        return "STABLE_WITH_WARNINGS"
    return "HEALTHY"


def _recommendations(severity: str, conflicts: List[Dict[str, str]], stale_reports: List[Dict[str, Any]], missing_reports: List[Dict[str, str]], policy_violations: List[Dict[str, str]]) -> List[str]:
    recommendations: List[str] = []
    if policy_violations:
        recommendations.append("Immediately restore PAPER_ONLY/read-only governance constraints before any further review.")
    if conflicts:
        recommendations.append("Review governance conflicts manually; keep all execution and deployment paths disabled.")
    if stale_reports:
        recommendations.append("Regenerate stale governance reports using read-only analytics jobs only.")
    if missing_reports:
        recommendations.append("Generate missing governance artifacts before trusting promotion/risk summaries.")
    if severity == "NONE":
        recommendations.append("No governance audit action required; continue PAPER_ONLY monitoring.")
    recommendations.append("Do not execute trades, deploy strategies, retrain models, or promote Phase 3 from this audit.")
    return recommendations


def run_governance_audit(
    *,
    output_path: str | None = REPORT_PATH,
    write_report: bool = True,
    stale_hours: int = STALE_HOURS_DEFAULT,
) -> Dict[str, Any]:
    """Run a read-only governance consistency audit and optionally write the report artifact."""
    now = _utc_now()
    reports = {name: _read_json(path) for name, path in REQUIRED_REPORTS.items()}
    missing_reports = _find_missing_reports(reports)
    stale_reports = _find_stale_reports(reports, now=now, stale_hours=stale_hours)
    policy_violations = _check_policy_violations(reports)
    conflicts = _check_conflicts(reports)
    brake_context = _audit_brake_context(reports.get("emergency_brake", {}))
    severity = _severity(conflicts, stale_reports, missing_reports, policy_violations)
    consistency_score = _score(conflicts, stale_reports, missing_reports, policy_violations)
    health = _health(consistency_score, severity)

    result: Dict[str, Any] = {
        "generated_at": now.isoformat(),
        "paper_only": True,
        "consistency_score": consistency_score,
        "governance_health": health,
        "conflicts": conflicts,
        "brake_context": brake_context,
        "stale_reports": stale_reports,
        "missing_reports": missing_reports,
        "policy_violations": policy_violations,
        "audit_severity": severity,
        "recommendations": _recommendations(severity, conflicts, stale_reports, missing_reports, policy_violations),
        "governance_constraints": GOVERNANCE_CONSTRAINTS.copy(),
        "artifact_health": _artifact_health(reports),
    }
    if write_report and output_path:
        _write_json(output_path, result)
    return result


def format_governance_audit(result: Dict[str, Any]) -> str:
    violations = result.get("policy_violations") or []
    return (
        "🧠 GOVERNANCE AUDIT\n\n"
        f"Consistency: {int(result.get('consistency_score', 0))}%\n"
        f"Health: {result.get('governance_health', 'UNKNOWN')}\n"
        f"Conflicts: {len(result.get('conflicts') or [])}\n"
        f"Brake Context: {(result.get('brake_context') or {}).get('brake_risk_level', 'NONE')} "
        f"(triggers={(result.get('brake_context') or {}).get('trigger_count', 0)}, "
        f"source={(result.get('brake_context') or {}).get('brake_source', 'NONE')})\n"
        f"Stale Reports: {len(result.get('stale_reports') or [])}\n"
        f"Violations: {'none' if not violations else len(violations)}\n"
        f"Status: {result.get('audit_severity', 'UNKNOWN')}\n"
        "Mode: PAPER_ONLY read-only analytics; no execution, deployment, live trading, retraining, or Phase 3 promotion."
    )


if __name__ == "__main__":
    print(format_governance_audit(run_governance_audit()))
