import json
import os
from datetime import datetime, timezone
from typing import Any, Dict


SUPPORTED_MARKETS = {"crypto", "forex", "stocks", "etf", "gold"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market_type(symbol: str, requested: str = "crypto") -> str:
    market = str(requested or "crypto").lower()
    if market in SUPPORTED_MARKETS:
        return market
    text = str(symbol or "").upper()
    if text.endswith("USDT"):
        return "crypto"
    if text in {"XAUUSD", "GOLD"}:
        return "gold"
    return "crypto"


def build_webhook_payload(
    symbol: str,
    side: str = "LONG",
    price: float = 0.0,
    confidence: float = 0.0,
    regime: str = "UNKNOWN",
    macro_state: str = "UNKNOWN",
    allocation_tier: str = "WATCH",
    market: str = "crypto",
    source: str = "mamuyy-binance-hunter",
) -> Dict[str, Any]:
    return {
        "source": source,
        "mode": "PAPER_ONLY",
        "timestamp": _now(),
        "symbol": symbol,
        "market": _market_type(symbol, market),
        "side": str(side or "LONG").upper(),
        "price": float(price or 0.0),
        "confidence": float(confidence or 0.0),
        "regime": regime or "UNKNOWN",
        "macro_state": macro_state or "UNKNOWN",
        "allocation_tier": allocation_tier or "WATCH",
        "safety": {
            "paper_mode_only": True,
            "broker_execution": False,
            "exchange_order": False,
            "public_endpoint": False,
            "localhost_test_only": True,
        },
    }


def save_payload(payload: Dict[str, Any], path: str = "logs/webhook_test_payload.json") -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2)
    return path


def webhook_test_payload(path: str = "logs/webhook_test_payload.json") -> Dict[str, Any]:
    payload = build_webhook_payload(
        symbol="BTCUSDT",
        side="LONG",
        price=100000.0,
        confidence=75.0,
        regime="LOCALHOST_TEST",
        macro_state="PAPER_ONLY",
        allocation_tier="WATCH",
        market="crypto",
    )
    save_payload(payload, path)
    return {"ok": True, "payload": payload, "output_path": path}


def format_webhook_test(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "TRADINGVIEW WEBHOOK TEST",
            f"OK: {result.get('ok')}",
            f"Output: {result.get('output_path')}",
            json.dumps(result.get("payload", {}), indent=2),
        ]
    )
