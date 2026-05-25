#!/usr/bin/env python3
"""
Build a regime-enriched ML dataset from historical_outcomes.

Safety:
- SQLite read-only connection.
- Does not write to the database.
- Writes generated outputs only to data/ and logs/.
"""

import argparse
import bisect
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path("/home/ubuntu/mamuyy-binance-hunter")
DEFAULT_DB = PROJECT_DIR / "mamuyy_hunter.db"
DEFAULT_OUT_CSV = PROJECT_DIR / "data/ml_dataset_regime_enriched.csv"
DEFAULT_OUT_JSON = PROJECT_DIR / "logs/ml_dataset_build_report.json"


FIELDNAMES = [
    "id",
    "signal_timestamp",
    "close_timestamp",
    "symbol",
    "entry",
    "exit_price",
    "pnl_pct",
    "status",
    "win_loss",
    "sl",
    "tp1",
    "tp2",
    "score",
    "holding_candles",
    "exit_reason",
    "matched_regime",
    "matched_regime_score",
    "regime_match_delta_seconds",
    "match_quality",
]


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def to_epoch(ts: Any) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def match_quality(delta: int | None) -> str:
    if delta is None:
        return "UNMATCHED"
    if delta <= 300:
        return "EXACT_5M"
    if delta <= 900:
        return "NEAR_15M"
    if delta <= 1800:
        return "FAR_30M"
    return "UNMATCHED"


def fetch_rows(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], list[dict[str, Any]]]:
    outcomes = conn.execute(
        """
        SELECT
            id, signal_timestamp, close_timestamp, symbol, entry, exit_price,
            pnl_pct, status, win_loss, sl, tp1, tp2, score, holding_candles, exit_reason
        FROM historical_outcomes
        ORDER BY signal_timestamp
        """
    ).fetchall()

    regime_rows = conn.execute(
        """
        SELECT
            timestamp, regime_name, regime_score, btc_price, btc_change_24h,
            atr_percent, volume_ratio, funding_rate
        FROM regime_logs
        ORDER BY timestamp
        """
    ).fetchall()

    regimes: list[dict[str, Any]] = []
    for row in regime_rows:
        item = dict(row)
        item["epoch"] = to_epoch(row["timestamp"])
        if item["epoch"] is not None:
            regimes.append(item)

    regimes.sort(key=lambda x: x["epoch"])
    return outcomes, regimes


def nearest_regime(signal_epoch: int | None, regimes: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int | None]:
    if signal_epoch is None or not regimes:
        return None, None

    epochs = [r["epoch"] for r in regimes]
    pos = bisect.bisect_left(epochs, signal_epoch)

    candidates = []
    if pos < len(regimes):
        candidates.append(regimes[pos])
    if pos > 0:
        candidates.append(regimes[pos - 1])

    if not candidates:
        return None, None

    best = min(candidates, key=lambda r: abs(signal_epoch - int(r["epoch"])))
    delta = abs(signal_epoch - int(best["epoch"]))

    if match_quality(delta) == "UNMATCHED":
        return None, None

    return best, delta


def build_dataset(db_path: Path, out_csv: Path, out_json: Path) -> dict[str, Any]:
    quality_count = {"EXACT_5M": 0, "NEAR_15M": 0, "FAR_30M": 0, "UNMATCHED": 0}
    rows: list[dict[str, Any]] = []

    with connect_readonly(db_path) as conn:
        outcomes, regimes = fetch_rows(conn)

        for outcome in outcomes:
            signal_epoch = to_epoch(outcome["signal_timestamp"])
            best, delta = nearest_regime(signal_epoch, regimes)
            q = match_quality(delta)

            quality_count[q] += 1

            rows.append(
                {
                    "id": outcome["id"],
                    "signal_timestamp": outcome["signal_timestamp"],
                    "close_timestamp": outcome["close_timestamp"],
                    "symbol": outcome["symbol"],
                    "entry": outcome["entry"],
                    "exit_price": outcome["exit_price"],
                    "pnl_pct": outcome["pnl_pct"],
                    "status": outcome["status"],
                    "win_loss": outcome["win_loss"],
                    "sl": outcome["sl"],
                    "tp1": outcome["tp1"],
                    "tp2": outcome["tp2"],
                    "score": outcome["score"],
                    "holding_candles": outcome["holding_candles"],
                    "exit_reason": outcome["exit_reason"],
                    "matched_regime": best["regime_name"] if best else "",
                    "matched_regime_score": best["regime_score"] if best else "",
                    "regime_match_delta_seconds": delta if delta is not None else "",
                    "match_quality": q,
                }
            )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    matched = total - quality_count["UNMATCHED"]
    matched_pct = round((matched / total) * 100, 4) if total else 0.0

    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_EXPORT",
        "database": str(db_path),
        "total_outcomes": total,
        "total_regime_logs": len(regimes),
        "matched_rows": matched,
        "matched_pct": matched_pct,
        "match_quality_count": quality_count,
        "output_csv": str(out_csv),
        "output_json": str(out_json),
        "verdict": "READ_ONLY_EXPORT_COMPLETE",
    }

    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build regime-enriched ML dataset from Hunter DB.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-csv", type=Path, default=DEFAULT_OUT_CSV)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_dataset(args.db, args.out_csv, args.out_json)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
