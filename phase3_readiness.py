from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPORT_PATH = "reports/phase3_readiness.json"
READINESS_CRITERIA_COUNT = 10
MIN_GOVERNANCE_CONSISTENCY = 95.0
MIN_CLOSED_PAPER_TRADES = 100
CLOSED_TRADE_STATUSES = {
    "WIN",
    "LOSS",
    "CLOSED",
    "TRADE CLOSED",
    "TP2 HIT",
    "SL HIT",
    "STOP LOSS",
    "TAKE PROFIT",
    "EXPIRED",
}
RISK_RECOMMENDATION_RANK = {
    "NORMAL": 0,
    "WATCH": 1,
    "DEFENSIVE": 2,
    "HOLD": 3,
    "HALT": 4,
}
GOVERNANCE_CONSTRAINTS = {
    "paper_only": "PAPER_ONLY",
    "read_only_analytics": True,
    "no_execution_changes": True,
    "no_broker_routing": True,
    "no_strategy_promotion": True,
    "no_phase_3_unlock_automation": True,
    "no_live_trading": True,
}


def _now_iso() -> str:
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


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def _nested_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _connect_read_only(db_path: str) -> sqlite3.Connection | None:
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _query_scalar(db_path: str, query: str, default: Any = 0) -> Any:
    connection = _connect_read_only(db_path)
    if connection is None:
        return default
    try:
        with connection:
            row = connection.execute(query).fetchone()
        if row is None:
            return default
        return row[0]
    except sqlite3.Error:
        return default
    finally:
        connection.close()


