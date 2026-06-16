#!/usr/bin/env python3
"""Phase 3.02 unified completion gate for MAMUYY Hunter.

This gate is deliberately PAPER_ONLY and read-only with respect to the runtime
SQLite database. It consolidates database integrity, Alpha Validation, the
Portfolio V2 live advisory, and hard execution locks into one operator-facing
verdict. It never sends Telegram messages, routes broker orders, or unlocks real
trading.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import alpha_validation_report
import portfolio_v2_live_advisory


DEFAULT_DB = "mamuyy_hunter.db"
DEFAULT_OUTPUT_DIR = "logs"
JSON_NAME = "hunter_completion_gate.json"
MARKDOWN_NAME = "hunter_completion_gate.md"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def inspect_database(db_path: str) -> dict[str, Any]:
    """Inspect SQLite in read-only mode and return a compact integrity snapshot."""
    path = Path(db_path)
    if not path.exists():
        return {
            "status": "MISSING",
            "path": str(path),
            "quick_check": None,
            "tables": [],
            "error": "database_not_found",
        }

    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            quick_values = [str(row[0]) for row in quick_rows]
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {
            "status": "ERROR",
            "path": str(path),
            "quick_check": None,
            "tables": [],
            "error": str(exc),
        }

    healthy = bool(quick_values) and all(value.lower() == "ok" for value in quick_values)
    return {
        "status": "OK" if healthy else "FAILED",
        "path": str(path),
        "quick_check": quick_values,
        "tables": tables,
        "table_count": len(tables),
        "error": None,
    }


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_completion(
    database: dict[str, Any],
    alpha: dict[str, Any],
    portfolio: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate operational readiness without treating it as live-trading approval."""
    alpha_verdict = alpha.get("verdict", {})
    alpha_quality = alpha.get("data_quality", {})
    readiness = alpha.get("readiness_references", {})

    safety_checks = {
        "portfolio_execution_gates_safe": portfolio.get("execution_gates_safe") is True,
        "portfolio_broker_routing_disabled": portfolio.get("broker_routing_enabled") is False,
        "portfolio_order_not_attempted": portfolio.get("order_attempted") is False,
        "alpha_real_trading_locked": alpha_verdict.get("real_trading") == "LOCKED",
        "alpha_phase3_not_unlocked": alpha_verdict.get("phase_3") == "NOT UNLOCKED",
    }
    safety_ok = all(safety_checks.values())

    database_ok = database.get("status") == "OK"
    portfolio_ready = portfolio.get("status") == "READY"
    runtime_ready = database_ok and portfolio_ready

    usable_trades = int(alpha_quality.get("rows_usable_for_calculation") or 0)
    alpha_data_ready = not alpha.get("critical_data_quality_failure") and usable_trades > 0
    research_verdict = alpha_verdict.get("research_audit_verdict", "INCONCLUSIVE")
    alpha_positive = research_verdict == "ALPHA_POSITIVE"

    if not safety_ok:
        final_verdict = "BLOCKED_SAFETY"
    elif not runtime_ready:
        final_verdict = "BLOCKED_RUNTIME"
    elif not alpha_data_ready:
        final_verdict = "PAPER_OPERATIONAL_DATA_HOLD"
    elif not alpha_positive:
        final_verdict = "PAPER_OPERATIONAL_RESEARCH_HOLD"
    else:
        final_verdict = "PAPER_OPERATIONAL_ALPHA_POSITIVE"

    blocking_reasons: list[str] = []
    if not database_ok:
        blocking_reasons.append(f"database status is {database.get('status')}")
    if not portfolio_ready:
        reasons = portfolio.get("blocked_reasons") or []
        blocking_reasons.append(
            "portfolio live advisory is not READY"
            + (": " + "; ".join(str(item) for item in reasons) if reasons else "")
        )
    for name, passed in safety_checks.items():
        if not passed:
            blocking_reasons.append(f"safety check failed: {name}")
    if not alpha_data_ready:
        blocking_reasons.append("alpha validation has no usable closed paper trades")
    elif not alpha_positive:
        blocking_reasons.append(f"alpha research verdict remains {research_verdict}")

    rolling_wr = readiness.get("rolling_win_rate_ge_45", {})
    rolling_pf = readiness.get("rolling_profit_factor_ge_1_3", {})
    max_dd = readiness.get("maximum_drawdown_pct_le_15", {})

    return {
        "final_verdict": final_verdict,
        "paper_operations_complete": safety_ok and runtime_ready,
        "research_promotion_ready": safety_ok and runtime_ready and alpha_positive,
        "real_trading": "LOCKED",
        "phase_3_unlock": "NOT_AUTHORIZED",
        "safety_ok": safety_ok,
        "runtime_ready": runtime_ready,
        "database_ok": database_ok,
        "portfolio_live_advisory_ready": portfolio_ready,
        "alpha_data_ready": alpha_data_ready,
        "alpha_positive": alpha_positive,
        "alpha_research_verdict": research_verdict,
        "usable_closed_paper_trades": usable_trades,
        "safety_checks": safety_checks,
        "readiness_references": {
            "closed_trades_500": readiness.get("closed_trades_500", False),
            "rolling_win_rate_ge_45": rolling_wr.get("passed", False),
            "rolling_profit_factor_ge_1_3": rolling_pf.get("passed", False),
            "maximum_drawdown_pct_le_15": max_dd.get("passed", "UNKNOWN"),
            "latest_rolling_win_rate": _safe_number(rolling_wr.get("value")),
            "latest_rolling_profit_factor": _safe_number(rolling_pf.get("value")),
            "maximum_drawdown_pct": _safe_number(max_dd.get("value")),
        },
        "blocking_reasons": blocking_reasons,
    }


