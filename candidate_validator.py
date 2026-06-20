import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

QUEUE_PATH = Path("reports/binance_candidate_queue.json")
OUTPUT_PATH = Path("reports/candidate_validation_report.json")
DB_PATH = Path("mamuyy_hunter.db")
HORIZONS_HOURS = [24, 48, 72]


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def nearest_price_after(conn: sqlite3.Connection, symbol: str, target_ts: datetime) -> tuple[str, float] | None:
    row = conn.execute(
        """
        SELECT timestamp, close
        FROM historical_klines
        WHERE symbol = ?
          AND timestamp >= ?
          AND close IS NOT NULL
        ORDER BY timestamp ASC
        LIMIT 1
        """,
        (symbol, target_ts.isoformat()),
    ).fetchone()

    if row is None:
        return None

    return str(row[0]), float(row[1])


def validate_candidate(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("symbol", ""))
    base_price = float(item.get("price") or 0.0)
    signal_ts = parse_ts(str(item.get("timestamp")))
    score = float(item.get("score") or 0.0)

    horizons: dict[str, Any] = {}

    for hours in HORIZONS_HOURS:
        target_ts = signal_ts + timedelta(hours=hours)
        found = nearest_price_after(conn, symbol, target_ts)

        key = f"{hours}h"
        if found is None or base_price <= 0:
            horizons[key] = {
                "status": "PENDING",
                "target_timestamp": target_ts.isoformat(),
                "observed_timestamp": None,
                "observed_price": None,
                "return_pct": None,
                "direction_hit": None,
            }
            continue

        observed_ts, observed_price = found
        return_pct = ((observed_price - base_price) / base_price) * 100
        predicted_up = score >= 85
        actual_up = return_pct > 0

        horizons[key] = {
            "status": "READY",
            "target_timestamp": target_ts.isoformat(),
            "observed_timestamp": observed_ts,
            "observed_price": observed_price,
            "return_pct": round(return_pct, 4),
            "direction_hit": bool(predicted_up == actual_up),
        }

    ready_hits = [
        h["direction_hit"]
        for h in horizons.values()
        if h.get("status") == "READY" and h.get("direction_hit") is not None
    ]

    direction_accuracy = None
    if ready_hits:
        direction_accuracy = round((sum(1 for x in ready_hits if x) / len(ready_hits)) * 100, 2)

    return {
        "rank": item.get("rank"),
        "symbol": symbol,
        "signal_timestamp": item.get("timestamp"),
        "base_price": base_price,
        "score": score,
        "predicted_direction": "UP" if score >= 85 else "DOWN",
        "regime_name": item.get("regime_name"),
        "whale_activity": item.get("whale_activity"),
        "horizons": horizons,
        "direction_accuracy": direction_accuracy,
        "status": "READY" if ready_hits else "PENDING",
    }


def main() -> None:
    print("=== MAMUYY HUNTER PHASE 9B - CANDIDATE VALIDATION ENGINE ===")

    if not QUEUE_PATH.exists():
        raise SystemExit("[FAIL] Candidate queue report missing. Run binance_candidate_queue_v1.py first.")

    data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])

    if not candidates:
        raise SystemExit("[INFO] No candidates found in candidate queue.")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    results = [validate_candidate(conn, item) for item in candidates]
    conn.close()

    ready = [r for r in results if r["status"] == "READY"]
    ready_acc = [r["direction_accuracy"] for r in ready if r["direction_accuracy"] is not None]
    overall_accuracy = round(sum(ready_acc) / len(ready_acc), 2) if ready_acc else None

    report = {
        "phase": "Phase 9B Candidate Validation",
        "mode": "READ_ONLY_ANALYTICS",
        "source_queue": str(QUEUE_PATH),
        "source_db": str(DB_PATH),
        "candidate_count": len(results),
        "ready_count": len(ready),
        "pending_count": len(results) - len(ready),
        "overall_direction_accuracy": overall_accuracy,
        "governance": {
            "paper_only": True,
            "writes_to_database": False,
            "writes_to_broker": False,
            "execution_allowed": False,
        },
        "results": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Candidates: {len(results)} | Ready: {len(ready)} | Pending: {len(results) - len(ready)}")
    print(f"Overall Direction Accuracy: {overall_accuracy}")
    print(f"Report generated: {OUTPUT_PATH}")

    for row in results[:10]:
        print(f"{row['symbol']} | {row['predicted_direction']} | status={row['status']} | accuracy={row['direction_accuracy']}")


if __name__ == "__main__":
    main()
