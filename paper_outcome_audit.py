import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


REPORT_PATH = "reports/paper_outcome_audit.json"
CLOSED_TARGET = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_read_only(db_path: str) -> sqlite3.Connection | None:
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _context_value(context: Any, *keys: str) -> Any:
    """Return the first non-empty value from a dict-like or sqlite row context."""

    if context is None:
        return None
    available_keys = None
    if isinstance(context, sqlite3.Row):
        available_keys = set(context.keys())
    for key in keys:
        value = None
        if isinstance(context, sqlite3.Row):
            if key not in available_keys:
                continue
            value = context[key]
        elif isinstance(context, Mapping):
            value = context.get(key)
        else:
            value = getattr(context, key, None)
        if value not in (None, ""):
            return value
    return None


def build_prediction_outcome_linkage_fields(
    prediction: Any = None,
    trade: Any = None,
    outcome: Any = None,
) -> Dict[str, Any]:
    """Build forward-only prediction/outcome linkage fields from available context.

    The helper intentionally does not synthesize fallback IDs. Missing upstream
    identifiers remain null so downstream validation/audits can flag them.
    """

    return {
        "prediction_id": _context_value(
            prediction,
            "prediction_id",
            "ml_prediction_id",
        ),
        "trade_id": _context_value(trade, "trade_id", "paper_trade_id", "id"),
        "signal_id": _context_value(trade, "signal_id", "source_signal_id"),
        "symbol": (
            _context_value(outcome, "symbol")
            or _context_value(trade, "symbol")
            or _context_value(prediction, "symbol")
        ),
        "source_signal_timestamp": _context_value(
            prediction,
            "source_signal_timestamp",
            "signal_timestamp",
        ) or _context_value(trade, "source_signal_timestamp", "signal_timestamp"),
        "target_timestamp": _context_value(
            prediction,
            "target_timestamp",
            "evaluation_target_timestamp",
            "label_target_timestamp",
        ),
        "closed_at": _context_value(outcome, "closed_at", "updated_at")
        or _context_value(trade, "closed_at", "updated_at"),
        "outcome": _context_value(outcome, "outcome", "label", "status"),
        "label": _context_value(outcome, "label", "outcome", "status"),
        "predicted_probability": _context_value(
            prediction,
            "predicted_probability",
            "prediction_probability",
            "probability",
            "win_probability",
        ),
        "model_version": _context_value(prediction, "model_version", "model_id"),
        "evaluation_contract": _context_value(prediction, "evaluation_contract", "label_contract"),
    }


def validate_prediction_outcome_linkage_fields(row: Mapping[str, Any]) -> List[str]:
    """Return non-fatal diagnostic flags for missing linkage observability fields."""

    flags: List[str] = []
    if not row.get("prediction_id"):
        flags.append("MISSING_PREDICTION_ID")
    if not (row.get("trade_id") or row.get("signal_id")):
        flags.append("MISSING_TRADE_OR_SIGNAL_ID")
    if row.get("predicted_probability") in (None, ""):
        flags.append("MISSING_PREDICTED_PROBABILITY")
    if not row.get("target_timestamp"):
        flags.append("MISSING_TARGET_TIMESTAMP")
    if not row.get("model_version"):
        flags.append("MISSING_MODEL_VERSION")
    if not row.get("evaluation_contract"):
        flags.append("MISSING_EVALUATION_CONTRACT")
    return flags


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_value(row: sqlite3.Row, column: str, default: Any = None) -> Any:
    return row[column] if column in row.keys() else default


def _pnl_percent(row: sqlite3.Row) -> float:
    pnl = _num(_row_value(row, "pnl"))
    if pnl is not None:
        return pnl

    entry = _num(_row_value(row, "entry_price"))
    exit_price = _num(_row_value(row, "exit_price"))
    current = _num(_row_value(row, "current_price"))
    resolved_exit = exit_price if exit_price is not None else current
    if entry and resolved_exit is not None:
        return (resolved_exit - entry) / entry * 100.0
    return 0.0


