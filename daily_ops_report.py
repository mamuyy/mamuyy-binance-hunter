import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from config import config
from database import db_health_check, init_db
from health_guardian import _tmux_available, _tmux_session_exists, resolve_runtime_heartbeat
from portfolio_analytics import calculate_portfolio_analytics
from telegram_notifier import send_or_preview


TABLES = [
    "signals",
    "paper_trades",
    "flow_logs",
    "regime_logs",
    "ml_results",
    "walkforward_results",
    "shadow_trades",
    "internal_paper_trades",
    "broadcast_events",
    "telegram_events",
    "risk_events",
    "runtime_heartbeats",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if pd.isna(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


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


def _age_minutes(value: Any) -> float:
    timestamp = _parse_timestamp(value)
    if not timestamp:
        return 9999.0
    return round((datetime.now(timezone.utc) - timestamp).total_seconds() / 60, 2)


def _read_table(db_path: str, table: str, limit: int = 100) -> pd.DataFrame:
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            if not exists:
                return pd.DataFrame()
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _table_counts(db_path: str) -> Dict[str, int]:
    counts = {}
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            for table in TABLES:
                try:
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = 0
    except sqlite3.Error:
        return {table: 0 for table in TABLES}
    return counts


def _latest_csv_row(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    if df.empty:
        return {}
    return df.iloc[-1].to_dict()


def _broadcast_counts(db_path: str) -> Dict[str, int]:
    broadcasts = _read_table(db_path, "broadcast_events", limit=1000)
    if broadcasts.empty or "route_status" not in broadcasts.columns:
        return {"accepted": 0, "rejected": 0}
    status = broadcasts["route_status"].fillna("").astype(str).str.upper()
    return {
        "accepted": int((status == "ROUTED").sum()),
        "rejected": int(status.isin(["REJECTED", "SKIPPED", "FAILED"]).sum()),
    }


def _latest_telegram_status(db_path: str) -> str:
    telegram = _read_table(db_path, "telegram_events", limit=1)
    if telegram.empty:
        return "-"
    row = telegram.iloc[0]
    return str(row.get("send_status") or "-")


def _latest_guardian_event(db_path: str) -> Dict[str, Any]:
    risks = _read_table(db_path, "risk_events", limit=20)
    if risks.empty:
        return {}
    guardian = risks[
        risks.get("regime_name", pd.Series(dtype=str)).fillna("").astype(str).str.upper().eq("HEALTH_GUARDIAN")
        | risks.get("session_name", pd.Series(dtype=str)).fillna("").astype(str).str.len().gt(0)
    ]
    if guardian.empty:
        return {}
    row = guardian.iloc[0]
    return {
        "status": str(row.get("status") or "-"),
        "reason": str(row.get("reason") or row.get("reasons_json") or "-"),
        "source": "risk_events",
        "timestamp": str(row.get("timestamp") or ""),
        "age_minutes": _age_minutes(row.get("timestamp")),
    }


def _latest_guardian_status(db_path: str, db_ok: bool, heartbeat: Dict[str, Any]) -> Dict[str, Any]:
    heartbeat_age = _num(heartbeat.get("age_minutes"), 9999.0)
    heartbeat_fresh = bool(heartbeat.get("timestamp")) and heartbeat_age <= config.health_guardian_stale_minutes
    heartbeat_source = str(heartbeat.get("source") or "-")
    primary_heartbeat_fresh = heartbeat_fresh and not heartbeat_source.startswith("fallback_")

    tmux_available = _tmux_available()
    hunter_running = _tmux_session_exists(config.health_guardian_hunter_session) if tmux_available else False
    dashboard_running = _tmux_session_exists(config.health_guardian_dashboard_session) if tmux_available else False

    if db_ok and primary_heartbeat_fresh and hunter_running and dashboard_running:
        return {
            "status": "SAFE",
            "reason": "fresh heartbeat and tmux sessions running",
            "source": "runtime_fresh",
            "hunter_session": "RUNNING",
            "dashboard_session": "RUNNING",
            "tmux_available": tmux_available,
        }

    runtime_reasons: List[str] = []
    if not db_ok:
        runtime_reasons.append("database health check failed")
    if not heartbeat_fresh:
        runtime_reasons.append(f"heartbeat stale/missing: {heartbeat_age}m")
    elif heartbeat_source.startswith("fallback_"):
        runtime_reasons.append(f"heartbeat table stale; using {heartbeat_source}")
    if not tmux_available:
        runtime_reasons.append("tmux unavailable in current environment")
    else:
        if not hunter_running:
            runtime_reasons.append(f"tmux session missing: {config.health_guardian_hunter_session}")
        if not dashboard_running:
            runtime_reasons.append(f"tmux session missing: {config.health_guardian_dashboard_session}")

    stale_event_cutoff = max(30, config.health_guardian_stale_minutes * 3)
    event = _latest_guardian_event(db_path)
    runtime_status_known = bool(heartbeat.get("timestamp")) or tmux_available
    if event and not runtime_status_known and _num(event.get("age_minutes"), 9999.0) <= stale_event_cutoff:
        return event

    status = "WATCH" if heartbeat_fresh and db_ok else "HALT"
    return {
        "status": status,
        "reason": "; ".join(runtime_reasons) or "runtime status unresolved",
        "source": "runtime_fresh" if runtime_status_known else "runtime_unresolved",
        "hunter_session": "RUNNING" if hunter_running else "MISSING",
        "dashboard_session": "RUNNING" if dashboard_running else "MISSING",
        "tmux_available": tmux_available,
    }


def _warnings(report: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    if not report.get("database_ok"):
        warnings.append("database health check failed")
    if _num(report.get("heartbeat_age_minutes"), 9999) > config.health_guardian_stale_minutes:
        warnings.append(f"heartbeat stale: {report.get('heartbeat_age_minutes')}m")
    if str(report.get("macro_state")).upper() in {"HIGH_STRESS", "PANIC"}:
        warnings.append(f"macro stress active: {report.get('macro_state')}")
    if str(report.get("cross_market_state")).upper() in {"CROSS_MARKET_STRESS", "SAFE_HAVEN_ROTATION"}:
        warnings.append(f"cross-market stress active: {report.get('cross_market_state')}")
    if _num(report.get("internal_paper_max_drawdown")) < -10:
        warnings.append(f"paper drawdown warning: {report.get('internal_paper_max_drawdown')}%")
    if str(report.get("latest_guardian_status")).upper() == "HALT":
        warnings.append("health guardian status HALT")
    if str(report.get("latest_telegram_send_status")).upper() == "FAILED":
        warnings.append("latest Telegram send failed")
    telegram_result = report.get("telegram_result", {})
    if str(telegram_result.get("log_status", "")).upper() == "DEGRADED":
        warnings.append("telegram event logging degraded")
    return warnings[:8]


def _warning_category(reason: str) -> str:
    text = str(reason or "").lower()
    if "database" in text or "db lock" in text or "locked" in text:
        return "DB_LOCK"
    if "heartbeat stale" in text:
        return "HEARTBEAT_STALE"
    if "broadcast" in text and ("reject" in text or "skip" in text):
        return "BROADCAST_REJECTION"
    if "macro stress" in text or "cross-market stress" in text:
        return "REGIME_STRESS"
    if "telegram" in text and ("degraded" in text or "failed" in text):
        return "TELEMETRY_DEGRADED"
    if "guardian" in text and ("halt" in text or "missing" in text):
        return "GUARDIAN_RECOVERY"
    if "freshness" in text or "stale" in text:
        return "DATA_FRESHNESS"
    if "drawdown" in text:
        return "RESOURCE_PRESSURE"
    return "TELEMETRY_DEGRADED"


def _warning_reason_aggregation(report: Dict[str, Any], incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}
    for reason in report.get("top_warning_reasons", []):
        key = str(reason)
        item = aggregated.setdefault(
            key,
            {"warning_reason": key, "category": _warning_category(key), "occurrence_count": 0},
        )
        item["occurrence_count"] += 1

    for incident in incidents:
        reason = str(incident.get("incident_type") or "incident")
        key = f"incident:{reason}"
        item = aggregated.setdefault(
            key,
            {"warning_reason": key, "category": str(incident.get("incident_type") or "TELEMETRY_DEGRADED"), "occurrence_count": 0},
        )
        item["occurrence_count"] += int(incident.get("occurrence_count") or 0)

    return sorted(aggregated.values(), key=lambda x: x["occurrence_count"], reverse=True)[:8]


def _regime_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    macro_state = str(report.get("macro_state") or "UNKNOWN")
    cross_state = str(report.get("cross_market_state") or "UNKNOWN")
    stress_score = max(_num(report.get("macro_risk_score")), _num(report.get("cross_market_stress_score")))
    stress_regime = macro_state if macro_state in {"HIGH_STRESS", "PANIC"} else cross_state
    transition = macro_state != "UNKNOWN" and cross_state != "UNKNOWN" and macro_state != cross_state
    confidence = "HIGH" if stress_score < 0.4 else "MEDIUM" if stress_score < 0.7 else "LOW"
    warning_reason = "none"
    if stress_score >= 0.7:
        warning_reason = f"stress score elevated: {stress_score}"
    elif transition:
        warning_reason = f"regime transition observed: macro={macro_state} cross={cross_state}"
    return {
        "dominant_regime": macro_state if macro_state != "UNKNOWN" else cross_state,
        "stress_regime": stress_regime,
        "regime_transition_detected": transition,
        "regime_confidence": confidence,
        "regime_warning_reason": warning_reason,
    }


def _recommended_action(warnings: List[str], report: Dict[str, Any]) -> str:
    if any("heartbeat stale" in warning for warning in warnings):
        return "Check hunter tmux session and run python main.py --health-guardian-once."
    if any("database" in warning for warning in warnings):
        return "Run python main.py --db-check and verify SQLite disk permissions."
    if any("macro stress" in warning or "cross-market stress" in warning for warning in warnings):
        return "Keep PAPER_ONLY mode conservative; review Opportunity Allocation and Broadcast Control."
    if _num(report.get("broadcast_rejected_count")) > _num(report.get("broadcast_accepted_count")):
        return "Review allocation tiers and rejected broadcast reasons before relaxing routing."
    return "System is monitorable. Continue data collection and review dashboard."


def _markdown(report: Dict[str, Any]) -> str:
    warnings = report.get("top_warning_reasons") or ["none"]
    return "\n".join(
        [
            "# MAMUYY Hunter Daily Ops Report",
            "",
            f"- Generated: `{report.get('timestamp')}`",
            f"- PAPER_ONLY: `{report.get('paper_only')}`",
            "",
            "## Runtime",
            f"- Runtime Status: `{report.get('runtime_status')}`",
            f"- Heartbeat Age: `{report.get('heartbeat_age_minutes')}m`",
            f"- Heartbeat Source: `{report.get('heartbeat_source')}`",
            f"- Database OK: `{report.get('database_ok')}`",
            "",
            "## Macro",
            f"- Macro State: `{report.get('macro_state')}`",
            f"- Macro Risk Score: `{report.get('macro_risk_score')}`",
            f"- Cross Market State: `{report.get('cross_market_state')}`",
            f"- Cross Market Stress Score: `{report.get('cross_market_stress_score')}`",
            "",
            "## Regime Summary",
            f"- Dominant Regime: `{report.get('regime_summary', {}).get('dominant_regime')}`",
            f"- Stress Regime: `{report.get('regime_summary', {}).get('stress_regime')}`",
            f"- Regime Transition Detected: `{report.get('regime_summary', {}).get('regime_transition_detected')}`",
            f"- Regime Confidence: `{report.get('regime_summary', {}).get('regime_confidence')}`",
            f"- Regime Warning Reason: `{report.get('regime_summary', {}).get('regime_warning_reason')}`",
            "",
            "## Paper Trading",
            f"- Internal Paper Trade Count: `{report.get('internal_paper_trade_count')}`",
            f"- Internal Paper Total PnL: `{report.get('internal_paper_total_pnl')}`",
            f"- Internal Paper Max Drawdown: `{report.get('internal_paper_max_drawdown')}`",
            "",
            "## Routing & Telegram",
            f"- Broadcast Accepted: `{report.get('broadcast_accepted_count')}`",
            f"- Broadcast Rejected/Skipped: `{report.get('broadcast_rejected_count')}`",
            f"- Latest Telegram Status: `{report.get('latest_telegram_send_status')}`",
            f"- Latest Guardian Status: `{report.get('latest_guardian_status')}`",
            f"- Guardian Source: `{report.get('latest_guardian_source')}`",
            f"- Guardian Reason: `{report.get('latest_guardian_reason')}`",
            "",
            "## Warnings",
            *[f"- {warning}" for warning in warnings],
            "",
            "## Warning Reason Aggregation",
            *[
                f"- [{item.get('category')}] {item.get('warning_reason')} (count={item.get('occurrence_count')})"
                for item in report.get("warning_reason_aggregation", [])
            ],
            "",
            "## Recommended Next Action",
            report.get("recommended_next_action", "-"),
        ]
    )


def _telegram_message(report: Dict[str, Any]) -> str:
    warnings = report.get("top_warning_reasons") or ["none"]
    return "\n".join(
        [
            "MAMUYY HUNTER DAILY OPS",
            "PAPER_ONLY",
            "",
            f"Runtime: {report.get('runtime_status')} ({report.get('heartbeat_source')}, {report.get('heartbeat_age_minutes')}m)",
            f"Macro: {report.get('macro_state')} risk={report.get('macro_risk_score')}",
            f"Cross: {report.get('cross_market_state')} stress={report.get('cross_market_stress_score')}",
            f"Paper: trades={report.get('internal_paper_trade_count')} pnl={report.get('internal_paper_total_pnl')} dd={report.get('internal_paper_max_drawdown')}",
            f"Broadcast: routed={report.get('broadcast_accepted_count')} rejected={report.get('broadcast_rejected_count')}",
            f"Telegram: {report.get('latest_telegram_send_status')}",
            f"Guardian: {report.get('latest_guardian_status')} via {report.get('latest_guardian_source')}",
            f"Incidents: {len(report.get('incident_log', []))}",
            f"Warnings: {' | '.join(map(str, warnings[:3]))}",
            f"Next: {report.get('recommended_next_action')}",
        ]
    )




def _db_lock_incident_summary(log_path: str = "orchestrator_log.csv") -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "incident_type": "DB_LOCK",
        "affected_module": "runtime_heartbeats",
        "first_seen_utc": "",
        "last_seen_utc": "",
        "occurrence_count": 0,
        "auto_recovered": False,
        "severity": "LOW",
        "recommended_action": "observe",
        "governance_impact": "monitoring_only",
    }
    if not os.path.exists(log_path):
        return summary

    failures: List[str] = []
    successes: List[str] = []
    with open(log_path, newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if row.get("engine") != "heartbeat_db":
                continue
            message = str(row.get("message") or "")
            ts = str(row.get("timestamp") or "")
            if "runtime_heartbeats write failed" in message and "database is locked" in message:
                failures.append(ts)
            elif "runtime_heartbeats write success" in message:
                successes.append(ts)

    if not failures:
        return summary

    summary["first_seen_utc"] = failures[0]
    summary["last_seen_utc"] = failures[-1]
    summary["occurrence_count"] = len(failures)
    summary["auto_recovered"] = bool(successes and successes[-1] >= failures[-1])
    if summary["occurrence_count"] >= 5:
        summary["severity"] = "MEDIUM"
    if summary["occurrence_count"] >= 20:
        summary["severity"] = "HIGH"
    if summary["severity"] != "LOW" or not summary["auto_recovered"]:
        summary["recommended_action"] = "increase sqlite busy_timeout/retry, audit concurrent writers, verify WAL mode"
        summary["governance_impact"] = "promotion_hold_until_stable"
    return summary
def generate_daily_ops_report(
    db_path: str = "mamuyy_hunter.db",
    markdown_path: str = "logs/daily_ops_report.md",
    json_path: str = "logs/daily_ops_report.json",
    send_telegram: bool = True,
) -> Dict[str, Any]:
    db_health = db_health_check(database_url=db_path, migrate_csv=False, backup=False)
    heartbeat = resolve_runtime_heartbeat(db_path, "orchestrator_log.csv", config.health_guardian_stale_minutes)
    counts = _table_counts(db_path)
    macro = _latest_csv_row("logs/macro_observer.csv")
    cross = _latest_csv_row("logs/cross_market_intelligence.csv")
    portfolio = calculate_portfolio_analytics(db_path)
    metrics = portfolio.get("metrics", {})
    broadcast = _broadcast_counts(db_path)
    guardian = _latest_guardian_status(db_path, bool(db_health.get("ok")), heartbeat)
    db_lock_incident = _db_lock_incident_summary("orchestrator_log.csv")

    report = {
        "timestamp": _now(),
        "paper_only": True,
        "runtime_status": "OK" if db_health.get("ok") and _num(heartbeat.get("age_minutes"), 9999) <= config.health_guardian_stale_minutes else "WATCH",
        "heartbeat_age_minutes": heartbeat.get("age_minutes", 9999.0),
        "heartbeat_source": heartbeat.get("source", "-"),
        "database_ok": bool(db_health.get("ok")),
        "database_row_counts": counts,
        "macro_state": macro.get("macro_state", "UNKNOWN"),
        "macro_risk_score": _num(macro.get("macro_risk_score")),
        "cross_market_state": cross.get("cross_market_state", "UNKNOWN"),
        "cross_market_stress_score": _num(cross.get("cross_market_stress_score")),
        "internal_paper_trade_count": int(metrics.get("trade_count", 0) or 0),
        "internal_paper_total_pnl": metrics.get("total_pnl", 0.0),
        "internal_paper_max_drawdown": metrics.get("max_drawdown", 0.0),
        "broadcast_accepted_count": broadcast["accepted"],
        "broadcast_rejected_count": broadcast["rejected"],
        "latest_telegram_send_status": _latest_telegram_status(db_path),
        "latest_guardian_status": guardian["status"],
        "latest_guardian_reason": guardian["reason"],
        "latest_guardian_source": guardian.get("source", "-"),
        "hunter_session": guardian.get("hunter_session", "-"),
        "dashboard_session": guardian.get("dashboard_session", "-"),
        "incident_log": [db_lock_incident] if db_lock_incident.get("occurrence_count", 0) > 0 else [],
        "warning_taxonomy_codes": [
            "DB_LOCK",
            "HEARTBEAT_STALE",
            "BROADCAST_REJECTION",
            "REGIME_STRESS",
            "TELEMETRY_DEGRADED",
            "GUARDIAN_RECOVERY",
            "DATA_FRESHNESS",
            "RESOURCE_PRESSURE",
        ],
    }
    warning_reasons = _warnings(report)
    report["top_warning_reasons"] = warning_reasons or ["none"]
    report["regime_summary"] = _regime_summary(report)
    report["warning_reason_aggregation"] = _warning_reason_aggregation(report, report.get("incident_log", []))
    report["recommended_next_action"] = _recommended_action(warning_reasons, report)

    os.makedirs(os.path.dirname(markdown_path) or ".", exist_ok=True)
    markdown = _markdown(report)
    with open(markdown_path, "w", encoding="utf-8") as markdown_file:
        markdown_file.write(markdown + "\n")
    with open(json_path, "w", encoding="utf-8") as json_file:
        json.dump(report, json_file, indent=2, default=str)

    telegram = {"send_status": "SKIPPED", "enabled": config.telegram_enabled, "log_status": "SKIPPED", "log_warning": ""}
    if send_telegram:
        try:
            telegram = send_or_preview("DAILY_OPS_REPORT", _telegram_message(report), db_path)
        except Exception as exc:
            telegram = {
                "send_status": "DEGRADED",
                "enabled": config.telegram_enabled,
                "error_message": str(exc),
                "log_status": "DEGRADED",
                "log_warning": f"telegram reporting degraded: {exc}",
            }
    report["telegram_result"] = {
        "send_status": telegram.get("send_status"),
        "enabled": telegram.get("enabled"),
        "error_message": telegram.get("error_message", ""),
        "log_status": telegram.get("log_status", "UNKNOWN"),
        "log_warning": telegram.get("log_warning", ""),
    }
    report["markdown_path"] = markdown_path
    report["json_path"] = json_path
    return report


def format_daily_ops_report(report: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "DAILY OPS REPORT",
            f"PAPER_ONLY: {report.get('paper_only')}",
            f"Runtime: {report.get('runtime_status')}",
            f"Heartbeat: {report.get('heartbeat_age_minutes')}m via {report.get('heartbeat_source')}",
            f"Macro: {report.get('macro_state')} ({report.get('macro_risk_score')})",
            f"Cross Market: {report.get('cross_market_state')} ({report.get('cross_market_stress_score')})",
            f"Paper Trades: {report.get('internal_paper_trade_count')} | PnL: {report.get('internal_paper_total_pnl')} | DD: {report.get('internal_paper_max_drawdown')}",
            f"Broadcast: routed={report.get('broadcast_accepted_count')} rejected={report.get('broadcast_rejected_count')}",
            f"Telegram: {report.get('telegram_result', {}).get('send_status')}",
            f"Guardian: {report.get('latest_guardian_status')} via {report.get('latest_guardian_source')}",
            f"Warnings: {', '.join(map(str, report.get('top_warning_reasons', [])))}",
            f"Next Action: {report.get('recommended_next_action')}",
            f"Markdown: {report.get('markdown_path')}",
            f"JSON: {report.get('json_path')}",
        ]
    )
