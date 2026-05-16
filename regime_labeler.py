import sqlite3
from collections import Counter
from typing import Any, Dict, List

from database import init_db


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def derive_regime_label(row: Dict[str, Any]) -> tuple[str, int]:
    volume_spike = _safe_float(row.get("volume_spike"))
    funding_zscore = _safe_float(row.get("funding_zscore"))
    oi_expansion_rate = _safe_float(row.get("oi_expansion_rate"))
    taker_delta = _safe_float(row.get("taker_delta"))
    pressure_score = _safe_float(row.get("pressure_score"), 50.0)
    squeeze_probability = _safe_float(row.get("squeeze_probability"))
    breakout = _is_true(row.get("breakout"))
    liquidity_sweep = _is_true(row.get("liquidity_sweep"))
    squeeze_risk = str(row.get("squeeze_risk") or "").upper()
    whale_activity = str(row.get("whale_activity") or "").upper()

    if breakout and oi_expansion_rate > 0 and volume_spike >= 2:
        return "BREAKOUT EXPANSION", 78
    if liquidity_sweep:
        return "MEAN REVERSION", 74
    if squeeze_risk in {"HIGH", "HIGH SHORT SQUEEZE RISK", "HIGH LONG SQUEEZE RISK"}:
        return "HIGH VOLATILITY", 76
    if squeeze_probability >= 60 or abs(funding_zscore) >= 2.0 or volume_spike >= 3:
        return "HIGH VOLATILITY", 70
    if taker_delta >= 0.15 and oi_expansion_rate > 0 and pressure_score >= 55:
        return "TRENDING BULL", 72
    if taker_delta <= -0.15 and oi_expansion_rate > 0 and pressure_score <= 45:
        return "TRENDING BEAR", 72
    if "ACCUMULATION" in whale_activity and taker_delta > 0:
        return "TRENDING BULL", 68
    if "DISTRIBUTION" in whale_activity and taker_delta < 0:
        return "TRENDING BEAR", 68
    if abs(taker_delta) < 0.12 and volume_spike < 2 and abs(oi_expansion_rate) < 2:
        return "SIDEWAYS / CHOPPY", 66
    return "HISTORICAL_DERIVED", 50


def _distribution(connection: sqlite3.Connection) -> Dict[str, int]:
    rows = connection.execute(
        """
        SELECT COALESCE(NULLIF(s.regime_name, ''), 'UNKNOWN') AS regime_name, COUNT(*) AS total
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        GROUP BY COALESCE(NULLIF(s.regime_name, ''), 'UNKNOWN')
        ORDER BY total DESC
        """
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _unknown_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM historical_outcomes o
            LEFT JOIN signals s
              ON s.symbol = o.symbol
             AND s.timestamp = o.signal_timestamp
            WHERE s.id IS NULL
               OR s.regime_name IS NULL
               OR TRIM(s.regime_name) = ''
               OR UPPER(TRIM(s.regime_name)) IN ('UNKNOWN', 'HISTORICAL_BACKTEST')
            """
        ).fetchone()[0]
    )


def fix_historical_regime_labels(database_path: str = "mamuyy_hunter.db") -> Dict[str, Any]:
    init_db(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        unknown_before = _unknown_count(connection)
        rows = connection.execute(
            """
            SELECT
                s.id AS signal_id,
                o.signal_timestamp,
                o.symbol,
                s.regime_name,
                s.volume_spike,
                s.breakout,
                s.liquidity_sweep,
                f.funding_zscore,
                f.oi_expansion_rate,
                f.taker_delta,
                f.pressure_score,
                f.squeeze_probability,
                f.squeeze_risk,
                f.whale_activity
            FROM historical_outcomes o
            LEFT JOIN signals s
              ON s.symbol = o.symbol
             AND s.timestamp = o.signal_timestamp
            LEFT JOIN flow_logs f
              ON f.symbol = o.symbol
             AND f.timestamp = o.signal_timestamp
            WHERE s.id IS NOT NULL
              AND (
                    s.regime_name IS NULL
                 OR TRIM(s.regime_name) = ''
                 OR UPPER(TRIM(s.regime_name)) IN ('UNKNOWN', 'HISTORICAL_BACKTEST')
              )
            ORDER BY o.signal_timestamp ASC
            """
        ).fetchall()

        fixed = 0
        labels: List[str] = []
        for row in rows:
            label, score = derive_regime_label(dict(row))
            connection.execute(
                "UPDATE signals SET regime_name = ?, regime_score = ? WHERE id = ?",
                (label, score, row["signal_id"]),
            )
            fixed += 1
            labels.append(label)
        connection.commit()

        unknown_after = _unknown_count(connection)
        distribution = _distribution(connection)

    result = {
        "unknown_before": unknown_before,
        "fixed_labels_count": fixed,
        "unknown_after": unknown_after,
        "derived_distribution": dict(Counter(labels).most_common()),
        "top_regime_distribution": distribution,
    }
    print("REGIME LABEL FIX")
    print(f"UNKNOWN before: {unknown_before}")
    print(f"Fixed labels: {fixed}")
    print(f"UNKNOWN after: {unknown_after}")
    print(f"Top regime distribution: {distribution}")
    return result
