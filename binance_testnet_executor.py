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
    daily_count, daily_limit, daily_passed, daily_reason = daily_limit_status()
    return {
        "generated_at": utc_now(),
        "mode": mode,
        "base_url": os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL),
        "symbol": (args.symbol or "").upper() or None,
        "side": (args.side or "").upper() or None,
        "order_type": (args.order_type or "").upper() or None,
        "quantity": args.quantity,
        "price": args.price,
        "reduce_only": bool(args.reduce_only),
        "close_position_requested": bool(args.close_position),
        "close_side": None,
        "close_quantity": None,
        "already_flat": False,
        "position_before_amt": None,
        "position_after_amt": None,
        "dry_run": bool(args.dry_run),
        "send_requested": bool(args.send),
        "order_test": bool(args.order_test),
        "safety_passed": False,
        "endpoint_safety_passed": False,
        "symbol_filter_passed": False,
        "daily_actual_order_count": daily_count,
        "daily_order_limit": daily_limit,
        "daily_limit_passed": daily_passed,
        "daily_limit_reason": daily_reason,
        "order_attempted": False,
        "order_success": False,
        "estimated_price": None,
        "estimated_price_source": None,
        "estimated_notional_usdt": None,
        "max_notional_usdt": env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL),
        "notional_limit_passed": False,
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


def today_actual_order_count(path: str = ORDERS_PATH) -> int:
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
            if (
                str(item.get("generated_at", "")).startswith(today)
                and item.get("mode") in {"actual_order", "actual_close_position"}
                and item.get("order_success") is True
                and item.get("order_test") is False
                and item.get("dry_run") is False
                and (item.get("mode") != "actual_close_position" or item.get("reduce_only") is True)
            ):
                count += 1
    return count


def order_allowlist_passed(symbol: str) -> bool:
    allowlist = env_list("TESTNET_ORDER_ALLOWLIST")
    return bool(allowlist and symbol.upper() in allowlist)


def daily_order_limit() -> int:
    return env_int(
        "TESTNET_MAX_ORDERS_PER_DAY",
        env_int("TESTNET_DAILY_ORDER_LIMIT", DEFAULT_DAILY_ORDER_LIMIT),
    )


def daily_limit_status() -> Tuple[int, int, bool, str]:
    count = today_actual_order_count()
    limit = daily_order_limit()
    passed = count < limit
    if passed:
        reason = f"{count} actual successful orders today; limit {limit}; actual order allowed."
    else:
        reason = f"{count} actual successful orders today; limit {limit}; actual order blocked."
    return count, limit, passed, reason


def apply_daily_limit_status(result: Dict[str, Any]) -> None:
    count, limit, passed, reason = daily_limit_status()
    result["daily_actual_order_count"] = count
    result["daily_order_limit"] = limit
    result["daily_limit_passed"] = passed
    result["daily_limit_reason"] = reason


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_quantity(value: float) -> str:
    formatted = f"{value:.12f}".rstrip("0").rstrip(".")
    return formatted or "0"


def position_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def fetch_symbol_position(api: BinanceFuturesTestnetClient, symbol: str) -> Tuple[Optional[Dict[str, Any]], float]:
    normalized_symbol = symbol.upper()
    payload = api.get_position_risk(normalized_symbol)
    for item in position_items(payload):
        if str(item.get("symbol", "")).upper() == normalized_symbol:
            return item, parse_float(item.get("positionAmt")) or 0.0
    return None, 0.0


def reducing_order_blocked_reason(side: str, quantity: Any, position_amt: float) -> Optional[str]:
    quantity_float = parse_float(quantity)
    if quantity_float is None or quantity_float <= 0:
        return "reduce-only quantity must be greater than zero."
    normalized_side = side.upper()
    if position_amt == 0:
        return "reduce-only order blocked because position is already flat."
    if normalized_side == "SELL" and position_amt <= 0:
        return "reduce-only SELL would not reduce the current position."
    if normalized_side == "BUY" and position_amt >= 0:
        return "reduce-only BUY would not reduce the current position."
    if quantity_float > abs(position_amt):
        return "reduce-only quantity exceeds current position size."
    return None


def estimate_notional(quantity: Any, price: Any) -> Optional[float]:
    quantity_float = parse_float(quantity)
    price_float = parse_float(price)
    if quantity_float is None or price_float is None:
        return None
    return quantity_float * price_float


