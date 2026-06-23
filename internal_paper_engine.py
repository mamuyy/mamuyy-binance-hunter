import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import pandas as pd

from bridge_tradingview import build_webhook_payload
from config import config
from cross_market_intelligence import latest_cross_market_state
from database import init_db
from macro_observer import latest_macro_state

CLOSED_STATUSES = {"CLOSED", "WIN", "LOSS", "STOP_LOSS", "TAKE_PROFIT"}
ACTIVE_STATUSES = {"OPEN", "TP1 HIT"}
MAX_CONCURRENT_PER_SYMBOL = 3
MAX_CONCURRENT_GLOBAL = 20
LIFECYCLE_REPORT_PATH = "reports/paper_trade_lifecycle.json"

PREDICTION_LINKAGE_FIELDS = (
    "prediction_id",
    "predicted_probability",
    "model_version",
    "evaluation_contract",
    "target_timestamp",
    "source_signal_timestamp",
    "symbol",
)


def _mapping_value(context: Any, key: str) -> Any:
    if context is None:
        return None
    if isinstance(context, Mapping):
        return context.get(key)
    if hasattr(context, "get"):
        try:
            return context.get(key)
        except Exception:
            return None
    return getattr(context, key, None)


def _dict_context(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
        except Exception:
            converted = None
        if isinstance(converted, Mapping):
            return dict(converted)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def extract_prediction_linkage_metadata(signal_or_prediction: Any) -> Dict[str, Any]:
    """Extract explicitly present ML linkage metadata without synthesizing IDs.

    This is a forward-only carrier for paper trade lifecycle state. It copies
    only present values from the signal/prediction context and bounded nested
    payloads; it never fabricates prediction IDs from symbol/time fallbacks.
    """

    contexts: List[Mapping[str, Any]] = []
    root = _dict_context(signal_or_prediction)
    if root:
        contexts.append(root)
    elif signal_or_prediction is not None:
        contexts.append(
            {field: _mapping_value(signal_or_prediction, field) for field in PREDICTION_LINKAGE_FIELDS}
        )

    payload = _dict_context(_mapping_value(signal_or_prediction, "payload_json"))
    if payload:
        contexts.append(payload)

    for container in (root, payload):
        for nested_key in ("prediction", "ml", "model"):
            nested = _dict_context(container.get(nested_key) if container else None)
            if nested:
                contexts.append(nested)

    metadata: Dict[str, Any] = {}
    aliases = {
        "prediction_id": ("prediction_id", "ml_prediction_id"),
        "predicted_probability": (
            "predicted_probability",
            "prediction_probability",
            "probability",
            "win_probability",
        ),
        "model_version": ("model_version", "model_id"),
        "evaluation_contract": ("evaluation_contract", "label_contract"),
        "target_timestamp": (
            "target_timestamp",
            "evaluation_target_timestamp",
            "label_target_timestamp",
        ),
        "source_signal_timestamp": ("source_signal_timestamp", "signal_timestamp", "timestamp"),
        "symbol": ("symbol",),
    }
    for field, keys in aliases.items():
        for context in contexts:
            for key in keys:
                value = context.get(key)
                if value not in (None, ""):
                    metadata[field] = value
                    break
            if field in metadata:
                break
    return metadata


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff_24h() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def _read_table(db_path: str, table: str, limit: int = 500) -> pd.DataFrame:
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
    if not os.path.exists(path):
        return pd.DataFrame()
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


def _macro_state(regime: str) -> str:
    upper = str(regime or "").upper()
    if "RISK OFF" in upper or "PANIC" in upper:
        return "RISK_OFF"
    if "HIGH VOLATILITY" in upper:
        return "MACRO_STRESS"
    if "SIDEWAYS" in upper:
        return "CHOPPY"
    return "NORMAL"


def _market_type(symbol: str) -> str:
    text = str(symbol or "").upper()
    if text.endswith("USDT"):
        return "crypto"
    if text in {"XAUUSD", "GOLD"}:
        return "gold"
    return "crypto"


def _score_from_signal(signal: pd.Series | Dict[str, Any]) -> float:
    for field in ("model_confidence", "adaptive_confidence_score", "shadow_score", "score", "calculated_score"):
        score = _num(signal.get(field), default=-1.0)
        if score >= 0:
            return score
    return 0.0


def _latest_signal_candidates(signals: pd.DataFrame, allocations: pd.DataFrame) -> pd.DataFrame:
    if signals.empty or "symbol" not in signals.columns:
        return pd.DataFrame()
    latest = signals.sort_values("id").copy()
    if "price" in latest.columns:
        latest = latest[pd.to_numeric(latest["price"], errors="coerce").fillna(0.0) > 0]
    if "score" in latest.columns:
        latest = latest[pd.to_numeric(latest["score"], errors="coerce").fillna(0.0) >= config.alert_score_threshold]
    latest = latest.drop_duplicates(["symbol", "timestamp"], keep="last").tail(100).copy()
    if not allocations.empty and {"symbol", "allocation_tier"}.issubset(allocations.columns):
        latest = latest.merge(
            allocations[["symbol", "allocation_tier", "opportunity_score", "suggested_max_weight_pct"]],
            on="symbol",
            how="left",
        )
    if "allocation_tier" not in latest.columns:
        latest["allocation_tier"] = "WATCH"
    latest["allocation_tier"] = latest["allocation_tier"].fillna("WATCH")
    return latest[~latest["allocation_tier"].astype(str).str.upper().isin(["AVOID"])]


def _macro_adjusted_confidence(confidence: float, macro_state: str) -> float:
    if macro_state == "PANIC":
        return confidence * 0.45
    if macro_state == "HIGH_STRESS":
        return confidence * 0.65
    if macro_state == "CAUTION":
        return confidence * 0.85
    return confidence


def _cross_market_adjusted_confidence(confidence: float, cross_market: Dict[str, Any]) -> float:
    state = str(cross_market.get("cross_market_state") or "UNKNOWN")
    stress = _num(cross_market.get("cross_market_stress_score"))
    dxy_pressure = _num(cross_market.get("dxy_pressure"))
    multiplier = 1.0
    if state in {"CROSS_MARKET_STRESS", "SAFE_HAVEN_ROTATION"}:
        multiplier *= 0.70
    elif state == "CAUTION":
        multiplier *= 0.85
    if dxy_pressure >= 15:
        multiplier *= 0.85
    if stress >= 70:
        multiplier *= 0.80
    return confidence * multiplier


def _ensure_internal_paper_columns(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(internal_paper_trades)")}
    columns = {
        "current_price": "REAL",
        "sl": "REAL",
        "tp1": "REAL",
        "tp2": "REAL",
        "exit_reason": "TEXT",
        "updated_at": "TEXT",
        "prediction_id": "TEXT",
        "predicted_probability": "REAL",
        "model_version": "TEXT",
        "evaluation_contract": "TEXT",
        "target_timestamp": "TEXT",
    }
    for column, column_type in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE internal_paper_trades ADD COLUMN {column} {column_type}")


def _active_count(db_path: str, symbol: str | None = None) -> int:
    """Count active (OPEN / TP1 HIT) positions, optionally filtered by symbol."""
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES))
    params: list = list(ACTIVE_STATUSES)
    where = f"status IN ({placeholders})"
    if symbol is not None:
        where += " AND symbol = ?"
        params.append(symbol)
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM internal_paper_trades WHERE {where}",
                params,
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _insert_trade(db_path: str, trade: Dict[str, Any]) -> bool:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        _ensure_internal_paper_columns(connection)
        fields = [
            "timestamp",
            "source_signal_timestamp",
            "symbol",
            "market_type",
            "side",
            "entry_price",
            "current_price",
            "sl",
            "tp1",
            "tp2",
            "exit_price",
            "pnl",
            "confidence",
            "regime",
            "macro_state",
            "allocation_tier",
            "status",
            "exit_reason",
            "updated_at",
            "payload_json",
            "prediction_id",
            "predicted_probability",
            "model_version",
            "evaluation_contract",
            "target_timestamp",
        ]
        placeholders = ", ".join(["?"] * len(fields))
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO internal_paper_trades ({', '.join(fields)}) VALUES ({placeholders})",
            [trade.get(field) for field in fields],
        )
        connection.commit()
        return cursor.rowcount > 0