def _closed_trade_payload(row: sqlite3.Row) -> Dict[str, Any]:
    pnl = _pnl_percent(row)
    payload = {
        "id": _row_value(row, "id"),
        "symbol": _row_value(row, "symbol", ""),
        "side": _row_value(row, "side", ""),
        "status": str(_row_value(row, "status", "") or "").upper(),
        "entry_price": _num(_row_value(row, "entry_price")),
        "exit_price": _num(_row_value(row, "exit_price")),
        "current_price": _num(_row_value(row, "current_price")),
        "realized_pnl_pct": round(pnl, 4),
        "exit_reason": _row_value(row, "exit_reason", "") or "",
        "opened_at": _row_value(row, "timestamp"),
        "closed_at": _row_value(row, "updated_at"),
    }
    outcome_context = {
        "symbol": payload["symbol"],
        "closed_at": payload["closed_at"],
        "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
        "label": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
        "status": payload["status"],
    }
    linkage = build_prediction_outcome_linkage_fields(prediction=row, trade=row, outcome=outcome_context)
    payload.update(linkage)
    payload["prediction_outcome_linkage_flags"] = validate_prediction_outcome_linkage_fields(payload)
    return payload


def _trade_label(trade: Dict[str, Any] | None) -> str:
    if not trade:
        return "- n/a"
    symbol = trade.get("symbol") or "-"
    pnl = _num(trade.get("realized_pnl_pct"))
    return f"{symbol} {pnl:+.2f}%" if pnl is not None else f"{symbol} n/a"


def _empty_report(db_path: str, database_available: bool, warning: str, output_path: str) -> Dict[str, Any]:
    return {
        "generated_at": _now(),
        "paper_only": True,
        "read_only": True,
        "safety": "PAPER_ONLY; read-only closed outcome analytics; no broker routing; no live trading; no order placement; no trade mutation; no paper engine change; no readiness gate change; no scoring change; no ML change; no scheduler cadence change; no Phase 3 unlock",
        "database": db_path,
        "database_available": database_available,
        "warning": warning,
        "closed_target": CLOSED_TARGET,
        "closed_trades_total": 0,
        "closed_progress": f"0/{CLOSED_TARGET}",
        "win_count": 0,
        "loss_count": 0,
        "breakeven_count": 0,
        "winrate": 0.0,
        "net_pnl": 0.0,
        "average_pnl": 0.0,
        "best_trade": None,
        "worst_trade": None,
        "top_symbols_by_realized_pnl": [],
        "exit_reason_distribution": {},
        "closed_trades": [],
        "artifact_path": output_path,
    }


