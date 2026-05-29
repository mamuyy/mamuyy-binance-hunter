"""Read-only governance report refresh helpers for Phase 3 readiness.

The refresh command is intentionally bounded to report artifacts. It does not
change databases, models, thresholds, labels, execution state, broker routing,
live trading settings, or Phase 3 lock state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPORT_SPECS: Dict[str, Dict[str, Any]] = {
    "drift_detection": {
        "path": "reports/drift_detection_report.json",
        "script": "scripts/regime_drift_detection.py",
    },
    "emergency_brake": {
        "path": "reports/emergency_brake_simulation.json",
        "script": "scripts/apply_emergency_brake.py",
    },
    "transition_prediction": {
        "path": "reports/transition_prediction_report.json",
        "script": "scripts/regime_transition_prediction.py",
    },
}

READ_ONLY_GOVERNANCE = {
    "PAPER_ONLY": True,
    "read_only": True,
    "strategy_mutation": False,
    "broker_order_execution_changes": False,
    "auto_promotion": False,
    "recommendation_only": True,
    "live_execution": False,
    "engine_changes": False,
    "strategy_deployment": False,
    "database_mutation": False,
    "model_retraining": False,
    "threshold_tuning": False,
    "phase3_unlock": False,
}


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
    for key in ("generated_at", "generated_at_utc"):
        timestamp = _parse_timestamp(payload.get(key))
        if timestamp is not None:
            return timestamp
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return None


def _age_hours(path: str, payload: Dict[str, Any], now: datetime) -> float | None:
    timestamp = _report_timestamp(path, payload)
    if timestamp is None:
        return None
    return round(max(0.0, (now - timestamp).total_seconds()) / 3600.0, 2)


def _fallback_report(report_name: str, path: str, previous_payload: Dict[str, Any], generated_at: str, reason: str) -> Dict[str, Any]:
    payload = deepcopy(previous_payload) if previous_payload else {}
    payload.update(
        {
            "generated_at": generated_at,
            "source": "READ_ONLY_REFRESH",
            "paper_only": True,
            "execution_enabled": False,
            "live_trading_enabled": False,
            "refresh_status": "FALLBACK_GENERATED",
            "refresh_reason": reason,
            "governance": READ_ONLY_GOVERNANCE.copy(),
        }
    )

    if report_name == "drift_detection":
        payload.setdefault("drift_label", "UNKNOWN")
        payload.setdefault("drift_score", 0.0)
        payload.setdefault("summary", {})
        payload["summary"].setdefault("drift_label", payload.get("drift_label", "UNKNOWN"))
        payload["summary"].setdefault("drift_score", payload.get("drift_score", 0.0))
        payload.setdefault("collapse", {})
    elif report_name == "emergency_brake":
        payload.setdefault("brake_active", False)
        payload.setdefault("brake_risk_level", "NONE")
        payload.setdefault("summary", {})
        payload["summary"].setdefault("brake_trigger_count", payload.get("trigger_count", 0))
        payload["summary"].setdefault("brake_source", "READ_ONLY_REFRESH")
        payload["summary"].setdefault("brake_risk_level", payload.get("brake_risk_level", "NONE"))
    elif report_name == "transition_prediction":
        payload.setdefault("latest_early_warning", {})
        payload["latest_early_warning"].setdefault("score", payload.get("early_warning_score", 0.0))
        payload["latest_early_warning"].setdefault("label", payload.get("early_warning_label", "UNKNOWN"))
        payload["latest_early_warning"].setdefault("timestamp", generated_at)
        payload.setdefault("transition_matrix", {"rows": 0, "top_transitions": []})

    if "generated_at_utc" in payload:
        payload["generated_at_utc"] = generated_at
    payload["artifact_path"] = path
    return payload


def _stamp_generated_report(path: str, generated_at: str) -> Dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return {}
    payload["generated_at"] = generated_at
    payload["source"] = "READ_ONLY_REFRESH"
    payload["paper_only"] = True
    payload["execution_enabled"] = False
    payload["live_trading_enabled"] = False
    payload["refresh_status"] = "REFRESHED"
    payload["governance"] = {**READ_ONLY_GOVERNANCE, **(payload.get("governance") if isinstance(payload.get("governance"), dict) else {})}
    _write_json(path, payload)
    return payload


def _run_existing_generator(script_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, script_path],
        check=False,
        text=True,
        capture_output=True,
    )


def refresh_governance_reports() -> Dict[str, Any]:
    """Refresh governance report artifacts and return CLI diagnostics."""
    now = _utc_now()
    diagnostics: Dict[str, Any] = {
        "generated_at": now.isoformat(),
        "paper_only": True,
        "source": "READ_ONLY_REFRESH",
        "refreshed": [],
        "skipped": [],
        "safety": {
            "database_mutation": False,
            "model_retraining": False,
            "threshold_tuning": False,
            "execution_mutation": False,
            "broker_routing": False,
            "live_trading": False,
            "phase3_unlock": False,
        },
    }

    for report_name, spec in REPORT_SPECS.items():
        path = str(spec["path"])
        script = str(spec["script"])
        previous_payload = _read_json(path)
        previous_age = _age_hours(path, previous_payload, now)
        generated_at = _utc_now().isoformat()
        method = "existing_generator"
        reason = ""

        if Path(script).exists():
            completed = _run_existing_generator(script)
            if completed.returncode == 0 and Path(path).exists():
                payload = _stamp_generated_report(path, generated_at)
                status = "refreshed"
            else:
                method = "safe_fallback"
                reason = (completed.stderr or completed.stdout or f"Generator exited {completed.returncode}").strip()[-500:]
                payload = _fallback_report(report_name, path, previous_payload, generated_at, reason)
                _write_json(path, payload)
                status = "fallback_refreshed"
        else:
            method = "safe_fallback"
            reason = f"Generator script not found: {script}"
            payload = _fallback_report(report_name, path, previous_payload, generated_at, reason)
            _write_json(path, payload)
            status = "fallback_refreshed"

        if payload:
            diagnostics["refreshed"].append(
                {
                    "report": report_name,
                    "path": path,
                    "status": status,
                    "method": method,
                    "previous_age_hours": previous_age,
                    "new_generated_at": payload.get("generated_at") or generated_at,
                    "reason": reason,
                }
            )
        else:
            diagnostics["skipped"].append(
                {
                    "report": report_name,
                    "path": path,
                    "reason": "Generator produced no JSON payload and fallback could not be written.",
                    "previous_age_hours": previous_age,
                }
            )

    return diagnostics


def format_refresh_diagnostics(result: Dict[str, Any]) -> str:
    lines: List[str] = [
        "GOVERNANCE REPORT REFRESH",
        "Mode: PAPER_ONLY read-only analytics; no database/model/threshold/execution/routing/live-trading mutation; no Phase 3 unlock.",
        f"Generated At: {result.get('generated_at')}",
        "Refreshed Reports:",
    ]
    refreshed = result.get("refreshed") or []
    if refreshed:
        for item in refreshed:
            previous_age = item.get("previous_age_hours")
            previous_age_text = "unknown" if previous_age is None else f"{previous_age}h"
            reason = f" | reason={item.get('reason')}" if item.get("reason") else ""
            lines.append(
                f"- {item.get('report')} ({item.get('path')}): {item.get('status')} via {item.get('method')} | "
                f"previous_age={previous_age_text} | new_generated_at={item.get('new_generated_at')}{reason}"
            )
    else:
        lines.append("- none")

    lines.append("Skipped Reports:")
    skipped = result.get("skipped") or []
    if skipped:
        for item in skipped:
            lines.append(f"- {item.get('report')} ({item.get('path')}): {item.get('reason')}")
    else:
        lines.append("- none")
    lines.append("Next: portfolio risk budget -> promotion scorecard -> governance audit -> phase3 readiness")
    return "\n".join(lines)