def _status_counts_from_series(status: pd.Series) -> Dict[str, int]:
    if status.empty:
        return {}
    normalized = status.fillna("OPEN").astype(str).str.strip().str.upper().replace("", "OPEN")
    return {str(key): int(value) for key, value in normalized.value_counts().sort_index().items()}


def _active_status_counts(status_counts: Dict[str, int]) -> Dict[str, int]:
    return {
        status: count
        for status, count in status_counts.items()
        if status not in CLOSED_STATUSES and count > 0
    }


def _paper_metrics(trades: pd.DataFrame) -> Dict[str, Any]:
    if trades.empty:
        return {"trade_count": 0, "open_count": 0, "active_count": 0, "closed_count": 0, "winrate": 0.0, "total_pnl": 0.0, "max_drawdown": 0.0}
    status = trades.get("status", pd.Series(dtype=str)).astype(str).str.upper()
    status_counts = _status_counts_from_series(status)
    active_status_counts = _active_status_counts(status_counts)
    closed = trades[status.isin(CLOSED_STATUSES)]
    pnl = pd.to_numeric(closed.get("pnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
    return {
        "trade_count": int(len(trades)),
        "open_count": int(status.isin(ACTIVE_STATUSES).sum()),
        "active_count": int(sum(active_status_counts.values())),
        "closed_count": int(status.isin(CLOSED_STATUSES).sum()),
        "status_counts": status_counts,
        "active_status_counts": active_status_counts,
        "winrate": round(float((pnl > 0).mean() * 100), 2) if len(pnl) else 0.0,
        "total_pnl": round(float(pnl.sum()), 4),
        "max_drawdown": round(float(drawdown.min()), 4) if not drawdown.empty else 0.0,
    }


def _latest_prices_from_signals(signals: pd.DataFrame) -> Dict[str, float]:
    if signals.empty or not {"symbol", "price"}.issubset(signals.columns):
        return {}
    latest = signals.sort_values("id").drop_duplicates("symbol", keep="last")
    return {str(row["symbol"]): _num(row["price"]) for _, row in latest.iterrows() if _num(row.get("price")) > 0}


def _resolve_status(entry: float, current_price: float, sl: float, tp1: float, tp2: float, previous_status: str) -> tuple[str, str, float | None, float]:
    if entry <= 0 or current_price <= 0:
        return previous_status or "OPEN", "", None, 0.0
    pnl = (current_price - entry) / entry * 100.0
    if current_price <= sl:
        return "CLOSED", "STOP_LOSS", current_price, pnl
    if current_price >= tp2:
        return "CLOSED", "TAKE_PROFIT_2", current_price, pnl
    if current_price >= tp1:
        return "TP1 HIT", "TAKE_PROFIT_1", None, pnl
    return previous_status if previous_status in ACTIVE_STATUSES else "OPEN", "", None, pnl


def _update_open_trades(db_path: str, latest_prices: Dict[str, float]) -> int:
    if not latest_prices:
        return 0
    init_db(db_path)
    closed = 0
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_internal_paper_columns(connection)
        rows = connection.execute(
            "SELECT * FROM internal_paper_trades WHERE UPPER(COALESCE(status, 'OPEN')) IN ('OPEN', 'TP1 HIT')"
        ).fetchall()
        for row in rows:
            symbol = str(row["symbol"] or "")
            current_price = latest_prices.get(symbol)
            if not current_price:
                continue
            entry = _num(row["entry_price"])
            sl = _num(row["sl"], entry * 0.98)
            tp1 = _num(row["tp1"], entry * 1.03)
            tp2 = _num(row["tp2"], entry * 1.05)
            status, exit_reason, exit_price, pnl = _resolve_status(entry, current_price, sl, tp1, tp2, str(row["status"] or "OPEN"))
            if status == "CLOSED":
                closed += 1
            connection.execute(
                """
                UPDATE internal_paper_trades
                SET current_price = ?, sl = ?, tp1 = ?, tp2 = ?, status = ?, exit_reason = ?,
                    exit_price = COALESCE(?, exit_price), pnl = ?, updated_at = ?
                WHERE id = ?
                """,
                (round(current_price, 8), round(sl, 8), round(tp1, 8), round(tp2, 8), status, exit_reason, round(exit_price, 8) if exit_price else None, round(pnl, 6), _now(), row["id"]),
            )
        connection.commit()
    return closed


def _write_lifecycle_report(report: Dict[str, Any], output_path: str = LIFECYCLE_REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2, default=str)


def run_internal_paper_engine(
    db_path: str = "mamuyy_hunter.db",
    allocation_path: str = "logs/opportunity_allocation.csv",
    max_new_trades: int = 25,
) -> Dict[str, Any]:
    signals = _read_table(db_path, "signals", limit=2000)
    allocations = _read_allocations(allocation_path)
    latest_prices = _latest_prices_from_signals(signals)
    naturally_closed = _update_open_trades(db_path, latest_prices)
    real_macro = latest_macro_state("logs/macro_observer.csv")
    real_macro_state = str(real_macro.get("macro_state") or "UNKNOWN")
    cross_market = latest_cross_market_state("logs/cross_market_intelligence.csv")
    candidates = _latest_signal_candidates(signals, allocations).head(max_new_trades)
    inserted = 0
    generated: List[Dict[str, Any]] = []
    for _, signal in candidates.iterrows():
        symbol = str(signal.get("symbol") or "")
        if not symbol:
            continue
        price = _num(signal.get("price"))
        if price <= 0:
            continue
        confidence = _score_from_signal(signal)
        regime = str(signal.get("regime_name") or "UNKNOWN")
        macro_state = real_macro_state if real_macro_state != "UNKNOWN" else _macro_state(regime)
        confidence = _macro_adjusted_confidence(confidence, macro_state)
        confidence = _cross_market_adjusted_confidence(confidence, cross_market)
        allocation_tier = str(signal.get("allocation_tier") or "WATCH").upper()
        sl = price * 0.98
        tp1 = price * 1.03
        tp2 = price * 1.05
        payload = build_webhook_payload(
            symbol=symbol,
            side="LONG",
            price=price,
            confidence=confidence,
            regime=regime,
            macro_state=macro_state,
            allocation_tier=allocation_tier,
            market=_market_type(symbol),
        )
        linkage_metadata = extract_prediction_linkage_metadata(signal)
        trade = {
            "timestamp": _now(),
            "source_signal_timestamp": (
                linkage_metadata.get("source_signal_timestamp") or signal.get("timestamp") or _now()
            ),
            "symbol": linkage_metadata.get("symbol") or symbol,
            "market_type": _market_type(symbol),
            "side": "LONG",
            "entry_price": round(price, 8),
            "current_price": round(price, 8),
            "sl": round(sl, 8),
            "tp1": round(tp1, 8),
            "tp2": round(tp2, 8),
            "exit_price": None,
            "pnl": 0.0,
            "confidence": round(confidence, 4),
            "regime": regime,
            "macro_state": macro_state,
            "allocation_tier": allocation_tier,
            "status": "OPEN",
            "exit_reason": "",
            "updated_at": _now(),
            "payload_json": json.dumps(payload, default=str),
            "prediction_id": linkage_metadata.get("prediction_id"),
            "predicted_probability": linkage_metadata.get("predicted_probability"),
            "model_version": linkage_metadata.get("model_version"),
            "evaluation_contract": linkage_metadata.get("evaluation_contract"),
            "target_timestamp": linkage_metadata.get("target_timestamp"),
        }
        sym_count = _active_count(db_path, symbol)
        if sym_count >= MAX_CONCURRENT_PER_SYMBOL:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "POLICY_BLOCK per-symbol symbol=%s active=%d limit=%d — skipping",
                symbol, sym_count, MAX_CONCURRENT_PER_SYMBOL,
            )
            continue
        global_count = _active_count(db_path)
        if global_count >= MAX_CONCURRENT_GLOBAL:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "POLICY_BLOCK global active=%d limit=%d — skipping",
                global_count, MAX_CONCURRENT_GLOBAL,
            )
            continue
        if _insert_trade(db_path, trade):
            inserted += 1
            generated.append(trade)

    trades = _read_table(db_path, "internal_paper_trades", limit=5000)
    diagnostics = generate_paper_trade_diagnostics(db_path=db_path, write_report=False)
    metrics = _paper_metrics(trades)
    result = {
        "ok": True,
        "paper_mode_only": True,
        "safety": "PAPER_ONLY; no broker routing; no order placement; open-first lifecycle; natural exits only",
        "generated": generated,
        "inserted": inserted,
        "naturally_closed": naturally_closed,
        "active_count": metrics.get("active_count", 0),
        "status_counts": metrics.get("status_counts", {}),
        "active_status_counts": metrics.get("active_status_counts", {}),
        "metrics": metrics,
        "diagnostics": diagnostics,
    }
    _write_lifecycle_report(result)
    return result


