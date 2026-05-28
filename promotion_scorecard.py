from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List

REPORT_PATH = "reports/promotion_scorecard.json"
STRATEGY_GENOME_RESULTS_PATH = "logs/strategy_genome_results.csv"
MAX_DB_ROWS = 500
RECOMMENDATIONS = {"PROMOTE_CANDIDATE", "WATCHLIST", "HOLD", "REJECT", "FREEZE"}

GOVERNANCE_CONSTRAINTS: Dict[str, Any] = {
    "paper_only": "PAPER_ONLY",
    "read_only_analytics": True,
    "no_real_execution": True,
    "no_broker_routing": True,
    "no_execution_mutation": True,
    "no_order_placement": True,
    "no_live_trading": True,
    "no_strategy_auto_promotion": True,
    "no_model_retraining": True,
    "no_auto_deployment": True,
    "no_phase_3_promotion": True,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return round(min(high, max(low, value)), 2)


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as report_file:
            payload = json.load(report_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nested_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _read_csv_rows(path: str, limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as csv_file:
            rows = [row for row in csv.DictReader(csv_file)]
        return rows[-limit:]
    except OSError:
        return []


def _connect_read_only(db_path: str) -> sqlite3.Connection | None:
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _query_rows(db_path: str, query: str, params: tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    connection = _connect_read_only(db_path)
    if connection is None:
        return []
    try:
        with connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _latest_row(db_path: str, table: str) -> Dict[str, Any]:
    rows = _query_rows(db_path, f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else {}


def _drift_assessment(drift: Dict[str, Any]) -> Dict[str, Any]:
    if not drift:
        return {"label": "UNKNOWN", "score": 50.0, "risk": 50.0, "reason": "drift report missing"}
    label = str(
        drift.get("drift_label")
        or drift.get("status")
        or _nested_get(drift, "summary", "drift_label", default=None)
        or _nested_get(drift, "collapse_risk", "label", default=None)
        or "LOW"
    ).upper()
    collapse_timestamp = str(
        _nested_get(drift, "collapse_risk", "collapse_timestamp", default=None)
        or drift.get("collapse_timestamp")
        or "-"
    )
    holding_after = _nested_get(drift, "holding_candles", "mean_after", default=None)
    raw_score = _number(
        drift.get("drift_score")
        or drift.get("risk_score")
        or _nested_get(drift, "summary", "drift_score", default=None),
        0.0,
    )
    if "HIGH" in label or "COLLAPSE" in label or (collapse_timestamp and collapse_timestamp != "-"):
        risk = max(75.0, raw_score)
    elif "ELEVATED" in label or "WARNING" in label or "MEDIUM" in label:
        risk = max(55.0, raw_score)
    elif holding_after is not None and _number(holding_after, 99.0) < 10.0:
        risk = 60.0
    else:
        risk = raw_score if raw_score > 0 else 20.0
    risk = _clamp(risk)
    return {"label": label, "score": _clamp(100.0 - risk), "risk": risk, "reason": f"label={label}, collapse={collapse_timestamp}"}


def _governance_assessment(db_path: str, brake: Dict[str, Any]) -> Dict[str, Any]:
    latest_risk = _latest_row(db_path, "risk_events")
    status = str(latest_risk.get("status") or "SAFE").upper()
    safe = bool(latest_risk.get("safe", 1))
    risk_score = _number(latest_risk.get("risk_score"), 0.0)
    trigger_count = int(_number(brake.get("trigger_count") or _nested_get(brake, "summary", "trigger_count", default=0), 0.0))
    emergency_escalated = trigger_count > 0 or status == "HALT" or not safe
    if status == "HALT" or trigger_count >= 50:
        score = 10.0
    elif trigger_count > 0 or status == "WATCH" or not safe:
        score = 60.0 - min(30.0, risk_score * 0.3)
    else:
        score = 90.0 - min(25.0, risk_score * 0.25)
    return {
        "status": status,
        "safe": safe,
        "score": _clamp(score),
        "emergency_brake_trigger_count": trigger_count,
        "emergency_escalated": emergency_escalated,
    }


def _regime_stability(db_path: str, transition: Dict[str, Any]) -> Dict[str, Any]:
    latest_regime = _latest_row(db_path, "regime_logs")
    warning_score = _number(
        _nested_get(transition, "latest_early_warning", "score", default=None)
        or transition.get("early_warning_score")
        or transition.get("warning_score"),
        0.0,
    )
    warning_label = str(
        _nested_get(transition, "latest_early_warning", "label", default=None)
        or transition.get("early_warning_label")
        or transition.get("warning_label")
        or "UNKNOWN"
    ).upper()
    regime_score = _number(latest_regime.get("regime_score"), 50.0)
    score = _clamp((regime_score * 0.7) + ((100.0 - warning_score) * 0.3))
    if warning_label in {"BRAKE_CANDIDATE", "RISK_ELEVATED"}:
        score = min(score, 45.0)
    return {"score": score, "label": warning_label, "current_regime": latest_regime.get("regime_name") or "UNKNOWN"}


def _walkforward_quality(db_path: str) -> Dict[str, Any]:
    rows = _query_rows(
        db_path,
        """
        SELECT train_accuracy, test_accuracy, winrate, profit_factor
        FROM walkforward_results
        ORDER BY id DESC
        LIMIT ?
        """,
        (MAX_DB_ROWS,),
    )
    if not rows:
        return {"score": 50.0, "folds": 0, "reason": "walkforward data missing"}
    test_values = [_number(row.get("test_accuracy")) for row in rows]
    train_values = [_number(row.get("train_accuracy")) for row in rows]
    winrates = [_number(row.get("winrate")) for row in rows]
    pfs = [_number(row.get("profit_factor"), 1.0) for row in rows]
    avg_test = sum(test_values) / len(test_values)
    avg_train = sum(train_values) / len(train_values) if train_values else avg_test
    avg_winrate = sum(winrates) / len(winrates) if winrates else 0.0
    avg_pf = sum(min(pf, 3.0) for pf in pfs) / len(pfs) if pfs else 1.0
    if avg_test <= 1.0:
        avg_test *= 100.0
    if avg_train <= 1.0:
        avg_train *= 100.0
    overfit_penalty = max(0.0, avg_train - avg_test) * 1.2
    score = (avg_test * 0.45) + (avg_winrate * 0.30) + (min(avg_pf, 3.0) / 3.0 * 100.0 * 0.25) - overfit_penalty
    return {
        "score": _clamp(score),
        "folds": len(rows),
        "average_test_accuracy": round(avg_test, 4),
        "average_winrate": round(avg_winrate, 4),
        "average_profit_factor": round(avg_pf, 4),
        "overfit_penalty": round(overfit_penalty, 4),
    }


def _risk_budget_assessment(risk_budget: Dict[str, Any]) -> Dict[str, Any]:
    if not risk_budget:
        return {
            "score": 50.0,
            "compatible": False,
            "recommendation": "UNKNOWN",
            "reason": "risk budget missing",
            "utilization_ratio": 0.0,
            "brake_context": {},
            "brake_risk_level": "NONE",
        }
    recommendation = str(risk_budget.get("recommendation", "NORMAL")).upper()
    utilization = _number(risk_budget.get("risk_budget_utilization"), 0.0)
    utilization_ratio = _number(risk_budget.get("utilization_ratio"), utilization / 100.0 if utilization > 1.0 else utilization)
    concentration = _number(risk_budget.get("concentration_score"), 0.0)
    brake_context = risk_budget.get("brake_context") if isinstance(risk_budget.get("brake_context"), dict) else {}
    brake_risk_level = str(brake_context.get("brake_risk_level", "NONE")).upper()
    if recommendation == "NORMAL":
        base = 90.0
        compatible = True
    elif recommendation == "DEFENSIVE":
        base = 68.0
        compatible = True
    elif recommendation == "REDUCE EXPOSURE":
        base = 38.0
        compatible = False
    elif recommendation == "FREEZE NEW ALLOCATION":
        base = 15.0
        compatible = False
    else:
        base = 55.0
        compatible = False
    score = base - max(0.0, utilization - 75.0) * 0.3 - max(0.0, concentration - 50.0) * 0.2
    return {
        "score": _clamp(score),
        "compatible": compatible,
        "recommendation": recommendation,
        "utilization": round(utilization, 2),
        "utilization_ratio": round(utilization_ratio, 4),
        "concentration_score": round(concentration, 2),
        "brake_context": brake_context,
        "brake_risk_level": brake_risk_level,
    }


def _candidate_sources(db_path: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    genome_rows = _read_csv_rows(STRATEGY_GENOME_RESULTS_PATH, limit=100)
    for row in genome_rows:
        name = row.get("strategy_name") or row.get("strategy_id") or row.get("setup")
        if name:
            candidates.append({"name": str(name), "source": "strategy_genome", "raw": row})

    signal_rows = _query_rows(
        db_path,
        """
        SELECT symbol, AVG(score) AS avg_score, COUNT(*) AS rows
        FROM (SELECT symbol, score FROM signals WHERE symbol IS NOT NULL AND symbol != '' ORDER BY id DESC LIMIT ?) recent
        GROUP BY symbol
        ORDER BY avg_score DESC, rows DESC
        LIMIT 30
        """,
        (MAX_DB_ROWS,),
    )
    for row in signal_rows:
        candidates.append({"name": str(row.get("symbol")), "source": "signals", "raw": row})

    paper_rows = _query_rows(
        db_path,
        """
        SELECT symbol, AVG(pnl) AS avg_pnl, AVG(confidence) AS avg_confidence, COUNT(*) AS rows
        FROM (SELECT symbol, pnl, confidence FROM internal_paper_trades WHERE symbol IS NOT NULL AND symbol != '' ORDER BY id DESC LIMIT ?) recent
        GROUP BY symbol
        ORDER BY avg_pnl DESC, avg_confidence DESC
        LIMIT 30
        """,
        (MAX_DB_ROWS,),
    )
    for row in paper_rows:
        candidates.append({"name": str(row.get("symbol")), "source": "internal_paper", "raw": row})

    if not candidates:
        candidates.append({"name": "GLOBAL_SETUP_HEALTH", "source": "governance_fallback", "raw": {}})

    merged: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate["name"].upper()
        if key not in merged:
            merged[key] = {"name": candidate["name"], "sources": [], "raw": []}
        merged[key]["sources"].append(candidate["source"])
        merged[key]["raw"].append(candidate["raw"])
    return list(merged.values())


def _candidate_local_quality(candidate: Dict[str, Any]) -> float:
    scores: List[float] = []
    for raw in candidate.get("raw", []):
        for key in ("stability_score", "regime_survival_score", "macro_survival_score", "cross_market_survival_score", "avg_score", "avg_confidence"):
            if key in raw and raw.get(key) not in (None, ""):
                scores.append(_number(raw.get(key)))
        if "profit_factor" in raw:
            scores.append(min(_number(raw.get("profit_factor"), 1.0), 3.0) / 3.0 * 100.0)
        if "overfit_risk" in raw:
            scores.append(100.0 - _number(raw.get("overfit_risk")))
        if "avg_pnl" in raw:
            scores.append(50.0 + _number(raw.get("avg_pnl")) * 5.0)
    return _clamp(sum(scores) / len(scores)) if scores else 60.0


def _recommendation(health: float, governance: Dict[str, Any], drift: Dict[str, Any], risk_budget: Dict[str, Any]) -> tuple[str, str]:
    if risk_budget.get("recommendation") == "FREEZE NEW ALLOCATION":
        return "FREEZE", "Risk budget freeze overrides promotion readiness."
    if governance.get("emergency_escalated"):
        return "FREEZE", "Emergency brake/risk freeze constraint is active."
    if drift.get("risk", 100.0) >= 75.0 or governance.get("score", 0.0) < 35.0:
        return "REJECT", "Drift/governance risk exceeds readiness constraints."
    readiness_pass = governance.get("score", 0.0) >= 70.0 and drift.get("risk", 100.0) < 55.0 and bool(risk_budget.get("compatible"))
    if readiness_pass and health >= 78.0:
        return "PROMOTE_CANDIDATE", "Passes read-only promotion readiness gates; manual PAPER_ONLY review required."
    if readiness_pass and health >= 62.0:
        return "WATCHLIST", "Governance gates pass but score needs more evidence."
    if health >= 45.0:
        return "HOLD", "Insufficient readiness for promotion candidate status."
    return "REJECT", "Composite health is too weak for promotion readiness."


def generate_promotion_scorecard(
    db_path: str = "mamuyy_hunter.db",
    output_path: str | None = REPORT_PATH,
    write_report: bool = True,
    top_n: int = 10,
) -> Dict[str, Any]:
    drift = _drift_assessment(_read_json("reports/drift_detection_report.json"))
    governance = _governance_assessment(db_path, _read_json("reports/emergency_brake_simulation.json"))
    regime = _regime_stability(db_path, _read_json("reports/transition_prediction_report.json"))
    walkforward = _walkforward_quality(db_path)
    risk_budget = _risk_budget_assessment(_read_json("reports/portfolio_risk_budget.json"))

    candidates: List[Dict[str, Any]] = []
    for candidate in _candidate_sources(db_path):
        local_quality = _candidate_local_quality(candidate)
        health_score = _clamp(
            walkforward["score"] * 0.25
            + drift["score"] * 0.20
            + governance["score"] * 0.20
            + risk_budget["score"] * 0.20
            + regime["score"] * 0.10
            + local_quality * 0.05
        )
        recommendation, reason = _recommendation(health_score, governance, drift, risk_budget)
        promotion_readiness = "PASS" if recommendation in {"PROMOTE_CANDIDATE", "WATCHLIST"} else "FAIL"
        risk_budget_override = "INACTIVE"
        if risk_budget["recommendation"] == "FREEZE NEW ALLOCATION":
            recommendation = "FREEZE"
            promotion_readiness = "FREEZE"
            risk_budget_override = "ACTIVE"
            reason = "Risk budget freeze overrides promotion readiness."
        elif risk_budget.get("utilization_ratio", 0.0) > 1.0 and recommendation in {"PROMOTE_CANDIDATE", "WATCHLIST"}:
            recommendation = "HOLD"
            promotion_readiness = "HOLD"
            risk_budget_override = "ACTIVE"
            reason = "Risk budget utilization above 100% caps promotion readiness at HOLD."
        elif risk_budget.get("brake_risk_level") == "HIGH" and recommendation in {"PROMOTE_CANDIDATE", "WATCHLIST"}:
            recommendation = "HOLD"
            promotion_readiness = "HOLD"
            risk_budget_override = "ACTIVE"
            reason = "High emergency brake risk caps promotion readiness at HOLD."
        candidates.append(
            {
                "strategy_setup_name": candidate["name"],
                "sources": sorted(set(candidate.get("sources", []))),
                "health_score": health_score,
                "governance_score": governance["score"],
                "drift_risk": drift["label"],
                "drift_risk_score": drift["risk"],
                "regime_stability": regime["score"],
                "walkforward_quality": walkforward["score"],
                "risk_budget_compatibility": "PASS" if risk_budget["compatible"] and risk_budget_override != "ACTIVE" else "FAIL",
                "risk_budget_override": risk_budget_override,
                "promotion_readiness": promotion_readiness,
                "recommendation": recommendation,
                "recommendation_reason": reason,
                "governance_compatibility": "PASS" if governance["score"] >= 70.0 and not governance["emergency_escalated"] else "FAIL",
            }
        )

    candidates = sorted(candidates, key=lambda row: (row["health_score"], row["governance_score"]), reverse=True)[:top_n]
    distribution = Counter(candidate["recommendation"] for candidate in candidates)
    readiness_distribution = Counter(candidate["promotion_readiness"] for candidate in candidates)
    top_candidate = candidates[0] if candidates else {}
    result: Dict[str, Any] = {
        "generated_at": _now(),
        "paper_only": True,
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "top_candidate": top_candidate.get("strategy_setup_name", "-"),
            "top_recommendation": top_candidate.get("recommendation", "HOLD"),
            "readiness_distribution": dict(readiness_distribution),
            "recommendation_distribution": {key: distribution.get(key, 0) for key in sorted(RECOMMENDATIONS)},
            "governance_status": governance["status"],
            "governance_score": governance["score"],
            "drift_label": drift["label"],
            "regime_stability": regime["score"],
            "walkforward_quality": walkforward["score"],
            "risk_budget_recommendation": risk_budget["recommendation"],
            "risk_budget_utilization_ratio": risk_budget.get("utilization_ratio", 0.0),
            "risk_budget_override": "ACTIVE" if any(candidate.get("risk_budget_override") == "ACTIVE" for candidate in candidates) else "INACTIVE",
        },
        "governance_constraints": GOVERNANCE_CONSTRAINTS,
    }
    if write_report and output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as report_file:
            json.dump(result, report_file, indent=2, sort_keys=True)
            report_file.write("\n")
    return result


def format_promotion_scorecard(result: Dict[str, Any]) -> str:
    summary = result.get("summary", {}) if isinstance(result, dict) else {}
    candidates = result.get("candidates", []) if isinstance(result, dict) else []
    top = candidates[0] if candidates else {}
    lines = [
        "PROMOTION SCORECARD (PAPER_ONLY)",
        "Governance: read-only analytics, no real execution, no auto deployment, no Phase 3 promotion.",
        f"Top Candidate: {top.get('strategy_setup_name', '-')}",
        f"Readiness: {top.get('recommendation', summary.get('top_recommendation', 'HOLD'))}",
        f"Governance: {top.get('governance_compatibility', '-')}",
        f"Risk Budget Override: {top.get('risk_budget_override', summary.get('risk_budget_override', 'INACTIVE'))}",
        f"Drift: {top.get('drift_risk', summary.get('drift_label', 'UNKNOWN'))}",
        f"Candidates: {summary.get('candidate_count', 0)}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_promotion_scorecard(generate_promotion_scorecard()))
