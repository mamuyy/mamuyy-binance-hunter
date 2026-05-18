import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from bridge_tradingview import build_webhook_payload
from competition_control import evaluate_profile, market_type
from database import init_db
from macro_observer import latest_macro_state


TARGETS = [
    {"target_name": "tradingview_paper", "target_type": "TradingView paper", "profile": "balanced"},
    {"target_name": "internal_paper", "target_type": "internal paper engine", "profile": "balanced"},
    {"target_name": "telegram_alert", "target_type": "Telegram alert", "profile": "defensive"},
    {"target_name": "csv_archive", "target_type": "CSV archive", "profile": "aggressive"},
    {"target_name": "future_broker_bridge", "target_type": "future broker bridge", "profile": "defensive"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_table(db_path: str, table: str, limit: int = 200) -> pd.DataFrame:
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


def _read_allocations(path: str = "logs/opportunity_allocation.csv") -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _payload_hash(payload: Dict[str, Any], target_name: str) -> str:
    stable = json.dumps({**payload, "timestamp": "", "target_name": target_name}, sort_keys=True)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _cooldown_active(connection: sqlite3.Connection, symbol: str, target_name: str, cooldown_seconds: int) -> bool:
    row = connection.execute(
        """
        SELECT timestamp
        FROM broadcast_events
        WHERE symbol = ?
          AND target_name = ?
          AND route_status = 'ROUTED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (symbol, target_name),
    ).fetchone()
    if not row or not row[0]:
        return False
    parsed = pd.to_datetime(row[0], errors="coerce", utc=True)
    if pd.isna(parsed):
        return False
    return (datetime.now(timezone.utc) - parsed.to_pydatetime()).total_seconds() < cooldown_seconds


def _insert_event(db_path: str, event: Dict[str, Any]) -> bool:
    init_db(db_path)
    fields = [
        "timestamp",
        "symbol",
        "side",
        "confidence",
        "macro_state",
        "allocation_tier",
        "target_name",
        "target_type",
        "target_profile",
        "route_status",
        "route_reason",
        "payload_hash",
    ]
    with sqlite3.connect(db_path) as connection:
        placeholders = ", ".join(["?"] * len(fields))
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO broadcast_events ({', '.join(fields)}) VALUES ({placeholders})",
            [event.get(field) for field in fields],
        )
        connection.commit()
        return cursor.rowcount > 0


def _latest_signal(db_path: str, allocation_path: str) -> Dict[str, Any]:
    signals = _read_table(db_path, "signals", limit=100)
    if signals.empty:
        return {}
    signal = signals.sort_values("id").iloc[-1].to_dict()
    allocations = _read_allocations(allocation_path)
    if not allocations.empty and "symbol" in allocations.columns:
        match = allocations[allocations["symbol"].astype(str) == str(signal.get("symbol"))]
        if not match.empty:
            signal.update(match.iloc[0].to_dict())
    macro = latest_macro_state("logs/macro_observer.csv")
    signal["macro_state"] = macro.get("macro_state", "UNKNOWN")
    signal["confidence"] = _num(
        signal.get("model_confidence")
        or signal.get("adaptive_confidence_score")
        or signal.get("shadow_score")
        or signal.get("score")
    )
    signal["market"] = market_type(str(signal.get("symbol") or ""))
    signal["allocation_tier"] = str(signal.get("allocation_tier") or "WATCH").upper()
    return signal


def broadcast_test(
    db_path: str = "mamuyy_hunter.db",
    allocation_path: str = "logs/opportunity_allocation.csv",
    cooldown_seconds: int = 300,
) -> Dict[str, Any]:
    init_db(db_path)
    signal = _latest_signal(db_path, allocation_path)
    if not signal:
        signal = {
            "symbol": "BTCUSDT",
            "price": 100000.0,
            "confidence": 75.0,
            "regime_name": "LOCALHOST_TEST",
            "macro_state": "RISK_ON",
            "allocation_tier": "WATCH",
            "market": "crypto",
        }

    events: List[Dict[str, Any]] = []
    with sqlite3.connect(db_path) as connection:
        for target in TARGETS:
            payload = build_webhook_payload(
                symbol=str(signal.get("symbol") or ""),
                side="LONG",
                price=_num(signal.get("price")),
                confidence=_num(signal.get("confidence")),
                regime=str(signal.get("regime_name") or "UNKNOWN"),
                macro_state=str(signal.get("macro_state") or "UNKNOWN"),
                allocation_tier=str(signal.get("allocation_tier") or "WATCH"),
                market=str(signal.get("market") or "crypto"),
                source=f"mamuyy-broadcast:{target['target_name']}",
            )
            profile = evaluate_profile(payload, target["profile"])
            payload_hash = _payload_hash(payload, target["target_name"])
            duplicate = connection.execute(
                "SELECT 1 FROM broadcast_events WHERE payload_hash = ? AND target_name = ? LIMIT 1",
                (payload_hash, target["target_name"]),
            ).fetchone()
            if duplicate:
                status = "SKIPPED"
                reason = "duplicate payload"
            elif _cooldown_active(connection, str(signal.get("symbol") or ""), target["target_name"], cooldown_seconds):
                status = "SKIPPED"
                reason = "cooldown active"
            elif not profile["allowed"]:
                status = "REJECTED"
                reason = profile["reason"]
            else:
                status = "ROUTED"
                reason = f"paper-only route accepted; {profile['reason']}"

            event = {
                "timestamp": _now(),
                "symbol": payload["symbol"],
                "side": payload["side"],
                "confidence": payload["confidence"],
                "macro_state": payload["macro_state"],
                "allocation_tier": payload["allocation_tier"],
                "target_name": target["target_name"],
                "target_type": target["target_type"],
                "target_profile": target["profile"],
                "route_status": status,
                "route_reason": reason,
                "payload_hash": payload_hash,
            }
            _insert_event(db_path, event)
            events.append(event)

    return {
        "ok": True,
        "paper_only": True,
        "broker_execution": False,
        "events": events,
        "routed": sum(1 for event in events if event["route_status"] == "ROUTED"),
        "rejected": sum(1 for event in events if event["route_status"] == "REJECTED"),
        "skipped": sum(1 for event in events if event["route_status"] == "SKIPPED"),
    }


def format_broadcast_result(result: Dict[str, Any]) -> str:
    rows = pd.DataFrame(result.get("events", []))
    return "\n".join(
        [
            "BROADCAST ROUTER TEST",
            f"OK: {result.get('ok')}",
            f"Paper Only: {result.get('paper_only')}",
            f"Broker Execution: {result.get('broker_execution')}",
            f"Routed: {result.get('routed')} Rejected: {result.get('rejected')} Skipped: {result.get('skipped')}",
            rows.to_string(index=False) if not rows.empty else "No events.",
        ]
    )