def fetch_market_price(api: BinanceFuturesTestnetClient, symbol: str) -> Tuple[Optional[float], Optional[str]]:
    try:
        mark_price = parse_float(api.get_mark_price(symbol))
    except BinanceFuturesTestnetClientError:
        mark_price = None
    if mark_price is not None:
        return mark_price, "markPrice"
    try:
        ticker_price = parse_float(api.get_ticker_price(symbol))
    except BinanceFuturesTestnetClientError:
        ticker_price = None
    if ticker_price is not None:
        return ticker_price, "tickerPrice"
    return None, None


def estimate_order_notional(
    api: BinanceFuturesTestnetClient, args: argparse.Namespace
) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    order_type = args.order_type.upper()
    if order_type == "MARKET":
        estimated_price, estimated_price_source = fetch_market_price(api, args.symbol)
    else:
        estimated_price = parse_float(args.price)
        estimated_price_source = "limitPrice" if estimated_price is not None else None
    return estimated_price, estimated_price_source, estimate_notional(args.quantity, estimated_price)


def apply_notional_estimate(result: Dict[str, Any], args: argparse.Namespace, api: BinanceFuturesTestnetClient) -> None:
    estimated_price, estimated_price_source, estimated_notional = estimate_order_notional(api, args)
    max_notional = env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL)
    result["estimated_price"] = estimated_price
    result["estimated_price_source"] = estimated_price_source
    result["estimated_notional_usdt"] = estimated_notional
    result["max_notional_usdt"] = max_notional
    result["notional_limit_passed"] = estimated_notional is not None and estimated_notional <= max_notional


def notional_blocked_reason(result: Dict[str, Any]) -> Optional[str]:
    if result.get("estimated_notional_usdt") is None:
        return "notional estimate missing"
    if not result.get("notional_limit_passed"):
        return "notional exceeds TESTNET_MAX_NOTIONAL_USDT"
    return None