def _symbol_totals(trades: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for trade in trades:
        symbol = str(trade.get("symbol") or "UNKNOWN")
        bucket = grouped.setdefault(symbol, {"symbol": symbol, "trade_count": 0, "realized_pnl_pct": 0.0})
        bucket["trade_count"] += 1
        bucket["realized_pnl_pct"] += float(trade.get("realized_pnl_pct") or 0.0)
    ranked = sorted(grouped.values(), key=lambda item: item["realized_pnl_pct"], reverse=True)
    for item in ranked:
        item["realized_pnl_pct"] = round(float(item["realized_pnl_pct"]), 4)
    return ranked[:limit]


def _exit_reason_distribution(trades: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    distribution: Dict[str, int] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason") or "").strip() or "UNKNOWN"
        distribution[reason] = distribution.get(reason, 0) + 1
    return dict(sorted(distribution.items()))


def generate_paper_outcome_audit(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = REPORT_PATH,
    closed_target: int = CLOSED_TARGET,
    top_symbol_limit: int = 10,
    write_report: bool = True,
) -> Dict[str, Any]:
    """Build a PAPER_ONLY read-only outcome audit for CLOSED internal paper trades."""

    connection = _connect_read_only(db_path)
    if connection is None:
        report = _empty_report(db_path, False, "database unavailable", output_path)
    else:
        try:
            if not _table_exists(connection, "internal_paper_trades"):
                report = _empty_report(db_path, True, "internal_paper_trades table unavailable", output_path)
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM internal_paper_trades
                    WHERE UPPER(COALESCE(status, '')) = 'CLOSED'
                    ORDER BY id DESC
                    """
                ).fetchall()
                trades = [_closed_trade_payload(row) for row in rows]
                pnl_values = [float(trade.get("realized_pnl_pct") or 0.0) for trade in trades]
                total = len(trades)
                wins = sum(1 for pnl in pnl_values if pnl > 0)
                losses = sum(1 for pnl in pnl_values if pnl < 0)
                breakeven = total - wins - losses
                net_pnl = sum(pnl_values)
                best = max(trades, key=lambda trade: float(trade.get("realized_pnl_pct") or 0.0), default=None)
                worst = min(trades, key=lambda trade: float(trade.get("realized_pnl_pct") or 0.0), default=None)
                report = {
                    "generated_at": _now(),
                    "paper_only": True,
                    "read_only": True,
                    "safety": "PAPER_ONLY; read-only closed outcome analytics; no broker routing; no live trading; no order placement; no trade mutation; no paper engine change; no readiness gate change; no scoring change; no ML change; no scheduler cadence change; no Phase 3 unlock",
                    "database": db_path,
                    "database_available": True,
                    "warning": "",
                    "closed_target": closed_target,
                    "closed_trades_total": total,
                    "closed_progress": f"{total}/{closed_target}",
                    "win_count": wins,
                    "loss_count": losses,
                    "breakeven_count": breakeven,
                    "winrate": round((wins / total) * 100.0, 2) if total else 0.0,
                    "net_pnl": round(net_pnl, 4),
                    "average_pnl": round(net_pnl / total, 4) if total else 0.0,
                    "best_trade": best,
                    "worst_trade": worst,
                    "top_symbols_by_realized_pnl": _symbol_totals(trades, top_symbol_limit),
                    "exit_reason_distribution": _exit_reason_distribution(trades),
                    "closed_trades": trades,
                    "artifact_path": output_path,
                }
        finally:
            connection.close()

    if write_report:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as report_file:
            json.dump(report, report_file, indent=2, default=str)
    return report


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f}%"


def format_paper_outcome_audit(report: Dict[str, Any]) -> str:
    lines = [
        "PAPER OUTCOME AUDIT",
        f"Paper Mode Only: {report.get('paper_only')}",
        f"Read Only: {report.get('read_only')}",
        "Safety: no trade mutation, no broker routing, no live trading, no order placement, no Phase 3 unlock.",
        f"Closed Trades: {report.get('closed_progress', '0/100')}",
        f"Wins: {report.get('win_count', 0)}",
        f"Losses: {report.get('loss_count', 0)}",
        f"Breakeven: {report.get('breakeven_count', 0)}",
        f"Winrate: {float(report.get('winrate') or 0.0):.2f}%",
        f"Net PnL: {_fmt_pct(report.get('net_pnl'))}",
        f"Average PnL: {_fmt_pct(report.get('average_pnl'))}",
        f"Best: {_trade_label(report.get('best_trade'))}",
        f"Worst: {_trade_label(report.get('worst_trade'))}",
        "Top Symbols by Realized PnL:",
    ]
    top_symbols = report.get("top_symbols_by_realized_pnl", [])
    if isinstance(top_symbols, list) and top_symbols:
        for index, item in enumerate(top_symbols[:10], start=1):
            lines.append(
                f"{index}. {item.get('symbol', '-')} | trades={item.get('trade_count', 0)} | pnl={_fmt_pct(item.get('realized_pnl_pct'))}"
            )
    else:
        lines.append("none")
    exit_distribution = report.get("exit_reason_distribution", {})
    if isinstance(exit_distribution, dict) and exit_distribution:
        lines.append("Exit Reasons: " + " | ".join(f"{reason}: {count}" for reason, count in exit_distribution.items()))
    lines.append(f"Report: {report.get('artifact_path', REPORT_PATH)}")
    if report.get("warning"):
        lines.append(f"Warning: {report.get('warning')}")
    return "\n".join(lines)
