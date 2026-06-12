import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from binance_futures_testnet_client import (
    DEMO_FUTURES_BASE_URL,
    BinanceFuturesTestnetClient,
    BinanceFuturesTestnetClientError,
    assert_testnet_base_url,
    load_dotenv_file,
)

BROKER_MODE_REQUIRED = "BINANCE_FUTURES_TESTNET_ONLY"
RESULT_PATH = "logs/binance_testnet_executor_result.json"
ORDERS_PATH = "logs/binance_testnet_orders.jsonl"
STATUS_PATH = "reports/binance_testnet_status.json"
DEFAULT_DAILY_ORDER_LIMIT = 1
DEFAULT_MAX_NOTIONAL = 25.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_list(name: str) -> List[str]:
    value = os.getenv(name, "")
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as json_file:
        json.dump(payload, json_file, indent=2, sort_keys=True)
        json_file.write("\n")


def append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as jsonl_file:
        jsonl_file.write(json.dumps(payload, sort_keys=True) + "\n")


def redact_binance_response(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            lower_key = key.lower()
            if "secret" in lower_key or "apikey" in lower_key or "api_key" in lower_key:
                redacted[key] = "REDACTED"
            else:
                redacted[key] = redact_binance_response(value)
        return redacted
    if isinstance(payload, list):
        return [redact_binance_response(item) for item in payload]
    return payload


def build_result(args: argparse.Namespace, mode: str) -> Dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "mode": mode,
        "base_url": os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL),
        "symbol": (args.symbol or "").upper() or None,
        "side": (args.side or "").upper() or None,
        "order_type": (args.order_type or "").upper() or None,
        "quantity": args.quantity,
        "price": args.price,
        "dry_run": bool(args.dry_run),
        "send_requested": bool(args.send),
        "order_test": bool(args.order_test),
        "safety_passed": False,
        "endpoint_safety_passed": False,
        "symbol_filter_passed": False,
        "daily_limit_passed": False,
        "order_attempted": False,
        "order_success": False,
        "blocked_reason": None,
        "binance_response_redacted": None,
        "real_binance_enabled": False,
        "broker_mode": BROKER_MODE_REQUIRED,
    }


def config_safety() -> Tuple[bool, List[str], str]:
    reasons: List[str] = []
    base_url = os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL)
    try:
        safe_base_url = assert_testnet_base_url(base_url)
    except BinanceFuturesTestnetClientError as exc:
        safe_base_url = base_url
        reasons.append(str(exc))
    broker_mode = os.getenv("BROKER_MODE", BROKER_MODE_REQUIRED)
    if broker_mode != BROKER_MODE_REQUIRED:
        reasons.append(f"BROKER_MODE must be {BROKER_MODE_REQUIRED}.")
    if env_bool("REAL_BINANCE_ENABLED", False):
        reasons.append("REAL_BINANCE_ENABLED must be false.")
    if env_bool("ALLOW_REAL_BINANCE_ORDER", False):
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be false.")
    env_bool("ALLOW_TESTNET_ORDER", False)
    env_bool("ALLOW_AUTO_TESTNET_ORDER", False)
    return not reasons, reasons, safe_base_url


def client() -> BinanceFuturesTestnetClient:
    return BinanceFuturesTestnetClient()


def exchange_symbols(exchange_info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    symbols = exchange_info.get("symbols", [])
    return {str(item.get("symbol", "")).upper(): item for item in symbols if item.get("symbol")}


def validate_symbol(symbol: Optional[str], exchange_info: Dict[str, Any]) -> Tuple[bool, str]:
    if not symbol:
        return False, "Symbol is required."
    item = exchange_symbols(exchange_info).get(symbol.upper())
    if not item:
        return False, f"Unknown testnet symbol: {symbol.upper()}"
    if item.get("status") != "TRADING":
        return False, f"Symbol is not TRADING: {symbol.upper()}"
    return True, ""


def today_order_attempt_count(path: str = ORDERS_PATH) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, "r", encoding="utf-8") as jsonl_file:
        for line in jsonl_file:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(item.get("generated_at", "")).startswith(today) and item.get("order_attempted"):
                count += 1
    return count


