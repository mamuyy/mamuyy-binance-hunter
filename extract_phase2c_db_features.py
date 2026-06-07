#!/usr/bin/env python3
"""Read-only Phase 2C VTM DB feature extraction.

Primary source:
- data/ml_calibration_matched_20260520.csv

DB use:
- read-only SQLite schema inspection and as-of joins only
- historical_klines
- historical_funding
- historical_open_interest when available

Outputs:
- data/ml_calibration_with_vtm_db_features.csv
- logs/phase2c_vtm_db_extraction_report.json
"""

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CALIBRATION_CSV = ROOT / "data/ml_calibration_matched_20260520.csv"
DB_PATH = ROOT / "mamuyy_hunter.db"
OUT_CSV = ROOT / "data/ml_calibration_with_vtm_db_features.csv"
OUT_JSON = ROOT / "logs/phase2c_vtm_db_extraction_report.json"

REQUIRED_TABLES = ("historical_klines", "historical_funding")
OPTIONAL_TABLES = ("historical_open_interest",)
KLINE_INTERVAL_PRIORITY = ("1h", "30m", "15m", "5m", "1m")
BASE_FEATURES = [
    "db_volume",
    "db_volume_ratio_20",
    "db_ema20",
    "db_ema200",
    "db_ema_distance_pct",
    "db_btc_above_ema200",
    "db_atr14",
    "db_atr_percent",
    "db_funding_rate",
]
OI_FEATURES = [
    "db_open_interest",
    "db_open_interest_delta_1",
    "db_open_interest_change_pct_1",
]


def to_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_iso(dt):
    return dt.isoformat() if dt else ""


def open_db_read_only(path):
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def table_names(con):
    rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def table_columns(con, table):
    return [row["name"] for row in con.execute(f"PRAGMA table_info({table})")]


def inspect_schemas(con):
    tables = table_names(con)
    schemas = {}
    for table in REQUIRED_TABLES + OPTIONAL_TABLES:
        schemas[table] = {
            "present": table in tables,
            "columns": table_columns(con, table) if table in tables else [],
        }
    return schemas


def require_schema(schemas):
    missing_tables = [table for table in REQUIRED_TABLES if not schemas[table]["present"]]
    if missing_tables:
        raise RuntimeError(f"Missing required DB tables: {missing_tables}")

    required_columns = {
        "historical_klines": {"timestamp", "symbol", "close", "high", "low", "volume"},
        "historical_funding": {"timestamp", "symbol", "funding_rate"},
    }
    missing_columns = {}
    for table, cols in required_columns.items():
        present = set(schemas[table]["columns"])
        missing = sorted(cols - present)
        if missing:
            missing_columns[table] = missing
    if missing_columns:
        raise RuntimeError(f"Missing required DB columns: {missing_columns}")


def load_calibration_rows(path):
    if not path.exists():
        raise FileNotFoundError(f"Calibration CSV not found: {path}")
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, reader.fieldnames or []


def pick_kline_interval(con, schemas):
    if "interval" not in schemas["historical_klines"]["columns"]:
        return None
    rows = con.execute(
        "SELECT interval, COUNT(*) AS n FROM historical_klines GROUP BY interval"
    ).fetchall()
    counts = {row["interval"]: row["n"] for row in rows}
    for interval in KLINE_INTERVAL_PRIORITY:
        if counts.get(interval):
            return interval
    return max(counts, key=counts.get) if counts else None


def fetch_klines_asof(con, symbol, signal_dt, interval, limit=240):
    interval_sql = "AND interval = ?" if interval else ""
    params = [symbol, as_iso(signal_dt)]
    if interval:
        params.append(interval)
    params.append(limit)
    rows = con.execute(
        f"""
        SELECT timestamp, open, high, low, close, volume
        FROM historical_klines
        WHERE symbol = ?
          AND timestamp <= ?
          {interval_sql}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def fetch_funding_asof(con, symbol, signal_dt):
    row = con.execute(
        """
        SELECT timestamp, funding_rate
        FROM historical_funding
        WHERE symbol = ?
          AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, as_iso(signal_dt)),
    ).fetchone()
    return dict(row) if row else None


