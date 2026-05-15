import csv
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List


PAPER_TRADE_FIELDS = [
    "timestamp",
    "symbol",
    "entry",
    "current_price",
    "pnl_percent",
    "status",
    "sl",
    "tp1",
    "tp2",
    "score",
    "regime_name",
    "regime_score",
]

ACTIVE_STATUSES = {"OPEN", "TP1 HIT"}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_paper_trades_file(path: str = "paper_trades.csv") -> None:
    if os.path.exists(path):
        return

    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=PAPER_TRADE_FIELDS)
        writer.writeheader()


def load_paper_trades(path: str = "paper_trades.csv") -> List[Dict[str, Any]]:
    ensure_paper_trades_file(path)

    with open(path, "r", newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        return list(reader)


def save_paper_trades(
    trades: Iterable[Dict[str, Any]],
    path: str = "paper_trades.csv",
) -> None:
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=PAPER_TRADE_FIELDS)
        writer.writeheader()
        for trade in trades:
            writer.writerow({field: trade.get(field, "") for field in PAPER_TRADE_FIELDS})


def calculate_pnl_percent(entry: float, current_price: float) -> float:
    if entry <= 0:
        return 0.0
    return ((current_price - entry) / entry) * 100


def resolve_trade_status(
    current_price: float,
    sl: float,
    tp1: float,
    tp2: float,
    previous_status: str = "OPEN",
) -> str:
    if current_price <= sl:
        return "LOSS"
    if current_price >= tp2:
        return "WIN"
    if current_price >= tp1:
        return "TP1 HIT"
    return previous_status if previous_status in ACTIVE_STATUSES else "OPEN"


def has_active_trade(symbol: str, trades: Iterable[Dict[str, Any]]) -> bool:
    return any(
        trade.get("symbol") == symbol and trade.get("status") in ACTIVE_STATUSES
        for trade in trades
    )


def create_paper_trade(signal: Dict[str, Any]) -> Dict[str, Any]:
    entry = _to_float(signal.get("price"))
    current_price = entry
    sl = entry * 0.98
    tp1 = entry * 1.03
    tp2 = entry * 1.05

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": signal.get("symbol", ""),
        "entry": entry,
        "current_price": current_price,
        "pnl_percent": 0.0,
        "status": "OPEN",
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": signal.get("score", 0),
        "regime_name": signal.get("regime_name", "UNKNOWN"),
        "regime_score": signal.get("regime_score", 0),
    }


def open_paper_trades(
    signals: Iterable[Dict[str, Any]],
    path: str = "paper_trades.csv",
) -> List[Dict[str, Any]]:
    trades = load_paper_trades(path)
    created = []

    for signal in signals:
        symbol = signal.get("symbol")
        if not symbol or has_active_trade(symbol, trades):
            continue

        trade = create_paper_trade(signal)
        trades.append(trade)
        created.append(trade)

    if created:
        save_paper_trades(trades, path)

    return created


def update_paper_trades(
    prices: Dict[str, float],
    path: str = "paper_trades.csv",
) -> List[Dict[str, Any]]:
    trades = load_paper_trades(path)
    updated_trades = []

    for trade in trades:
        if trade.get("status") not in ACTIVE_STATUSES:
            updated_trades.append(trade)
            continue

        symbol = trade.get("symbol", "")
        if symbol not in prices:
            updated_trades.append(trade)
            continue

        entry = _to_float(trade.get("entry"))
        current_price = _to_float(prices[symbol])
        sl = _to_float(trade.get("sl"))
        tp1 = _to_float(trade.get("tp1"))
        tp2 = _to_float(trade.get("tp2"))
        pnl_percent = calculate_pnl_percent(entry, current_price)

        trade["current_price"] = current_price
        trade["pnl_percent"] = pnl_percent
        trade["status"] = resolve_trade_status(
            current_price=current_price,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            previous_status=trade.get("status", "OPEN"),
        )
        updated_trades.append(trade)

    save_paper_trades(updated_trades, path)
    return updated_trades


def build_paper_summary(path: str = "paper_trades.csv") -> Dict[str, Any]:
    trades = load_paper_trades(path)
    total_trade = len(trades)
    wins = [trade for trade in trades if trade.get("status") == "WIN"]
    losses = [trade for trade in trades if trade.get("status") == "LOSS"]

    pnl_values = [_to_float(trade.get("pnl_percent")) for trade in trades]
    winrate = (len(wins) / total_trade * 100) if total_trade else 0.0
    average_pnl = (sum(pnl_values) / total_trade) if total_trade else 0.0

    best_trade = max(trades, key=lambda trade: _to_float(trade.get("pnl_percent")), default=None)
    worst_trade = min(trades, key=lambda trade: _to_float(trade.get("pnl_percent")), default=None)

    return {
        "total_trade": total_trade,
        "win": len(wins),
        "loss": len(losses),
        "winrate": winrate,
        "average_pnl": average_pnl,
        "best_coin": best_trade.get("symbol", "-") if best_trade else "-",
        "worst_coin": worst_trade.get("symbol", "-") if worst_trade else "-",
    }


def should_send_daily_summary(state_path: str = ".paper_summary_state") -> bool:
    today = date.today().isoformat()
    if not os.path.exists(state_path):
        return True

    with open(state_path, "r", encoding="utf-8") as state_file:
        return state_file.read().strip() != today


def mark_daily_summary_sent(state_path: str = ".paper_summary_state") -> None:
    with open(state_path, "w", encoding="utf-8") as state_file:
        state_file.write(date.today().isoformat())
