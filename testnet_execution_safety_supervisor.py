"""Phase 2.96 read-only Binance Futures Demo/Testnet execution safety supervisor.

This supervisor intentionally performs only read-only checks. It never places,
tests, cancels, or mutates an order, never changes account settings, and never
marks manual approval requests as used.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from binance_futures_testnet_client import (
    DEMO_FUTURES_BASE_URL,
    BinanceFuturesTestnetClient,
    BinanceFuturesTestnetClientError,
    assert_testnet_base_url,
    load_dotenv_file,
)
from binance_testnet_executor import BROKER_MODE_REQUIRED, DEFAULT_DAILY_ORDER_LIMIT

BRIDGE_RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
APPROVAL_REQUEST_PATH = "logs/manual_testnet_approval_request.json"
APPROVAL_STATE_PATH = "logs/manual_testnet_approval_state.json"
ORDERS_LOG_PATH = "logs/binance_testnet_orders.jsonl"
RESULT_PATH = "logs/testnet_execution_safety_supervisor_result.json"
AUDIT_PATH = "logs/testnet_execution_safety_supervisor_audit.jsonl"
TELEGRAM_PREVIEW_PATH = "logs/testnet_execution_safety_supervisor_telegram_preview.json"
HALT_FILE_PATH = "runtime/TESTNET_EXECUTION_HALT"

DEFAULT_MAX_OPEN_POSITIONS = 1
DEFAULT_MAX_TOTAL_EXPOSURE_USDT = 25.0
DEFAULT_MAX_OPEN_ORDERS_BEFORE_ENTRY = 0
DEFAULT_DUPLICATE_COOLDOWN_MINUTES = 30
DEFAULT_MIN_NOTIONAL_USDT = 20.0
DEFAULT_MAX_NOTIONAL_USDT = 25.0

READ_ONLY_ENDPOINT_ALLOWLIST = {
    "/fapi/v1/time",
    "/fapi/v2/account",
    "/fapi/v2/positionRisk",
    "/fapi/v1/openOrders",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/exchangeInfo",
}
SECRET_KEY_FRAGMENTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "SIGNATURE", "CHAT_ID")


def utc_now_dt() -> datetime:
    override = os.getenv("TESTNET_SUPERVISOR_NOW")
    if override:
        parsed = parse_time(override)
        if parsed is not None:
            return parsed
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
        json.dump(redact(payload), output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(redact(payload), sort_keys=True) + "\n")


def redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            if str(key) == "dedupe_key":
                out[key] = value
            elif str(key) == "payload_sha256":
                out[key] = short_hash(value)
            elif any(fragment in str(key).upper() for fragment in SECRET_KEY_FRAGMENTS):
                out[key] = "REDACTED"
            else:
                out[key] = redact(value)
        return out
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 12) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)[:length]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_false_or_unset(name: str) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return True
    return value.strip().lower() in {"0", "false", "no", "n", "off"}


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


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def decimal_text(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def is_nonzero_position(item: Dict[str, Any]) -> bool:
    amount = parse_decimal(item.get("positionAmt"))
    return amount is not None and amount != 0


def client() -> BinanceFuturesTestnetClient:
    return BinanceFuturesTestnetClient(base_url=DEMO_FUTURES_BASE_URL)


def normalize_base_url() -> str:
    return os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL).strip().rstrip("/")


def safe_request_id(request: Optional[Dict[str, Any]]) -> Optional[str]:
    return short_hash(request.get("request_id"), 8) if request else None


def build_base_result(mode: str, symbol: Optional[str]) -> Dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "mode": mode,
        "status": "BLOCKED",
        "read_only": True,
        "execution_permitted": False,
        "manual_execution_required": mode == "preflight",
        "order_attempted": False,
        "order_success": False,
        "base_url": normalize_base_url(),
        "broker_mode": os.getenv("BROKER_MODE", ""),
        "real_binance_enabled": env_bool("REAL_BINANCE_ENABLED", False),
        "allow_real_binance_order": env_bool("ALLOW_REAL_BINANCE_ORDER", False),
        "allow_auto_testnet_order": env_bool("ALLOW_AUTO_TESTNET_ORDER", False),
        "allow_testnet_order": env_bool("ALLOW_TESTNET_ORDER", False),
        "allow_manual_testnet_approval": env_bool("ALLOW_MANUAL_TESTNET_APPROVAL", False),
        "execution_halt_env": env_bool("TESTNET_EXECUTION_HALT", False),
        "execution_halt_file": os.path.exists(HALT_FILE_PATH),
        "execution_halt_active": False,
        "account_read_passed": False,
        "can_trade": None,
        "wallet_balance": None,
        "available_balance": None,
        "open_position_count": 0,
        "max_open_positions": env_int("TESTNET_MAX_OPEN_POSITIONS", DEFAULT_MAX_OPEN_POSITIONS),
        "position_limit_passed": False,
        "existing_symbol_position_amt": None,
        "existing_symbol_position_notional": None,
        "current_total_exposure_usdt": 0.0,
        "proposed_notional_usdt": 0.0,
        "projected_total_exposure_usdt": 0.0,
        "max_total_exposure_usdt": env_float("TESTNET_MAX_TOTAL_EXPOSURE_USDT", DEFAULT_MAX_TOTAL_EXPOSURE_USDT),
        "exposure_limit_passed": False,
        "open_order_count": 0,
        "symbol_open_order_count": 0,
        "max_open_orders_before_entry": env_int("TESTNET_MAX_OPEN_ORDERS_BEFORE_ENTRY", DEFAULT_MAX_OPEN_ORDERS_BEFORE_ENTRY),
        "open_order_guard_passed": False,
        "daily_actual_order_count": 0,
        "daily_order_limit": env_int("TESTNET_MAX_ORDERS_PER_DAY", DEFAULT_DAILY_ORDER_LIMIT),
        "daily_limit_passed": False,
        "daily_limit_reason": None,
        "request_available": False,
        "request_id_present": False,
        "request_id_short": None,
        "request_expired": None,
        "request_used": None,
        "request_integrity_passed": False,
        "payload_sha256_matches": False,
        "bridge_payload_matches": False,
        "symbol": symbol.upper() if symbol else None,
        "side": None,
        "approved_quantity": None,
        "live_mark_price": None,
        "live_proposed_notional_usdt": None,
        "min_notional_usdt": env_float("TESTNET_MIN_NOTIONAL_USDT", DEFAULT_MIN_NOTIONAL_USDT),
        "max_notional_usdt": env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL_USDT),
        "minimum_notional_passed": False,
        "maximum_notional_passed": False,
        "notional_policy_passed": False,
        "quantity_filter_passed": False,
        "dedupe_key": None,
        "dedupe_key_short": None,
        "duplicate_detected": False,
        "duplicate_reason": None,
        "duplicate_guard_passed": False,
        "blocked_reasons": [],
        "next_action": None,
    }


def environment_reasons(result: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    try:
        base_url = assert_testnet_base_url(result["base_url"])
        result["base_url"] = base_url
    except BinanceFuturesTestnetClientError as exc:
        reasons.append(str(exc))
    if result["base_url"] != DEMO_FUTURES_BASE_URL:
        reasons.append(f"base URL must be exactly {DEMO_FUTURES_BASE_URL}.")
    if result["broker_mode"] != BROKER_MODE_REQUIRED:
        reasons.append(f"BROKER_MODE must be {BROKER_MODE_REQUIRED}.")
    for gate in [
        ("real_binance_enabled", "REAL_BINANCE_ENABLED must be false."),
        ("allow_real_binance_order", "ALLOW_REAL_BINANCE_ORDER must be false."),
        ("allow_auto_testnet_order", "ALLOW_AUTO_TESTNET_ORDER must be false or unset."),
        ("allow_testnet_order", "ALLOW_TESTNET_ORDER must remain false or unset for supervisor."),
        ("allow_manual_testnet_approval", "ALLOW_MANUAL_TESTNET_APPROVAL must remain false or unset for supervisor."),
    ]:
        if result[gate[0]]:
            reasons.append(gate[1])
    if not env_false_or_unset("ALLOW_AUTO_TESTNET_ORDER"):
        reasons.append("ALLOW_AUTO_TESTNET_ORDER must be false or unset.")
    if not env_false_or_unset("ALLOW_TESTNET_ORDER"):
        reasons.append("ALLOW_TESTNET_ORDER must remain false or unset for supervisor.")
    if not env_false_or_unset("ALLOW_MANUAL_TESTNET_APPROVAL"):
        reasons.append("ALLOW_MANUAL_TESTNET_APPROVAL must remain false or unset for supervisor.")
    result["execution_halt_active"] = bool(result["execution_halt_env"] or result["execution_halt_file"])
    if result["execution_halt_env"]:
        reasons.append("TESTNET_EXECUTION_HALT=true is active.")
    if result["execution_halt_file"]:
        reasons.append(f"halt file exists: {HALT_FILE_PATH}.")
    return sorted(set(reasons), key=reasons.index)


def load_positions(api: BinanceFuturesTestnetClient) -> List[Dict[str, Any]]:
    payload = api.get_position_risk()
    return payload if isinstance(payload, list) else []


def load_open_orders(api: BinanceFuturesTestnetClient, symbol: Optional[str]) -> List[Dict[str, Any]]:
    payload = api.get_open_orders(symbol=None)
    return payload if isinstance(payload, list) else []


def account_checks(result: Dict[str, Any], api: BinanceFuturesTestnetClient) -> List[str]:
    try:
        account = api.get_account()
    except BinanceFuturesTestnetClientError as exc:
        return [f"account request failed: {exc}"]
    result["account_read_passed"] = True
    result["can_trade"] = bool(account.get("canTrade"))
    result["wallet_balance"] = account.get("totalWalletBalance")
    result["available_balance"] = account.get("availableBalance")
    return [] if result["can_trade"] is True else ["account canTrade is not true."]


def mark_price(api: BinanceFuturesTestnetClient, symbol: str) -> Optional[Decimal]:
    try:
        return parse_decimal(api.get_mark_price(symbol))
    except BinanceFuturesTestnetClientError:
        return None


def position_and_exposure_checks(
    result: Dict[str, Any], api: BinanceFuturesTestnetClient, positions: List[Dict[str, Any]], symbol: Optional[str], proposed_notional: Decimal
) -> List[str]:
    reasons: List[str] = []
    open_positions = [item for item in positions if is_nonzero_position(item)]
    result["open_position_count"] = len(open_positions)
    current_exposure = Decimal("0")
    existing_symbol_amt = Decimal("0")
    existing_symbol_notional = Decimal("0")
    for item in open_positions:
        pos_symbol = str(item.get("symbol") or "").upper()
        amount = parse_decimal(item.get("positionAmt")) or Decimal("0")
        price = parse_decimal(item.get("markPrice")) or (mark_price(api, pos_symbol) if pos_symbol else None) or Decimal("0")
        notional = abs(amount * price)
        current_exposure += notional
        if symbol and pos_symbol == symbol.upper():
            existing_symbol_amt = amount
            existing_symbol_notional = notional
    result["existing_symbol_position_amt"] = decimal_text(existing_symbol_amt) if symbol else None
    result["existing_symbol_position_notional"] = decimal_float(existing_symbol_notional) if symbol else None
    result["current_total_exposure_usdt"] = decimal_float(current_exposure) or 0.0
    result["proposed_notional_usdt"] = decimal_float(proposed_notional) or 0.0
    projected = current_exposure + proposed_notional
    result["projected_total_exposure_usdt"] = decimal_float(projected) or 0.0
    max_positions = int(result["max_open_positions"])
    max_exposure = Decimal(str(result["max_total_exposure_usdt"]))
    if symbol and existing_symbol_amt != 0:
        reasons.append(f"existing non-zero position for {symbol.upper()}.")
    if proposed_notional > 0 and len(open_positions) != 0:
        reasons.append("position limit requires zero current open positions before a proposed entry.")
    if proposed_notional > 0 and len(open_positions) + 1 > max_positions:
        reasons.append(f"opening proposed entry would exceed TESTNET_MAX_OPEN_POSITIONS={max_positions}.")
    result["position_limit_passed"] = not any("position" in reason.lower() for reason in reasons)
    if projected > max_exposure:
        reasons.append(f"projected exposure {decimal_text(projected)} exceeds TESTNET_MAX_TOTAL_EXPOSURE_USDT={max_exposure}.")
    result["exposure_limit_passed"] = projected <= max_exposure
    if proposed_notional == 0:
        result["position_limit_passed"] = len(open_positions) <= max_positions
        result["exposure_limit_passed"] = current_exposure <= max_exposure
    return reasons


def open_order_checks(result: Dict[str, Any], open_orders: List[Dict[str, Any]], symbol: Optional[str], proposed: bool) -> List[str]:
    reasons: List[str] = []
    symbol_orders = [o for o in open_orders if symbol and str(o.get("symbol") or "").upper() == symbol.upper()]
    result["open_order_count"] = len(open_orders)
    result["symbol_open_order_count"] = len(symbol_orders)
    limit = int(result["max_open_orders_before_entry"])
    if proposed and symbol_orders:
        reasons.append(f"existing open Binance order for {symbol.upper()}.")
    if proposed and len(open_orders) > limit:
        reasons.append(f"open order count {len(open_orders)} exceeds TESTNET_MAX_OPEN_ORDERS_BEFORE_ENTRY={limit}.")
    result["open_order_guard_passed"] = not reasons if proposed else len(open_orders) <= limit
    return reasons


def today_actual_order_count(path: str) -> int:
    today = utc_now_dt().date().isoformat()
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


def daily_checks(result: Dict[str, Any], orders_log_path: str) -> List[str]:
    count = today_actual_order_count(orders_log_path)
    limit = int(result["daily_order_limit"])
    result["daily_actual_order_count"] = count
    result["daily_limit_passed"] = count < limit
    if result["daily_limit_passed"]:
        result["daily_limit_reason"] = f"{count} actual successful orders today; limit {limit}; actual order allowed."
        return []
    result["daily_limit_reason"] = f"{count} actual successful orders today; limit {limit}; actual order blocked."
    return [result["daily_limit_reason"]]


def request_used(request: Optional[Dict[str, Any]], state_path: str) -> bool:
    if not request:
        return False
    if bool(request.get("used")):
        return True
    state = read_json(state_path) or {}
    return str(request.get("request_id") or "") in {str(item) for item in state.get("used_request_ids", []) if item}


def bridge_signal_metadata(bridge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": str(bridge.get("symbol") or "").upper(),
        "side": str(bridge.get("side") or "").upper(),
        "quantity": str(bridge.get("quantity") or ""),
        "estimated_notional_usdt": bridge.get("estimated_notional_usdt"),
        "signal_score": bridge.get("signal_score"),
        "overlay_decision": bridge.get("overlay_decision"),
        "trade_rank": bridge.get("trade_rank"),
        "suggested_risk": bridge.get("suggested_risk"),
        "overlay_report_path": bridge.get("overlay_report_path"),
    }


def approval_checks(
    result: Dict[str, Any], request_path: str, state_path: str, bridge_path: str, cli_symbol: Optional[str]
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    reasons: List[str] = []
    request = read_json(request_path)
    bridge = read_json(bridge_path)
    result["request_available"] = request is not None
    if request is None:
        return [f"approval request not readable: {request_path}"], None
    result["request_id_present"] = bool(request.get("request_id"))
    result["request_id_short"] = safe_request_id(request)
    payload = request.get("approval_payload") if isinstance(request.get("approval_payload"), dict) else None
    if not result["request_id_present"]:
        reasons.append("approval request_id missing.")
    expired = parse_time(request.get("expires_at")) is None or utc_now_dt() > (parse_time(request.get("expires_at")) or utc_now_dt())
    used = request_used(request, state_path)
    result["request_expired"] = expired
    result["request_used"] = used
    if expired:
        reasons.append("approval request is expired.")
    if used:
        reasons.append("approval request is already used.")
    if payload is None:
        reasons.append("approval_payload missing or invalid.")
        return reasons, None
    digest = request.get("payload_sha256")
    result["payload_sha256_matches"] = bool(digest and digest == payload_sha256(payload))
    if not result["payload_sha256_matches"]:
        reasons.append("approval payload SHA256 does not match.")
    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("side") or "").upper()
    quantity = parse_decimal(payload.get("approved_quantity") or payload.get("quantity"))
    order_type = str(payload.get("order_type") or "").upper()
    result["symbol"] = symbol or result["symbol"]
    result["side"] = side or None
    result["approved_quantity"] = decimal_text(quantity)
    if cli_symbol and symbol != cli_symbol.upper():
        reasons.append(f"approval request symbol {symbol} does not match CLI symbol {cli_symbol.upper()}.")
    if quantity is None or quantity <= 0:
        reasons.append("approved quantity must be positive.")
    if order_type != "MARKET":
        reasons.append("order type must be MARKET.")
    if side not in {"BUY", "SELL"}:
        reasons.append("side must be BUY or SELL.")
    proposed = request.get("proposed_order_test_payload") if isinstance(request.get("proposed_order_test_payload"), dict) else {}
    if str(proposed.get("base_url") or DEMO_FUTURES_BASE_URL).rstrip("/") != DEMO_FUTURES_BASE_URL:
        reasons.append("request base URL must be Binance Futures Demo/Testnet only.")
    bridge_required = {
        "status": "WOULD_ORDER",
        "safety_passed": True,
        "policy_passed": True,
        "order_attempted": False,
        "order_success": False,
        "real_binance_enabled": False,
        "allow_auto_testnet_order": False,
    }
    if not isinstance(bridge, dict):
        reasons.append(f"bridge result not readable: {bridge_path}")
    else:
        for key, expected in bridge_required.items():
            if bridge.get(key) != expected:
                reasons.append(f"bridge {key} must be {expected!r}.")
        result["bridge_payload_matches"] = canonical_json(bridge_signal_metadata(bridge)) == canonical_json(payload.get("bridge_signal_metadata") or {})
        if not result["bridge_payload_matches"]:
            reasons.append("bridge signal identity does not match approval payload.")
    result["request_integrity_passed"] = not reasons
    return reasons, payload


def active_quantity_filter(symbol_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    filters = symbol_info.get("filters") or []
    market = next((f for f in filters if f.get("filterType") == "MARKET_LOT_SIZE"), None)
    lot = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), None)
    return market or lot


def quantity_filter_passed(quantity: Optional[Decimal], symbol_info: Dict[str, Any]) -> bool:
    if quantity is None or quantity <= 0:
        return False
    filt = active_quantity_filter(symbol_info)
    if not filt:
        return False
    min_qty = parse_decimal(filt.get("minQty")) or Decimal("0")
    max_qty = parse_decimal(filt.get("maxQty")) or Decimal("0")
    step = parse_decimal(filt.get("stepSize")) or Decimal("0")
    if quantity < min_qty or (max_qty > 0 and quantity > max_qty):
        return False
    if step > 0 and ((quantity - min_qty) % step) != 0:
        return False
    return True


def live_notional_checks(result: Dict[str, Any], api: BinanceFuturesTestnetClient, payload: Dict[str, Any]) -> Tuple[List[str], Decimal]:
    reasons: List[str] = []
    symbol = str(payload.get("symbol") or result.get("symbol") or "").upper()
    quantity = parse_decimal(payload.get("approved_quantity") or payload.get("quantity"))
    price = mark_price(api, symbol) if symbol else None
    result["live_mark_price"] = decimal_float(price)
    proposed = Decimal("0")
    if price is None:
        reasons.append("live mark price unavailable from Binance Futures Demo premiumIndex.")
    elif quantity is not None:
        proposed = price * quantity
    result["live_proposed_notional_usdt"] = decimal_float(proposed)
    result["proposed_notional_usdt"] = decimal_float(proposed) or 0.0
    minimum = Decimal(str(result["min_notional_usdt"]))
    maximum = Decimal(str(result["max_notional_usdt"]))
    result["minimum_notional_passed"] = proposed >= minimum if proposed else False
    result["maximum_notional_passed"] = proposed <= maximum if proposed else False
    result["notional_policy_passed"] = bool(result["minimum_notional_passed"] and result["maximum_notional_passed"])
    if not result["minimum_notional_passed"]:
        reasons.append("live proposed notional is below TESTNET_MIN_NOTIONAL_USDT.")
    if proposed and not result["maximum_notional_passed"]:
        reasons.append("live proposed notional exceeds TESTNET_MAX_NOTIONAL_USDT.")
    try:
        exchange_info = api.get_exchange_info()
        symbol_info = next((s for s in exchange_info.get("symbols", []) if str(s.get("symbol") or "").upper() == symbol), None)
    except BinanceFuturesTestnetClientError:
        symbol_info = None
    result["quantity_filter_passed"] = bool(symbol_info and quantity_filter_passed(quantity, symbol_info))
    if not result["quantity_filter_passed"]:
        reasons.append("approved quantity does not pass current MARKET_LOT_SIZE/LOT_SIZE filter.")
    return reasons, proposed


def make_dedupe_key(payload: Dict[str, Any]) -> str:
    identity = payload.get("bridge_signal_metadata") if isinstance(payload.get("bridge_signal_metadata"), dict) else {}
    raw = "|".join(
        [
            str(payload.get("symbol") or "").upper(),
            str(payload.get("side") or "").upper(),
            str(payload.get("approved_quantity") or payload.get("quantity") or ""),
            canonical_json(identity),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def duplicate_checks(
    result: Dict[str, Any], payload: Dict[str, Any], request_path: str, orders_log_path: str, open_orders: List[Dict[str, Any]], positions: List[Dict[str, Any]]
) -> List[str]:
    reasons: List[str] = []
    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("side") or "").upper()
    qty = str(payload.get("approved_quantity") or payload.get("quantity") or "")
    dedupe = make_dedupe_key(payload)
    result["dedupe_key"] = dedupe
    result["dedupe_key_short"] = short_hash(dedupe)
    for item in positions:
        if str(item.get("symbol") or "").upper() == symbol and is_nonzero_position(item):
            reasons.append(f"existing non-zero position for {symbol}.")
            break
    if any(str(o.get("symbol") or "").upper() == symbol for o in open_orders):
        reasons.append(f"existing open Binance order for {symbol}.")
    current = read_json(request_path) or {}
    current_id = str(current.get("request_id") or "")
    current_payload = current.get("approval_payload") if isinstance(current.get("approval_payload"), dict) else {}
    if (
        current_id
        and current_id != str(payload.get("request_id") or "")
        and not bool(current.get("used"))
        and parse_time(current.get("expires_at")) is not None
        and utc_now_dt() <= (parse_time(current.get("expires_at")) or utc_now_dt())
        and str(current_payload.get("symbol") or "").upper() == symbol
        and str(current_payload.get("side") or "").upper() == side
        and str(current_payload.get("approved_quantity") or current_payload.get("quantity") or "") == qty
    ):
        reasons.append("unused active approval request already exists for same symbol, side, and quantity.")
    cooldown = timedelta(minutes=env_int("TESTNET_DUPLICATE_COOLDOWN_MINUTES", DEFAULT_DUPLICATE_COOLDOWN_MINUTES))
    cutoff = utc_now_dt() - cooldown
    if os.path.exists(orders_log_path):
        with open(orders_log_path, "r", encoding="utf-8") as jsonl_file:
            for line in jsonl_file:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not (
                    item.get("mode") == "actual_order"
                    and item.get("order_success") is True
                    and item.get("order_test") is False
                    and item.get("dry_run") is False
                ):
                    continue
                generated = parse_time(item.get("generated_at"))
                if generated is None or generated < cutoff:
                    continue
                item_key = item.get("dedupe_key") or make_dedupe_key(
                    {
                        "symbol": item.get("symbol"),
                        "side": item.get("side"),
                        "approved_quantity": item.get("approved_quantity") or item.get("quantity"),
                        "bridge_signal_metadata": item.get("bridge_signal_metadata") or {},
                    }
                )
                if item_key == dedupe:
                    reasons.append("successful actual-order log with same dedupe key is within cooldown.")
                    break
    result["duplicate_detected"] = bool(reasons)
    result["duplicate_reason"] = "; ".join(reasons) if reasons else None
    result["duplicate_guard_passed"] = not reasons
    return reasons


def finish(result: Dict[str, Any]) -> Dict[str, Any]:
    if result["blocked_reasons"]:
        result["status"] = "BLOCKED"
        result["next_action"] = "Resolve all blocked_reasons; no order sent."
    elif result["mode"] == "preflight":
        result["status"] = "READY_FOR_MANUAL_DUMMY_ORDER"
        result["manual_execution_required"] = True
        result["next_action"] = "Proposal passed read-only safety supervisor; use a separately designed manual actual-dummy-order phase."
    else:
        if result["open_position_count"] == 0 and result["open_order_count"] == 0:
            result["status"] = "SAFE_IDLE"
            result["next_action"] = "Remain idle; execution gates stay disabled."
        else:
            result["status"] = "BLOCKED"
            result["blocked_reasons"].append("status mode requires no positions and no open orders for SAFE_IDLE.")
            result["next_action"] = "Review existing testnet exposure/orders; no order sent."
    result["execution_permitted"] = False
    result["order_attempted"] = False
    result["order_success"] = False
    write_json(RESULT_PATH, result)
    audit = dict(result)
    audit["event"] = "testnet_execution_safety_supervisor"
    append_jsonl(AUDIT_PATH, audit)
    return result


def build_telegram_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    status_text = result["status"].replace("_", " ")
    approval_text = "VALID" if result.get("request_integrity_passed") else "INVALID"
    duplicate_text = "PASS" if result.get("duplicate_guard_passed") else "BLOCKED"
    halt_text = "ON" if result.get("execution_halt_active") else "OFF"
    text = "\n".join(
        [
            "🛡 TESTNET EXECUTION SAFETY SUPERVISOR",
            "",
            f"Status: {status_text}",
            f"Symbol: {result.get('symbol') or 'N/A'}",
            f"Side: {result.get('side') or 'N/A'}",
            f"Quantity: {result.get('approved_quantity') or 'N/A'}",
            f"Live Notional: {result.get('live_proposed_notional_usdt') if result.get('live_proposed_notional_usdt') is not None else 'N/A'} USDT",
            "",
            f"Positions: {result.get('open_position_count')} / {result.get('max_open_positions')}",
            f"Open Orders: {result.get('open_order_count')}",
            f"Exposure: {result.get('current_total_exposure_usdt'):.2f} → {result.get('projected_total_exposure_usdt'):.2f} / {result.get('max_total_exposure_usdt'):.2f} USDT",
            f"Daily Actual Orders: {result.get('daily_actual_order_count')} / {result.get('daily_order_limit')}",
            f"HALT: {halt_text}",
            f"Duplicate Guard: {duplicate_text}",
            f"Approval Request: {approval_text}",
            "",
            "Read-only supervisor.",
            "No order sent.",
            "Manual execution still required.",
            "Real Binance OFF.",
        ]
    )
    preview = {"generated_at": result["generated_at"], "status": result["status"], "payload_text": text, "send_attempted": False}
    write_json(TELEGRAM_PREVIEW_PATH, preview)
    return preview


def run(args: argparse.Namespace) -> Dict[str, Any]:
    mode = "preflight" if args.preflight else "status"
    symbol = args.symbol.upper() if args.symbol else None
    result = build_base_result(mode, symbol)
    blocked: List[str] = []
    blocked.extend(environment_reasons(result))
    proposed_payload: Optional[Dict[str, Any]] = None
    proposed_notional = Decimal("0")

    api = client()
    blocked.extend(account_checks(result, api))
    positions: List[Dict[str, Any]] = []
    open_orders: List[Dict[str, Any]] = []
    try:
        positions = load_positions(api)
    except BinanceFuturesTestnetClientError as exc:
        blocked.append(f"positionRisk request failed: {exc}")
    try:
        open_orders = load_open_orders(api, symbol)
    except BinanceFuturesTestnetClientError as exc:
        blocked.append(f"openOrders request failed: {exc}")

    if args.preflight:
        approval_reasons, proposed_payload = approval_checks(
            result, args.approval_request_path, APPROVAL_STATE_PATH, args.bridge_result_path, symbol
        )
        blocked.extend(approval_reasons)
        if proposed_payload is not None:
            notional_reasons, proposed_notional = live_notional_checks(result, api, proposed_payload)
            blocked.extend(notional_reasons)
    else:
        result["request_integrity_passed"] = False
        result["payload_sha256_matches"] = False

    blocked.extend(position_and_exposure_checks(result, api, positions, result.get("symbol"), proposed_notional))
    result["projected_total_exposure_usdt"] = (result["current_total_exposure_usdt"] or 0.0) + (result["proposed_notional_usdt"] or 0.0)
    blocked.extend(open_order_checks(result, open_orders, result.get("symbol"), args.preflight))
    blocked.extend(daily_checks(result, args.orders_log_path))
    if args.preflight and proposed_payload is not None:
        proposed_payload = dict(proposed_payload)
        # Local-only metadata for duplicate checking; never mutate request files.
        proposed_payload["request_id"] = (read_json(args.approval_request_path) or {}).get("request_id")
        blocked.extend(duplicate_checks(result, proposed_payload, args.approval_request_path, args.orders_log_path, open_orders, positions))
    elif not args.preflight:
        result["duplicate_guard_passed"] = True

    result["blocked_reasons"] = sorted(set(blocked), key=blocked.index)
    final = finish(result)
    if args.telegram_preview:
        build_telegram_preview(final)
    print(f"TESTNET_EXECUTION_SAFETY_SUPERVISOR: {final['status']}")
    if final["blocked_reasons"]:
        for reason in final["blocked_reasons"]:
            print(f"BLOCKED: {reason}")
    print("READ_ONLY=true ORDER_SENT=false")
    return final


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Binance Futures Demo/Testnet execution safety supervisor.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--preflight", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--telegram-preview", action="store_true")
    parser.add_argument("--bridge-result-path", default=BRIDGE_RESULT_PATH)
    parser.add_argument("--approval-request-path", default=APPROVAL_REQUEST_PATH)
    parser.add_argument("--orders-log-path", default=ORDERS_LOG_PATH)
    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    try:
        result = run(parse_args())
    except BinanceFuturesTestnetClientError as exc:
        result = build_base_result("error", None)
        result["blocked_reasons"] = [str(exc)]
        finish(result)
        print(f"TESTNET_EXECUTION_SAFETY_SUPERVISOR: BLOCKED\nBLOCKED: {exc}\nREAD_ONLY=true ORDER_SENT=false")
    return 0 if result.get("status") in {"SAFE_IDLE", "READY_FOR_MANUAL_DUMMY_ORDER"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