def build_completion_report(
    db_path: str = DEFAULT_DB,
    allocation_path: str | None = None,
    heartbeat_max_age_minutes: int = portfolio_v2_live_advisory.HB_MAX_AGE,
    signal_max_age_minutes: int = portfolio_v2_live_advisory.SIGNAL_MAX_AGE,
) -> dict[str, Any]:
    database = inspect_database(db_path)
    alpha = alpha_validation_report.build_report(db_path)
    portfolio = portfolio_v2_live_advisory.build_report(
        allocation_path=allocation_path,
        database_path=db_path,
        heartbeat_max_age_minutes=heartbeat_max_age_minutes,
        signal_max_age_minutes=signal_max_age_minutes,
    )
    evaluation = evaluate_completion(database, alpha, portfolio)
    return {
        "generated_at": utc_now_iso(),
        "phase": "3.02",
        "mode": "PAPER_ONLY",
        "purpose": "UNIFIED_OPERATIONAL_COMPLETION_GATE",
        "database": database,
        "alpha_validation": {
            "critical_data_quality_failure": alpha.get("critical_data_quality_failure"),
            "data_quality": alpha.get("data_quality", {}),
            "core_performance": alpha.get("core_performance", {}),
            "stability": alpha.get("stability", {}),
            "uncertainty": alpha.get("uncertainty", {}),
            "readiness_references": alpha.get("readiness_references", {}),
            "verdict": alpha.get("verdict", {}),
        },
        "portfolio_v2_live_advisory": {
            "status": portfolio.get("status"),
            "blocked_reasons": portfolio.get("blocked_reasons", []),
            "research_baseline": portfolio.get("research_baseline", {}),
            "live_overlay": portfolio.get("live_overlay", {}),
            "execution_gates_safe": portfolio.get("execution_gates_safe"),
            "active_execution_gates": portfolio.get("active_execution_gates", []),
            "broker_routing_enabled": portfolio.get("broker_routing_enabled"),
            "order_attempted": portfolio.get("order_attempted"),
        },
        "evaluation": evaluation,
        "safety": {
            "database_open_mode": "READ_ONLY",
            "telegram_send": "DISABLED_BY_DESIGN",
            "broker_routing": "DISABLED",
            "order_attempted": False,
            "runtime_mutation": False,
            "real_trading": "LOCKED",
        },
    }