def _connect_read_only(db_path: str) -> sqlite3.Connection | None:
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _count(connection: sqlite3.Connection, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
    try:
        query = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        return int(connection.execute(query, tuple(params)).fetchone()[0])
    except sqlite3.Error:
        return 0


def _eligible_signal_rows(connection: sqlite3.Connection, cutoff: str) -> tuple[int, Counter]:
    columns = _table_columns(connection, "signals")
    reasons: Counter = Counter()
    if not columns:
        reasons["missing_signals_table"] += 1
        return 0, reasons
    required = {"timestamp", "symbol", "price"}
    missing = sorted(required - set(columns))
    if missing:
        reasons[f"missing_signal_columns:{','.join(missing)}"] += 1
        return 0, reasons
    rows = connection.execute("SELECT * FROM signals WHERE timestamp >= ?", (cutoff,)).fetchall()
    eligible = 0
    for row in rows:
        symbol = str(row["symbol"] or "")
        price = _num(row["price"])
        score_values = [
            _num(row[key], default=-1.0)
            for key in ("score", "calculated_score", "shadow_score", "adaptive_confidence_score", "model_confidence")
            if key in columns
        ]
        score = max(score_values) if score_values else 0.0
        if not symbol:
            reasons["missing_symbol"] += 1
        elif price <= 0:
            reasons["invalid_price"] += 1
        elif score < config.alert_score_threshold:
            reasons["below_alert_score_threshold"] += 1
        else:
            eligible += 1
    return eligible, reasons


def generate_paper_trade_diagnostics(
    db_path: str = "mamuyy_hunter.db",
    target_closed: int = 100,
    output_path: str = LIFECYCLE_REPORT_PATH,
    write_report: bool = True,
) -> Dict[str, Any]:
    cutoff = _cutoff_24h()
    tables = ["signals", "shadow_trades", "paper_trades", "internal_paper_trades", "telegram_events"]
    connection = _connect_read_only(db_path)
    if connection is None:
        report = {
            "generated_at": _now(),
            "paper_only": True,
            "database": db_path,
            "database_available": False,
            "table_detection": {table: {"exists": False, "columns": []} for table in tables},
            "signal_count_last_24h": 0,
            "eligible_paper_signals_last_24h": 0,
            "paper_entries_created_last_24h": 0,
            "paper_exits_closed_last_24h": 0,
            "rejection_reasons": {"database_unavailable": 1},
            "current_closed_count": 0,
            "active_count": 0,
            "status_counts": {},
            "active_status_counts": {},
            "target_closed_count": target_closed,
            "progress": f"0/{target_closed}",
        }
        if write_report:
            _write_lifecycle_report(report, output_path)
        return report

    try:
        detection = {table: {"exists": bool(_table_columns(connection, table)), "columns": _table_columns(connection, table)} for table in tables}
        signal_count = _count(connection, "signals", "timestamp >= ?", (cutoff,))
        eligible, reasons = _eligible_signal_rows(connection, cutoff)
        entries = _count(connection, "internal_paper_trades", "timestamp >= ?", (cutoff,))
        closed_where = "UPPER(COALESCE(status, '')) IN ('CLOSED','WIN','LOSS','STOP_LOSS','TAKE_PROFIT')"
        exits = _count(connection, "internal_paper_trades", f"{closed_where} AND COALESCE(updated_at, timestamp) >= ?", (cutoff,))
        closed_count = _count(connection, "internal_paper_trades", closed_where)
        status_rows = connection.execute(
            """
            SELECT UPPER(COALESCE(NULLIF(TRIM(status), ''), 'OPEN')) AS normalized_status, COUNT(*) AS count
            FROM internal_paper_trades
            GROUP BY normalized_status
            """
        ).fetchall()
        status_counts = {str(row["normalized_status"]): int(row["count"]) for row in status_rows}
        active_status_counts = _active_status_counts(status_counts)
        active_count = int(sum(active_status_counts.values()))
        if detection["paper_trades"]["exists"] and _count(connection, "paper_trades") == 0:
            reasons["legacy_paper_trades_cli_not_running_or_no_csv_migration"] += 1
        if detection["internal_paper_trades"]["exists"] and entries == 0 and eligible > 0:
            reasons["internal_paper_engine_not_scheduled_or_not_run_last_24h"] += 1
        report = {
            "generated_at": _now(),
            "paper_only": True,
            "database": db_path,
            "database_available": True,
            "table_detection": detection,
            "signal_count_last_24h": signal_count,
            "eligible_paper_signals_last_24h": eligible,
            "paper_entries_created_last_24h": entries,
            "paper_exits_closed_last_24h": exits,
            "rejection_reasons": dict(reasons),
            "current_closed_count": closed_count,
            "active_count": active_count,
            "status_counts": status_counts,
            "active_status_counts": active_status_counts,
            "target_closed_count": target_closed,
            "progress": f"{closed_count}/{target_closed}",
        }
        if write_report:
            _write_lifecycle_report(report, output_path)
        return report
    finally:
        connection.close()


def format_paper_diagnostics(report: Dict[str, Any]) -> str:
    table_bits = []
    for table, payload in report.get("table_detection", {}).items():
        columns = payload.get("columns", []) if isinstance(payload, dict) else []
        table_bits.append(f"{table}={'yes' if payload.get('exists') else 'no'}({len(columns)} cols)")
    return "\n".join(
        [
            "PAPER TRADE DIAGNOSTICS",
            f"Paper Mode Only: {report.get('paper_only')}",
            f"Signal Count Last 24h: {report.get('signal_count_last_24h', 0)}",
            f"Eligible Paper Signals Last 24h: {report.get('eligible_paper_signals_last_24h', 0)}",
            f"Paper Entries Created Last 24h: {report.get('paper_entries_created_last_24h', 0)}",
            f"Paper Exits/Closed Last 24h: {report.get('paper_exits_closed_last_24h', 0)}",
            f"Active Paper Trades: {report.get('active_count', 0)}",
            f"Active Status Counts: {report.get('active_status_counts', {}) or {'none': 0}}",
            f"Rejection Reasons: {report.get('rejection_reasons', {}) or {'none': 0}}",
            f"Table/Column Detection: {', '.join(table_bits)}",
            f"Closed Count vs Target: {report.get('progress', '0/100')}",
            f"Report: {LIFECYCLE_REPORT_PATH}",
        ]
    )


def format_paper_engine_result(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "INTERNAL PAPER ENGINE",
            f"OK: {result.get('ok')}",
            f"Paper Mode Only: {result.get('paper_mode_only')}",
            "Safety: PAPER_ONLY, no broker routing, no live order placement.",
            f"Inserted Open Trades: {result.get('inserted', 0)}",
            f"Naturally Closed Trades: {result.get('naturally_closed', 0)}",
            f"Metrics: {result.get('metrics', {})}",
            f"Report: {LIFECYCLE_REPORT_PATH}",
        ]
    )
