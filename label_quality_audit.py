from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from config import config

REPORT_PATH = "reports/label_quality_audit.json"
TABLE_CANDIDATES = ("historical_outcomes", "paper_trades", "internal_paper_trades", "shadow_trades")
LABEL_COLUMNS = ("status", "win_loss", "outcome", "label", "lifecycle_status")
PNL_COLUMNS = ("pnl_pct", "pnl_percent", "pnl", "profit", "profit_pct")
TIMESTAMP_COLUMNS = ("timestamp", "signal_timestamp", "close_timestamp", "created_at")
REGIME_COLUMNS = ("regime_name", "regime", "market_regime")
UNKNOWN_LABELS = {"", "UNKNOWN", "FLAT", "NONE", "NULL", "N/A"}
POSITIVE_LABELS = {"WIN", "TP1 HIT", "TP2 HIT", "TAKE PROFIT", "PROFIT", "PROFIT_MATURED"}
NEGATIVE_LABELS = {"LOSS", "SL HIT", "STOP LOSS", "EXPIRED_NEGATIVE"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _pick(columns: Sequence[str], candidates: Iterable[str]) -> str:
    lower_map = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return ""


def _read_rows(conn: sqlite3.Connection, table: str, limit: int = 10000) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def _audit_table(conn: sqlite3.Connection, table: str) -> Dict[str, Any]:
    columns = _columns(conn, table)
    label_column = _pick(columns, LABEL_COLUMNS)
    pnl_column = _pick(columns, PNL_COLUMNS)
    regime_column = _pick(columns, REGIME_COLUMNS)
    timestamp_columns = [column for column in columns if column.lower() in {item.lower() for item in TIMESTAMP_COLUMNS}]
    rows = _read_rows(conn, table)
    distribution: Counter[str] = Counter()
    regime_distribution: Dict[str, Counter[str]] = {}
    mismatch_count = 0
    pnl_checked = 0
    unknown_count = 0
    timestamp_checked = 0
    timestamp_missing_or_invalid = 0
    future_timestamps = 0
    inverted_close_timestamps = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        raw_label = str(row.get(label_column, "") if label_column else "").strip().upper()
        label = raw_label or "UNKNOWN"
        distribution[label] += 1
        if label in UNKNOWN_LABELS:
            unknown_count += 1
        if regime_column:
            regime = str(row.get(regime_column) or "UNKNOWN").strip().upper() or "UNKNOWN"
            regime_distribution.setdefault(regime, Counter())[label] += 1
        pnl = _number(row.get(pnl_column)) if pnl_column else None
        if pnl is not None and label_column:
            pnl_checked += 1
            if label in POSITIVE_LABELS and pnl < 0:
                mismatch_count += 1
            elif label in NEGATIVE_LABELS and pnl > 0:
                mismatch_count += 1
        parsed_by_col: Dict[str, datetime] = {}
        for timestamp_column in timestamp_columns:
            timestamp_checked += 1
            parsed = _parse_timestamp(row.get(timestamp_column))
            if parsed is None:
                timestamp_missing_or_invalid += 1
            else:
                parsed_by_col[timestamp_column] = parsed
                if parsed > now:
                    future_timestamps += 1
        signal_ts = parsed_by_col.get("signal_timestamp")
        close_ts = parsed_by_col.get("close_timestamp")
        if signal_ts and close_ts and close_ts < signal_ts:
            inverted_close_timestamps += 1

    total_rows = len(rows)
    mismatch_rate = (mismatch_count / pnl_checked) if pnl_checked else 0.0
    unknown_rate = (unknown_count / total_rows) if total_rows else 0.0
    timestamp_issue_rate = ((timestamp_missing_or_invalid + future_timestamps + inverted_close_timestamps) / timestamp_checked) if timestamp_checked else 0.0
    return {
        "table": table,
        "row_sample_count": total_rows,
        "columns": {
            "label": label_column,
            "pnl": pnl_column,
            "regime": regime_column,
            "timestamps": timestamp_columns,
        },
        "label_distribution": dict(distribution),
        "flat_unknown": {
            "count": unknown_count,
            "rate": round(unknown_rate, 6),
            "handled_as_review": unknown_count > 0,
        },
        "pnl_mismatch": {
            "checked_rows": pnl_checked,
            "mismatch_count": mismatch_count,
            "mismatch_rate": round(mismatch_rate, 6),
        },
        "timestamp_integrity": {
            "checked_values": timestamp_checked,
            "missing_or_invalid": timestamp_missing_or_invalid,
            "future_values": future_timestamps,
            "inverted_signal_close_pairs": inverted_close_timestamps,
            "issue_rate": round(timestamp_issue_rate, 6),
        },
        "per_regime_distribution": {regime: dict(counter) for regime, counter in regime_distribution.items()},
    }


def generate_label_quality_audit(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = REPORT_PATH,
    write_report: bool = True,
) -> Dict[str, Any]:
    blockers: List[str] = []
    warnings: List[str] = []
    table_reports: List[Dict[str, Any]] = []
    if not os.path.exists(db_path):
        blockers.append(f"Database not found: {db_path}")
    else:
        try:
            with _connect_read_only(db_path) as conn:
                available = _tables(conn)
                for table in TABLE_CANDIDATES:
                    if table in available:
                        report = _audit_table(conn, table)
                        if report.get("row_sample_count", 0) > 0:
                            table_reports.append(report)
        except sqlite3.Error as exc:
            blockers.append(f"Read-only label audit failed: {exc}")

    if not table_reports and not blockers:
        blockers.append("No label/outcome rows available in audited tables.")

    total_rows = sum(int(item.get("row_sample_count", 0)) for item in table_reports)
    total_mismatches = sum(int(item.get("pnl_mismatch", {}).get("mismatch_count", 0)) for item in table_reports)
    total_pnl_checked = sum(int(item.get("pnl_mismatch", {}).get("checked_rows", 0)) for item in table_reports)
    total_unknown = sum(int(item.get("flat_unknown", {}).get("count", 0)) for item in table_reports)
    timestamp_issues = sum(
        int(item.get("timestamp_integrity", {}).get("missing_or_invalid", 0))
        + int(item.get("timestamp_integrity", {}).get("future_values", 0))
        + int(item.get("timestamp_integrity", {}).get("inverted_signal_close_pairs", 0))
        for item in table_reports
    )
    total_timestamps = sum(int(item.get("timestamp_integrity", {}).get("checked_values", 0)) for item in table_reports)
    mismatch_rate = (total_mismatches / total_pnl_checked) if total_pnl_checked else 0.0
    unknown_rate = (total_unknown / total_rows) if total_rows else 0.0
    timestamp_issue_rate = (timestamp_issues / total_timestamps) if total_timestamps else 0.0

    if total_pnl_checked == 0:
        warnings.append("No PnL/profit column found for mismatch validation; audit is REVIEW at best.")
    if total_unknown > 0:
        warnings.append("FLAT/UNKNOWN/empty labels detected and explicitly counted for review.")
    if timestamp_issues > 0:
        warnings.append("Timestamp integrity issues detected.")

    if blockers or mismatch_rate > 0.05 or timestamp_issue_rate > 0.02:
        verdict = "FAIL"
    elif warnings or unknown_rate > 0.15 or total_pnl_checked == 0:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    report: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "mode": "READ_ONLY_PAPER_ONLY_LABEL_QUALITY_AUDIT",
        "paper_only": True,
        "read_only": True,
        "verdict": verdict,
        "database_path": db_path,
        "summary": {
            "tables_audited": [item.get("table") for item in table_reports],
            "total_rows_sampled": total_rows,
            "pnl_checked_rows": total_pnl_checked,
            "pnl_mismatch_count": total_mismatches,
            "pnl_mismatch_rate": round(mismatch_rate, 6),
            "flat_unknown_count": total_unknown,
            "flat_unknown_rate": round(unknown_rate, 6),
            "timestamp_checked_values": total_timestamps,
            "timestamp_issue_count": timestamp_issues,
            "timestamp_issue_rate": round(timestamp_issue_rate, 6),
        },
        "tables": table_reports,
        "warnings": warnings,
        "blockers": blockers,
        "safety": [
            "PAPER_ONLY enforced",
            "Read-only SELECT queries only",
            "No label mutation, retraining, threshold tuning, or promotion",
        ],
    }
    if write_report:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, sort_keys=True)
            file.write("\n")
    return report


def format_label_quality_audit(report: Dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    return (
        "LABEL QUALITY AUDIT\n"
        f"Verdict: {report.get('verdict', 'FAIL')}\n"
        f"Rows Sampled: {summary.get('total_rows_sampled', 0)}\n"
        f"PnL Mismatch Rate: {summary.get('pnl_mismatch_rate', 0)}\n"
        f"FLAT/UNKNOWN Rate: {summary.get('flat_unknown_rate', 0)}\n"
        f"Timestamp Issue Rate: {summary.get('timestamp_issue_rate', 0)}\n"
        "PAPER_ONLY read-only audit. No labels or model artifacts were changed."
    )


if __name__ == "__main__":
    result = generate_label_quality_audit(db_path=config.database_path, output_path=REPORT_PATH)
    print(format_label_quality_audit(result))
    print(f"Report generated: {REPORT_PATH}")
