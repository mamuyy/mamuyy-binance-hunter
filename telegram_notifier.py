import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
import requests

from config import config
from database import init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_table(db_path: str, table: str, limit: int = 20) -> pd.DataFrame:
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_latest_macro(path: str = "logs/macro_observer.csv") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    if df.empty:
        return {}
    return df.iloc[-1].to_dict()


def log_telegram_event(
    event_type: str,
    message: str,
    send_status: str,
    error_message: str = "",
    db_path: str = "mamuyy_hunter.db",
    retries: int = 3,
    base_backoff_seconds: float = 0.1,
) -> Dict[str, Any]:
    init_db(db_path)
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            with sqlite3.connect(db_path, timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute(
                    """
                    INSERT INTO telegram_events
                        (timestamp, event_type, message, send_status, error_message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (_now(), event_type, message, send_status, error_message),
                )
                connection.commit()
            return {"ok": True, "status": "LOGGED", "attempts": attempt + 1, "warning": ""}
        except sqlite3.OperationalError as exc:
            lock_error = "database is locked" in str(exc).lower()
            if not lock_error or attempt >= attempts - 1:
                return {
                    "ok": False,
                    "status": "DEGRADED",
                    "attempts": attempt + 1,
                    "warning": f"telegram event logging failed: {exc}",
                }
            time.sleep(base_backoff_seconds * (attempt + 1))
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "status": "DEGRADED",
                "attempts": attempt + 1,
                "warning": f"telegram event logging failed: {exc}",
            }
    return {
        "ok": False,
        "status": "DEGRADED",
        "attempts": attempts,
        "warning": "telegram event logging failed: retry loop exhausted",
    }


def _send_to_telegram(message: str) -> tuple[str, str]:
    if not config.telegram_enabled:
        return "PREVIEW_DISABLED", "TELEGRAM_ENABLED=false or Telegram credentials are incomplete"

    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=payload, timeout=config.request_timeout_seconds)
        response.raise_for_status()
        return "SENT", ""
    except requests.RequestException as exc:
        return "FAILED", str(exc)


def send_or_preview(
    event_type: str,
    message: str,
    db_path: str = "mamuyy_hunter.db",
) -> Dict[str, Any]:
    status, error = _send_to_telegram(message)
    log_result = log_telegram_event(event_type, message, status, error, db_path)
    return {
        "event_type": event_type,
        "enabled": config.telegram_enabled,
        "send_status": status,
        "error_message": error,
        "message": message,
        "log_status": log_result.get("status", "UNKNOWN"),
        "log_warning": log_result.get("warning", ""),
        "log_attempts": log_result.get("attempts", 0),
    }


def format_event_message(event_type: str, payload: Dict[str, Any]) -> str:
    symbol = payload.get("symbol", "-")
    side = payload.get("side", "-")
    confidence = payload.get("confidence", payload.get("score", "-"))
    macro_state = payload.get("macro_state", "-")
    allocation_tier = payload.get("allocation_tier", "-")
    reason = payload.get("reason") or payload.get("route_reason") or payload.get("status") or "-"
    return (
        "MAMUYY HUNTER\n"
        "PAPER_ONLY\n\n"
        f"Event: {event_type}\n"
        f"Symbol: {symbol}\n"
        f"Side: {side}\n"
        f"Confidence: {confidence}\n"
        f"Macro: {macro_state}\n"
        f"Allocation: {allocation_tier}\n"
        f"Reason: {reason}"
    )


def telegram_test(db_path: str = "mamuyy_hunter.db") -> Dict[str, Any]:
    message = format_event_message(
        "TELEGRAM_TEST",
        {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "confidence": 75,
            "macro_state": "TEST",
            "allocation_tier": "WATCH",
            "reason": "phone notification layer test",
        },
    )
    return send_or_preview("TELEGRAM_TEST", message, db_path)


def _latest_paper_trade_event(db_path: str) -> Dict[str, Any] | None:
    trades = _read_table(db_path, "internal_paper_trades", limit=1)
    if trades.empty:
        return None
    row = trades.iloc[0].to_dict()
    row["reason"] = row.get("status", "internal paper trade")
    return {"event_type": "INTERNAL_PAPER_TRADE", "payload": row}


def _latest_broadcast_event(db_path: str) -> Dict[str, Any] | None:
    broadcasts = _read_table(db_path, "broadcast_events", limit=1)
    if broadcasts.empty:
        return None
    row = broadcasts.iloc[0].to_dict()
    return {"event_type": "BROADCAST_EVENT", "payload": row}


def _macro_stress_event() -> Dict[str, Any] | None:
    macro = _load_latest_macro()
    state = str(macro.get("macro_state", "")).upper()
    if state not in {"HIGH_STRESS", "PANIC"}:
        return None
    return {
        "event_type": f"MACRO_{state}",
        "payload": {
            "symbol": "GLOBAL",
            "side": "-",
            "confidence": macro.get("macro_risk_score", "-"),
            "macro_state": state,
            "allocation_tier": "DEFENSIVE",
            "reason": macro.get("stress_contributors", "macro stress active"),
        },
    }


def _health_guardian_event(db_path: str) -> Dict[str, Any] | None:
    risks = _read_table(db_path, "risk_events", limit=20)
    if risks.empty:
        return None
    health_rows = risks[
        risks.get("session_name", pd.Series(dtype=str)).fillna("").astype(str).str.len() > 0
    ]
    if health_rows.empty:
        return None
    row = health_rows.iloc[0].to_dict()
    action = str(row.get("action") or "").upper()
    session_name = row.get("session_name") or "-"
    event_type = "HEALTH_GUARDIAN_RECOVERY" if "RECOVER" in action or "START" in action else "HEALTH_GUARDIAN_SESSION"
    return {
        "event_type": event_type,
        "payload": {
            "symbol": "RUNTIME",
            "side": "-",
            "confidence": row.get("risk_score", "-"),
            "macro_state": "-",
            "allocation_tier": "SYSTEM",
            "reason": f"{session_name}: {row.get('action', '-')} {row.get('result', '-')}",
        },
    }


def _model_drift_event(registry_path: str = "model_registry.json") -> Dict[str, Any] | None:
    registry = _load_json(registry_path)
    warnings = [str(item) for item in registry.get("warnings", []) if str(item).strip()]
    drift = [item for item in warnings if "DRIFT" in item.upper() or "AGING" in item.upper() or "RETRAIN" in item.upper()]
    if not drift:
        return None
    return {
        "event_type": "MODEL_DRIFT_WARNING",
        "payload": {
            "symbol": "MODEL",
            "side": "-",
            "confidence": registry.get("candidate", {}).get("accuracy", "-") if isinstance(registry.get("candidate"), dict) else "-",
            "macro_state": "-",
            "allocation_tier": "WATCH",
            "reason": " | ".join(drift[:3]),
        },
    }


def collect_notification_events(db_path: str = "mamuyy_hunter.db") -> List[Dict[str, Any]]:
    candidates = [
        _latest_paper_trade_event(db_path),
        _latest_broadcast_event(db_path),
        _macro_stress_event(),
        _health_guardian_event(db_path),
        _model_drift_event(),
    ]
    return [item for item in candidates if item]


def notify_summary(db_path: str = "mamuyy_hunter.db") -> Dict[str, Any]:
    events = collect_notification_events(db_path)
    if not events:
        message = (
            "MAMUYY HUNTER\n"
            "PAPER_ONLY\n\n"
            "Event: NOTIFY_SUMMARY\n"
            "Reason: no important notification events found"
        )
    else:
        lines = ["MAMUYY HUNTER", "PAPER_ONLY", "", "Notify Summary:"]
        for event in events:
            payload = event["payload"]
            lines.append(
                "- "
                f"{event['event_type']} | "
                f"{payload.get('symbol', '-')} | "
                f"{payload.get('side', '-')} | "
                f"conf={payload.get('confidence', '-')} | "
                f"macro={payload.get('macro_state', '-')} | "
                f"tier={payload.get('allocation_tier', '-')} | "
                f"{payload.get('reason', payload.get('route_reason', '-'))}"
            )
        message = "\n".join(lines)

    result = send_or_preview("NOTIFY_SUMMARY", message, db_path)
    result["event_count"] = len(events)
    return result


def format_notification_result(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "TELEGRAM NOTIFIER",
            f"Enabled: {result.get('enabled')}",
            f"Status: {result.get('send_status')}",
            f"Event Type: {result.get('event_type')}",
            f"Events: {result.get('event_count', 1)}",
            f"Error: {result.get('error_message') or '-'}",
            "",
            "Preview:",
            str(result.get("message") or ""),
        ]
    )