def fetch_open_interest_asof(con, symbol, signal_dt):
    rows = con.execute(
        """
        SELECT timestamp, open_interest
        FROM historical_open_interest
        WHERE symbol = ?
          AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 2
        """,
        (symbol, as_iso(signal_dt)),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def ema(values, span):
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    current = values[0]
    for value in values[1:]:
        current = (value * alpha) + (current * (1.0 - alpha))
    return current


def true_ranges(klines):
    ranges = []
    prev_close = None
    for row in klines:
        high = to_float(row.get("high"))
        low = to_float(row.get("low"))
        close = to_float(row.get("close"))
        if prev_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return ranges


def mean(values):
    return sum(values) / len(values) if values else 0.0


def kline_feature_values(klines):
    if not klines:
        return {
            "db_volume": 0.0,
            "db_volume_ratio_20": 0.0,
            "db_ema20": 0.0,
            "db_ema200": 0.0,
            "db_ema_distance_pct": 0.0,
            "db_btc_above_ema200": 0,
            "db_atr14": 0.0,
            "db_atr_percent": 0.0,
        }

    closes = [to_float(row.get("close")) for row in klines]
    volumes = [to_float(row.get("volume")) for row in klines]
    latest_close = closes[-1]
    latest_volume = volumes[-1]
    avg_volume_20 = mean(volumes[-20:])
    ema20 = ema(closes[-20:], 20)
    ema200 = ema(closes[-200:], 200)
    atr14 = mean(true_ranges(klines)[-14:])

    return {
        "db_volume": latest_volume,
        "db_volume_ratio_20": latest_volume / avg_volume_20 if avg_volume_20 else 0.0,
        "db_ema20": ema20,
        "db_ema200": ema200,
        "db_ema_distance_pct": ((ema20 - ema200) / ema200) if ema200 else 0.0,
        "db_btc_above_ema200": int(latest_close > ema200) if ema200 else 0,
        "db_atr14": atr14,
        "db_atr_percent": atr14 / latest_close if latest_close else 0.0,
    }


def open_interest_features(rows):
    if not rows:
        return {
            "db_open_interest": 0.0,
            "db_open_interest_delta_1": 0.0,
            "db_open_interest_change_pct_1": 0.0,
        }
    latest = to_float(rows[-1].get("open_interest"))
    previous = to_float(rows[-2].get("open_interest")) if len(rows) > 1 else latest
    delta = latest - previous
    return {
        "db_open_interest": latest,
        "db_open_interest_delta_1": delta,
        "db_open_interest_change_pct_1": delta / previous if previous else 0.0,
    }


def label_value(row):
    win_loss = (row.get("win_loss") or "").upper()
    status = (row.get("status") or "").upper()
    if win_loss == "WIN" or status == "TP1 HIT":
        return 1
    if win_loss == "LOSS":
        return 0
    return None


def auc_score(labels, scores):
    pairs = [(s, y) for y, s in zip(labels, scores) if y in (0, 1)]
    positives = sum(1 for _, y in pairs if y == 1)
    negatives = sum(1 for _, y in pairs if y == 0)
    if positives == 0 or negatives == 0:
        return None

    sorted_pairs = sorted(pairs, key=lambda x: x[0])
    rank_sum_pos = 0.0
    rank = 1
    i = 0
    while i < len(sorted_pairs):
        j = i + 1
        while j < len(sorted_pairs) and sorted_pairs[j][0] == sorted_pairs[i][0]:
            j += 1
        avg_rank = (rank + rank + (j - i) - 1) / 2.0
        for k in range(i, j):
            if sorted_pairs[k][1] == 1:
                rank_sum_pos += avg_rank
        rank += j - i
        i = j

    auc = (rank_sum_pos - (positives * (positives + 1) / 2.0)) / (positives * negatives)
    return round(auc, 6)


def extract_features(con, calibration_rows, schemas, kline_interval):
    use_oi = (
        schemas["historical_open_interest"]["present"]
        and {"timestamp", "symbol", "open_interest"}.issubset(
            set(schemas["historical_open_interest"]["columns"])
        )
    )
    out_rows = []
    join_counts = {
        "rows": 0,
        "valid_signal_timestamp": 0,
        "kline_asof_matches": 0,
        "funding_asof_matches": 0,
        "open_interest_asof_matches": 0,
    }

    for row in calibration_rows:
        join_counts["rows"] += 1
        signal_dt = to_dt(row.get("signal_timestamp"))
        symbol = row.get("symbol")
        features = {name: 0.0 for name in BASE_FEATURES}
        if use_oi:
            features.update({name: 0.0 for name in OI_FEATURES})

        if signal_dt and symbol:
            join_counts["valid_signal_timestamp"] += 1
            klines = fetch_klines_asof(con, symbol, signal_dt, kline_interval)
            if klines:
                join_counts["kline_asof_matches"] += 1
            features.update(kline_feature_values(klines))

            funding = fetch_funding_asof(con, symbol, signal_dt)
            if funding:
                join_counts["funding_asof_matches"] += 1
                features["db_funding_rate"] = to_float(funding.get("funding_rate"))

            if use_oi:
                oi_rows = fetch_open_interest_asof(con, symbol, signal_dt)
                if oi_rows:
                    join_counts["open_interest_asof_matches"] += 1
                features.update(open_interest_features(oi_rows))

        out_rows.append({**row, **features})

    return out_rows, join_counts, use_oi


def feature_aucs(rows, feature_names):
    labels = [label_value(row) for row in rows]
    aucs = {}
    for feature in feature_names:
        aucs[feature] = auc_score(labels, [to_float(row.get(feature)) for row in rows])
    return aucs


def write_outputs(rows, original_fieldnames, feature_names, report):
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(original_fieldnames)
    for feature in feature_names:
        if feature not in fieldnames:
            fieldnames.append(feature)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main():
    calibration_rows, fieldnames = load_calibration_rows(CALIBRATION_CSV)
    with open_db_read_only(DB_PATH) as con:
        schemas = inspect_schemas(con)
        require_schema(schemas)
        kline_interval = pick_kline_interval(con, schemas)
        rows, join_counts, use_oi = extract_features(
            con, calibration_rows, schemas, kline_interval
        )

    feature_names = BASE_FEATURES + (OI_FEATURES if use_oi else [])
    labels = [label_value(row) for row in rows]
    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PHASE2C_VTM_DB_FEATURE_EXTRACTION",
        "inputs": {
            "calibration_csv": str(CALIBRATION_CSV),
            "db_path": str(DB_PATH),
            "primary_source": "calibration_csv",
            "historical_outcomes_used": False,
            "regime_logs_used": False,
        },
        "outputs": {
            "csv": str(OUT_CSV),
            "json": str(OUT_JSON),
        },
        "sqlite_read_only": {
            "mode_ro": True,
            "pragma_query_only_on": True,
        },
        "schemas": schemas,
        "asof_join": {
            "rule": "db.timestamp <= calibration.signal_timestamp",
            "future_data_allowed": False,
            "kline_interval": kline_interval,
            **join_counts,
        },
        "features_appended": feature_names,
        "optional_open_interest_features_enabled": use_oi,
        "label_definition": {
            "positive": ["WIN", "TP1 HIT"],
            "negative": ["LOSS"],
            "positive_rows": sum(1 for label in labels if label == 1),
            "negative_rows": sum(1 for label in labels if label == 0),
            "unlabeled_rows": sum(1 for label in labels if label not in (0, 1)),
        },
        "auc_vs_win_loss": feature_aucs(rows, feature_names),
        "forbidden_imports_or_calls": [
            "execution_engine",
            "broker",
            "telegram",
            "flow_engine",
            "orchestrator",
        ],
        "safety": {
            "db_write": False,
            "execution_change": False,
            "production_scoring_change": False,
        },
    }
    write_outputs(rows, fieldnames, feature_names, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
