import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


CLOSED_STATUSES = {"CLOSED", "WIN", "LOSS", "STOP_LOSS", "TAKE_PROFIT"}
REPORT_PATH = "reports/paper_portfolio.json"


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
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_status(value: Any) -> str:
    return str(value or "OPEN").strip().upper() or "OPEN"


def _is_closed(row: sqlite3.Row) -> bool:
    status = _normalized_status(row["status"] if "status" in row.keys() else None)
    return status in CLOSED_STATUSES


def _pnl_percent(row: sqlite3.Row) -> float | None:
    entry = _num(row["entry_price"] if "entry_price" in row.keys() else None)
    current = _num(row["current_price"] if "current_price" in row.keys() else None)
    if entry and current:
        return (current - entry) / entry * 100.0
    return _num(row["pnl"] if "pnl" in row.keys() else None)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_hours(opened_at: Any, now: datetime) -> float | None:
    opened = _parse_dt(opened_at)
    if opened is None:
        return None
    return max(0.0, round((now - opened).total_seconds() / 3600.0, 2))


def _status_counts(rows: Iterable[sqlite3.Row]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        status = _normalized_status(row["status"] if "status" in row.keys() else None)
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _trade_payload(row: sqlite3.Row, now: datetime) -> Dict[str, Any]:
    pnl = _pnl_percent(row)
    opened_at = row["timestamp"] if "timestamp" in row.keys() else None
    return {
        "id": row["id"] if "id" in row.keys() else None,
        "symbol": row["symbol"] if "symbol" in row.keys() else "",
        "status": _normalized_status(row["status"] if "status" in row.keys() else None),
        "entry_price": _num(row["entry_price"] if "entry_price" in row.keys() else None),
        "current_price": _num(row["current_price"] if "current_price" in row.keys() else None),
        "virtual_unrealized_pnl_pct": round(pnl, 4) if pnl is not None else None,
        "opened_at": opened_at,
        "age_hours": _age_hours(opened_at, now),
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
    }


def _empty_report(db_path: str, database_available: bool, warning: str) -> Dict[str, Any]:
    return {
        "generated_at": _now(),
        "paper_only": True,
        "read_only": True,
        "safety": "PAPER_ONLY; read-only analytics; no broker routing; no live trading; no order placement; no execution mutation; no fake trades; no Phase 3 auto-unlock",
        "database": db_path,
        "database_available": database_available,
        "warning": warning,
        "total_active_trades": 0,
        "total_closed_trades": 0,
        "closed_target": 100,
        "closed_progress": "0/100",
        "status_distribution": {},
        "active_status_distribution": {},
        "top_active_trades": [],
        "active_trades": [],
        "artifact_path": REPORT_PATH,
    }


def generate_paper_portfolio_report(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = REPORT_PATH,
    closed_target: int = 100,
    top_limit: int = 10,
    write_report: bool = True,
) -> Dict[str, Any]:
    """Build a read-only active internal paper portfolio report from SQLite."""

    connection = _connect_read_only(db_path)
    if connection is None:
        report = _empty_report(db_path, False, "database unavailable")
    else:
        try:
            if not _table_exists(connection, "internal_paper_trades"):
                report = _empty_report(db_path, True, "internal_paper_trades table unavailable")
            else:
                rows = connection.execute("SELECT * FROM internal_paper_trades ORDER BY id DESC").fetchall()
                now = datetime.now(timezone.utc)
                closed_rows = [row for row in rows if _is_closed(row)]
                active_rows = [row for row in rows if not _is_closed(row)]
                status_distribution = _status_counts(rows)
                active_status_distribution = _status_counts(active_rows)
                active_trades = [_trade_payload(row, now) for row in active_rows]
                top_active = sorted(
                    active_trades,
                    key=lambda item: item["virtual_unrealized_pnl_pct"] if item["virtual_unrealized_pnl_pct"] is not None else float("-inf"),
                    reverse=True,
                )[:top_limit]
                report = {
                    "generated_at": _now(),
                    "paper_only": True,
                    "read_only": True,
                    "safety": "PAPER_ONLY; read-only analytics; no broker routing; no live trading; no order placement; no execution mutation; no fake trades; no Phase 3 auto-unlock",
                    "database": db_path,
                    "database_available": True,
                    "warning": "",
                    "total_active_trades": len(active_rows),
                    "total_closed_trades": len(closed_rows),
                    "closed_target": closed_target,
                    "closed_progress": f"{len(closed_rows)}/{closed_target}",
                    "status_distribution": status_distribution,
                    "active_status_distribution": active_status_distribution,
                    "top_active_trades": top_active,
                    "active_trades": active_trades,
                    "artifact_path": output_path,
                }
        finally:
            connection.close()

    if write_report:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as report_file:
            json.dump(report, report_file, indent=2, default=str)
    return report


def _fmt_price(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number:.8g}"


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f}%"


def format_paper_portfolio_report(report: Dict[str, Any]) -> str:
    status_bits = [f"{status}: {count}" for status, count in report.get("active_status_distribution", {}).items()]
    lines = [
        "PAPER PORTFOLIO",
        f"Paper Mode Only: {report.get('paper_only')}",
        f"Read Only: {report.get('read_only')}",
        "Safety: no broker routing, no live trading, no order placement, no execution mutation.",
        f"Active Trades: {report.get('total_active_trades', 0)}",
        f"Closed Trades: {report.get('closed_progress', '0/100')}",
        f"Status Distribution: {report.get('status_distribution', {}) or {'none': 0}}",
        f"Active Status Distribution: {' | '.join(status_bits) if status_bits else 'none'}",
        "Top Active Trades:",
    ]
    top = report.get("top_active_trades", [])
    if top:
        for index, trade in enumerate(top, start=1):
            lines.append(
                f"{index}. {trade.get('symbol', '-')} | {trade.get('status', '-')} | "
                f"entry={_fmt_price(trade.get('entry_price'))} | current={_fmt_price(trade.get('current_price'))} | "
                f"pnl={_fmt_pct(trade.get('virtual_unrealized_pnl_pct'))} | "
                f"opened_at={trade.get('opened_at') or '-'} | age_h={trade.get('age_hours') if trade.get('age_hours') is not None else '-'}"
            )
    else:
        lines.append("none")
    lines.append(f"Report: {report.get('artifact_path', REPORT_PATH)}")
    if report.get("warning"):
        lines.append(f"Warning: {report.get('warning')}")
    return "\n".join(lines)