def write_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_markdown(report: dict[str, Any], path: Path) -> None:
    evaluation = report["evaluation"]
    database = report["database"]
    alpha = report["alpha_validation"]
    portfolio = report["portfolio_v2_live_advisory"]
    refs = evaluation["readiness_references"]

    lines = [
        "# MAMUYY HUNTER — Phase 3.02 Completion Gate",
        "",
        "Safety: **PAPER_ONLY / READ_ONLY / NO TELEGRAM SEND / NO BROKER ROUTING / NO ORDER ATTEMPT**.",
        "Real trading remains **LOCKED**. This report cannot authorize a Phase 3 unlock.",
        "",
        "## Final Verdict",
        f"- Verdict: **{evaluation['final_verdict']}**",
        f"- Paper operations complete: {evaluation['paper_operations_complete']}",
        f"- Research promotion ready: {evaluation['research_promotion_ready']}",
        f"- Real trading: {evaluation['real_trading']}",
        "",
        "## Runtime",
        f"- Database integrity: {database.get('status')}",
        f"- Database tables: {database.get('table_count', 0)}",
        f"- Portfolio V2 live advisory: {portfolio.get('status')}",
        "",
        "## Alpha Validation",
        f"- Research verdict: {evaluation['alpha_research_verdict']}",
        f"- Usable closed paper trades: {evaluation['usable_closed_paper_trades']}",
        f"- Stability: {alpha.get('stability', {}).get('assessment', 'INCONCLUSIVE')}",
        f"- Closed trades >= 500: {refs['closed_trades_500']}",
        f"- Latest rolling win rate >= 45%: {refs['rolling_win_rate_ge_45']}",
        f"- Latest rolling PF >= 1.3: {refs['rolling_profit_factor_ge_1_3']}",
        f"- Maximum drawdown pct <= 15%: {refs['maximum_drawdown_pct_le_15']}",
        "",
        "## Safety Locks",
    ]
    for name, passed in evaluation["safety_checks"].items():
        lines.append(f"- {name}: {'PASS' if passed else 'FAIL'}")

    lines.extend(["", "## Holds / Blocking Reasons"])
    reasons = evaluation.get("blocking_reasons") or []
    if reasons:
        lines.extend(f"- {reason}" for reason in reasons)
    else:
        lines.append("- None for PAPER_ONLY operations.")

    lines.extend(
        [
            "",
            "## Operator Meaning",
            "- `PAPER_OPERATIONAL_ALPHA_POSITIVE`: paper runtime and research evidence pass this gate; live trading is still locked.",
            "- `PAPER_OPERATIONAL_RESEARCH_HOLD`: paper runtime is operational, but alpha evidence remains inconclusive or negative.",
            "- `PAPER_OPERATIONAL_DATA_HOLD`: paper runtime is operational, but closed-trade evidence is not yet usable.",
            "- `BLOCKED_RUNTIME` or `BLOCKED_SAFETY`: repair the listed condition before relying on the system.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAMUYY Hunter Phase 3.02 completion gate")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--allocation-path")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--heartbeat-max-age-minutes",
        type=int,
        default=portfolio_v2_live_advisory.HB_MAX_AGE,
    )
    parser.add_argument(
        "--signal-max-age-minutes",
        type=int,
        default=portfolio_v2_live_advisory.SIGNAL_MAX_AGE,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_completion_report(
        db_path=args.db,
        allocation_path=args.allocation_path,
        heartbeat_max_age_minutes=args.heartbeat_max_age_minutes,
        signal_max_age_minutes=args.signal_max_age_minutes,
    )
    output_dir = Path(args.output_dir)
    json_path = output_dir / JSON_NAME
    markdown_path = output_dir / MARKDOWN_NAME
    write_json(report, json_path)
    write_markdown(report, markdown_path)

    evaluation = report["evaluation"]
    print("MAMUYY HUNTER — PHASE 3.02 COMPLETION GATE")
    print(f"Verdict: {evaluation['final_verdict']}")
    print(f"Paper Operations Complete: {evaluation['paper_operations_complete']}")
    print(f"Research Promotion Ready: {evaluation['research_promotion_ready']}")
    print("Real Trading: LOCKED")
    print(f"Created: {json_path}")
    print(f"Created: {markdown_path}")

    return 0 if evaluation["paper_operations_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
