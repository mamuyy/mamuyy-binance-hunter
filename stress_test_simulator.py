from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from config import config

REPORT_PATH = "reports/stress_test_report.json"
DOC_PATH = "docs/STRESS_TEST_REPORT.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _scenario(name: str, status: str, detail: str, blocker: str = "") -> Dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "blocker": blocker}


def generate_stress_test_report(
    output_path: str = REPORT_PATH,
    markdown_path: str = DOC_PATH,
    write_report: bool = True,
    write_markdown: bool = True,
) -> Dict[str, Any]:
    risk = _read_json("reports/portfolio_risk_budget.json")
    promotion = _read_json("reports/promotion_scorecard.json")
    brake = _read_json("reports/emergency_brake_simulation.json")
    readiness = _read_json("reports/phase3_readiness.json")

    total_exposure = _num(risk.get("total_exposure"), 0.0)
    max_allowed = _num(risk.get("max_allowed_exposure"), 50.0)
    utilization = _num(risk.get("risk_budget_utilization") or risk.get("utilization_ratio"), 0.0)
    max_symbol_exposure = _num(risk.get("max_symbol_exposure"), 0.0)
    risk_recommendation = str(risk.get("recommendation") or "UNKNOWN").upper()
    brake_context = risk.get("brake_context", {}) if isinstance(risk.get("brake_context"), dict) else {}
    brake_level = str(brake_context.get("brake_risk_level") or brake.get("risk_level") or "UNKNOWN").upper()
    trigger_count = int(_num(brake_context.get("trigger_count") or brake.get("trigger_count"), 0.0))
    top_recommendation = str(
        (promotion.get("summary", {}) if isinstance(promotion.get("summary"), dict) else {}).get("top_recommendation")
        or "UNKNOWN"
    ).upper()

    scenarios: List[Dict[str, Any]] = []
    if max_allowed <= 0:
        scenarios.append(_scenario("concentration pressure", "FAIL", "max_allowed_exposure is zero or missing", "Risk budget cap unavailable."))
    elif total_exposure > max_allowed:
        scenarios.append(_scenario(
            "concentration pressure",
            "REVIEW",
            f"total_exposure={total_exposure:.2f}% exceeds max_allowed={max_allowed:.2f}%",
            "Exposure must normalize naturally before Phase 3 unlock.",
        ))
    elif max_symbol_exposure > max_allowed * 0.5:
        scenarios.append(_scenario("concentration pressure", "REVIEW", f"max_symbol_exposure={max_symbol_exposure:.2f}% is concentrated"))
    else:
        scenarios.append(_scenario("concentration pressure", "PASS", f"total_exposure={total_exposure:.2f}%, max_allowed={max_allowed:.2f}%"))

    if utilization >= 300:
        dd_status = "REVIEW"
        dd_detail = f"risk_budget_utilization={utilization:.2f}% implies severe drawdown pressure if losses cluster"
    elif utilization >= 100:
        dd_status = "REVIEW"
        dd_detail = f"risk_budget_utilization={utilization:.2f}% implies elevated drawdown pressure"
    else:
        dd_status = "PASS"
        dd_detail = f"risk_budget_utilization={utilization:.2f}% within simulated drawdown tolerance"
    scenarios.append(_scenario("drawdown pressure", dd_status, dd_detail, "Reduce PAPER exposure naturally; do not tune thresholds." if dd_status == "REVIEW" else ""))

    if brake_level in {"HIGH", "CRITICAL", "HALT"} or trigger_count > 0:
        scenarios.append(_scenario("emergency brake behavior", "REVIEW", f"brake_level={brake_level}, trigger_count={trigger_count}; lock/freeze behavior recognized"))
    elif brake:
        scenarios.append(_scenario("emergency brake behavior", "PASS", f"brake_level={brake_level}, trigger_count={trigger_count}"))
    else:
        scenarios.append(_scenario("emergency brake behavior", "REVIEW", "emergency brake report missing; relying on risk budget context"))

    if risk_recommendation in {"HALT", "HOLD", "FREEZE"} or top_recommendation == "FREEZE":
        scenarios.append(_scenario("risk budget freeze behavior", "REVIEW", f"risk_budget={risk_recommendation}, promotion={top_recommendation}; Phase 3 must remain LOCKED"))
    elif risk_recommendation in {"NORMAL", "WATCH", "DEFENSIVE"}:
        scenarios.append(_scenario("risk budget freeze behavior", "PASS", f"risk_budget={risk_recommendation}, promotion={top_recommendation}"))
    else:
        scenarios.append(_scenario("risk budget freeze behavior", "REVIEW", f"risk_budget={risk_recommendation}, promotion={top_recommendation}"))

    fail_count = sum(1 for item in scenarios if item["status"] == "FAIL")
    review_count = sum(1 for item in scenarios if item["status"] == "REVIEW")
    if fail_count:
        verdict = "FAIL"
    elif review_count:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    report: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "mode": "READ_ONLY_PAPER_ONLY_STRESS_TEST",
        "paper_only": True,
        "read_only": True,
        "verdict": verdict,
        "scenarios": scenarios,
        "source_reports": {
            "portfolio_risk_budget": "reports/portfolio_risk_budget.json" if risk else "missing",
            "promotion_scorecard": "reports/promotion_scorecard.json" if promotion else "missing",
            "emergency_brake_simulation": "reports/emergency_brake_simulation.json" if brake else "missing",
            "phase3_readiness_previous": "reports/phase3_readiness.json" if readiness else "missing",
        },
        "summary": {
            "fail_count": fail_count,
            "review_count": review_count,
            "pass_count": sum(1 for item in scenarios if item["status"] == "PASS"),
            "phase3_unlock_allowed": False,
        },
        "safety": [
            "PAPER_ONLY enforced",
            "Read-only assessment of existing reports only",
            "No broker routing, order placement, model retraining, threshold tuning, or auto promotion",
        ],
    }
    if write_report:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, sort_keys=True)
            file.write("\n")
    if write_markdown:
        Path(markdown_path).parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Stress Test Report",
            "",
            f"Generated: `{report['generated_at']}`",
            "",
            f"Verdict: **{verdict}**",
            "",
            "MAMUYY Hunter remains **PAPER_ONLY**. This report is read-only and does not unlock Phase 3.",
            "",
            "## Scenarios",
            "",
        ]
        for scenario in scenarios:
            lines.append(f"- **{scenario['name']}**: {scenario['status']} — {scenario['detail']}")
        lines.extend([
            "",
            "## Safety",
            "",
            "- No live execution.",
            "- No broker routing or order placement.",
            "- No model retraining, threshold tuning, or strategy auto-promotion.",
        ])
        with open(markdown_path, "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
    return report


def format_stress_test_report(report: Dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    return (
        "STRESS TEST REPORT\n"
        f"Verdict: {report.get('verdict', 'FAIL')}\n"
        f"Pass/Review/Fail: {summary.get('pass_count', 0)}/{summary.get('review_count', 0)}/{summary.get('fail_count', 0)}\n"
        "PAPER_ONLY read-only stress assessment. Phase 3 unlock is not allowed by this report."
    )


if __name__ == "__main__":
    result = generate_stress_test_report()
    print(format_stress_test_report(result))
    print(f"Report generated: {REPORT_PATH}")
    if os.path.exists(DOC_PATH):
        print(f"Markdown generated: {DOC_PATH}")
