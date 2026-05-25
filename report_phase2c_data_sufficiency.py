#!/usr/bin/env python3
"""
Read-only Phase 2C data sufficiency report.

Safety:
- Reads from mamuyy_hunter.db (read-only) and existing CSV artifacts only.
- Does not write to DB.
- Does not alter runtime/execution behavior.
- Writes logs/phase2c_data_sufficiency_report.json only.
"""

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
DB_PATH = PROJECT_DIR / "mamuyy_hunter.db"
CSV_PATH = PROJECT_DIR / "data/ml_calibration_matched_20260520.csv"
OUT_JSON = PROJECT_DIR / "logs/phase2c_data_sufficiency_report.json"

TRAIN_START = "2026-05-20"
TRAIN_END = "2026-05-23"  # exclusive
VALID_START = "2026-05-23"

CLOSED_MIN = 100
VALID_MIN = 30
REGIME_VALID_MIN = 20
IMBALANCE_MAX = 5.0
SPARSE_BUCKET_MIN = 20
SPARSE_REGIME_BUCKET_MIN = 20

PROFITABLE_LABELS = {"WIN", "TP1 HIT", "TP2 HIT", "TP3 HIT"}
NON_PROFITABLE_LABELS = {"LOSS", "SL HIT", "BREAKEVEN", "TIMEOUT"}


def to_dt(value: str | None):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def bucket_name(score: float) -> str:
    if score < 20:
        return "00-19"
    if score < 40:
        return "20-39"
    if score < 60:
        return "40-59"
    if score < 80:
        return "60-79"
    return "80-100"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table_name,)
    ).fetchone()
    return row is not None


def detect_closed_expression(conn: sqlite3.Connection) -> str | None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(historical_outcomes)").fetchall()]
    if "close_timestamp" in cols:
        return "close_timestamp IS NOT NULL"
    if "status" in cols:
        return "status IN ('WIN','LOSS','TP1 HIT','TP2 HIT','TP3 HIT','SL HIT','BREAKEVEN','TIMEOUT')"
    if "win_loss" in cols:
        return "win_loss IN ('WIN','LOSS')"
    return None


def fetch_db_stats() -> dict:
    stats = {
        "source_detected": False,
        "total_outcomes": 0,
        "closed_outcomes": 0,
        "label_distribution": {},
        "binary_distribution": {"profitable": 0, "non_profitable": 0, "unknown": 0},
    }

    if not DB_PATH.exists():
        return stats

    with connect_readonly(DB_PATH) as conn:
        if not table_exists(conn, "historical_outcomes"):
            return stats

        stats["source_detected"] = True

        row = conn.execute("SELECT COUNT(*) AS c FROM historical_outcomes").fetchone()
        stats["total_outcomes"] = int(row["c"] if row else 0)

        closed_expr = detect_closed_expression(conn)
        if closed_expr:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM historical_outcomes WHERE {closed_expr}").fetchone()
            stats["closed_outcomes"] = int(row["c"] if row else 0)

            distribution_rows = conn.execute(
                f"""
                SELECT
                  COALESCE(status, win_loss, 'UNKNOWN') AS label,
                  COUNT(*) AS rows
                FROM historical_outcomes
                WHERE {closed_expr}
                GROUP BY COALESCE(status, win_loss, 'UNKNOWN')
                ORDER BY rows DESC
                """
            ).fetchall()

            label_distribution = {str(r["label"]): int(r["rows"]) for r in distribution_rows}
            stats["label_distribution"] = label_distribution

            profitable = 0
            non_profitable = 0
            unknown = 0
            for label, count in label_distribution.items():
                if label in PROFITABLE_LABELS:
                    profitable += count
                elif label in NON_PROFITABLE_LABELS:
                    non_profitable += count
                elif label == "WIN":
                    profitable += count
                elif label == "LOSS":
                    non_profitable += count
                else:
                    unknown += count
            stats["binary_distribution"] = {
                "profitable": int(profitable),
                "non_profitable": int(non_profitable),
                "unknown": int(unknown),
            }

    return stats


def fetch_csv_stats() -> dict:
    stats = {
        "source_detected": False,
        "rows": [],
        "train_rows": 0,
        "validation_rows": 0,
        "per_regime_counts": {},
        "sparse_regime_flags": [],
    }

    if not CSV_PATH.exists():
        return stats

    stats["source_detected"] = True
    rows = []
    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            win_loss = row.get("win_loss") or ""
            if win_loss not in ("WIN", "LOSS"):
                continue
            score = to_float(row.get("score"))
            rows.append(
                {
                    "signal_dt": to_dt(row.get("signal_timestamp")),
                    "regime": row.get("matched_regime") or "UNKNOWN",
                    "bucket": bucket_name(score),
                    "win_loss": win_loss,
                }
            )

    train_start = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    train_end = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    valid_start = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)

    train_rows = [r for r in rows if r["signal_dt"] and train_start <= r["signal_dt"] < train_end]
    valid_rows = [r for r in rows if r["signal_dt"] and r["signal_dt"] >= valid_start]

    valid_regime_counts = Counter(r["regime"] for r in valid_rows)
    train_bucket_counts = Counter(r["bucket"] for r in train_rows)
    train_regime_bucket_counts = Counter(f"{r['regime']}|{r['bucket']}" for r in train_rows)

    sparse_flags = []
    for regime, count in sorted(valid_regime_counts.items()):
        if count < REGIME_VALID_MIN:
            sparse_flags.append(
                {
                    "type": "validation_regime_sparse",
                    "key": regime,
                    "rows": count,
                    "threshold": REGIME_VALID_MIN,
                }
            )

    for bucket, count in sorted(train_bucket_counts.items()):
        if count < SPARSE_BUCKET_MIN:
            sparse_flags.append(
                {
                    "type": "train_bucket_sparse",
                    "key": bucket,
                    "rows": count,
                    "threshold": SPARSE_BUCKET_MIN,
                }
            )

    for key, count in sorted(train_regime_bucket_counts.items()):
        if count < SPARSE_REGIME_BUCKET_MIN:
            sparse_flags.append(
                {
                    "type": "train_regime_bucket_sparse",
                    "key": key,
                    "rows": count,
                    "threshold": SPARSE_REGIME_BUCKET_MIN,
                }
            )

    stats.update(
        {
            "rows": rows,
            "train_rows": len(train_rows),
            "validation_rows": len(valid_rows),
            "per_regime_counts": dict(sorted(valid_regime_counts.items())),
            "sparse_regime_flags": sparse_flags,
        }
    )
    return stats


