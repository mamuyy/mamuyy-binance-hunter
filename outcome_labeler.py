from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from database import get_connection, init_db


@dataclass
class LabelingResult:
    scanned_signals: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    skipped_no_candles: int = 0
    wins: int = 0
    losses: int = 0
    open_or_flat: int = 0

    def as_dict(self) -> Dict[str, Any]:
        total_closed = self.wins + self.losses
        winrate = (self.wins / total_closed * 100) if total_closed else 0.0
        return {
            "scanned_signals": self.scanned_signals,
            "inserted": self.inserted,
            "skipped_duplicates": self.skipped_duplicates,
            "skipped_no_candles": self.skipped_no_candles,
            "wins": self.wins,
            "losses": self.losses,
            "open_or_flat": self.open_or_flat,
            "winrate": round(winrate, 2),
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff_iso(days: int) -> str:
    return (_utc_now() - timedelta(days=max(days, 1))).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _label_path(
    entry: float,
    future_candles: List[Dict[str, Any]],
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: float,
    holding_candles: int,
) -> Dict[str, Any]:
    sl_price = entry * (1 - sl_pct / 100)
    tp1_price = entry * (1 + tp1_pct / 100)
    tp2_price = entry * (1 + tp2_pct / 100)

    for index, candle in enumerate(future_candles[:holding_candles], start=1):
        low = _safe_float(candle.get("low"))
        high = _safe_float(candle.get("high"))
        timestamp = candle.get("timestamp")

        if low <= sl_price:
            return {
                "close_timestamp": timestamp,
                "exit_price": sl_price,
                "pnl_pct": -abs(sl_pct),
                "status": "LOSS",
                "win_loss": "LOSS",
                "holding_candles": index,
                "exit_reason": "SL",
            }
        if high >= tp2_price:
            return {
                "close_timestamp": timestamp,
                "exit_price": tp2_price,
                "pnl_pct": abs(tp2_pct),
                "status": "WIN",
                "win_loss": "WIN",
                "holding_candles": index,
                "exit_reason": "TP2",
            }
        if high >= tp1_price:
            return {
                "close_timestamp": timestamp,
                "exit_price": tp1_price,
                "pnl_pct": abs(tp1_pct),
                "status": "TP1 HIT",
                "win_loss": "WIN",
                "holding_candles": index,
                "exit_reason": "TP1",
            }

    if not future_candles:
        return {
            "close_timestamp": "",
            "exit_price": entry,
            "pnl_pct": 0.0,
            "status": "OPEN",
            "win_loss": "FLAT",
            "holding_candles": 0,
            "exit_reason": "NO_FUTURE_CANDLES",
        }

    last = future_candles[min(len(future_candles), holding_candles) - 1]
    exit_price = _safe_float(last.get("close"), entry)
    pnl_pct = ((exit_price - entry) / entry * 100) if entry else 0.0
    win_loss = "WIN" if pnl_pct > 0 else "LOSS" if pnl_pct < 0 else "FLAT"
    return {
        "close_timestamp": last.get("timestamp"),
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "status": win_loss,
        "win_loss": win_loss,
        "holding_candles": min(len(future_candles), holding_candles),
        "exit_reason": "HOLDING_PERIOD",
    }


def label_historical_outcomes(
    database_url: str,
    days: int = 7,
    holding_candles: int = 20,
    sl_pct: float = 2.0,
    tp1_pct: float = 3.0,
    tp2_pct: float = 5.0,
) -> Dict[str, Any]:
    init_db(database_url)
    cutoff = _cutoff_iso(days)
    result = LabelingResult()

    print(
        "Label historical outcomes "
        f"days={days} holding_candles={holding_candles} "
        f"SL={sl_pct}% TP1={tp1_pct}% TP2={tp2_pct}%"
    )

    with get_connection(database_url) as connection:
        signals = connection.execute(
            """
            SELECT s.timestamp, s.symbol, s.score, h.close AS entry
            FROM signals s
            JOIN historical_klines h
              ON h.symbol = s.symbol
             AND h.timestamp = s.timestamp
            WHERE s.timestamp >= ?
            ORDER BY s.timestamp ASC
            """,
            (cutoff,),
        ).fetchall()
        result.scanned_signals = len(signals)

        for index, signal in enumerate(signals, start=1):
            signal_timestamp = signal["timestamp"]
            symbol = signal["symbol"]
            exists = connection.execute(
                "SELECT 1 FROM historical_outcomes WHERE symbol = ? AND signal_timestamp = ? LIMIT 1",
                (symbol, signal_timestamp),
            ).fetchone()
            if exists:
                result.skipped_duplicates += 1
                continue

            future_candles = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT timestamp, high, low, close
                    FROM historical_klines
                    WHERE symbol = ?
                      AND timestamp > ?
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (symbol, signal_timestamp, max(holding_candles, 1)),
                ).fetchall()
            ]
            if not future_candles:
                result.skipped_no_candles += 1

            entry = _safe_float(signal["entry"])
            outcome = _label_path(entry, future_candles, sl_pct, tp1_pct, tp2_pct, max(holding_candles, 1))
            row = {
                "signal_timestamp": signal_timestamp,
                "close_timestamp": outcome["close_timestamp"],
                "symbol": symbol,
                "entry": entry,
                "exit_price": outcome["exit_price"],
                "pnl_pct": outcome["pnl_pct"],
                "status": outcome["status"],
                "win_loss": outcome["win_loss"],
                "sl": entry * (1 - sl_pct / 100),
                "tp1": entry * (1 + tp1_pct / 100),
                "tp2": entry * (1 + tp2_pct / 100),
                "score": signal["score"],
                "holding_candles": outcome["holding_candles"],
                "exit_reason": outcome["exit_reason"],
            }
            columns = list(row.keys())
            before = connection.total_changes
            connection.execute(
                f"INSERT OR IGNORE INTO historical_outcomes ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
                [row[column] for column in columns],
            )
            if connection.total_changes > before:
                result.inserted += 1
                if row["win_loss"] == "WIN":
                    result.wins += 1
                elif row["win_loss"] == "LOSS":
                    result.losses += 1
                else:
                    result.open_or_flat += 1

            if index % 500 == 0:
                print(f"  labeled {index}/{len(signals)} signals...")

        connection.commit()

    summary = result.as_dict()
    print(f"Outcome labeling selesai: {summary}")
    return summary