def order_payload(args: argparse.Namespace, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = {
        "symbol": args.symbol.upper() if args.symbol else None,
        "side": args.side.upper() if args.side else None,
        "order_type": args.order_type.upper() if args.order_type else None,
        "quantity": args.quantity,
        "price": args.price,
        "reduce_only": bool(args.reduce_only),
    }
    if result is not None:
        payload.update(
            {
                "estimated_price": result.get("estimated_price"),
                "estimated_price_source": result.get("estimated_price_source"),
                "estimated_notional_usdt": result.get("estimated_notional_usdt"),
                "max_notional_usdt": result.get("max_notional_usdt"),
                "notional_limit_passed": result.get("notional_limit_passed"),
                "blocked_reason": result.get("blocked_reason"),
                "daily_actual_order_count": result.get("daily_actual_order_count"),
                "daily_order_limit": result.get("daily_order_limit"),
                "daily_limit_passed": result.get("daily_limit_passed"),
                "daily_limit_reason": result.get("daily_limit_reason"),
                "close_position_requested": result.get("close_position_requested"),
                "close_side": result.get("close_side"),
                "close_quantity": result.get("close_quantity"),
                "already_flat": result.get("already_flat"),
                "position_before_amt": result.get("position_before_amt"),
                "position_after_amt": result.get("position_after_amt"),
            }
        )
    else:
        payload["estimated_notional_usdt"] = estimate_notional(args.quantity, args.price)
    return payload


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
    symbol_filter = args.symbol.upper() if args.symbol else None
    payload = client().get_position_risk(symbol_filter)
    if symbol_filter and isinstance(payload, list):
        payload = [item for item in payload if str(item.get("symbol", "")).upper() == symbol_filter]
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


def run_close_position(args: argparse.Namespace) -> int:
    args.reduce_only = True
    args.order_type = "MARKET"
    mode = "close_position" if args.dry_run or not args.send else "actual_close_position"
    result = build_result(args, mode)
    result["reduce_only"] = True
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

    if not args.symbol:
        result["blocked_reason"] = "symbol is required for --close-position."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    api = client()
    exchange_info = api.get_exchange_info()
    symbol_ok, symbol_reason = validate_symbol(args.symbol, exchange_info)
    result["symbol_filter_passed"] = symbol_ok
    apply_daily_limit_status(result)
    if not symbol_ok:
        result["blocked_reason"] = symbol_reason
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(symbol_reason)
        return 1

    position, position_amt = fetch_symbol_position(api, args.symbol)
    result["position_before_amt"] = format_quantity(position_amt)
    result["binance_response_redacted"] = {"position_before": redact_binance_response(position)}

    if position_amt == 0:
        result["already_flat"] = True
        result["blocked_reason"] = None
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print("already_flat")
        return 0

    close_side = "SELL" if position_amt > 0 else "BUY"
    close_quantity = format_quantity(abs(position_amt))
    args.side = close_side
    args.quantity = close_quantity
    args.price = None
    result["side"] = close_side
    result["quantity"] = close_quantity
    result["order_type"] = "MARKET"
    result["close_side"] = close_side
    result["close_quantity"] = close_quantity
    apply_notional_estimate(result, args, api)
    result["binance_response_redacted"] = {
        "position_before": redact_binance_response(position),
        "estimate": order_payload(args, result),
    }

    if args.dry_run or not args.send:
        result["blocked_reason"] = None
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print("dry_run_ok order_attempted=false")
        print(json.dumps(order_payload(args, result), sort_keys=True))
        return 0

    blocked_reason = notional_blocked_reason(result)
    if blocked_reason:
        result["blocked_reason"] = blocked_reason
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    if not env_bool("ALLOW_TESTNET_ORDER", False):
        result["blocked_reason"] = "ALLOW_TESTNET_ORDER must be true for any send action."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    if not order_allowlist_passed(args.symbol):
        result["blocked_reason"] = "Symbol is not in TESTNET_ORDER_ALLOWLIST."
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1
    if not result["daily_limit_passed"]:
        result["blocked_reason"] = result["daily_limit_reason"]
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

    result["order_attempted"] = True
    try:
        response = api.place_order(args.symbol, close_side, "MARKET", close_quantity, reduce_only=True)
    except BinanceFuturesTestnetClientError as exc:
        result["order_success"] = False
        result["blocked_reason"] = str(exc)
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1
    result["order_success"] = True
    _, position_after_amt = fetch_symbol_position(api, args.symbol)
    result["position_after_amt"] = format_quantity(position_after_amt)
    result["binance_response_redacted"] = redact_binance_response(response)
    write_json(RESULT_PATH, result)
    append_jsonl(ORDERS_PATH, result)
    print("close_position_sent_ok")
    return 0


def run_order_action(args: argparse.Namespace) -> int:
    mode = "dry_run" if args.dry_run or not args.send else "order_test" if args.order_test else "actual_order"
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
    apply_daily_limit_status(result)
    if not symbol_ok:
        result["blocked_reason"] = symbol_reason
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(symbol_reason)
        return 1

    if args.reduce_only:
        _, position_amt = fetch_symbol_position(api, args.symbol)
        result["position_before_amt"] = format_quantity(position_amt)
        blocked_reason = reducing_order_blocked_reason(args.side, args.quantity, position_amt)
        if blocked_reason:
            result["blocked_reason"] = blocked_reason
            result["binance_response_redacted"] = {"estimate": order_payload(args, result)}
            write_json(RESULT_PATH, result)
            append_jsonl(ORDERS_PATH, result)
            print(result["blocked_reason"])
            return 1

    apply_notional_estimate(result, args, api)
    result["binance_response_redacted"] = {"estimate": order_payload(args, result)}

    if args.dry_run or not args.send:
        result["blocked_reason"] = None
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print("dry_run_ok order_attempted=false")
        print(json.dumps(order_payload(args, result), sort_keys=True))
        return 0

    blocked_reason = notional_blocked_reason(result)
    if blocked_reason:
        result["blocked_reason"] = blocked_reason
        result["binance_response_redacted"] = {"estimate": order_payload(args, result)}
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1

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
            reduce_only=args.reduce_only,
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
        result["blocked_reason"] = result["daily_limit_reason"]
        write_json(RESULT_PATH, result)
        append_jsonl(ORDERS_PATH, result)
        print(result["blocked_reason"])
        return 1
    result["order_attempted"] = True
    response = api.place_order(
        args.symbol,
        args.side,
        args.order_type,
        args.quantity,
        price=args.price,
        reduce_only=args.reduce_only,
    )
    result["order_success"] = True
    if args.reduce_only:
        _, position_after_amt = fetch_symbol_position(api, args.symbol)
        result["position_after_amt"] = format_quantity(position_after_amt)
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
    parser.add_argument("--reduce-only", action="store_true")
    parser.add_argument("--close-position", action="store_true")
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
        if args.close_position:
            return run_close_position(args)
        return run_order_action(args)
    except BinanceFuturesTestnetClientError as exc:
        result = build_result(args, "error")
        result["blocked_reason"] = str(exc)
        write_json(RESULT_PATH, result)
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
