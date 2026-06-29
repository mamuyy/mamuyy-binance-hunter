"""
CP-044C: Lifecycle Health Guard

READ-ONLY lifecycle health evidence generator. This script opens
mamuyy_hunter.db in SQLite read-only URI mode and writes only local report
artifacts under reports/. It does not modify database rows, runtime behavior,
thresholds, model registry, execution logic, Telegram, dashboards, candidate
queues, risk management, broker/exchange code, Phase 3 state, CP-045 state, or
model promotion state.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DB_PATH = "mamuyy_hunter.db"
BASELINE_TIMESTAMP = "2026-06-22T18:05:35.736930+00:00"
MAX_CONCURRENT_GLOBAL = 20
MIN_SCORE95_FORWARD_ROWS = 30

REPORT_MD = "reports/cp044c_lifecycle_health_guard.md"
REPORT_JSON = "reports/cp044c_lifecycle_health_guard.json"

IPT = "internal_paper_trades"
TABLES_TO_READ = ("internal_paper_trades", "signal_candidates", "signals", "shadow_trades", "paper_trades")
FRESHNESS_TABLES = ("signals", "signal_candidates", "shadow_trades")
ACTIVE_STATUSES = ("OPEN", "TP1 HIT")
CLOSED_STATUSES = ("CLOSED",)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    if not _table_exists(connection, table):
        return []
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")]


def _pick_column(columns: Sequence[str], candidates: Iterable[str]) -> Optional[str]:
    lowered = {column.lower(): column for column in columns}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found:
            return found
    return None


def _count_where(connection: sqlite3.Connection, table: str, where: str = "1=1", params: Sequence[Any] = ()) -> int:
    return int(connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where}", tuple(params)
    ).fetchone()[0] or 0)


def _scalar(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = connection.execute(sql, tuple(params)).fetchone()
    return row[0] if row else None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _score_column(columns: Sequence[str]) -> Optional[str]:
    return _pick_column(columns, ("confidence", "score", "ml_score", "predicted_score", "predicted_probability"))


def _timestamp_column(columns: Sequence[str]) -> Optional[str]:
    return _pick_column(columns, ("timestamp", "source_signal_timestamp", "created_at", "updated_at", "closed_at", "target_timestamp"))


def _status_counts(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> Dict[str, int]:
    if "status" not in columns:
        return {}
    rows = connection.execute(
        f"SELECT COALESCE(status, 'UNKNOWN') AS status, COUNT(*) AS count "
        f"FROM {_quote_identifier(table)} GROUP BY COALESCE(status, 'UNKNOWN') ORDER BY count DESC, status"
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _latest_timestamp(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> Dict[str, Optional[str]]:
    column = _timestamp_column(columns)
    if not column:
        return {"column": None, "latest": None}
    latest = _scalar(connection, f"SELECT MAX({_quote_identifier(column)}) FROM {_quote_identifier(table)}")
    return {"column": column, "latest": latest}


def _select_columns(columns: Sequence[str]) -> str:
    preferred = (
        "id", "timestamp", "source_signal_timestamp", "symbol", "side", "status", "exit_reason",
        "pnl", "confidence", "score", "regime", "created_at", "updated_at", "closed_at", "target_timestamp",
    )
    selected = [column for column in preferred if column in columns]
    return ", ".join(f"{_quote_identifier(column)} AS {_quote_identifier(column)}" for column in selected) or "*"


def _active_where() -> str:
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    return f"UPPER(COALESCE(status, '')) IN ({placeholders})"


def _closed_where() -> str:
    placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
    return f"UPPER(COALESCE(status, '')) IN ({placeholders})"


def _internal_paper_metrics(connection: sqlite3.Connection, columns: Sequence[str]) -> Dict[str, Any]:
    if not columns:
        return {"available": False}

    total_rows = _count_where(connection, IPT)
    status_counts = _status_counts(connection, IPT, columns)
    active_count = sum(int(status_counts.get(status, 0)) for status in ACTIVE_STATUSES)
    source_col = "source_signal_timestamp" if "source_signal_timestamp" in columns else None
    updated_col = "updated_at" if "updated_at" in columns else None
    score_col = _score_column(columns)
    now = _now_dt()
    stale_24h = 0
    stale_7d = 0

    active_rows_for_age: List[Dict[str, Any]] = []
    if "status" in columns and source_col:
        rows = connection.execute(
            f"SELECT {_quote_identifier(source_col)} AS source_signal_timestamp FROM {_quote_identifier(IPT)} WHERE {_active_where()}",
            tuple(ACTIVE_STATUSES),
        ).fetchall()
        active_rows_for_age = _rows_to_dicts(rows)
        for row in active_rows_for_age:
            ts = _parse_timestamp(row.get("source_signal_timestamp"))
            if ts and ts < now - timedelta(hours=24):
                stale_24h += 1
            if ts and ts < now - timedelta(days=7):
                stale_7d += 1

    active_by_symbol: Dict[str, int] = {}
    if "status" in columns and "symbol" in columns:
        rows = connection.execute(
            f"SELECT COALESCE(symbol, 'UNKNOWN') AS symbol, COUNT(*) AS count FROM {_quote_identifier(IPT)} "
            f"WHERE {_active_where()} GROUP BY COALESCE(symbol, 'UNKNOWN') ORDER BY count DESC, symbol",
            tuple(ACTIVE_STATUSES),
        ).fetchall()
        active_by_symbol = {str(row["symbol"]): int(row["count"]) for row in rows}

    since_24h = (now - timedelta(hours=24)).isoformat()
    inserted_col = _pick_column(columns, ("created_at", "inserted_at", "timestamp", "source_signal_timestamp"))
    closed_col = _pick_column(columns, ("closed_at", "updated_at", "target_timestamp", "source_signal_timestamp", "timestamp"))
    forward_col = _pick_column(columns, ("source_signal_timestamp", "timestamp", "target_timestamp", "updated_at", "closed_at"))

    inserted_last_24h = _count_where(connection, IPT, f"{_quote_identifier(inserted_col)} >= ?", (since_24h,)) if inserted_col else None
    closed_last_24h = _count_where(connection, IPT, f"{_closed_where()} AND {_quote_identifier(closed_col)} >= ?", tuple(CLOSED_STATUSES) + (since_24h,)) if "status" in columns and closed_col else None
    closed_after_baseline = _count_where(connection, IPT, f"{_closed_where()} AND {_quote_identifier(forward_col)} > ?", tuple(CLOSED_STATUSES) + (BASELINE_TIMESTAMP,)) if "status" in columns and forward_col else 0
    if "status" in columns and forward_col and score_col:
        closed_score95_after_baseline = _count_where(
            connection, IPT,
            f"{_closed_where()} AND {_quote_identifier(forward_col)} > ? AND CAST({_quote_identifier(score_col)} AS REAL) >= ?",
            tuple(CLOSED_STATUSES) + (BASELINE_TIMESTAMP, 95),
        )
    else:
        closed_score95_after_baseline = 0

    latest_active_rows: List[Dict[str, Any]] = []
    latest_closed_rows: List[Dict[str, Any]] = []
    order_active = _pick_column(columns, ("updated_at", "source_signal_timestamp", "timestamp", "id")) or "rowid"
    order_closed = _pick_column(columns, ("closed_at", "updated_at", "target_timestamp", "source_signal_timestamp", "timestamp", "id")) or "rowid"
    if "status" in columns:
        latest_active_rows = _rows_to_dicts(connection.execute(
            f"SELECT {_select_columns(columns)} FROM {_quote_identifier(IPT)} WHERE {_active_where()} "
            f"ORDER BY {_quote_identifier(order_active)} DESC LIMIT 20",
            tuple(ACTIVE_STATUSES),
        ).fetchall())
        if forward_col:
            latest_closed_rows = _rows_to_dicts(connection.execute(
                f"SELECT {_select_columns(columns)} FROM {_quote_identifier(IPT)} WHERE {_closed_where()} "
                f"AND {_quote_identifier(forward_col)} > ? ORDER BY {_quote_identifier(order_closed)} DESC LIMIT 20",
                tuple(CLOSED_STATUSES) + (BASELINE_TIMESTAMP,),
            ).fetchall())

    return {
        "available": True,
        "total_rows": total_rows,
        "status_counts": status_counts,
        "active_statuses": list(ACTIVE_STATUSES),
        "active_count": active_count,
        "active_cap": MAX_CONCURRENT_GLOBAL,
        "active_cap_comparison": f"{active_count}/{MAX_CONCURRENT_GLOBAL}",
        "active_by_symbol": active_by_symbol,
        "stale_active_older_than_24h_by_source_signal_timestamp": stale_24h,
        "stale_active_older_than_7d_by_source_signal_timestamp": stale_7d,
        "latest_source_signal_timestamp": _scalar(connection, f"SELECT MAX(source_signal_timestamp) FROM {_quote_identifier(IPT)}") if source_col else None,
        "latest_updated_at": _scalar(connection, f"SELECT MAX(updated_at) FROM {_quote_identifier(IPT)}") if updated_col else None,
        "inserted_rows_last_24h": inserted_last_24h,
        "closed_rows_last_24h": closed_last_24h,
        "closed_rows_after_baseline": closed_after_baseline,
        "closed_score_gte_95_rows_after_baseline": closed_score95_after_baseline,
        "score_column": score_col,
        "latest_20_active_rows": latest_active_rows,
        "latest_20_closed_rows_after_baseline": latest_closed_rows,
    }


def _candidate_buckets(connection: sqlite3.Connection, columns: Sequence[str]) -> Dict[str, Any]:
    timestamp_col = _pick_column(columns, ("timestamp", "created_at", "source_signal_timestamp", "updated_at"))
    score_col = _score_column(columns)
    result: Dict[str, Any] = {"timestamp_column": timestamp_col, "score_column": score_col, "gte_85": None, "gte_90": None, "gte_95": None}
    if not timestamp_col or not score_col:
        return result
    for threshold in (85, 90, 95):
        result[f"gte_{threshold}"] = _count_where(
            connection,
            "signal_candidates",
            f"{_quote_identifier(timestamp_col)} > ? AND CAST({_quote_identifier(score_col)} AS REAL) >= ?",
            (BASELINE_TIMESTAMP, threshold),
        )
    return result


def _audit() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "cp_id": "CP-044C",
        "generated_at_utc": _now(),
        "read_only": True,
        "db_path": DB_PATH,
        "sqlite_uri_mode": "mode=ro",
        "baseline_timestamp": BASELINE_TIMESTAMP,
        "max_concurrent_global": MAX_CONCURRENT_GLOBAL,
        "paper_only": True,
        "phase3_locked": True,
        "cp045_approved": False,
        "live_execution": "OFF",
        "classifier_state": "FROZEN",
        "model_promotion": "HOLD",
        "tables": {},
        "freshness": {},
        "signal_candidates_score_buckets_after_baseline": {},
        "internal_paper_lifecycle": {},
        "verdicts": {},
        "overall_status": "UNKNOWN",
    }

    with _connect_read_only(DB_PATH) as connection:
        for table in TABLES_TO_READ:
            exists = _table_exists(connection, table)
            columns = _table_columns(connection, table) if exists else []
            row_count = _count_where(connection, table) if exists else None
            report["tables"][table] = {
                "exists": exists,
                "columns": columns,
                "row_count": row_count,
                "latest_timestamp": _latest_timestamp(connection, table, columns) if exists else {"column": None, "latest": None},
            }

        ipt_columns = report["tables"][IPT]["columns"]
        report["internal_paper_lifecycle"] = _internal_paper_metrics(connection, ipt_columns)
        for table in FRESHNESS_TABLES:
            report["freshness"][table] = {
                "row_count": report["tables"].get(table, {}).get("row_count"),
                "latest_timestamp": report["tables"].get(table, {}).get("latest_timestamp", {"column": None, "latest": None}),
            }
        sc_columns = report["tables"].get("signal_candidates", {}).get("columns", [])
        if report["tables"].get("signal_candidates", {}).get("exists"):
            report["signal_candidates_score_buckets_after_baseline"] = _candidate_buckets(connection, sc_columns)

    lifecycle = report["internal_paper_lifecycle"]
    db_readable = True
    active_count = int(lifecycle.get("active_count") or 0)
    stale_24h = int(lifecycle.get("stale_active_older_than_24h_by_source_signal_timestamp") or 0)
    stale_7d = int(lifecycle.get("stale_active_older_than_7d_by_source_signal_timestamp") or 0)
    closed_after_baseline = int(lifecycle.get("closed_rows_after_baseline") or 0)
    score95_closed = int(lifecycle.get("closed_score_gte_95_rows_after_baseline") or 0)
    paper_exists = bool(report["tables"].get("paper_trades", {}).get("exists"))
    paper_rows = report["tables"].get("paper_trades", {}).get("row_count")

    verdicts = {
        "DB_READABLE": db_readable,
        "INTERNAL_PAPER_AVAILABLE": bool(lifecycle.get("available")),
        "ACTIVE_CAP_OK": active_count <= MAX_CONCURRENT_GLOBAL,
        "ACTIVE_CAP_FULL": active_count == MAX_CONCURRENT_GLOBAL,
        "ACTIVE_CAP_OVERFLOW": active_count > MAX_CONCURRENT_GLOBAL,
        "STALE_ACTIVE_WARNING": stale_24h > 0,
        "STALE_ACTIVE_CRITICAL": stale_7d > 0,
        "FORWARD_CLOSED_ROWS_PRESENT": closed_after_baseline > 0,
        "SCORE95_EVIDENCE_INSUFFICIENT": score95_closed < MIN_SCORE95_FORWARD_ROWS,
        "LEGACY_PAPER_TRADES_DEPRECATED": paper_exists and (paper_rows == 0 or bool(lifecycle.get("available"))),
        "CP045_APPROVED": False,
        "PHASE3_LOCKED": True,
    }
    report["verdicts"] = verdicts

    if active_count > MAX_CONCURRENT_GLOBAL or stale_7d > 0:
        overall = "BLOCKED_ACTIVE_OVERFLOW"
    elif stale_24h > 0:
        overall = "STALE_CONGESTION_RISK"
    elif active_count == MAX_CONCURRENT_GLOBAL:
        overall = "WATCH_ACTIVE_CAP_FULL"
    elif active_count <= MAX_CONCURRENT_GLOBAL and stale_7d == 0 and db_readable:
        overall = "HEALTHY_OBSERVATION"
    else:
        overall = "UNKNOWN"
    report["overall_status"] = overall
    return report


def _write_markdown(report: Dict[str, Any]) -> None:
    lifecycle = report.get("internal_paper_lifecycle", {})
    verdicts = report.get("verdicts", {})
    freshness = report.get("freshness", {})
    lines = [
        "# CP-044C Lifecycle Health Guard",
        "",
        "## Governance",
        "* Guard type: **READ-ONLY observation only**",
        f"* Database: `{report['db_path']}` opened with SQLite URI `{report['sqlite_uri_mode']}`",
        f"* Baseline timestamp: `{report['baseline_timestamp']}`",
        "* CP-045: **NOT APPROVED**",
        "* Phase 3: **LOCKED**",
        "* Live execution: **OFF**",
        "* PAPER_ONLY: **TRUE**",
        "* Classifier: **FROZEN**",
        "* Model promotion: **HOLD**",
        "",
        "## Lifecycle Summary",
        f"* Total internal paper rows: `{lifecycle.get('total_rows')}`",
        f"* Status counts: `{json.dumps(lifecycle.get('status_counts', {}), sort_keys=True)}`",
        f"* Active statuses: `{', '.join(lifecycle.get('active_statuses', []))}`",
        f"* Active by symbol: `{json.dumps(lifecycle.get('active_by_symbol', {}), sort_keys=True)}`",
        f"* Latest `source_signal_timestamp`: `{lifecycle.get('latest_source_signal_timestamp')}`",
        f"* Latest `updated_at`: `{lifecycle.get('latest_updated_at')}`",
        f"* Inserted rows last 24h: `{lifecycle.get('inserted_rows_last_24h')}`",
        f"* Closed rows last 24h: `{lifecycle.get('closed_rows_last_24h')}`",
        f"* Closed rows after baseline: `{lifecycle.get('closed_rows_after_baseline')}`",
        "",
        "## Active Cap Status",
        f"* Active count (`OPEN` + `TP1 HIT`): `{lifecycle.get('active_count')}`",
        f"* Active cap comparison: `{lifecycle.get('active_cap_comparison')}`",
        f"* ACTIVE_CAP_OK: `{verdicts.get('ACTIVE_CAP_OK')}`",
        f"* ACTIVE_CAP_FULL: `{verdicts.get('ACTIVE_CAP_FULL')}`",
        f"* ACTIVE_CAP_OVERFLOW: `{verdicts.get('ACTIVE_CAP_OVERFLOW')}`",
        "",
        "## Stale Active Status",
        f"* Active rows older than 24h by `source_signal_timestamp`: `{lifecycle.get('stale_active_older_than_24h_by_source_signal_timestamp')}`",
        f"* Active rows older than 7d by `source_signal_timestamp`: `{lifecycle.get('stale_active_older_than_7d_by_source_signal_timestamp')}`",
        f"* STALE_ACTIVE_WARNING: `{verdicts.get('STALE_ACTIVE_WARNING')}`",
        f"* STALE_ACTIVE_CRITICAL: `{verdicts.get('STALE_ACTIVE_CRITICAL')}`",
        "",
        "## Freshness Summary",
    ]
    for table in FRESHNESS_TABLES:
        info = freshness.get(table, {})
        ts = info.get("latest_timestamp", {})
        lines.append(f"* `{table}` rows: `{info.get('row_count')}`; latest `{ts.get('column')}`: `{ts.get('latest')}`")
    lines.extend([
        "",
        "## Signal Candidate Score Buckets After Baseline",
        f"* Buckets: `{json.dumps(report.get('signal_candidates_score_buckets_after_baseline', {}), sort_keys=True)}`",
        "",
        "## Score95 Evidence Status",
        f"* Closed score>=95 rows after baseline: `{lifecycle.get('closed_score_gte_95_rows_after_baseline')}`",
        f"* Required minimum rows: `{MIN_SCORE95_FORWARD_ROWS}`",
        f"* SCORE95_EVIDENCE_INSUFFICIENT: `{verdicts.get('SCORE95_EVIDENCE_INSUFFICIENT')}`",
        "",
        "## Latest 20 Active Rows",
        "```json",
        json.dumps(lifecycle.get("latest_20_active_rows", []), indent=2, sort_keys=True),
        "```",
        "",
        "## Latest 20 Closed Rows After Baseline",
        "```json",
        json.dumps(lifecycle.get("latest_20_closed_rows_after_baseline", []), indent=2, sort_keys=True),
        "```",
        "",
        "## Verdicts",
        f"* Overall status: **{report.get('overall_status')}**",
        f"* Verdict map: `{json.dumps(verdicts, sort_keys=True)}`",
        "",
        "## Final Decision",
        "* CP-045 **NOT APPROVED**",
        "* Phase 3 **LOCKED**",
        "* Live execution **OFF**",
        "* PAPER_ONLY **TRUE**",
        "",
        "This guard is read-only and writes only this Markdown report plus the paired JSON artifact.",
    ])
    with open(REPORT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _unavailable_report(error: Exception) -> Dict[str, Any]:
    return {
        "cp_id": "CP-044C",
        "generated_at_utc": _now(),
        "read_only": True,
        "db_path": DB_PATH,
        "sqlite_uri_mode": "mode=ro",
        "baseline_timestamp": BASELINE_TIMESTAMP,
        "paper_only": True,
        "phase3_locked": True,
        "cp045_approved": False,
        "live_execution": "OFF",
        "classifier_state": "FROZEN",
        "model_promotion": "HOLD",
        "error": str(error),
        "tables": {},
        "freshness": {},
        "signal_candidates_score_buckets_after_baseline": {},
        "internal_paper_lifecycle": {"available": False},
        "verdicts": {
            "DB_READABLE": False,
            "INTERNAL_PAPER_AVAILABLE": False,
            "CP045_APPROVED": False,
            "PHASE3_LOCKED": True,
            "SCORE95_EVIDENCE_INSUFFICIENT": True,
        },
        "overall_status": "DB_UNREADABLE",
    }


def main() -> int:
    os.makedirs("reports", exist_ok=True)
    try:
        report = _audit()
    except sqlite3.Error as exc:
        report = _unavailable_report(exc)

    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    _write_markdown(report)

    lifecycle = report.get("internal_paper_lifecycle", {})
    verdicts = report.get("verdicts", {})
    print("CP-044C Lifecycle Health Guard")
    print(f"overall: {report.get('overall_status')}")
    print(f"db_readable: {verdicts.get('DB_READABLE')}")
    print(f"active: {lifecycle.get('active_cap_comparison', 'N/A')}")
    print(f"stale_active_24h: {lifecycle.get('stale_active_older_than_24h_by_source_signal_timestamp', 'N/A')}")
    print(f"stale_active_7d: {lifecycle.get('stale_active_older_than_7d_by_source_signal_timestamp', 'N/A')}")
    print(f"closed_score>=95_after_baseline: {lifecycle.get('closed_score_gte_95_rows_after_baseline', 'N/A')}")
    print("CP-045: NOT APPROVED | Phase 3: LOCKED | Live execution: OFF | PAPER_ONLY: TRUE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
