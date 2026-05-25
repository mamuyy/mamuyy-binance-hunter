#!/usr/bin/env python3
"""
Audit regime coverage gaps.

Purpose:
- Explain why many rows in ml_dataset_regime_enriched.csv are UNMATCHED.
- Read-only against SQLite DB.
- Reads generated dataset CSV.
- Writes logs/regime_coverage_report.json only.
"""

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
DB_PATH = PROJECT_DIR / "mamuyy_hunter.db"
CSV_PATH = PROJECT_DIR / "data/ml_dataset_regime_enriched.csv"
OUT_JSON = PROJECT_DIR / "logs/regime_coverage_report.json"


def to_dt(value: Any):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def date_key(value: Any) -> str:
    dt = to_dt(value)
    return dt.date().isoformat() if dt else "UNKNOWN"


def connect_readonly() -> sqlite3.Connection:
    uri = f"file:{DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_db_summary(conn: sqlite3.Connection) -> dict:
    outcome = conn.execute("""
        SELECT
          COUNT(*) AS rows,
          MIN(signal_timestamp) AS first_signal,
          MAX(signal_timestamp) AS last_signal,
          MIN(close_timestamp) AS first_close,
          MAX(close_timestamp) AS last_close
        FROM historical_outcomes
    """).fetchone()

    regime = conn.execute("""
        SELECT
          COUNT(*) AS rows,
          MIN(timestamp) AS first_regime,
          MAX(timestamp) AS last_regime
        FROM regime_logs
    """).fetchone()

    return {
        "historical_outcomes": dict(outcome),
        "regime_logs": dict(regime),
    }


def fetch_regime_gaps(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT timestamp
        FROM regime_logs
        ORDER BY timestamp
    """).fetchall()

    gaps = []
    prev_dt = None
    prev_ts = None

    for row in rows:
        ts = row["timestamp"]
        dt = to_dt(ts)
        if not dt:
            continue

        if prev_dt is not None:
            delta_seconds = int((dt - prev_dt).total_seconds())
            if delta_seconds > 1800:
                gaps.append({
                    "from": prev_ts,
                    "to": ts,
                    "gap_seconds": delta_seconds,
                    "gap_minutes": round(delta_seconds / 60, 2),
                })

        prev_dt = dt
        prev_ts = ts

    gaps.sort(key=lambda x: x["gap_seconds"], reverse=True)
    return gaps


def analyze_dataset() -> dict:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Dataset CSV not found: {CSV_PATH}")

    by_date = defaultdict(lambda: {
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "exact_5m": 0,
        "near_15m": 0,
        "far_30m": 0,
    })

    by_symbol = defaultdict(lambda: {
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
    })

    total = {
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "exact_5m": 0,
        "near_15m": 0,
        "far_30m": 0,
    }

    first_signal = None
    last_signal = None

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            total["rows"] += 1

            signal_ts = row.get("signal_timestamp")
            dt = to_dt(signal_ts)
            if dt:
                if first_signal is None or dt < first_signal:
                    first_signal = dt
                if last_signal is None or dt > last_signal:
                    last_signal = dt

            day = date_key(signal_ts)
            symbol = row.get("symbol") or "UNKNOWN"
            q = row.get("match_quality") or "UNMATCHED"
            matched = q != "UNMATCHED"

            by_date[day]["rows"] += 1
            by_symbol[symbol]["rows"] += 1

            if matched:
                total["matched"] += 1
                by_date[day]["matched"] += 1
                by_symbol[symbol]["matched"] += 1
            else:
                total["unmatched"] += 1
                by_date[day]["unmatched"] += 1
                by_symbol[symbol]["unmatched"] += 1

            if q == "EXACT_5M":
                total["exact_5m"] += 1
                by_date[day]["exact_5m"] += 1
            elif q == "NEAR_15M":
                total["near_15m"] += 1
                by_date[day]["near_15m"] += 1
            elif q == "FAR_30M":
                total["far_30m"] += 1
                by_date[day]["far_30m"] += 1

    total["matched_pct"] = round((total["matched"] / total["rows"]) * 100, 4) if total["rows"] else 0.0
    total["unmatched_pct"] = round((total["unmatched"] / total["rows"]) * 100, 4) if total["rows"] else 0.0

    daily_rows = []
    for day, item in by_date.items():
        rows = item["rows"]
        item = dict(item)
        item["date"] = day
        item["matched_pct"] = round((item["matched"] / rows) * 100, 4) if rows else 0.0
        item["unmatched_pct"] = round((item["unmatched"] / rows) * 100, 4) if rows else 0.0
        daily_rows.append(item)

    symbol_rows = []
    for symbol, item in by_symbol.items():
        rows = item["rows"]
        item = dict(item)
        item["symbol"] = symbol
        item["matched_pct"] = round((item["matched"] / rows) * 100, 4) if rows else 0.0
        item["unmatched_pct"] = round((item["unmatched"] / rows) * 100, 4) if rows else 0.0
        symbol_rows.append(item)

    daily_rows.sort(key=lambda x: (x["unmatched_pct"], x["rows"]), reverse=True)
    symbol_rows.sort(key=lambda x: (x["unmatched_pct"], x["rows"]), reverse=True)

    return {
        "source_csv": str(CSV_PATH),
        "first_signal_in_csv": first_signal.isoformat() if first_signal else None,
        "last_signal_in_csv": last_signal.isoformat() if last_signal else None,
        "total": total,
        "worst_dates_by_unmatched_pct": daily_rows[:20],
        "worst_symbols_by_unmatched_pct": symbol_rows[:20],
        "all_dates": sorted(daily_rows, key=lambda x: x["date"]),
    }


def main() -> int:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    with connect_readonly() as conn:
        db_summary = fetch_db_summary(conn)
        regime_gaps = fetch_regime_gaps(conn)

    dataset_summary = analyze_dataset()

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_COVERAGE_AUDIT",
        "db_summary": db_summary,
        "dataset_summary": dataset_summary,
        "regime_gap_count_gt_30m": len(regime_gaps),
        "largest_regime_gaps": regime_gaps[:20],
        "interpretation": {
            "main_issue": "High UNMATCHED means many historical outcomes do not have nearby regime log context.",
            "next_action": "Improve/backfill regime_logs coverage before confidence calibration.",
        },
        "verdict": "READ_ONLY_REGIME_COVERAGE_AUDIT_COMPLETE",
    }

    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
