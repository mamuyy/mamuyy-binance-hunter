import json
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("mamuyy_hunter.db")
TMP_DIR = Path("tmp")
REPORTS_DIR = Path("reports")
SNAPSHOT_PATH = TMP_DIR / "mamuyy_hunter_candidate_queue_snapshot.db"
OUTPUT_PATH = REPORTS_DIR / "binance_candidate_queue.json"

EXCLUDED_SYMBOLS = {
    "XAUUSDT", "XAGUSDT",
    "SOXLUSDT", "MRVLUSDT", "SNDKUSDT", "MUUSDT", "INTCUSDT",
}

MIN_SCORE = 85
MAX_CANDIDATES = 20
MAX_SIGNAL_AGE_HOURS = 72


def snapshot_db() -> None:
    TMP_DIR.mkdir(exist_ok=True)
    shutil.copyfile(DB_PATH, SNAPSHOT_PATH)


def fetch_candidates():
    conn = sqlite3.connect(SNAPSHOT_PATH)
    conn.row_factory = sqlite3.Row

    query = """
    WITH latest AS (
        SELECT MAX(id) AS latest_id
        FROM signals
        WHERE score >= ?
          AND squeeze_risk = 'LOW'
          AND (funding_warning IS NULL OR funding_warning = '')
          AND timestamp >= ?
        GROUP BY symbol
    )
    SELECT s.*
    FROM signals s
    JOIN latest l ON s.id = l.latest_id
    ORDER BY s.score DESC, s.pressure_score DESC, s.id DESC
    LIMIT ?;
    """

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_SIGNAL_AGE_HOURS)).isoformat()
    rows = conn.execute(query, (MIN_SCORE, cutoff, MAX_CANDIDATES * 2)).fetchall()
    conn.close()

    candidates = []
    for row in rows:
        symbol = row["symbol"]
        if symbol in EXCLUDED_SYMBOLS:
            continue

        candidates.append({
            "rank": len(candidates) + 1,
            "symbol": symbol,
            "timestamp": row["timestamp"],
            "score": row["score"],
            "price": row["price"],
            "regime_name": row["regime_name"],
            "pressure_score": row["pressure_score"],
            "oi_expansion_rate": row["oi_expansion_rate"],
            "taker_delta": row["taker_delta"],
            "squeeze_probability": row["squeeze_probability"],
            "whale_activity": row["whale_activity"],
            "squeeze_risk": row["squeeze_risk"],
            "funding_warning": row["funding_warning"],
            "status": "PROPOSAL_ONLY",
            "execution_allowed": False,
        })

        if len(candidates) >= MAX_CANDIDATES:
            break

    return candidates


def main() -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    snapshot_db()
    candidates = fetch_candidates()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "Phase 9A Candidate Queue V1",
        "mode": "READ_ONLY_PROPOSAL",
        "source_db": str(DB_PATH),
        "snapshot_db": str(SNAPSHOT_PATH),
        "rules": {
            "min_score": MIN_SCORE,
            "squeeze_risk": "LOW",
            "funding_warning": "empty_or_null",
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
            "max_candidates": MAX_CANDIDATES,
            "max_signal_age_hours": MAX_SIGNAL_AGE_HOURS,
        },
        "safety": {
            "real_binance_enabled": False,
            "testnet_order_enabled": False,
            "auto_execution_enabled": False,
            "manual_review_required": True,
            "writes_to_database": False,
            "writes_to_broker": False,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Candidate Queue generated: {OUTPUT_PATH}")
    print(f"Candidates: {len(candidates)}")
    for item in candidates[:10]:
        print(
            f"#{item['rank']} {item['symbol']} | "
            f"Score {item['score']} | "
            f"Pressure {item['pressure_score']} | "
            f"Status {item['status']}"
        )


if __name__ == "__main__":
    main()
