import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from bridge_tradingview import build_webhook_payload
from database import init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_table(db_path: str, table: str, limit: int = 500) -> pd.DataFrame:
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _read_allocations(path: str = "logs/opportunity_allocation.csv") -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _macro_state(regime: str) -> str:
    upper = str(regime or "").upper()
    if "RISK OFF" in upper or "PANIC" in upper:
        return "RISK_OFF"
    if "HIGH VOLATILITY" in upper:
        return "MACRO_STRESS"
    if "SIDEWAYS" in upper:
        return "CHOPPY"
    return "NORMAL"


def _market_type(symbol: str) -> str:
    text = str(symbol or "").upper()
    if text.endswith("USDT"):
        return "crypto"
    if text in {"XAUUSD", "GOLD"}:
        return "gold"
    return "crypto"


def _latest_signal_candidates(signals: pd.DataFrame, allocations: pd.DataFrame) -> pd.DataFrame:
    if signals.empty or "symbol" not in signals.columns:
        return pd.DataFrame()
    latest = signals.sort_values("id").drop_duplicates("symbol", keep="last").head(25).copy()
    if not allocations.empty and {"symbol", "allocation_tier"}.issubset(allocations.columns):
        latest = latest.merge(
            allocations[["symbol", "allocation_tier", "opportunity_score", "suggested_max_weight_pct"]],
            on="symbol",
            how="left",
        )
    if "allocation_tier" not in latest.columns:
        latest["allocation_tier"] = "WATCH"
    latest["allocation_tier"] = latest["allocation_tier"].fillna("WATCH")
    tradable = latest[~latest["allocation_tier"].astype(str).str.upper().isin(["AVOID"])]
    return tradable if not tradable.empty else latest.head(3)


def _simulated_exit(entry: float, confidence: float, regime: str, tier: str) -> tuple[float, str]:
    if entry <= 0:
        return 0.0, "SKIPPED"
    edge = (confidence - 50.0) / 100.0
    regime_upper = str(regime or "").upper()
    if "RISK OFF" in regime_upper or "PANIC" in regime_upper:
        edge -= 0.03
    elif "SIDEWAYS" in regime_upper:
        edge -= 0.01
    elif "TRENDING BULL" in regime_upper:
        edge += 0.015
    if str(tier).upper() == "PRIORITY":
        edge += 0.01
    elif str(tier).upper() == "WATCH":
        edge -= 0.005
    pnl_pct = max(-2.0, min(3.0, edge * 6.0))
    return entry * (1 + pnl_pct / 100.0), "CLOSED"


def _insert_trade(db_path: str, trade: Dict[str, Any]) -> bool:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        fields = [
            "timestamp",
            "source_signal_timestamp",
            "symbol",
            "market_type",
            "side",
            "entry_price",
            "exit_price",
            "pnl",
            "confidence",
            "regime",
            "macro_state",
            "allocation_tier",
            "status",
            "payload_json",
        ]
        placeholders = ", ".join(["?"] * len(fields))
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO internal_paper_trades ({', '.join(fields)}) VALUES ({placeholders})",
            [trade.get(field) for field in fields],
        )
        connection.commit()
        return cursor.rowcount > 0


def _paper_metrics(trades: pd.DataFrame) -> Dict[str, float]:
    if trades.empty or "pnl" not in trades.columns:
        return {"trade_count": 0, "winrate": 0.0, "total_pnl": 0.0, "max_drawdown": 0.0}
    pnl = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
    return {
        "trade_count": int(len(pnl)),
        "winrate": round(float((pnl > 0).mean() * 100), 2) if len(pnl) else 0.0,
        "total_pnl": round(float(pnl.sum()), 4),
        "max_drawdown": round(float(drawdown.min()), 4) if not drawdown.empty else 0.0,
    }


def run_internal_paper_engine(
    db_path: str = "mamuyy_hunter.db",
    allocation_path: str = "logs/opportunity_allocation.csv",
    max_new_trades: int = 5,
) -> Dict[str, Any]:
    signals = _read_table(db_path, "signals", limit=500)
    allocations = _read_allocations(allocation_path)
    candidates = _latest_signal_candidates(signals, allocations).head(max_new_trades)
    inserted = 0
    generated: List[Dict[str, Any]] = []
    for _, signal in candidates.iterrows():
        symbol = str(signal.get("symbol") or "")
        if not symbol:
            continue
        price = _num(signal.get("price"))
        confidence = _num(signal.get("model_confidence") or signal.get("adaptive_confidence_score") or signal.get("shadow_score") or signal.get("score"))
        regime = str(signal.get("regime_name") or "UNKNOWN")
        allocation_tier = str(signal.get("allocation_tier") or "WATCH").upper()
        exit_price, status = _simulated_exit(price, confidence, regime, allocation_tier)
        pnl = ((exit_price - price) / price * 100.0) if price > 0 and exit_price > 0 else 0.0
        payload = build_webhook_payload(
            symbol=symbol,
            side="LONG",
            price=price,
            confidence=confidence,
            regime=regime,
            macro_state=_macro_state(regime),
            allocation_tier=allocation_tier,
            market=_market_type(symbol),
        )
        trade = {
            "timestamp": _now(),
            "source_signal_timestamp": signal.get("timestamp") or _now(),
            "symbol": symbol,
            "market_type": _market_type(symbol),
            "side": "LONG",
            "entry_price": round(price, 8),
            "exit_price": round(exit_price, 8),
            "pnl": round(pnl, 6),
            "confidence": round(confidence, 4),
            "regime": regime,
            "macro_state": _macro_state(regime),
            "allocation_tier": allocation_tier,
            "status": status,
            "payload_json": json.dumps(payload, default=str),
        }
        if _insert_trade(db_path, trade):
            inserted += 1
            generated.append(trade)

    trades = _read_table(db_path, "internal_paper_trades", limit=1000)
    return {
        "ok": True,
        "paper_mode_only": True,
        "generated": generated,
        "inserted": inserted,
        "metrics": _paper_metrics(trades),
    }


def format_paper_engine_result(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "INTERNAL PAPER ENGINE",
            f"OK: {result.get('ok')}",
            f"Paper Mode Only: {result.get('paper_mode_only')}",
            f"Inserted Trades: {result.get('inserted', 0)}",
            f"Metrics: {result.get('metrics', {})}",
        ]
    )
