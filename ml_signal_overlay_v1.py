"""ML Signal Overlay V1 for PAPER_ONLY Telegram advisory previews.

This module is intentionally read-only for Hunter runtime inputs. It inspects the
latest signal candidate (or a requested symbol), enriches it with available
portfolio intelligence artifacts, prints a Telegram-style advisory message, and
writes a standalone JSON preview report under logs/.

Safety constraints:
- PAPER_ONLY advisory output only.
- No broker API calls.
- No live execution.
- No execution engine imports or mutations.
- SQLite is opened in read-only mode when present.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DB_PATH = "mamuyy_hunter.db"
REPORT_PATH = "logs/ml_signal_overlay_v1_report.json"
SAFETY_TEXT = "PAPER_ONLY; read-only advisory; no broker API; no live execution; no order placement"

SIGNAL_TABLES = ("signals", "internal_paper_trades", "shadow_trades", "paper_trades")
SIGNAL_CSVS = ("logs/latest_signal.csv", "logs/signals.csv", "paper_trades.csv")
ALLOCATION_CSVS = (
    "logs/portfolio_v2_allocation.csv",
    "logs/trade_quality_ranking.csv",
    "logs/opportunity_allocation.csv",
)
ALLOCATION_JSONS = (
    "logs/portfolio_v2_allocation.json",
    "logs/trade_quality_ranking.json",
    "reports/portfolio_v2_allocation.json",
)
PORTFOLIO_HEALTH_JSONS = (
    "logs/portfolio_health.json",
    "logs/portfolio_v2_health.json",
    "reports/portfolio_risk_budget.json",
    "reports/phase3_readiness.json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_rank(value: Any) -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "APLUS": "A+",
        "A_PLUS": "A+",
        "AMINUS": "A-",
        "A_MINUS": "A-",
        "BPLUS": "B+",
        "B_PLUS": "B+",
    }
    return aliases.get(text, text)


def _rank_from_score(score: Optional[float]) -> str:
    if score is None:
        return "UNKNOWN"
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 72:
        return "A-"
    if score >= 65:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def _format_pct(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "N/A"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.2f}%"


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as input_file:
            return json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return None


def _read_csv_rows(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, newline="", encoding="utf-8") as input_file:
            return list(csv.DictReader(input_file))
    except OSError:
        return []


def _connect_read_only(db_path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(db_path):
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


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _pick_first(row: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
            return lowered[name.lower()]
    return default


def _latest_signal_from_db(db_path: str, symbol: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    connection = _connect_read_only(db_path)
    meta: Dict[str, Any] = {"database": db_path, "database_available": bool(connection), "source": "none"}
    if connection is None:
        return {}, meta
    try:
        for table in SIGNAL_TABLES:
            if not _table_exists(connection, table):
                continue
            columns = _table_columns(connection, table)
            if "symbol" not in {column.lower() for column in columns}:
                continue
            order_column = "id" if "id" in columns else "timestamp" if "timestamp" in columns else columns[0]
            if symbol:
                query = f"SELECT * FROM {table} WHERE UPPER(symbol)=? ORDER BY {order_column} DESC LIMIT 1"
                row = connection.execute(query, (symbol.upper(),)).fetchone()
            else:
                query = f"SELECT * FROM {table} ORDER BY {order_column} DESC LIMIT 1"
                row = connection.execute(query).fetchone()
            if row is not None:
                meta["source"] = f"sqlite:{table}"
                return _row_to_dict(row), meta
        return {}, meta
    finally:
        connection.close()


def _latest_signal_from_csv(symbol: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    for path in SIGNAL_CSVS:
        rows = _read_csv_rows(path)
        if not rows:
            continue
        candidates = rows
        if symbol:
            candidates = [row for row in rows if _normalize_symbol(_pick_first(row, ["symbol"])) == symbol.upper()]
        if not candidates:
            continue
        return candidates[-1], {"source": f"csv:{path}"}
    return {}, {"source": "none"}


def load_signal_candidate(symbol: str = "", db_path: str = DB_PATH) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    db_signal, db_meta = _latest_signal_from_db(db_path, symbol)
    if db_signal:
        return db_signal, db_meta
    csv_signal, csv_meta = _latest_signal_from_csv(symbol)
    if csv_signal:
        meta = {**db_meta, **csv_meta}
        return csv_signal, meta
    fallback_symbol = symbol.upper() if symbol else "UNKNOWN"
    return {"symbol": fallback_symbol, "direction": "UNKNOWN", "score": None}, {**db_meta, "source": "fallback:no_signal_found"}


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("allocations", "rankings", "candidates", "rows", "data", "symbols"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if "symbol" in payload:
        return [payload]
    return []


def load_portfolio_record(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    target = symbol.upper()
    searched: List[str] = []
    for path in ALLOCATION_CSVS:
        searched.append(path)
        for row in _read_csv_rows(path):
            if _normalize_symbol(_pick_first(row, ["symbol", "asset", "ticker"])) == target:
                return row, {"source": f"csv:{path}", "found": True, "searched": searched}
    for path in ALLOCATION_JSONS:
        searched.append(path)
        records = _extract_records(_read_json(path))
        for row in records:
            if _normalize_symbol(_pick_first(row, ["symbol", "asset", "ticker"])) == target:
                return row, {"source": f"json:{path}", "found": True, "searched": searched}
    return {}, {"source": "none", "found": False, "searched": searched}


def load_portfolio_health() -> Tuple[str, Dict[str, Any]]:
    for path in PORTFOLIO_HEALTH_JSONS:
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        explicit = _pick_first(payload, ["portfolio_health", "health", "health_status", "status"])
        if explicit:
            text = str(explicit).upper()
            if text in {"GREEN", "YELLOW", "RED"}:
                return text, {"source": path, "raw": explicit}
        recommendation = str(payload.get("recommendation") or "").upper()
        if recommendation:
            if recommendation in {"NORMAL", "OK", "ALLOW"}:
                return "GREEN", {"source": path, "raw": recommendation}
            if recommendation in {"DEFENSIVE", "CAUTION", "WATCH"}:
                return "YELLOW", {"source": path, "raw": recommendation}
            if recommendation in {"FREEZE", "HALT", "BLOCK", "AVOID"}:
                return "RED", {"source": path, "raw": recommendation}
        heat = str(payload.get("portfolio_heat") or payload.get("concentration_label") or "").upper()
        if heat:
            if heat == "LOW":
                return "GREEN", {"source": path, "raw": heat}
            if heat == "MEDIUM":
                return "YELLOW", {"source": path, "raw": heat}
            if heat == "HIGH":
                return "RED", {"source": path, "raw": heat}
    return "UNKNOWN", {"source": "none", "raw": "UNKNOWN"}


def _expected_value_from_record(record: Dict[str, Any]) -> Optional[float]:
    explicit = _safe_float(
        _pick_first(
            record,
            [
                "expected_value",
                "expected_value_pct",
                "ev",
                "ev_pct",
                "expected_return_pct",
                "avg_pnl",
            ],
        )
    )
    if explicit is not None:
        return explicit * 100 if -1 < explicit < 1 and explicit != 0 else explicit
    opportunity = _safe_float(_pick_first(record, ["opportunity_score", "quality_score", "score"]))
    risk = _safe_float(_pick_first(record, ["risk_score"])) or 0.0
    if opportunity is None:
        return None
    return round((opportunity - risk) / 100.0, 2)


def build_overlay(signal: Dict[str, Any], portfolio_record: Dict[str, Any], portfolio_health: str, symbol_missing: bool) -> Dict[str, Any]:
    score = _safe_float(_pick_first(signal, ["score", "signal_score", "shadow_score", "calculated_score", "confidence"]))
    rank = _normalize_rank(_pick_first(portfolio_record, ["trade_rank", "rank", "quality_rank", "portfolio_rank"]))
    if not rank:
        rank = _rank_from_score(_safe_float(_pick_first(portfolio_record, ["opportunity_score", "quality_score", "score"])))
    ev = _expected_value_from_record(portfolio_record) if portfolio_record else None
    allocation = _safe_float(
        _pick_first(
            portfolio_record,
            ["suggested_allocation_pct", "suggested_max_weight_pct", "allocation_pct", "weight_pct", "max_weight_pct"],
        )
    )
    tier = str(_pick_first(portfolio_record, ["allocation_tier", "tier", "eligibility"], "")).upper()
    risk_score = _safe_float(_pick_first(portfolio_record, ["risk_score"]))
    suggested_risk = str(_pick_first(portfolio_record, ["suggested_risk_level", "risk_level"], "")).upper()
    if not suggested_risk:
        if risk_score is None:
            suggested_risk = "NEED_REVIEW"
        elif risk_score <= 35:
            suggested_risk = "NORMAL"
        elif risk_score <= 55:
            suggested_risk = "ELEVATED"
        else:
            suggested_risk = "HIGH"
    eligible = _pick_first(portfolio_record, ["portfolio_eligible", "eligible", "is_eligible"])
    if eligible in (None, ""):
        eligible_bool = bool(portfolio_record) and tier not in {"AVOID", "BLOCK", "BLOCKED"} and (allocation is None or allocation > 0)
    else:
        eligible_bool = str(eligible).strip().upper() in {"1", "TRUE", "YES", "Y", "ELIGIBLE", "ALLOW"}

    if symbol_missing:
        decision = "UNKNOWN / NEED_REVIEW"
    elif rank == "D" or (ev is not None and ev < 0):
        decision = "AVOID / BLOCKED"
    elif rank == "C":
        decision = "LOW PRIORITY / PAPER_ONLY"
    elif portfolio_health == "GREEN" and rank in {"A+", "A", "A-", "B+"}:
        decision = "WATCH / PAPER_ONLY"
    else:
        decision = "UNKNOWN / NEED_REVIEW"

    return {
        "trade_rank": rank or "UNKNOWN",
        "expected_value": ev,
        "portfolio_eligible": "YES" if eligible_bool else "NO",
        "suggested_allocation_pct": allocation,
        "suggested_risk_level": suggested_risk,
        "portfolio_health": portfolio_health,
        "overlay_decision": decision,
        "signal_score": score,
    }


def format_message(signal: Dict[str, Any], overlay: Dict[str, Any]) -> str:
    symbol = _normalize_symbol(_pick_first(signal, ["symbol"])) or "UNKNOWN"
    direction = str(_pick_first(signal, ["direction", "side", "position_side"], "UNKNOWN")).upper()
    score = overlay.get("signal_score")
    score_text = "N/A" if score is None else f"{score:.0f}" if float(score).is_integer() else f"{score:.2f}"
    return "\n".join(
        [
            "🦅 HUNTER SIGNAL V2 — PAPER ONLY",
            "",
            f"Symbol: {symbol}",
            f"Direction: {direction}",
            f"Score: {score_text}",
            "",
            f"Trade Rank: {overlay['trade_rank']}",
            f"Expected Value: {_format_pct(overlay['expected_value'], signed=True)}",
            f"Portfolio Eligible: {overlay['portfolio_eligible']}",
            f"Suggested Allocation: {_format_pct(overlay['suggested_allocation_pct'])}",
            f"Suggested Risk: {overlay['suggested_risk_level']}",
            "",
            f"Portfolio Health: {overlay['portfolio_health']}",
            f"Overlay Decision: {overlay['overlay_decision']}",
            "",
            "⚠ Advisory only. No broker API. No live execution.",
        ]
    )


def write_report(report: Dict[str, Any], output_path: str = REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def run(symbol: str = "", dry_run: bool = False, db_path: str = DB_PATH, output_path: str = REPORT_PATH) -> Dict[str, Any]:
    requested_symbol = _normalize_symbol(symbol)
    signal, signal_meta = load_signal_candidate(requested_symbol, db_path=db_path)
    signal_symbol = _normalize_symbol(_pick_first(signal, ["symbol"])) or requested_symbol or "UNKNOWN"
    portfolio_record, portfolio_meta = load_portfolio_record(signal_symbol)
    portfolio_health, health_meta = load_portfolio_health()
    overlay = build_overlay(signal, portfolio_record, portfolio_health, symbol_missing=not portfolio_meta.get("found"))
    message = format_message(signal, overlay)
    report = {
        "generated_at": _now(),
        "mode": "PAPER_ONLY",
        "dry_run": bool(dry_run),
        "safety": SAFETY_TEXT,
        "telegram_send_enabled": False,
        "signal_source": signal_meta,
        "portfolio_source": portfolio_meta,
        "portfolio_health_source": health_meta,
        "signal": signal,
        "overlay": overlay,
        "telegram_message": message,
        "output_path": output_path,
    }
    write_report(report, output_path)
    print(message)
    print(f"\nJSON report saved: {output_path}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build PAPER_ONLY ML signal overlay advisory preview.")
    parser.add_argument("--symbol", default="", help="Optional symbol to overlay, e.g. HYPEUSDT.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; never sends Telegram or touches broker APIs.")
    parser.add_argument("--db-path", default=DB_PATH, help="SQLite database path opened read-only if present.")
    parser.add_argument("--output", default=REPORT_PATH, help="JSON report output path.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(symbol=args.symbol, dry_run=args.dry_run, db_path=args.db_path, output_path=args.output)
