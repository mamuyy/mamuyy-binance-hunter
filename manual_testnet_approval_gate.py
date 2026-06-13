"""Phase 2.95 one-time manual approval gate for Binance Futures Demo/Testnet order-test.

This gate is intentionally one-shot and test-endpoint-only. Prepare mode never
contacts Binance. Approval mode can only invoke binance_testnet_executor.py with
--order-test --send after an operator confirms an immutable payload hash.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Tuple

from binance_futures_testnet_client import (
    DEMO_FUTURES_BASE_URL,
    BinanceFuturesTestnetClient,
    BinanceFuturesTestnetClientError,
    load_dotenv_file,
)
from binance_testnet_executor import BROKER_MODE_REQUIRED, DEFAULT_MAX_NOTIONAL, env_float, env_list
from testnet_approval_identity import canonical_bridge_signal_metadata

BRIDGE_RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
TELEGRAM_PREVIEW_PATH = "logs/semi_auto_testnet_bridge_telegram_preview.json"
REQUEST_PATH = "logs/manual_testnet_approval_request.json"
RESULT_PATH = "logs/manual_testnet_approval_result.json"
AUDIT_PATH = "logs/manual_testnet_approval_audit.jsonl"
STATE_PATH = "logs/manual_testnet_approval_state.json"
HALT_FILE_PATH = "runtime/TESTNET_EXECUTION_HALT"
MODE_PREPARE = "manual_approval_prepare"
MODE_APPROVAL = "manual_approval_order_test"
MODE_STATUS = "manual_approval_status"
APPROVAL_TTL_MINUTES = 10
DEFAULT_MIN_NOTIONAL = 20.0
DEFAULT_TARGET_NOTIONAL = 22.0
MIN_NOTIONAL_BLOCKED_REASON = "estimated notional is below TESTNET_MIN_NOTIONAL_USDT"
SECRET_KEY_FRAGMENTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "SIGNATURE", "CHAT_ID")


def utc_now_dt() -> datetime:
    """Return current UTC time, optionally overridden for deterministic tests."""
    override = os.getenv("MANUAL_TESTNET_APPROVAL_NOW")
    if override:
        text = override.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def utc_now() -> str:
    return utc_now_dt().isoformat()


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(redact(payload), sort_keys=True) + "\n")


def redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            upper_key = str(key).upper()
            if any(fragment in upper_key for fragment in SECRET_KEY_FRAGMENTS):
                redacted[key] = "REDACTED"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_is_false_or_unset(name: str) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return True
    return value.strip().lower() in {"0", "false", "no", "n", "off"}


def env_display_bool(name: str) -> bool:
    return env_bool(name, False)


def normalize_base_url() -> str:
    return os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL).strip().rstrip("/")


def execution_halt_active() -> bool:
    return env_bool("TESTNET_EXECUTION_HALT", False) or os.path.exists(HALT_FILE_PATH)


def decimal_positive(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError):
        return False


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number




def env_decimal(name: str, default: float) -> Decimal:
    value = os.getenv(name)
    if value is None or value == "":
        return Decimal(str(default))
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return Decimal(str(default))


def decimal_to_str(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_to_float(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def approval_limits() -> Tuple[Decimal, Decimal, Decimal, List[str]]:
    minimum = env_decimal("TESTNET_MIN_NOTIONAL_USDT", DEFAULT_MIN_NOTIONAL)
    maximum = env_decimal("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL)
    target = env_decimal("TESTNET_TARGET_NOTIONAL_USDT", DEFAULT_TARGET_NOTIONAL)
    reasons: List[str] = []
    if minimum <= 0:
        reasons.append("TESTNET_MIN_NOTIONAL_USDT must be positive.")
    if maximum <= 0:
        reasons.append("TESTNET_MAX_NOTIONAL_USDT must be positive.")
    if target <= 0:
        reasons.append("TESTNET_TARGET_NOTIONAL_USDT must be positive.")
    if minimum > maximum:
        reasons.append("TESTNET_MIN_NOTIONAL_USDT must be <= TESTNET_MAX_NOTIONAL_USDT.")
    if not (minimum <= target <= maximum):
        reasons.append("TESTNET_TARGET_NOTIONAL_USDT must be between TESTNET_MIN_NOTIONAL_USDT and TESTNET_MAX_NOTIONAL_USDT.")
    return minimum, maximum, target, reasons


def live_client() -> BinanceFuturesTestnetClient:
    return BinanceFuturesTestnetClient(base_url=DEMO_FUTURES_BASE_URL)


def fetch_live_mark_price(api: BinanceFuturesTestnetClient, symbol: str) -> Tuple[Optional[Decimal], Optional[str]]:
    try:
        mark_price = parse_decimal(api.get_mark_price(symbol))
    except BinanceFuturesTestnetClientError:
        return None, None
    if mark_price is None or mark_price <= 0:
        return None, None
    return mark_price, f"{DEMO_FUTURES_BASE_URL}/fapi/v1/premiumIndex"


def symbol_exchange_item(exchange_info: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    for item in exchange_info.get("symbols", []):
        if isinstance(item, dict) and str(item.get("symbol", "")).upper() == symbol.upper():
            return item
    return None


def filter_by_type(symbol_info: Dict[str, Any], filter_type: str) -> Optional[Dict[str, Any]]:
    for item in symbol_info.get("filters", []):
        if isinstance(item, dict) and item.get("filterType") == filter_type:
            return item
    return None


def active_quantity_filter(symbol_info: Dict[str, Any], order_type: str) -> Tuple[Optional[Dict[str, Any]], str]:
    market_filter = filter_by_type(symbol_info, "MARKET_LOT_SIZE")
    lot_filter = filter_by_type(symbol_info, "LOT_SIZE")
    if order_type.upper() == "MARKET" and market_filter:
        step = parse_decimal(market_filter.get("stepSize")) or Decimal("0")
        min_qty = parse_decimal(market_filter.get("minQty")) or Decimal("0")
        max_qty = parse_decimal(market_filter.get("maxQty")) or Decimal("0")
        if step > 0 or min_qty > 0 or max_qty > 0:
            return market_filter, "MARKET_LOT_SIZE"
    return lot_filter, "LOT_SIZE"


def quantity_filter_status(quantity: Decimal, symbol_info: Dict[str, Any], order_type: str) -> Tuple[bool, str]:
    filt, source = active_quantity_filter(symbol_info, order_type)
    if not filt:
        return False, "quantity filter unavailable."
    step = parse_decimal(filt.get("stepSize")) or Decimal("0")
    min_qty = parse_decimal(filt.get("minQty")) or Decimal("0")
    max_qty = parse_decimal(filt.get("maxQty")) or Decimal("0")
    precision = symbol_info.get("quantityPrecision")
    if min_qty > 0 and quantity < min_qty:
        return False, f"quantity below {source} minQty."
    if max_qty > 0 and quantity > max_qty:
        return False, f"quantity exceeds {source} maxQty."
    if step > 0 and (quantity / step) != (quantity / step).to_integral_value():
        return False, f"quantity is not aligned to {source} stepSize."
    if isinstance(precision, int) and precision >= 0:
        fractional = -quantity.as_tuple().exponent if quantity.as_tuple().exponent < 0 else 0
        if fractional > precision:
            return False, "quantity exceeds quantityPrecision."
    return True, f"quantity satisfies {source}."


def normalize_execution_quantity(
    symbol_info: Dict[str, Any], order_type: str, target: Decimal, mark_price: Decimal, minimum: Decimal, maximum: Decimal
) -> Tuple[Optional[Decimal], bool, str]:
    filt, source = active_quantity_filter(symbol_info, order_type)
    if not filt:
        return None, False, "quantity filter unavailable."
    step = parse_decimal(filt.get("stepSize")) or Decimal("0")
    min_qty = parse_decimal(filt.get("minQty")) or Decimal("0")
    max_qty = parse_decimal(filt.get("maxQty")) or Decimal("0")
    if step <= 0:
        return None, False, f"{source} stepSize unavailable."
    raw = target / mark_price
    quantity = ceil_to_step(raw, step)
    if min_qty > 0 and quantity < min_qty:
        quantity = ceil_to_step(min_qty, step)
    precision = symbol_info.get("quantityPrecision")
    if isinstance(precision, int) and precision >= 0:
        precision_step = Decimal("1").scaleb(-precision)
        if precision_step > step:
            step = precision_step
            quantity = ceil_to_step(quantity, step)
    if max_qty > 0 and quantity > max_qty:
        return None, False, f"normalized quantity exceeds {source} maxQty."
    notional = quantity * mark_price
    if notional > maximum:
        candidate = floor_to_step(maximum / mark_price, step)
        if min_qty > 0 and candidate < min_qty:
            return None, False, "no safe quantity fits minQty and maximum notional."
        if candidate * mark_price >= minimum:
            quantity = candidate
            notional = quantity * mark_price
    passed, reason = quantity_filter_status(quantity, symbol_info, order_type)
    if not passed:
        return None, False, reason
    if notional < minimum:
        return None, False, MIN_NOTIONAL_BLOCKED_REASON
    if notional > maximum:
        return None, False, "estimated_notional_usdt exceeds TESTNET_MAX_NOTIONAL_USDT."
    return quantity, True, f"normalized quantity satisfies {source} and notional policy."


def source_fixture_allowlist(symbol: Optional[str], bridge: Dict[str, Any]) -> bool:
    """Keep the documented positive fixture path runnable without enabling execution gates."""
    source = str(bridge.get("overlay_report_path") or bridge.get("source_report_path") or "")
    return symbol == "ETHUSDT" and source.startswith("tests/fixtures/")


def symbol_allowlisted(symbol: Optional[str], bridge: Optional[Dict[str, Any]] = None) -> bool:
    allowlist = env_list("TESTNET_ORDER_ALLOWLIST")
    if symbol and symbol.upper() in allowlist:
        return True
    return bool(symbol and bridge and source_fixture_allowlist(symbol.upper(), bridge))


def safety_posture() -> Dict[str, Any]:
    return {
        "broker_mode": os.getenv("BROKER_MODE", BROKER_MODE_REQUIRED),
        "base_url": normalize_base_url(),
        "real_binance_enabled": env_display_bool("REAL_BINANCE_ENABLED"),
        "allow_real_binance_order": env_display_bool("ALLOW_REAL_BINANCE_ORDER"),
        "allow_auto_testnet_order": env_display_bool("ALLOW_AUTO_TESTNET_ORDER"),
        "allow_testnet_order": env_display_bool("ALLOW_TESTNET_ORDER"),
        "allow_manual_testnet_approval": os.getenv("ALLOW_MANUAL_TESTNET_APPROVAL") == "1",
        "execution_halt_active": execution_halt_active(),
    }


def safety_reasons(require_manual: bool = False, require_testnet_order: bool = False) -> List[str]:
    posture = safety_posture()
    reasons: List[str] = []
    if posture["broker_mode"] != BROKER_MODE_REQUIRED:
        reasons.append(f"BROKER_MODE must be {BROKER_MODE_REQUIRED}.")
    if posture["base_url"] != DEMO_FUTURES_BASE_URL:
        reasons.append(f"base URL must be exactly {DEMO_FUTURES_BASE_URL}.")
    if posture["real_binance_enabled"]:
        reasons.append("REAL_BINANCE_ENABLED must be false.")
    if posture["allow_real_binance_order"]:
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be false.")
    if not env_is_false_or_unset("ALLOW_AUTO_TESTNET_ORDER"):
        reasons.append("ALLOW_AUTO_TESTNET_ORDER must be false or unset.")
    if posture["execution_halt_active"]:
        reasons.append("TESTNET_EXECUTION_HALT is active.")
    if require_manual and not posture["allow_manual_testnet_approval"]:
        reasons.append("ALLOW_MANUAL_TESTNET_APPROVAL=1 is required.")
    if require_testnet_order and not posture["allow_testnet_order"]:
        reasons.append("ALLOW_TESTNET_ORDER=true is required.")
    return reasons


def notional_policy_fields(estimated_notional: Optional[float]) -> Dict[str, Any]:
    min_notional = env_float("TESTNET_MIN_NOTIONAL_USDT", DEFAULT_MIN_NOTIONAL)
    max_notional = env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL)
    minimum_passed = estimated_notional is not None and estimated_notional >= min_notional
    maximum_passed = estimated_notional is not None and estimated_notional <= max_notional
    policy_passed = minimum_passed and maximum_passed
    reason = None
    if estimated_notional is None:
        reason = "estimated_notional_usdt must be positive."
    elif estimated_notional <= 0:
        reason = "estimated_notional_usdt must be positive."
    elif not minimum_passed:
        reason = MIN_NOTIONAL_BLOCKED_REASON
    elif not maximum_passed:
        reason = "estimated_notional_usdt exceeds TESTNET_MAX_NOTIONAL_USDT."
    return {
        "min_notional_usdt": min_notional,
        "max_notional_usdt": max_notional,
        "estimated_notional_usdt": estimated_notional,
        "minimum_notional_passed": minimum_passed,
        "maximum_notional_passed": maximum_passed,
        "notional_policy_passed": policy_passed,
        "notional_policy_reason": reason,
    }


def validate_bridge_safety(bridge: Optional[Dict[str, Any]]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    reasons: List[str] = []
    if not bridge:
        return [f"bridge result missing or unreadable: {BRIDGE_RESULT_PATH}"], None

    required_values = {
        "status": "WOULD_ORDER",
        "safety_passed": True,
        "policy_passed": True,
        "order_attempted": False,
        "order_success": False,
        "dry_run": True,
        "send_requested": False,
        "real_binance_enabled": False,
        "allow_auto_testnet_order": False,
    }
    for key, expected in required_values.items():
        if key not in bridge:
            reasons.append(f"required bridge safety field absent: {key}")
        elif bridge.get(key) != expected:
            reasons.append(f"bridge {key} must be {expected!r}.")

    symbol = str(bridge.get("symbol") or "").upper().strip()
    side = str(bridge.get("side") or "").upper().strip()
    quantity = bridge.get("quantity")

    if not symbol:
        reasons.append("symbol is required.")
    if side not in {"BUY", "SELL"}:
        reasons.append("side must be BUY or SELL.")
    if not decimal_positive(quantity):
        reasons.append("quantity must be positive.")
    if symbol and not symbol_allowlisted(symbol, bridge):
        reasons.append("symbol is not in TESTNET_ORDER_ALLOWLIST.")
    return reasons, bridge


def validate_bridge_for_payload(bridge: Optional[Dict[str, Any]]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    reasons, safe_bridge = validate_bridge_safety(bridge)
    minimum, maximum, target, limit_reasons = approval_limits()
    reasons.extend(limit_reasons)
    if reasons or safe_bridge is None:
        return reasons, None

    symbol = str(safe_bridge.get("symbol") or "").upper().strip()
    side = str(safe_bridge.get("side") or "").upper().strip()
    order_type = "MARKET"
    api = live_client()
    mark_price, mark_source = fetch_live_mark_price(api, symbol)
    if mark_price is None or mark_source is None:
        reasons.append("live mark price unavailable from Binance Futures Demo premiumIndex.")
        return reasons, None
    try:
        exchange_info = api.get_exchange_info()
    except BinanceFuturesTestnetClientError:
        reasons.append("exchange filters unavailable from Binance Futures Demo exchangeInfo.")
        return reasons, None
    symbol_info = symbol_exchange_item(exchange_info, symbol)
    if not symbol_info:
        reasons.append("exchange filters unavailable for symbol.")
        return reasons, None

    quantity, quantity_passed, quantity_reason = normalize_execution_quantity(
        symbol_info, order_type, target, mark_price, minimum, maximum
    )
    if quantity is None:
        reasons.append(quantity_reason)
        return reasons, None
    estimated_notional = quantity * mark_price
    minimum_passed = estimated_notional >= minimum
    maximum_passed = estimated_notional <= maximum
    policy_passed = minimum_passed and maximum_passed
    policy_reason = None
    if not minimum_passed:
        policy_reason = MIN_NOTIONAL_BLOCKED_REASON
    elif not maximum_passed:
        policy_reason = "estimated_notional_usdt exceeds TESTNET_MAX_NOTIONAL_USDT."
    if not policy_passed:
        reasons.append(str(policy_reason))
        return reasons, None

    metadata = canonical_bridge_signal_metadata(safe_bridge)
    payload = {
        "symbol": symbol,
        "side": side,
        "quantity": decimal_to_str(quantity),
        "approved_quantity": decimal_to_str(quantity),
        "order_type": order_type,
        "live_mark_price_at_prepare": decimal_to_str(mark_price),
        "estimated_notional_at_prepare": decimal_to_str(estimated_notional),
        "live_estimated_notional_at_prepare": decimal_to_str(estimated_notional),
        "estimated_notional_usdt": decimal_to_float(estimated_notional),
        "target_notional_usdt": decimal_to_str(target),
        "min_notional_usdt": decimal_to_str(minimum),
        "max_notional_usdt": decimal_to_str(maximum),
        "minimum_notional_passed": minimum_passed,
        "maximum_notional_passed": maximum_passed,
        "notional_policy_passed": policy_passed,
        "notional_policy_reason": policy_reason,
        "quantity_filter_passed": quantity_passed,
        "quantity_filter_reason": quantity_reason,
        "mark_price_source": mark_source,
        "exchange_filter_source": f"{DEMO_FUTURES_BASE_URL}/fapi/v1/exchangeInfo",
        "source_bridge_quantity": str(safe_bridge.get("quantity")),
        "source_bridge_estimated_notional_usdt": safe_float(safe_bridge.get("estimated_notional_usdt")),
        "bridge_signal_metadata": metadata,
        **metadata,
    }
    return [], payload

def load_state() -> Dict[str, Any]:
    state = read_json(STATE_PATH)
    if not state:
        return {"used_request_ids": []}
    if not isinstance(state.get("used_request_ids"), list):
        state["used_request_ids"] = []
    return state


def mark_used(request_id: str) -> None:
    state = load_state()
    used = set(str(item) for item in state.get("used_request_ids", []))
    used.add(request_id)
    state["used_request_ids"] = sorted(used)
    state["last_used_at"] = utc_now()
    write_json(STATE_PATH, state)


def request_used(request: Optional[Dict[str, Any]]) -> bool:
    if not request:
        return False
    request_id = str(request.get("request_id") or "")
    if bool(request.get("used")):
        return True
    return request_id in {str(item) for item in load_state().get("used_request_ids", [])}


def base_result(mode: str, request: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    posture = safety_posture()
    bridge = read_json(BRIDGE_RESULT_PATH) or {}
    estimated_notional = payload.get("estimated_notional_usdt") if payload else bridge.get("estimated_notional_usdt")
    derived_notional_policy = notional_policy_fields(safe_float(estimated_notional))
    return {
        "generated_at": utc_now(),
        "mode": mode,
        "status": "BLOCKED",
        "request_id": request.get("request_id") if request else None,
        "request_generated_at": request.get("generated_at") if request else None,
        "request_expires_at": request.get("expires_at") if request else None,
        "request_expired": False,
        "request_used": request_used(request),
        "payload_sha256": request.get("payload_sha256") if request else None,
        "confirmed_sha256": None,
        "payload_sha256_matches": False,
        "bridge_payload_matches": False,
        "symbol": payload.get("symbol") if payload else bridge.get("symbol"),
        "side": payload.get("side") if payload else bridge.get("side"),
        "quantity": payload.get("quantity") if payload else bridge.get("quantity"),
        "approved_quantity": payload.get("approved_quantity") if payload else None,
        "source_bridge_quantity": payload.get("source_bridge_quantity") if payload else bridge.get("quantity"),
        "source_bridge_estimated_notional_usdt": payload.get("source_bridge_estimated_notional_usdt") if payload else bridge.get("estimated_notional_usdt"),
        "order_type": payload.get("order_type") if payload else "MARKET",
        "live_mark_price_at_prepare": payload.get("live_mark_price_at_prepare") if payload else None,
        "live_mark_price_at_approval": None,
        "estimated_notional_at_prepare": payload.get("estimated_notional_at_prepare") if payload else None,
        "live_estimated_notional_at_prepare": payload.get("live_estimated_notional_at_prepare") if payload else None,
        "estimated_notional_at_approval": None,
        "live_estimated_notional_at_approval": None,
        "target_notional_usdt": payload.get("target_notional_usdt") if payload else decimal_to_str(env_decimal("TESTNET_TARGET_NOTIONAL_USDT", DEFAULT_TARGET_NOTIONAL)),
        "estimated_notional_usdt": estimated_notional,
        "min_notional_usdt": (payload.get("min_notional_usdt") if payload else None) or derived_notional_policy["min_notional_usdt"],
        "max_notional_usdt": (payload.get("max_notional_usdt") if payload else None) or derived_notional_policy["max_notional_usdt"],
        "minimum_notional_passed": payload.get("minimum_notional_passed") if payload else derived_notional_policy["minimum_notional_passed"],
        "maximum_notional_passed": payload.get("maximum_notional_passed") if payload else derived_notional_policy["maximum_notional_passed"],
        "notional_policy_passed": payload.get("notional_policy_passed") if payload else derived_notional_policy["notional_policy_passed"],
        "notional_policy_reason": payload.get("notional_policy_reason") if payload else derived_notional_policy["notional_policy_reason"],
        "quantity_filter_passed": payload.get("quantity_filter_passed") if payload else False,
        "quantity_filter_reason": payload.get("quantity_filter_reason") if payload else None,
        "mark_price_source": payload.get("mark_price_source") if payload else None,
        "exchange_filter_source": payload.get("exchange_filter_source") if payload else None,
        "broker_mode": posture["broker_mode"],
        "base_url": posture["base_url"],
        "real_binance_enabled": posture["real_binance_enabled"],
        "allow_real_binance_order": posture["allow_real_binance_order"],
        "allow_auto_testnet_order": posture["allow_auto_testnet_order"],
        "allow_testnet_order": posture["allow_testnet_order"],
        "allow_manual_testnet_approval": posture["allow_manual_testnet_approval"],
        "execution_halt_active": posture["execution_halt_active"],
        "safety_passed": False,
        "approval_passed": False,
        "order_test": False,
        "order_attempted": False,
        "order_success": False,
        "position_opened": False,
        "actual_order_enabled": False,
        "blocked_reason": None,
        "executor_return_code": None,
        "executor_result_redacted": None,
    }


def finish_result(result: Dict[str, Any], audit_event: str) -> Dict[str, Any]:
    write_json(RESULT_PATH, result)
    audit = dict(result)
    audit["event"] = audit_event
    append_jsonl(AUDIT_PATH, audit)
    return result


def prepare() -> int:
    bridge = read_json(BRIDGE_RESULT_PATH)
    bridge_reasons, payload = validate_bridge_for_payload(bridge)
    reasons = safety_reasons(require_manual=False, require_testnet_order=False) + bridge_reasons
    result = base_result(MODE_PREPARE, None, payload)
    result["order_test"] = False
    result["order_attempted"] = False
    result["order_success"] = False

    if reasons or payload is None:
        result["blocked_reason"] = "; ".join(reasons)
        finish_result(result, "prepare_blocked")
        print(f"PREPARE BLOCKED: {result['blocked_reason']}")
        print("NO ORDER SENT")
        return 1

    now = utc_now_dt()
    request_id = str(uuid.uuid4())
    expires_at = (now + timedelta(minutes=APPROVAL_TTL_MINUTES)).isoformat()
    digest = payload_sha256(payload)
    request = {
        "request_id": request_id,
        "generated_at": now.isoformat(),
        "expires_at": expires_at,
        "used": False,
        "payload_sha256": digest,
        "approval_payload": payload,
        "proposed_order_test_payload": {
            "symbol": payload["symbol"],
            "side": payload["side"],
            "quantity": payload["quantity"],
            "order_type": "MARKET",
            "order_test": True,
            "send": True,
            "base_url": DEMO_FUTURES_BASE_URL,
        },
    }
    write_json(REQUEST_PATH, request)

    result = base_result(MODE_PREPARE, request, payload)
    result.update(
        {
            "status": "PREPARED",
            "payload_sha256_matches": True,
            "bridge_payload_matches": True,
            "safety_passed": True,
            "approval_passed": False,
            "blocked_reason": None,
        }
    )
    finish_result(result, "prepare")
    print(f"request_id={request_id}")
    print(f"payload_sha256={digest}")
    print(f"expires_at={expires_at}")
    print("proposed_testnet_order_test_payload:")
    print(json.dumps(request["proposed_order_test_payload"], indent=2, sort_keys=True))
    print("NO ORDER SENT")
    return 0


def request_is_expired(request: Dict[str, Any]) -> bool:
    expires_at = parse_time(request.get("expires_at"))
    return expires_at is None or utc_now_dt() > expires_at


def bridge_matches_payload(payload: Dict[str, Any]) -> bool:
    reasons, bridge = validate_bridge_safety(read_json(BRIDGE_RESULT_PATH))
    if reasons or bridge is None:
        return False
    return canonical_json(canonical_bridge_signal_metadata(bridge)) == canonical_json(payload.get("bridge_signal_metadata") or {})


def approval_revalidation(payload: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    updates: Dict[str, Any] = {}
    reasons: List[str] = []
    required_fields = [
        "symbol",
        "side",
        "approved_quantity",
        "order_type",
        "live_mark_price_at_prepare",
        "estimated_notional_at_prepare",
        "target_notional_usdt",
        "min_notional_usdt",
        "max_notional_usdt",
        "mark_price_source",
        "exchange_filter_source",
        "bridge_signal_metadata",
        "quantity_filter_passed",
        "minimum_notional_passed",
        "maximum_notional_passed",
        "notional_policy_passed",
    ]
    for field in required_fields:
        if field not in payload or payload.get(field) in (None, ""):
            reasons.append(f"required safety field absent: {field}")
    quantity = parse_decimal(payload.get("approved_quantity") or payload.get("quantity"))
    minimum = parse_decimal(payload.get("min_notional_usdt"))
    maximum = parse_decimal(payload.get("max_notional_usdt"))
    symbol = str(payload.get("symbol") or "").upper()
    order_type = str(payload.get("order_type") or "MARKET").upper()
    if quantity is None or quantity <= 0:
        reasons.append("approved_quantity must be positive.")
    if minimum is None or maximum is None or minimum <= 0 or maximum <= 0 or minimum > maximum:
        reasons.append("frozen notional limits are invalid.")
    if reasons:
        return reasons, updates
    api = live_client()
    mark_price, mark_source = fetch_live_mark_price(api, symbol)
    if mark_price is None or mark_source is None:
        reasons.append("live mark price unavailable from Binance Futures Demo premiumIndex.")
        return reasons, updates
    try:
        exchange_info = api.get_exchange_info()
    except BinanceFuturesTestnetClientError:
        reasons.append("exchange filters unavailable from Binance Futures Demo exchangeInfo.")
        return reasons, updates
    symbol_info = symbol_exchange_item(exchange_info, symbol)
    if not symbol_info:
        reasons.append("exchange filters unavailable for symbol.")
        return reasons, updates
    assert quantity is not None and minimum is not None and maximum is not None
    estimated = quantity * mark_price
    min_passed = estimated >= minimum
    max_passed = estimated <= maximum
    policy_passed = min_passed and max_passed
    q_passed, q_reason = quantity_filter_status(quantity, symbol_info, order_type)
    reason = None
    if not min_passed:
        reason = MIN_NOTIONAL_BLOCKED_REASON
    elif not max_passed:
        reason = "estimated_notional_usdt exceeds TESTNET_MAX_NOTIONAL_USDT."
    if not q_passed:
        reasons.append(q_reason)
    if not policy_passed:
        reasons.append(str(reason))
    updates.update(
        {
            "live_mark_price_at_approval": decimal_to_str(mark_price),
            "estimated_notional_at_approval": decimal_to_str(estimated),
            "live_estimated_notional_at_approval": decimal_to_str(estimated),
            "minimum_notional_passed": min_passed,
            "maximum_notional_passed": max_passed,
            "notional_policy_passed": policy_passed,
            "notional_policy_reason": reason,
            "quantity_filter_passed": q_passed,
            "quantity_filter_reason": q_reason,
        }
    )
    return reasons, updates


def run_executor(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    command = [
        sys.executable,
        "binance_testnet_executor.py",
        "--symbol",
        payload["symbol"],
        "--side",
        payload["side"],
        "--quantity",
        str(payload["quantity"]),
        "--order-type",
        "MARKET",
        "--order-test",
        "--send",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    executor_result = read_json("logs/binance_testnet_executor_result.json") or {}
    executor_result["command"] = command
    executor_result["stdout"] = completed.stdout[-4000:]
    executor_result["stderr"] = completed.stderr[-4000:]
    return completed.returncode, redact(executor_result)


def approve(args: argparse.Namespace) -> int:
    request = read_json(REQUEST_PATH)
    payload = request.get("approval_payload") if isinstance(request, dict) and isinstance(request.get("approval_payload"), dict) else None
    result = base_result(MODE_APPROVAL, request, payload)
    result["confirmed_sha256"] = args.confirm_sha256
    result["order_test"] = bool(args.order_test)

    reasons: List[str] = []
    if not args.order_test:
        reasons.append("--order-test is required.")
    if not args.send:
        reasons.append("--send is required.")
    reasons.extend(safety_reasons(require_manual=True, require_testnet_order=True))
    if request is None:
        reasons.append("approval request is missing.")
    elif payload is None:
        reasons.append("approval payload is missing.")
    else:
        expired = request_is_expired(request)
        used = request_used(request)
        stored_sha = str(request.get("payload_sha256") or "")
        computed_sha = payload_sha256(payload)
        matches = stored_sha == args.confirm_sha256 == computed_sha
        bridge_matches = bridge_matches_payload(payload)
        result.update(
            {
                "request_expired": expired,
                "request_used": used,
                "payload_sha256": stored_sha,
                "payload_sha256_matches": matches,
                "bridge_payload_matches": bridge_matches,
            }
        )
        if request.get("request_id") != args.approve:
            reasons.append("request ID mismatch.")
        if not matches:
            reasons.append("payload SHA256 mismatch.")
        if expired:
            reasons.append("request expired.")
        if used:
            reasons.append("request already used.")
        if not bridge_matches:
            reasons.append("bridge payload changed or is no longer safe.")
        bridge_reasons, _ = validate_bridge_safety(read_json(BRIDGE_RESULT_PATH))
        reasons.extend(bridge_reasons)
        revalidation_reasons, revalidation_updates = approval_revalidation(payload)
        result.update(revalidation_updates)
        reasons.extend(revalidation_reasons)

    result["safety_passed"] = not safety_reasons(require_manual=True, require_testnet_order=True)
    if reasons:
        result["blocked_reason"] = "; ".join(dict.fromkeys(reasons))
        event = "approval_blocked"
        if result.get("request_used"):
            event = "replay_attempt"
        elif result.get("request_expired"):
            event = "expired_request_attempt"
        elif result.get("execution_halt_active"):
            event = "halt_switch_block"
        finish_result(result, event)
        print(f"APPROVAL BLOCKED: {result['blocked_reason']}")
        print("NO BINANCE CALL")
        return 1

    assert payload is not None
    return_code, executor_result = run_executor(payload)
    result["executor_return_code"] = return_code
    result["executor_result_redacted"] = executor_result
    result["order_attempted"] = bool(executor_result.get("order_attempted"))
    result["order_success"] = bool(executor_result.get("order_success"))
    executor_order_test = bool(executor_result.get("order_test"))

    if return_code != 0 or not executor_order_test or not result["order_attempted"] or not result["order_success"]:
        result["blocked_reason"] = "executor order-test failed or did not report order-test success."
        finish_result(result, "approval_executor_failed")
        print("APPROVAL FAILED: executor order-test failed")
        return 1

    mark_used(str(request["request_id"]))
    request["used"] = True
    request["used_at"] = utc_now()
    write_json(REQUEST_PATH, request)
    result.update(
        {
            "status": "ORDER_TEST_SENT",
            "request_used": True,
            "approval_passed": True,
            "safety_passed": True,
            "order_test": True,
            "order_attempted": True,
            "order_success": True,
            "position_opened": False,
            "actual_order_enabled": False,
            "blocked_reason": None,
        }
    )
    finish_result(result, "successful_order_test_approval")
    print("APPROVAL ORDER-TEST SENT")
    print("Actual order disabled; position_opened=false")
    return 0


def status() -> int:
    request = read_json(REQUEST_PATH)
    result = base_result(MODE_STATUS, request, request.get("approval_payload") if request else None)
    result["status"] = "STATUS"
    if request:
        result["request_expired"] = request_is_expired(request)
        result["request_used"] = request_used(request)
    result["blocked_reason"] = None
    write_json(RESULT_PATH, result)

    print(f"request_available={bool(request)}")
    print(f"request_id={request.get('request_id') if request else None}")
    print(f"expires_at={request.get('expires_at') if request else None}")
    print(f"expired={request_is_expired(request) if request else None}")
    print(f"used={request_used(request) if request else None}")
    print(f"halt_active={execution_halt_active()}")
    print("current_safety_posture:")
    print(json.dumps(safety_posture(), indent=2, sort_keys=True))
    print("no secrets displayed")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual one-time Binance Futures Demo/Testnet approval gate.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--approve")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--confirm-sha256")
    parser.add_argument("--order-test", action="store_true")
    parser.add_argument("--send", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    args = parse_args()
    if args.prepare:
        return prepare()
    if args.status:
        return status()
    if not args.confirm_sha256:
        request = read_json(REQUEST_PATH)
        result = base_result(MODE_APPROVAL, request, request.get("approval_payload") if request else None)
        result["blocked_reason"] = "--confirm-sha256 is required."
        finish_result(result, "approval_blocked")
        print("APPROVAL BLOCKED: --confirm-sha256 is required.")
        return 1
    return approve(args)


if __name__ == "__main__":
    raise SystemExit(main())
