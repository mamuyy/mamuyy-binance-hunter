import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from candidate_validator import validate_candidate


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE historical_klines (
            symbol TEXT,
            timestamp TEXT,
            close REAL
        )
        """
    )
    return conn


def test_validate_candidate_preserves_regime_name_and_whale_activity_from_queue_item():
    conn = make_conn()
    item = {
        "rank": 1,
        "symbol": "BTCUSDT",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "price": 100.0,
        "score": 96.5,
        "regime_name": "BULL_EXPANSION",
        "whale_activity": "HIGH",
    }

    result = validate_candidate(conn, item)

    assert result["regime_name"] == "BULL_EXPANSION"
    assert result["whale_activity"] == "HIGH"