def order_allowlist_passed(symbol: str) -> bool:
    allowlist = env_list("TESTNET_ORDER_ALLOWLIST")
    return bool(allowlist and symbol.upper() in allowlist)


def daily_limit_passed() -> bool:
    limit = env_int("TESTNET_DAILY_ORDER_LIMIT", DEFAULT_DAILY_ORDER_LIMIT)
    return today_order_attempt_count() < limit


def estimate_notional(quantity: Any, price: Any) -> Optional[float]:
    if quantity is None or price is None:
        return None
    try:
        return float(quantity) * float(price)
    except (TypeError, ValueError):
        return None


def order_payload(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "symbol": args.symbol.upper(),
        "side": args.side.upper(),
        "order_type": args.order_type.upper(),
        "quantity": args.quantity,
        "price": args.price,
        "estimated_notional_usdt": estimate_notional(args.quantity, args.price),
    }


def run_status(args: argparse.Namespace) -> int:
    result = build_result(args, "status")
    safe, reasons, base_url = config_safety()
    result["base_url"] = base_url
    result["endpoint_safety_passed"] = not any("base URL" in reason for reason in reasons)
    result["safety_passed"] = safe
    if not safe:
        result["blocked_reason"] = "; ".join(reasons)
        write_json(RESULT_PATH, result)
        print(result["blocked_reason"])
        return 1
    api = client()
    server_time = api.get_server_time()
    exchange_info = api.get_exchange_info()
    status_payload = {
        "generated_at": utc_now(),
        "base_url": api.base_url,
        "server_time": server_time,
        "exchange_timezone": exchange_info.get("timezone"),
        "symbol_count": len(exchange_info.get("symbols", [])),
        "safety_passed": True,
        "broker_mode": BROKER_MODE_REQUIRED,
        "real_binance_enabled": False,
    }
    write_json(STATUS_PATH, status_payload)
    result["binance_response_redacted"] = redact_binance_response(status_payload)
    write_json(RESULT_PATH, result)
    print(f"status_ok base_url={api.base_url} symbols={status_payload['symbol_count']}")
    return 0


def run_account(args: argparse.Namespace) -> int:
    result = build_result(args, "account")
    safe, reasons, base_url = config_safety()
    result["base_url"] = base_url
    result["endpoint_safety_passed"] = not any("base URL" in reason for reason in reasons)
    result["safety_passed"] = safe
    if not safe:
        result["blocked_reason"] = "; ".join(reasons)
        write_json(RESULT_PATH, result)
        print(result["blocked_reason"])
        return 1
    payload = client().get_account()
    result["binance_response_redacted"] = redact_binance_response(
        {
            "canTrade": payload.get("canTrade"),
            "totalWalletBalance": payload.get("totalWalletBalance"),
            "availableBalance": payload.get("availableBalance"),
        }
    )
    write_json(RESULT_PATH, result)
    print(f"canTrade={payload.get('canTrade')}")
    print(f"totalWalletBalance={payload.get('totalWalletBalance')}")
    print(f"availableBalance={payload.get('availableBalance')}")
    return 0


def run_positions(args: argparse.Namespace) -> int:
    result = build_result(args, "positions")
    safe, reasons, base_url = config_safety()
    result["base_url"] = base_url
    result["endpoint_safety_passed"] = not any("base URL" in reason for reason in reasons)
    result["safety_passed"] = safe
    if not safe:
        result["blocked_reason"] = "; ".join(reasons)
        write_json(RESULT_PATH, result)
        print(result["blocked_reason"])
        return 1
    payload = client().get_position_risk(args.symbol.upper() if args.symbol else None)
    result["binance_response_redacted"] = redact_binance_response(payload)
    write_json(RESULT_PATH, result)
    for item in payload:
        print(
            "symbol={symbol} positionAmt={positionAmt} entryPrice={entryPrice} unrealizedProfit={unrealizedProfit}".format(
                symbol=item.get("symbol"),
                positionAmt=item.get("positionAmt"),
                entryPrice=item.get("entryPrice"),
                unrealizedProfit=item.get("unRealizedProfit", item.get("unrealizedProfit")),
            )
        )
    return 0