def imbalance_ratio(binary_distribution: dict) -> float | None:
    profitable = int(binary_distribution.get("profitable", 0))
    non_profitable = int(binary_distribution.get("non_profitable", 0))
    if profitable == 0 or non_profitable == 0:
        return None
    dominant = max(profitable, non_profitable)
    minority = min(profitable, non_profitable)
    return dominant / minority


def main():
    db_stats = fetch_db_stats()
    csv_stats = fetch_csv_stats()

    sources = {
        "db_historical_outcomes": db_stats["source_detected"],
        "calibration_csv": csv_stats["source_detected"],
        "db_path": str(DB_PATH),
        "csv_path": str(CSV_PATH),
    }

    label_distribution = db_stats["label_distribution"]
    if not label_distribution and csv_stats["source_detected"]:
        wl = Counter(r["win_loss"] for r in csv_stats["rows"])
        label_distribution = {k: int(v) for k, v in wl.items()}

    binary_distribution = db_stats["binary_distribution"]
    if binary_distribution.get("profitable", 0) == 0 and binary_distribution.get("non_profitable", 0) == 0:
        if csv_stats["source_detected"]:
            wl = Counter(r["win_loss"] for r in csv_stats["rows"])
            binary_distribution = {
                "profitable": int(wl.get("WIN", 0)),
                "non_profitable": int(wl.get("LOSS", 0)),
                "unknown": 0,
            }

    ratio = imbalance_ratio(binary_distribution)

    closed = int(db_stats.get("closed_outcomes", 0))
    train_rows = int(csv_stats.get("train_rows", 0))
    valid_rows = int(csv_stats.get("validation_rows", 0))
    sparse_flags = csv_stats.get("sparse_regime_flags", [])

    quality_flag = binary_distribution.get("unknown", 0) > max(10, int(0.1 * max(closed, 1)))

    sufficient = (
        closed >= CLOSED_MIN
        and valid_rows >= VALID_MIN
        and ratio is not None
        and ratio <= IMBALANCE_MAX
        and not any(f["type"] == "validation_regime_sparse" for f in sparse_flags)
    )

    if quality_flag:
        verdict = "REVIEW_DATA_QUALITY"
        recommendation = "Unknown/uncategorized labels are high. Review label mapping quality before retrying calibration."
    elif sufficient:
        verdict = "SUFFICIENT_TO_RETRY_PHASE_2C"
        recommendation = "Data sufficiency gates are met. Retry Phase 2C validation in PAPER_ONLY mode using existing read-only validators."
    else:
        verdict = "COLLECT_MORE_OUTCOMES"
        recommendation = "Continue PAPER_ONLY closed-outcome collection, then rerun this sufficiency report before next calibration retry."

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PHASE_2C_DATA_SUFFICIENCY",
        "paper_only": True,
        "source_tables_or_files_detected": sources,
        "total_outcomes": int(db_stats.get("total_outcomes", 0)),
        "closed_outcomes": closed,
        "train_rows": train_rows,
        "validation_rows": valid_rows,
        "per_regime_counts": csv_stats.get("per_regime_counts", {}),
        "label_distribution": label_distribution,
        "binary_distribution": binary_distribution,
        "imbalance_ratio": None if ratio is None else round(ratio, 6),
        "sparse_regime_flags": sparse_flags,
        "data_sufficiency_verdict": verdict,
        "recommendation": recommendation,
        "gates": {
            "closed_outcomes_min": CLOSED_MIN,
            "validation_rows_min": VALID_MIN,
            "validation_regime_rows_min": REGIME_VALID_MIN,
            "imbalance_ratio_max_preferred": IMBALANCE_MAX,
        },
        "safety": {
            "db_write": False,
            "execution_change": False,
            "broker_api": False,
            "phase_3": False,
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("===== PHASE 2C DATA SUFFICIENCY (READ-ONLY) =====")
    print(f"Verdict             : {verdict}")
    print(f"Closed outcomes     : {closed}")
    print(f"Train rows          : {train_rows}")
    print(f"Validation rows     : {valid_rows}")
    print(f"Imbalance ratio     : {report['imbalance_ratio']}")
    print(f"Sparse flags        : {len(sparse_flags)}")
    print(f"Report              : {OUT_JSON}")


if __name__ == "__main__":
    main()