def _read_csv_rows(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as csv_file:
            return [row for row in csv.DictReader(csv_file)]
    except OSError:
        return []


def _criterion(name: str, passed: bool, detail: str, blocker: str = "", next_action: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "detail": detail,
        "blocker": blocker if not passed else "",
        "next_action": next_action if not passed else "",
    }


def _governance_audit_criterion(path: str) -> Dict[str, Any]:
    audit = _read_json(path)
    consistency = _number(audit.get("consistency_score"), 0.0)
    conflicts = audit.get("conflicts", []) if isinstance(audit.get("conflicts"), list) else []
    passed = bool(audit) and consistency >= MIN_GOVERNANCE_CONSISTENCY and len(conflicts) == 0
    detail = f"consistency={consistency:.0f}%, conflicts={len(conflicts)}"
    return _criterion(
        "governance_audit consistency >= 95 and conflicts == 0",
        passed,
        detail,
        "Governance audit is below readiness threshold or has conflicts.",
        "Run python main.py --governance-audit and resolve every conflict before manual Phase 3 review.",
    )


def _portfolio_risk_budget_criterion(path: str) -> Dict[str, Any]:
    report = _read_json(path)
    recommendation = str(report.get("recommendation", "UNKNOWN")).upper()
    rank = RISK_RECOMMENDATION_RANK.get(recommendation, RISK_RECOMMENDATION_RANK["HALT"])
    passed = bool(report) and rank <= RISK_RECOMMENDATION_RANK["DEFENSIVE"]
    return _criterion(
        "portfolio_risk_budget recommendation not worse than DEFENSIVE",
        passed,
        f"recommendation={recommendation}",
        "Portfolio risk budget is missing or stricter than DEFENSIVE.",
        "Regenerate the read-only risk budget and reduce paper risk concentration until recommendation is NORMAL/WATCH/DEFENSIVE.",
    )


def _promotion_scorecard_criterion(path: str) -> Dict[str, Any]:
    report = _read_json(path)
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    top = str(summary.get("top_recommendation") or "UNKNOWN").upper()
    distribution = summary.get("recommendation_distribution", {}) if isinstance(summary.get("recommendation_distribution"), dict) else {}
    freeze_count = int(_number(distribution.get("FREEZE"), 0.0))
    candidates = report.get("candidates", []) if isinstance(report.get("candidates"), list) else []
    candidate_freeze = any(
        str(candidate.get("recommendation", "")).upper() == "FREEZE"
        or str(candidate.get("promotion_readiness", "")).upper() == "FREEZE"
        for candidate in candidates
        if isinstance(candidate, dict)
    )
    readiness_distribution = summary.get("readiness_distribution", {}) if isinstance(summary.get("readiness_distribution"), dict) else {}
    readiness_freeze_count = int(_number(readiness_distribution.get("FREEZE"), 0.0))
    passed = bool(report) and top != "FREEZE" and freeze_count == 0 and readiness_freeze_count == 0 and not candidate_freeze
    return _criterion(
        "promotion_scorecard readiness not FREEZE",
        passed,
        f"top_recommendation={top}, freeze_count={freeze_count}, readiness_freeze_count={readiness_freeze_count}",
        "Promotion scorecard is missing or contains a FREEZE recommendation.",
        "Keep Phase 3 locked and resolve scorecard freeze conditions in PAPER_ONLY review.",
    )


def _internal_paper_closed_count(db_path: str) -> int:
    quoted_statuses = ",".join(f"'{status}'" for status in sorted(CLOSED_TRADE_STATUSES | {"CLOSED", "STOP_LOSS", "TAKE_PROFIT"}))
    return int(_number(_query_scalar(db_path, f"SELECT COUNT(*) FROM internal_paper_trades WHERE UPPER(COALESCE(status, '')) IN ({quoted_statuses})", 0), 0.0))


def _paper_closed_trades_criterion(db_path: str, csv_path: str) -> Dict[str, Any]:
    internal_count = _internal_paper_closed_count(db_path)
    rows = _read_csv_rows(csv_path)
    csv_count = sum(1 for row in rows if str(row.get("status", "")).upper() in CLOSED_TRADE_STATUSES)
    closed_count = internal_count
    passed = closed_count >= MIN_CLOSED_PAPER_TRADES
    return _criterion(
        "internal_paper_trades closed >= 100",
        passed,
        f"closed_paper_trades: {closed_count}/{MIN_CLOSED_PAPER_TRADES} (internal_paper_trades={internal_count}, legacy_csv={csv_count})",
        "Insufficient closed PAPER trades for Phase 3 readiness evidence.",
        "Continue PAPER_ONLY collection until internal_paper_trades has at least 100 naturally closed paper trades.",
    )


def _label_quality_criterion(paths: Iterable[str]) -> Dict[str, Any]:
    selected_path = ""
    audit: Dict[str, Any] = {}
    for path in paths:
        payload = _read_json(path)
        if payload:
            selected_path = path
            audit = payload
            break
    verdict = str(audit.get("verdict") or audit.get("status") or audit.get("label_quality") or "MISSING").upper()
    passed = verdict in {"PASS", "REVIEW"}
    return _criterion(
        "label quality audit PASS/REVIEW",
        passed,
        f"verdict={verdict}, path={selected_path or 'missing'}",
        "Label quality audit is missing or failed.",
        "Run label_quality_audit.py and remediate any FAIL verdict before Phase 3 review.",
    )


def _backup_status_criterion(_backup_dir: str, _db_path: str) -> Dict[str, Any]:
    report = _read_json("reports/backup_verification.json")
    database = report.get("database", {}) if isinstance(report.get("database"), dict) else {}
    backup = report.get("backup", {}) if isinstance(report.get("backup"), dict) else {}
    valid = report.get("valid") is True
    verdict = str(report.get("verdict") or "MISSING").upper()
    backup_evidence = bool(backup.get("latest_exists") and backup.get("latest_integrity_ok"))
    db_ok = bool(database.get("exists") and database.get("integrity_ok"))
    passed = bool(report) and valid and db_ok and backup_evidence and verdict == "PASS"
    detail = (
        f"verdict={verdict}, db_integrity={database.get('integrity', 'missing')}, "
        f"backup={backup.get('latest_path') or 'missing'}, "
        f"backup_integrity={backup.get('latest_integrity', 'missing')}, "
        f"backup_age_hours={backup.get('latest_age_hours')}"
    )
    return _criterion(
        "backup verification artifact PASS",
        passed,
        detail,
        "No verified SQLite backup evidence is available.",
        "Run python backup_verification.py or python main.py --phase3-remediation; if no backup exists, create one through the approved maintenance path before review.",
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_minutes_from_timestamp(value: Any) -> float:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return 999999.0
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 60, 2)


def _file_age_minutes(path: str) -> float:
    try:
        modified = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        return round((datetime.now(timezone.utc) - modified).total_seconds() / 60, 2)
    except OSError:
        return 999999.0


def _stress_test_criterion(paths: Iterable[str], fresh_minutes: int = 24 * 60) -> Dict[str, Any]:
    selected_path = ""
    report: Dict[str, Any] = {}
    for path in paths:
        payload = _read_json(path)
        if payload:
            selected_path = path
            report = payload
            break
    verdict = str(report.get("verdict") or report.get("status") or report.get("result") or "MISSING").upper()
    generated_age = _age_minutes_from_timestamp(report.get("generated_at")) if report else 999999.0
    fresh = generated_age <= fresh_minutes
    passed = bool(report) and verdict in {"PASS", "REVIEW"} and fresh
    return _criterion(
        "stress test report PASS/REVIEW and fresh",
        passed,
        f"verdict={verdict}, path={selected_path or 'missing'}, age_minutes={generated_age:.2f}, fresh_minutes={fresh_minutes}",
        "Passing or reviewable fresh stress test report is missing.",
        "Run python stress_test_simulator.py or python main.py --phase3-remediation; keep any true risk-budget FREEZE/HALT as a blocker.",
    )


def _operator_runbook_criterion(paths: Iterable[str]) -> Dict[str, Any]:
    required_terms = {
        "dashboard access": ("dashboard", "access"),
        "restart orchestrator": ("restart", "orchestrator"),
        "restart dashboard": ("restart", "dashboard"),
        "governance incident rule": ("governance", "incident"),
        "git update safety": ("git", "update", "safety"),
        "PAPER_ONLY boundary": ("paper_only", "live execution"),
    }
    selected = ""
    missing: List[str] = []
    for path in paths:
        if not os.path.exists(path) or os.path.getsize(path) <= 0:
            continue
        selected = path
        try:
            text = Path(path).read_text(encoding="utf-8").lower()
        except OSError:
            text = ""
        normalized = text.replace("-", "_")
        missing = [label for label, terms in required_terms.items() if not all(term in normalized for term in terms)]
        if not missing:
            break
    passed = bool(selected) and not missing
    return _criterion(
        "operator runbook complete",
        passed,
        f"path={selected or 'missing'}, missing_sections={missing or 'none'}",
        "Operator runbook is missing or does not cover every required operations topic.",
        "Update docs/OPERATOR_RUNBOOK.md with dashboard access, restart procedures, governance incident rules, git update safety, and PAPER_ONLY/no-live-execution boundaries.",
    )


def _tmux_session_status(session_name: str) -> str:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNAVAILABLE"
    return "RUNNING" if result.returncode == 0 else "MISSING"


def _system_health_criterion(db_path: str, health_stale_minutes: int = 10) -> Dict[str, Any]:
    db_connection = _connect_read_only(db_path)
    db_readable = db_connection is not None
    if db_connection is not None:
        db_connection.close()
    heartbeat_age = _number(
        _query_scalar(
            db_path,
            "SELECT (julianday('now') - julianday(timestamp)) * 24 * 60 FROM runtime_heartbeats ORDER BY id DESC LIMIT 1",
            999999,
        ),
        999999.0,
    )
    latest_health = _number(
        _query_scalar(db_path, "SELECT system_health_score FROM runtime_heartbeats WHERE system_health_score IS NOT NULL ORDER BY id DESC LIMIT 1", 0),
        0.0,
    )
    diagnostics = _read_json("logs/orchestrator_diagnostics.json")
    diag_status = str(diagnostics.get("status") or diagnostics.get("event") or "UNKNOWN").upper()
    diagnostics_ok = diag_status not in {"CRASH", "FAILED", "ERROR"}
    hunter_tmux = _tmux_session_status("hunter")
    dashboard_tmux = _tmux_session_status("dashboard")
    daily_report_age = min(_file_age_minutes("logs/daily_ops_report.json"), _file_age_minutes("logs/daily_ops_report.md"))
    heartbeat_fresh = heartbeat_age <= health_stale_minutes
    daily_report_fresh = daily_report_age <= (24 * 60)
    tmux_ok_or_unavailable = hunter_tmux in {"RUNNING", "UNAVAILABLE"} and dashboard_tmux in {"RUNNING", "UNAVAILABLE"}
    passed = db_readable and heartbeat_fresh and latest_health >= 70 and diagnostics_ok and daily_report_fresh and tmux_ok_or_unavailable
    return _criterion(
        "system health stable",
        passed,
        (
            f"db_readable={db_readable}, heartbeat_age_minutes={heartbeat_age:.2f}, "
            f"system_health_score={latest_health:.0f}, diagnostics={diag_status}, "
            f"tmux_hunter={hunter_tmux}, tmux_dashboard={dashboard_tmux}, "
            f"daily_ops_report_age_minutes={daily_report_age:.2f}"
        ),
        "System health is not stable, heartbeat is stale/missing, or daily ops evidence is stale/missing.",
        "Stabilize orchestrator heartbeat, verify tmux sessions where available, refresh daily ops report, and resolve diagnostics before Phase 3 review.",
    )


def _paper_only_criterion(*reports: Dict[str, Any]) -> Dict[str, Any]:
    violations: List[str] = []
    for index, report in enumerate(reports, start=1):
        constraints = report.get("governance_constraints", {}) if isinstance(report.get("governance_constraints"), dict) else {}
        paper_only = report.get("paper_only", constraints.get("paper_only"))
        if paper_only not in {True, "PAPER_ONLY"}:
            violations.append(f"report_{index}:paper_only={paper_only}")
        for key in ("live_trading_enabled", "execution_enabled"):
            if report.get(key) is True:
                violations.append(f"report_{index}:{key}=true")
        for key in ("no_live_trading", "no_execution", "no_real_execution", "no_broker_routing", "no_broker_order_routing"):
            if key in constraints and constraints.get(key) is False:
                violations.append(f"report_{index}:{key}=false")
    passed = not violations
    return _criterion(
        "PAPER_ONLY enforced",
        passed,
        "violations=none" if passed else f"violations={violations}",
        "PAPER_ONLY governance constraint is not consistently enforced.",
        "Restore PAPER_ONLY/read-only/no-routing constraints before any Phase 3 readiness review.",
    )


def calculate_phase3_readiness(
    db_path: str = "mamuyy_hunter.db",
    paper_trades_path: str = "paper_trades.csv",
    backup_dir: str = "db_backups",
    output_path: str = REPORT_PATH,
    write_report: bool = True,
    health_stale_minutes: int = 10,
) -> Dict[str, Any]:
    governance_audit = _read_json("reports/governance_audit.json")
    portfolio_budget = _read_json("reports/portfolio_risk_budget.json")
    promotion_scorecard = _read_json("reports/promotion_scorecard.json")

    criteria = [
        _governance_audit_criterion("reports/governance_audit.json"),
        _portfolio_risk_budget_criterion("reports/portfolio_risk_budget.json"),
        _promotion_scorecard_criterion("reports/promotion_scorecard.json"),
        _paper_closed_trades_criterion(db_path, paper_trades_path),
        _label_quality_criterion(("logs/label_quality_audit.json", "reports/label_quality_audit.json")),
        _backup_status_criterion(backup_dir, db_path),
        _stress_test_criterion(("reports/stress_test_report.json", "reports/stress_test.json", "logs/stress_test_report.json")),
        _operator_runbook_criterion(("docs/OPERATOR_RUNBOOK.md", "docs/operator_runbook.md", "OPERATOR_RUNBOOK.md")),
        _system_health_criterion(db_path, health_stale_minutes=health_stale_minutes),
        _paper_only_criterion(governance_audit, portfolio_budget, promotion_scorecard),
    ]

    passed_criteria = [item["name"] for item in criteria if item["passed"]]
    failed_criteria = [item["name"] for item in criteria if not item["passed"]]
    blockers = [item["blocker"] for item in criteria if item.get("blocker")]
    next_actions = [item["next_action"] for item in criteria if item.get("next_action")]
    readiness_percent = round((len(passed_criteria) / READINESS_CRITERIA_COUNT) * 100, 2)

    if readiness_percent >= 100.0:
        status = "READY_FOR_REVIEW"
    elif readiness_percent >= 70.0:
        status = "CANDIDATE"
    else:
        status = "LOCKED"

    closed_paper_trades = _internal_paper_closed_count(db_path)
    report = {
        "generated_at": _now_iso(),
        "paper_only": True,
        "closed_paper_trades": closed_paper_trades,
        "closed_paper_trades_target": MIN_CLOSED_PAPER_TRADES,
        "closed_paper_trades_progress": f"{closed_paper_trades}/{MIN_CLOSED_PAPER_TRADES}",
        "readiness_percent": readiness_percent,
        "status": status,
        "passed_criteria": passed_criteria,
        "failed_criteria": failed_criteria,
        "criteria_details": criteria,
        "blockers": blockers,
        "next_actions": next_actions,
        "governance_constraints": GOVERNANCE_CONSTRAINTS,
        "source_reports": {
            "governance_audit": "reports/governance_audit.json",
            "portfolio_risk_budget": "reports/portfolio_risk_budget.json",
            "promotion_scorecard": "reports/promotion_scorecard.json",
            "backup_verification": "reports/backup_verification.json",
            "label_quality_audit": "reports/label_quality_audit.json",
            "stress_test_report": "reports/stress_test_report.json",
        },
    }
    if write_report:
        _write_json(output_path, report)
    return report


def format_phase3_readiness(report: Dict[str, Any]) -> str:
    top_blocker = report.get("blockers", ["none"])[0] if report.get("blockers") else "none"
    return (
        "PHASE 3 READINESS\n"
        f"Readiness: {report.get('readiness_percent', 0)}%\n"
        f"Status: {report.get('status', 'LOCKED')}\n"
        f"Closed Paper Trades: {report.get('closed_paper_trades_progress', '0/100')}\n"
        f"Passed: {len(report.get('passed_criteria', []))}/{READINESS_CRITERIA_COUNT}\n"
        f"Failed: {len(report.get('failed_criteria', []))}/{READINESS_CRITERIA_COUNT}\n"
        f"Top Blocker: {top_blocker}\n"
        "PAPER_ONLY remains active. No execution, broker routing, strategy promotion, or Phase 3 unlock automation was changed."
    )


if __name__ == "__main__":
    result = calculate_phase3_readiness()
    print(format_phase3_readiness(result))
    print(f"Report generated: {REPORT_PATH}")