def run_order_action(args: argparse.Namespace) -> int:
    mode = "dry_run" if args.dry_run or not args.send else "order_test" if args.order_test else "order"
    result = build_result(args, mode)
    safe, reasons, base_url = config_safety()
    result["base_url"] = base_url
    result["endpoint_safety_passed"] = not any("base URL" in reason for reason in reasons)
    result["safety_passed"] = safe
    if not safe:
        result["blocked_reason"] = "; ".join(reasons)
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    if args.auto_from_overlay and not env_bool("ALLOW_AUTO_TESTNET_ORDER", False):
        result["blocked_reason"] = "ALLOW_AUTO_TESTNET_ORDER must be true for --auto-from-overlay."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    if not args.symbol or not args.side or not args.quantity or not args.order_type:
        result["blocked_reason"] = "symbol, side, quantity, and order-type are required for order actions."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    api = client()
    exchange_info = api.get_exchange_info()
    symbol_ok, symbol_reason = validate_symbol(args.symbol, exchange_info)
    result["symbol_filter_passed"] = symbol_ok
    result["daily_limit_passed"] = daily_limit_passed()
    result["binance_response_redacted"] = {"estimate": order_payload(args)}
    if not symbol_ok:
        result["blocked_reason"] = symbol_reason
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(symbol_reason)
        return 1

    if args.dry_run or not args.send:
        result["blocked_reason"] = None
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print("dry_run_ok order_attempted=false")
        print(json.dumps(order_payload(args), sort_keys=True))
        return 0

    if not env_bool("ALLOW_TESTNET_ORDER", False):
        result["blocked_reason"] = "ALLOW_TESTNET_ORDER must be true for any send action."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    if args.order_test:
        result["order_attempted"] = True
        response = api.place_test_order(
            args.symbol,
            args.side,
            args.order_type,
            args.quantity,
            price=args.price,
        )
        result["order_success"] = True
        result["binance_response_redacted"] = redact_binance_response(response)
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print("order_test_ok")
        return 0

    if not order_allowlist_passed(args.symbol):
        result["blocked_reason"] = "Symbol is not in TESTNET_ORDER_ALLOWLIST."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1
    if not result["daily_limit_passed"]:
        result["blocked_reason"] = "TESTNET_DAILY_ORDER_LIMIT exceeded."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1
    notional = estimate_notional(args.quantity, args.price)
    max_notional = env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL)
    if notional is None or notional > max_notional:
        result["blocked_reason"] = "Order notional is unknown or exceeds TESTNET_MAX_NOTIONAL_USDT."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    result["order_attempted"] = True
    response = api.place_order(args.symbol, args.side, args.order_type, args.quantity, price=args.price)
    result["order_success"] = True
    result["binance_response_redacted"] = redact_binance_response(response)
    write_json(RESULT_PATH, result)
    append_jsonl(ORDERS_PATH, result)
    print("order_sent_ok")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance Futures Testnet executor for MAMUYY Hunter")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--account", action="store_true")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--side", choices=["BUY", "SELL"])
    parser.add_argument("--quantity")
    parser.add_argument("--order-type", choices=["MARKET", "LIMIT"])
    parser.add_argument("--price")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--order-test", action="store_true")
    parser.add_argument("--from-overlay", action="store_true")
    parser.add_argument("--auto-from-overlay", action="store_true")
    parser.add_argument("--allow-need-review", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    args = parse_args()
    try:
        if args.status:
            return run_status(args)
        if args.account:
            return run_account(args)
        if args.positions:
            return run_positions(args)
        return run_order_action(args)
    except BinanceFuturesTestnetClientError as exc:
        result = build_result(args, "error")
        result["blocked_reason"] = str(exc)
        write_json(RESULT_PATH, result)
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
