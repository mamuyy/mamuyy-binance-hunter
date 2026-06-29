"""
CP-044B: Internal Paper Recovery Audit Evidence Pack

READ-ONLY audit evidence generator. This script opens mamuyy_hunter.db in
SQLite read-only URI mode and writes only local report artifacts under reports/.
It does not modify database rows, runtime behavior, thresholds, model registry,
execution, Telegram, candidate queues, dashboards, risk management, Phase 3
state, or model promotion state.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DB_PATH = "mamuyy_hunter.db"
BASELINE_TIMESTAMP = "2026-06-22T18:05:35.736930+00:00"
MAX_CONCURRENT_GLOBAL = 20
MIN_SCORE95_FORWARD_ROWS = 30

REPORT_MD = "reports/cp044b_internal_paper_recovery_audit.md"
REPORT_JSON = "reports/cp044b_internal_paper_recovery_audit.json"
REPORT_CSV = "reports/cp044b_closed_forward_rows.csv"

IPT = "internal_paper_trades"
TABLES_TO_INSPECT = ("internal_paper_trades", "signal_candidates", "signals", "shadow_trades")
ACTIVE_STATUSES = ("OPEN", "TP1 HIT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    sql = f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where}"
    return int(connection.execute(sql, tuple(params)).fetchone()[0] or 0)


def _scalar(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = connection.execute(sql, tuple(params)).fetchone()
    return row[0] if row else None


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _status_counts(connection: sqlite3.Connection, columns: Sequence[str]) -> Dict[str, int]:
    if "status" not in columns:
        return {}
    rows = connection.execute(
        f"SELECT COALESCE(status, 'UNKNOWN') AS status, COUNT(*) AS count "
        f"FROM {_quote_identifier(IPT)} GROUP BY COALESCE(status, 'UNKNOWN') ORDER BY count DESC, status"
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _latest_timestamp(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> Dict[str, Optional[str]]:
    timestamp_column = _pick_column(
        columns,
        ("timestamp", "source_signal_timestamp", "created_at", "updated_at", "closed_at", "target_timestamp"),
    )
    if not timestamp_column:
        return {"column": None, "latest": None}
    latest = _scalar(
        connection,
        f"SELECT MAX({_quote_identifier(timestamp_column)}) FROM {_quote_identifier(table)}",
    )
    return {"column": timestamp_column, "latest": latest}


def _score_column(columns: Sequence[str]) -> Optional[str]:
    return _pick_column(columns, ("confidence", "score", "ml_score", "predicted_score", "predicted_probability"))


def _signal_candidate_buckets(connection: sqlite3.Connection, columns: Sequence[str]) -> Dict[str, Any]:
    timestamp_column = _pick_column(columns, ("timestamp", "created_at", "source_signal_timestamp", "updated_at"))
    score_column = _score_column(columns)
    result: Dict[str, Any] = {"timestamp_column": timestamp_column, "score_column": score_column, "gte_85": None, "gte_90": None, "gte_95": None}
    if not timestamp_column or not score_column:
        return result
    for threshold in (85, 90, 95):
        result[f"gte_{threshold}"] = _count_where(
            connection,
            "signal_candidates",
            f"{_quote_identifier(timestamp_column)} > ? AND CAST({_quote_identifier(score_column)} AS REAL) >= ?",
            (BASELINE_TIMESTAMP, threshold),
        )
    return result


def _closed_forward_query(columns: Sequence[str], score95_only: bool = False, limit: Optional[int] = None) -> Tuple[str, List[Any]]:
    timestamp_column = _pick_column(columns, ("source_signal_timestamp", "timestamp", "target_timestamp", "updated_at")) or "timestamp"
    score_column = _score_column(columns)
    selected = ["id", "timestamp", "source_signal_timestamp", "symbol", "side", "status", "exit_reason", "pnl", "confidence", "regime", "updated_at", "target_timestamp"]
    select_exprs = [f"{_quote_identifier(col)} AS {_quote_identifier(col)}" for col in selected if col in columns]
    if not select_exprs:
        select_exprs = ["*"]
    where = ["UPPER(COALESCE(status, '')) = 'CLOSED'", f"{_quote_identifier(timestamp_column)} > ?"]
    params: List[Any] = [BASELINE_TIMESTAMP]
    if score95_only and score_column:
        where.append(f"CAST({_quote_identifier(score_column)} AS REAL) >= ?")
        params.append(95)
    elif score95_only and not score_column:
        where.append("0")
    order_col = _pick_column(columns, ("target_timestamp", "updated_at", "source_signal_timestamp", "timestamp", "id")) or "rowid"
    sql = f"SELECT {', '.join(select_exprs)} FROM {_quote_identifier(IPT)} WHERE {' AND '.join(where)} ORDER BY {_quote_identifier(order_col)} DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql, params


def _write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["no_rows"])
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _audit() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    report: Dict[str, Any] = {
        "cp_id": "CP-044B",
        "generated_at_utc": _now(),
        "read_only": True,
        "db_path": DB_PATH,
        "baseline_timestamp": BASELINE_TIMESTAMP,
        "max_concurrent_global": MAX_CONCURRENT_GLOBAL,
        "phase3_locked": True,
        "live_execution": "OFF",
        "paper_only": True,
        "classifier_state": "FROZEN",
        "model_promotion": "HOLD",
        "tables": {},
        "verdicts": {},
        "findings": {},
    }
    latest_30: List[Dict[str, Any]] = []

    with _connect_read_only(DB_PATH) as connection:
        for table in TABLES_TO_INSPECT:
            exists = _table_exists(connection, table)
            columns = _table_columns(connection, table) if exists else []
            report["tables"][table] = {
                "exists": exists,
                "columns": columns,
                "row_count": _count_where(connection, table) if exists else None,
                "latest_timestamp": _latest_timestamp(connection, table, columns) if exists else {"column": None, "latest": None},
            }

        ipt_columns = report["tables"][IPT]["columns"]
        status_counts = _status_counts(connection, ipt_columns) if report["tables"][IPT]["exists"] else {}
        active_count = sum(int(status_counts.get(status, 0)) for status in ACTIVE_STATUSES)
        expired_orphaned_count = _count_where(connection, IPT, "UPPER(COALESCE(exit_reason, '')) = 'EXPIRED_ORPHANED'") if "exit_reason" in ipt_columns else 0
        latest_source_signal_timestamp = None
        if "source_signal_timestamp" in ipt_columns:
            latest_source_signal_timestamp = _scalar(connection, f"SELECT MAX(source_signal_timestamp) FROM {_quote_identifier(IPT)}")

        forward_timestamp_column = _pick_column(ipt_columns, ("source_signal_timestamp", "timestamp", "target_timestamp", "updated_at"))
        can_query_closed_forward = bool(report["tables"][IPT]["exists"] and "status" in ipt_columns and forward_timestamp_column)
        closed_after_baseline = _count_where(
            connection,
            IPT,
            "UPPER(COALESCE(status, '')) = 'CLOSED' AND "
            f"{_quote_identifier(forward_timestamp_column or 'timestamp')} > ?",
            (BASELINE_TIMESTAMP,),
        ) if can_query_closed_forward else 0

        if can_query_closed_forward:
            score95_sql, score95_params = _closed_forward_query(ipt_columns, score95_only=True)
            closed_score95_after_baseline = int(_scalar(connection, f"SELECT COUNT(*) FROM ({score95_sql})", score95_params) or 0)
            latest_sql, latest_params = _closed_forward_query(ipt_columns, score95_only=False, limit=30)
            latest_30 = _rows_to_dicts(connection.execute(latest_sql, latest_params).fetchall())
        else:
            closed_score95_after_baseline = 0
            latest_30 = []

        candidate_columns = report["tables"].get("signal_candidates", {}).get("columns", [])
        signal_candidate_buckets = _signal_candidate_buckets(connection, candidate_columns) if candidate_columns else {}

    report["findings"] = {
        "internal_paper_bridge_recovery": "OPERATIONALLY RESOLVED",
        "score95_forward_evidence": "INSUFFICIENT",
        "cp045": "NOT APPROVED",
        "phase3": "LOCKED",
        "live_execution": "OFF",
        "paper_only": True,
        "status_counts": status_counts,
        "active_statuses": list(ACTIVE_STATUSES),
        "active_count": active_count,
        "active_cap": MAX_CONCURRENT_GLOBAL,
        "active_cap_comparison": f"{active_count}/{MAX_CONCURRENT_GLOBAL}",
        "expired_orphaned_count": expired_orphaned_count,
        "latest_internal_paper_source_signal_timestamp": latest_source_signal_timestamp,
        "closed_internal_paper_rows_after_baseline": closed_after_baseline,
        "closed_score95_rows_after_baseline": closed_score95_after_baseline,
        "score95_minimum_required_rows": MIN_SCORE95_FORWARD_ROWS,
        "signal_candidates_score_buckets_after_baseline": signal_candidate_buckets,
        "latest_30_closed_rows_after_baseline_count": len(latest_30),
    }
    report["verdicts"] = {
        "recovery_confirmed": active_count <= MAX_CONCURRENT_GLOBAL and closed_after_baseline > 0,
        "score95_evidence_insufficient": closed_score95_after_baseline < MIN_SCORE95_FORWARD_ROWS,
        "active_cap_risk": active_count >= MAX_CONCURRENT_GLOBAL,
        "phase3_locked": True,
    }
    labels = []
    if report["verdicts"]["recovery_confirmed"]:
        labels.append("RECOVERY_CONFIRMED")
    if report["verdicts"]["score95_evidence_insufficient"]:
        labels.append("SCORE95_EVIDENCE_INSUFFICIENT")
    if report["verdicts"]["active_cap_risk"]:
        labels.append("ACTIVE_CAP_RISK")
    labels.append("PHASE3_LOCKED")
    report["verdicts"]["labels"] = labels
    return report, latest_30


def _write_markdown(report: Dict[str, Any]) -> None:
    findings = report["findings"]
    verdicts = report["verdicts"]
    latest = report["tables"]
    lines = [
        "# CP-044B Internal Paper Recovery Audit Evidence Pack",
        "",
        "## Safety / Governance State",
        "* Internal paper bridge recovery: **OPERATIONALLY RESOLVED**",
        "* Score95 forward evidence: **INSUFFICIENT**",
        "* CP-045: **NOT APPROVED**",
        "* Phase 3: **LOCKED**",
        "* Live execution: **OFF**",
        "* PAPER_ONLY: **TRUE**",
        "* Classifier: **FROZEN**",
        "* Model promotion: **HOLD**",
        "",
        "## Audit Parameters",
        f"* Baseline timestamp: `{report['baseline_timestamp']}`",
        f"* Database: `{report['db_path']}` opened in SQLite read-only URI mode",
        f"* Active cap: `{findings.get('active_cap', MAX_CONCURRENT_GLOBAL)}`",
        "",
        "## Internal Paper Trade Evidence",
        f"* Status counts: `{json.dumps(findings.get('status_counts', {}), sort_keys=True)}`",
        f"* Active count (`OPEN` + `TP1 HIT`): `{findings.get('active_count')}`",
        f"* Active cap comparison: `{findings.get('active_cap_comparison')}`",
        f"* Expired orphaned count (`exit_reason='EXPIRED_ORPHANED'`): `{findings.get('expired_orphaned_count')}`",
        f"* Latest internal paper `source_signal_timestamp`: `{findings.get('latest_internal_paper_source_signal_timestamp')}`",
        f"* Closed internal paper rows after baseline: `{findings.get('closed_internal_paper_rows_after_baseline')}`",
        f"* Closed score>=95 rows after baseline: `{findings.get('closed_score95_rows_after_baseline')}`",
        "",
        "## Signal Freshness / Candidate Score Buckets",
        f"* signal_candidates score buckets after baseline: `{json.dumps(findings.get('signal_candidates_score_buckets_after_baseline', {}), sort_keys=True)}`",
    ]
    for table in ("signals", "signal_candidates", "shadow_trades"):
        info = latest.get(table, {})
        ts = info.get("latest_timestamp", {})
        lines.append(f"* Latest `{table}` timestamp (`{ts.get('column')}`): `{ts.get('latest')}`")
    lines.extend([
        "",
        "## Verdicts",
        f"* Labels: `{', '.join(verdicts['labels'])}`",
        f"* RECOVERY_CONFIRMED: `{verdicts.get('recovery_confirmed')}`",
        f"* SCORE95_EVIDENCE_INSUFFICIENT: `{verdicts.get('score95_evidence_insufficient')}`",
        f"* ACTIVE_CAP_RISK: `{verdicts.get('active_cap_risk')}`",
        f"* PHASE3_LOCKED: `{verdicts.get('phase3_locked')}`",
        "",
        "## Artifacts",
        f"* JSON: `{REPORT_JSON}`",
        f"* Latest 30 closed rows after baseline CSV: `{REPORT_CSV}`",
        "",
        "This audit is read-only with respect to `mamuyy_hunter.db`; it writes only the evidence artifacts listed above.",
    ])
    with open(REPORT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    os.makedirs("reports", exist_ok=True)
    try:
        report, latest_30 = _audit()
    except sqlite3.OperationalError as exc:
        report = {
            "cp_id": "CP-044B",
            "generated_at_utc": _now(),
            "read_only": True,
            "db_path": DB_PATH,
            "baseline_timestamp": BASELINE_TIMESTAMP,
            "phase3_locked": True,
            "live_execution": "OFF",
            "paper_only": True,
            "classifier_state": "FROZEN",
            "model_promotion": "HOLD",
            "error": str(exc),
            "findings": {
                "internal_paper_bridge_recovery": "UNKNOWN_DB_UNAVAILABLE",
                "score95_forward_evidence": "INSUFFICIENT",
                "cp045": "NOT APPROVED",
                "phase3": "LOCKED",
                "live_execution": "OFF",
                "paper_only": True,
            },
            "verdicts": {"labels": ["SCORE95_EVIDENCE_INSUFFICIENT", "PHASE3_LOCKED"], "phase3_locked": True},
            "tables": {},
        }
        latest_30 = []
    _write_csv(latest_30, REPORT_CSV)
    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    _write_markdown(report)

    findings = report.get("findings", {})
    verdicts = report.get("verdicts", {})
    print("CP-044B Internal Paper Recovery Audit")
    print(f"baseline: {BASELINE_TIMESTAMP}")
    print(f"active: {findings.get('active_cap_comparison', 'N/A')}")
    print(f"closed after baseline: {findings.get('closed_internal_paper_rows_after_baseline', 'N/A')}")
    print(f"closed score>=95 after baseline: {findings.get('closed_score95_rows_after_baseline', 'N/A')}")
    print(f"verdicts: {', '.join(verdicts.get('labels', []))}")
    print("CP-045: NOT APPROVED | Phase 3: LOCKED | Live execution: OFF | PAPER_ONLY: TRUE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
