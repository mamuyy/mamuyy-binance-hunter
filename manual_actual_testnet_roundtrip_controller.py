"""Phase 2.97B1 manual actual Binance Futures Demo roundtrip controller.

Execution-capable, but deliberately gated for manual Binance Futures Demo only.
This controller never calls Binance order endpoints directly and never invokes
/order/test. It prepares immutable actual-testnet-only plans, then uses the
existing executor in a controlled subprocess only after explicit manual gates.
All tests for this phase mock subprocesses and read-only Binance responses.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from actual_testnet_roundtrip_state import (
    BLOCKED,
    CLOSE_FAILED,
    COMPLETED,
    EMERGENCY_CLOSE_INTENT_RECORDED,
    EMERGENCY_CLOSE_SENT,
    EMERGENCY_FLAT_VERIFIED,
    EMERGENCY_MANUAL_ACTION_REQUIRED,
    ENTRY_CONFIRMED,
    ENTRY_FAILED,
    ENTRY_INTENT_RECORDED,
    ENTRY_SENT,
    ENTRY_STATE_UNKNOWN,
    EXECUTION_LOCKED,
    FINAL_FLAT_VERIFIED,
    NO_PLAN,
    PREPARED,
    PRIMARY_CLOSE_INTENT_RECORDED,
    PRIMARY_CLOSE_SENT,
    append_jsonl,
    read_json,
    write_json,
)
from binance_futures_testnet_client import BinanceFuturesTestnetClient, BinanceFuturesTestnetClientError

DEMO_FUTURES_BASE_URL = "https://demo-fapi.binance.com"
BROKER_MODE_REQUIRED = "BINANCE_FUTURES_TESTNET_ONLY"

SUPERVISOR_RESULT_PATH = "logs/testnet_execution_safety_supervisor_result.json"
APPROVAL_REQUEST_PATH = "logs/manual_testnet_approval_request.json"
BRIDGE_RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
EXECUTOR_RESULT_PATH = "logs/binance_testnet_executor_result.json"
PLAN_PATH = "logs/manual_actual_testnet_roundtrip_plan.json"
RESULT_PATH = "logs/manual_actual_testnet_roundtrip_result.json"
STATUS_PATH = "logs/manual_actual_testnet_roundtrip_status.json"
STATE_PATH = "logs/manual_actual_testnet_roundtrip_state.json"
AUDIT_PATH = "logs/manual_actual_testnet_roundtrip_audit.jsonl"
LOCK_FILE_PATH = "runtime/MANUAL_ACTUAL_TESTNET_ROUNDTRIP.lock"
HALT_FILE_PATH = "runtime/TESTNET_EXECUTION_HALT"

ACTUAL_ROUNDTRIP_TTL_MINUTES = 10
REQUIRED_DAILY_ORDER_SLOTS = 3
MIN_NOTIONAL_USDT = Decimal("20")
MAX_NOTIONAL_USDT = Decimal("25")
MAX_POSITION_POLLS = 5
MAX_TOTAL_WAIT_SECONDS = 10
SECRET_KEY_FRAGMENTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "SIGNATURE", "CHAT_ID", "APIKEY", "API_KEY")
PRODUCTION_URL_FRAGMENTS = ("https://fapi.binance.com", "api.binance.com")


def utc_now_dt() -> datetime:
    override = os.getenv("MANUAL_ACTUAL_TESTNET_ROUNDTRIP_NOW")
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


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


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


def normalize_base_url() -> str:
    return os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL).strip().rstrip("/")


def execution_halt_active() -> bool:
    return env_bool("TESTNET_EXECUTION_HALT", False) or os.path.exists(HALT_FILE_PATH)


def activate_halt(reason: str) -> None:
    os.makedirs(os.path.dirname(HALT_FILE_PATH) or ".", exist_ok=True)
    with open(HALT_FILE_PATH, "w", encoding="utf-8") as halt_file:
        halt_file.write(f"{utc_now()} {reason}\n")


def decimal_value(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def opposite_side(side: str) -> Optional[str]:
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return None


def close_side_for_position(position_amt: Any) -> Optional[str]:
    amount = decimal_value(position_amt) or Decimal("0")
    if amount > 0:
        return "SELL"
    if amount < 0:
        return "BUY"
    return None


def redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            upper = str(key).upper()
            if any(fragment in upper for fragment in SECRET_KEY_FRAGMENTS):
                redacted[key] = "REDACTED"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    if isinstance(payload, str) and any(fragment.lower() in payload.lower() for fragment in ("api_secret", "api_key", "telegram_token")):
        return "REDACTED"
    return payload


def posture() -> Dict[str, Any]:
    return {
        "broker_mode": os.getenv("BROKER_MODE", BROKER_MODE_REQUIRED),
        "base_url": normalize_base_url(),
        "real_binance_enabled": env_bool("REAL_BINANCE_ENABLED", False),
        "allow_real_binance_order": env_bool("ALLOW_REAL_BINANCE_ORDER", False),
        "allow_auto_testnet_order": env_bool("ALLOW_AUTO_TESTNET_ORDER", False),
        "allow_testnet_order": env_bool("ALLOW_TESTNET_ORDER", False),
        "allow_manual_actual_roundtrip": env_bool("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP", False),
        "allow_manual_emergency_close": env_bool("ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE", False),
        "execution_halt_active": execution_halt_active(),
    }


def base_environment_reasons(require_testnet_order: Optional[bool] = None) -> List[str]:
    current = posture()
    reasons: List[str] = []
    if current["broker_mode"] != BROKER_MODE_REQUIRED:
        reasons.append(f"BROKER_MODE must be exactly {BROKER_MODE_REQUIRED}")
    if current["base_url"] != DEMO_FUTURES_BASE_URL:
        reasons.append(f"base URL must be exactly {DEMO_FUTURES_BASE_URL}")
    if current["base_url"] != DEMO_FUTURES_BASE_URL and any(fragment in current["base_url"] for fragment in PRODUCTION_URL_FRAGMENTS):
        reasons.append("production Binance URL is forbidden")
    if current["real_binance_enabled"]:
        reasons.append("REAL_BINANCE_ENABLED must be false")
    if current["allow_real_binance_order"]:
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be false")
    if not env_false_or_unset("ALLOW_AUTO_TESTNET_ORDER"):
        reasons.append("ALLOW_AUTO_TESTNET_ORDER must be false or unset")
    if require_testnet_order is True and not current["allow_testnet_order"]:
        reasons.append("ALLOW_TESTNET_ORDER=true is required")
    if require_testnet_order is False and current["allow_testnet_order"]:
        reasons.append("ALLOW_TESTNET_ORDER must be false during preparation")
    return reasons


def request_expired(request: Dict[str, Any]) -> bool:
    expires = parse_time(request.get("expires_at"))
    return expires is None or expires <= utc_now_dt()


def supervisor_required_reasons(supervisor: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    required = {
        "status": "READY_FOR_MANUAL_DUMMY_ORDER",
        "read_only": True,
        "execution_permitted": False,
        "manual_execution_required": True,
        "request_integrity_passed": True,
        "bridge_payload_matches": True,
        "payload_sha256_matches": True,
        "request_expired": False,
        "request_used": False,
        "position_limit_passed": True,
        "exposure_limit_passed": True,
        "open_order_guard_passed": True,
        "duplicate_guard_passed": True,
        "notional_policy_passed": True,
        "quantity_filter_passed": True,
        "execution_halt_active": False,
    }
    for key, expected in required.items():
        if supervisor.get(key) != expected:
            reasons.append(f"supervisor {key} must be {expected!r}")
    if supervisor.get("blocked_reasons") != []:
        reasons.append("supervisor blocked_reasons must be []")
    if int(supervisor.get("open_position_count") or 0) != 0:
        reasons.append("open_position_count must be 0")
    if int(supervisor.get("open_order_count") or 0) != 0:
        reasons.append("open_order_count must be 0")
    return reasons


def source_approval_reasons(request: Dict[str, Any], bridge: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not request:
        return ["source approval request missing"]
    payload = request.get("approval_payload") if isinstance(request.get("approval_payload"), dict) else {}
    if request_expired(request):
        reasons.append("source approval expired")
    if bool(request.get("used")):
        reasons.append("source approval used")
    if request.get("payload_sha256") != payload_sha256(payload):
        reasons.append("source approval payload sha256 mismatch")
    if bridge and payload.get("bridge_signal_metadata"):
        expected = {
            "bridge_status": bridge.get("status"),
            "signal_score": bridge.get("signal_score"),
            "overlay_decision": bridge.get("overlay_decision"),
            "trade_rank": bridge.get("trade_rank"),
            "suggested_risk": bridge.get("suggested_risk"),
            "source_report_path": bridge.get("overlay_report_path"),
        }
        if payload.get("bridge_signal_metadata") != expected:
            reasons.append("source approval bridge metadata mismatch")
    return reasons


def client() -> BinanceFuturesTestnetClient:
    return BinanceFuturesTestnetClient()


def position_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def get_live_snapshot(symbol: str) -> Dict[str, Any]:
    api = client()
    account = api.get_account()
    positions = position_items(api.get_position_risk())
    orders = api.get_open_orders(symbol.upper())
    mark_price = api.get_mark_price(symbol.upper())
    exchange_info = api.get_exchange_info()
    return normalize_snapshot(symbol, account, positions, orders, mark_price, exchange_info)


def normalize_snapshot(symbol: str, account: Dict[str, Any], positions: List[Dict[str, Any]], orders: Any, mark_price: Any, exchange_info: Dict[str, Any]) -> Dict[str, Any]:
    normalized_symbol = symbol.upper()
    symbol_position_amt = Decimal("0")
    other_nonzero_positions: List[str] = []
    for item in positions:
        item_symbol = str(item.get("symbol", "")).upper()
        amt = decimal_value(item.get("positionAmt")) or Decimal("0")
        if item_symbol == normalized_symbol:
            symbol_position_amt = amt
        elif amt != 0:
            other_nonzero_positions.append(item_symbol)
    open_orders = [item for item in (orders if isinstance(orders, list) else []) if isinstance(item, dict)]
    mark = decimal_value(mark_price)
    step_size = Decimal("0.001")
    min_qty = Decimal("0")
    for info in exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []:
        if str(info.get("symbol", "")).upper() != normalized_symbol:
            continue
        for flt in info.get("filters", []):
            if flt.get("filterType") == "LOT_SIZE":
                step_size = decimal_value(flt.get("stepSize")) or step_size
                min_qty = decimal_value(flt.get("minQty")) or min_qty
    return {
        "can_trade": bool(account.get("canTrade")),
        "symbol": normalized_symbol,
        "symbol_position_amt": decimal_text(symbol_position_amt),
        "symbol_position_abs": decimal_text(abs(symbol_position_amt)),
        "open_order_count": len(open_orders),
        "other_nonzero_position_symbols": other_nonzero_positions,
        "open_position_count": (1 if symbol_position_amt != 0 else 0) + len(other_nonzero_positions),
        "mark_price": decimal_text(mark) if mark is not None else None,
        "lot_size_step": decimal_text(step_size),
        "min_qty": decimal_text(min_qty),
    }


def quantize_quantity_for_notional(mark_price: Decimal) -> Decimal:
    raw = Decimal("22") / mark_price
    return raw.quantize(Decimal("0.001"), rounding=ROUND_DOWN)


def live_readiness_reasons(snapshot: Dict[str, Any], quantity: Decimal, min_notional: Decimal = MIN_NOTIONAL_USDT, max_notional: Decimal = MAX_NOTIONAL_USDT) -> Tuple[List[str], Optional[Decimal]]:
    reasons: List[str] = []
    mark = decimal_value(snapshot.get("mark_price"))
    notional = quantity * mark if mark is not None else None
    if not snapshot.get("can_trade"):
        reasons.append("account cannot trade")
    if decimal_value(snapshot.get("symbol_position_amt")) != Decimal("0"):
        reasons.append("symbol position must be exactly zero")
    if int(snapshot.get("open_order_count") or 0) != 0:
        reasons.append("symbol open orders must be zero")
    if snapshot.get("other_nonzero_position_symbols"):
        reasons.append("other non-zero positions are forbidden")
    if mark is None or mark <= 0:
        reasons.append("live mark price unavailable")
    if quantity <= 0:
        reasons.append("quantity filter invalid")
    step = decimal_value(snapshot.get("lot_size_step")) or Decimal("0.001")
    if step > 0 and (quantity / step) != (quantity / step).to_integral_value():
        reasons.append("quantity does not align with LOT_SIZE step")
    if notional is None or notional < min_notional or notional > max_notional:
        reasons.append("live notional must be between 20 and 25 USDT")
    return reasons, notional


def daily_capacity(supervisor: Dict[str, Any]) -> Tuple[int, int, int]:
    limit = int(supervisor.get("daily_order_limit") or os.getenv("TESTNET_MAX_ORDERS_PER_DAY", "0") or 0)
    count = int(supervisor.get("daily_actual_order_count") or 0)
    return count, limit, limit - count


def build_actual_payload(symbol: str, supervisor: Dict[str, Any], request: Dict[str, Any], bridge: Dict[str, Any], quantity: Decimal, notional: Decimal) -> Tuple[Dict[str, Any], List[str]]:
    reasons: List[str] = []
    approval_payload = request.get("approval_payload") if isinstance(request.get("approval_payload"), dict) else {}
    entry_side = str(supervisor.get("side") or approval_payload.get("side") or bridge.get("side") or "").upper()
    if entry_side not in {"BUY", "SELL"}:
        reasons.append("entry side must be BUY or SELL")
    if symbol.upper() != str(supervisor.get("symbol") or approval_payload.get("symbol") or bridge.get("symbol") or "").upper():
        reasons.append("symbol must match supervisor/source approval/bridge")
    payload = {
        "symbol": symbol.upper(),
        "entry_side": entry_side,
        "entry_quantity": decimal_text(quantity),
        "entry_order_type": "MARKET",
        "entry_reduce_only": False,
        "expected_entry_notional_usdt": decimal_text(notional),
        "close_side": opposite_side(entry_side),
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "source_supervisor_sha256": payload_sha256(supervisor),
        "source_approval_sha256": payload_sha256(request),
        "source_bridge_sha256": payload_sha256(bridge),
        "bridge_signal_metadata": approval_payload.get("bridge_signal_metadata") or {},
        "minimum_notional_usdt": decimal_text(MIN_NOTIONAL_USDT),
        "maximum_notional_usdt": decimal_text(MAX_NOTIONAL_USDT),
        "maximum_open_positions": supervisor.get("max_open_positions"),
        "maximum_total_exposure": supervisor.get("max_total_exposure_usdt"),
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "final_required_position_amount": "0",
        "base_url": DEMO_FUTURES_BASE_URL,
        "broker_mode": BROKER_MODE_REQUIRED,
    }
    return payload, reasons


def plan_expired(plan: Dict[str, Any]) -> bool:
    expires = parse_time(plan.get("expires_at"))
    return expires is None or expires <= utc_now_dt()


def build_result(mode: str, state: str, plan: Optional[Dict[str, Any]], reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    current = posture()
    payload = plan.get("actual_roundtrip_payload", {}) if plan else {}
    daily_before = plan.get("daily_actual_order_count_before") if plan else None
    remaining = plan.get("remaining_daily_order_slots") if plan else None
    result = {
        "generated_at": utc_now(),
        "mode": mode,
        "status": "COMPLETED" if state == COMPLETED else ("BLOCKED" if state == BLOCKED else ("FAILED" if state in {ENTRY_FAILED, ENTRY_STATE_UNKNOWN, CLOSE_FAILED, EMERGENCY_MANUAL_ACTION_REQUIRED} else state)),
        "state": state,
        "actual_testnet_only": True,
        "real_binance_enabled": current["real_binance_enabled"],
        "auto_execution_enabled": current["allow_auto_testnet_order"],
        "plan_available": bool(plan),
        "plan_expired": plan_expired(plan) if plan else False,
        "plan_consumed": bool(plan.get("consumed")) if plan else False,
        "execution_started": bool(plan.get("execution_started")) if plan else False,
        "completed": bool(plan.get("completed")) if plan else False,
        "plan_id_matches": None,
        "payload_sha256_matches": None,
        "source_supervisor_matches": None,
        "source_approval_matches": None,
        "source_bridge_matches": None,
        "execution_lock_acquired": False,
        "execution_halt_active_before_entry": current["execution_halt_active"],
        "symbol": payload.get("symbol"),
        "entry_side": payload.get("entry_side"),
        "planned_entry_quantity": payload.get("entry_quantity"),
        "entry_attempt_count": 0,
        "entry_executor_return_code": None,
        "entry_order_success": False,
        "entry_position_verified": False,
        "live_position_after_entry": None,
        "primary_close_attempt_count": 0,
        "primary_close_reduce_only": True,
        "primary_close_executor_return_code": None,
        "emergency_close_attempt_count": 0,
        "emergency_close_reduce_only": True,
        "live_position_after_close": None,
        "final_flat_verified": False,
        "open_orders_after_close": None,
        "daily_actual_order_count_before": daily_before,
        "daily_actual_order_count_after": daily_before,
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "remaining_daily_order_slots": remaining,
        "blocked_reasons": reasons or [],
        "operator_review_required": False,
        "next_action": None,
    }
    return result


def write_outputs(result: Dict[str, Any], state: Dict[str, Any], audit_event: str) -> None:
    write_json(RESULT_PATH, result)
    write_json(STATE_PATH, state)
    audit = dict(result)
    audit["event"] = audit_event
    append_jsonl(AUDIT_PATH, redact(audit))


def append_audit(event: str, payload: Dict[str, Any]) -> None:
    item = {"event": event, "generated_at": utc_now(), **payload}
    append_jsonl(AUDIT_PATH, redact(item))


def prepare(args: argparse.Namespace) -> int:
    supervisor = read_json(args.supervisor_result_path)
    request = read_json(args.approval_request_path)
    bridge = read_json(args.bridge_result_path)
    reasons: List[str] = []
    if not supervisor:
        reasons.append("supervisor result missing")
    if not bridge:
        reasons.append("bridge result missing")
    reasons.extend(base_environment_reasons(require_testnet_order=False))
    if supervisor:
        reasons.extend(supervisor_required_reasons(supervisor))
    reasons.extend(source_approval_reasons(request, bridge))
    daily_before, daily_limit, remaining = daily_capacity(supervisor)
    if remaining < REQUIRED_DAILY_ORDER_SLOTS:
        reasons.append("remaining_daily_order_slots must be at least 3")
    quantity = Decimal("0")
    notional = Decimal("0")
    try:
        snapshot = get_live_snapshot(args.symbol)
        mark = decimal_value(snapshot.get("mark_price"))
        if mark is not None and mark > 0:
            quantity = quantize_quantity_for_notional(mark)
        live_reasons, live_notional = live_readiness_reasons(snapshot, quantity)
        reasons.extend(live_reasons)
        notional = live_notional or Decimal("0")
    except (BinanceFuturesTestnetClientError, OSError, ValueError) as exc:
        snapshot = {}
        reasons.append(f"read-only Binance Futures Demo validation failed: {exc}")
    payload, payload_reasons = build_actual_payload(args.symbol, supervisor, request, bridge, quantity, notional)
    reasons.extend(payload_reasons)
    if reasons:
        result = build_result("prepare", BLOCKED, None, reasons)
        result.update({"remaining_daily_order_slots": remaining, "daily_actual_order_count_before": daily_before, "next_action": "Resolve blocked_reasons; no actual roundtrip plan created."})
        write_outputs(result, {"state": BLOCKED, "last_result": result}, "blocked execution")
        print("BLOCKED")
        return 1
    digest = payload_sha256(payload)
    now = utc_now_dt()
    plan = {
        "actual_roundtrip_plan_id": str(uuid.uuid4()),
        "actual_roundtrip_payload_sha256": digest,
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ACTUAL_ROUNDTRIP_TTL_MINUTES)).isoformat(),
        "execution_started": False,
        "entry_committed": False,
        "completed": False,
        "consumed": False,
        "actual_testnet_only": True,
        "actual_roundtrip_payload": payload,
        "source_supervisor_snapshot_sha256": payload_sha256(supervisor),
        "source_approval_snapshot_sha256": payload_sha256(request),
        "source_bridge_snapshot_sha256": payload_sha256(bridge),
        "daily_actual_order_count_before": daily_before,
        "daily_order_limit": daily_limit,
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "remaining_daily_order_slots": remaining,
    }
    write_json(PLAN_PATH, plan)
    result = build_result("prepare", PREPARED, plan, [])
    result["next_action"] = "Manually run --execute-roundtrip only on the VPS after reviewing the actual plan credentials."
    write_outputs(result, {"state": PREPARED, "actual_roundtrip_plan_id": plan["actual_roundtrip_plan_id"], "consumed": False}, "plan prepared")
    print(f"actual_roundtrip_plan_id={plan['actual_roundtrip_plan_id']}")
    print(f"actual_roundtrip_payload_sha256={digest}")
    print(f"actual_roundtrip_expires_at={plan['expires_at']}")
    print("PREPARED - NO ORDER SENT")
    return 0


def source_matches(plan: Dict[str, Any], supervisor_path: str, approval_path: str, bridge_path: str) -> Tuple[bool, bool, bool, List[str]]:
    reasons: List[str] = []
    supervisor = read_json(supervisor_path)
    approval = read_json(approval_path)
    bridge = read_json(bridge_path)
    supervisor_ok = bool(supervisor) and payload_sha256(supervisor) == plan.get("source_supervisor_snapshot_sha256")
    approval_ok = bool(approval) and payload_sha256(approval) == plan.get("source_approval_snapshot_sha256") and not request_expired(approval)
    bridge_ok = bool(bridge) and payload_sha256(bridge) == plan.get("source_bridge_snapshot_sha256")
    if not supervisor_ok:
        reasons.append("source supervisor result no longer matches frozen plan")
    if not approval_ok:
        reasons.append("source approval request is changed or expired")
    if not bridge_ok:
        reasons.append("source bridge result no longer matches frozen plan")
    return supervisor_ok, approval_ok, bridge_ok, reasons


class LockHandle:
    def __init__(self, path: str):
        self.path = path
        self.file = None
        self.acquired = False

    def __enter__(self) -> "LockHandle":
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.file = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.acquired = False
            return self
        self.acquired = True
        self.file.seek(0)
        self.file.truncate()
        self.file.write(f"pid={os.getpid()} acquired_at={utc_now()}\n")
        self.file.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.file:
            if self.acquired:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


def validate_plan_for_action(args: argparse.Namespace, mode: str, allow_consumed_for_recovery: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    plan = read_json(PLAN_PATH)
    reasons: List[str] = []
    if not plan:
        return plan, build_result(mode, BLOCKED, None, ["actual roundtrip plan missing"]), ["actual roundtrip plan missing"]
    plan_id_matches = args.approve == plan.get("actual_roundtrip_plan_id")
    sha_matches = args.confirm_sha256 == plan.get("actual_roundtrip_payload_sha256")
    if not plan_id_matches:
        reasons.append("actual roundtrip plan id mismatch")
    if not sha_matches:
        reasons.append("actual roundtrip payload sha256 mismatch")
    if plan_expired(plan):
        reasons.append("actual roundtrip plan expired")
    if plan.get("completed"):
        reasons.append("actual roundtrip plan already completed")
    if plan.get("consumed") and not allow_consumed_for_recovery:
        reasons.append("actual roundtrip plan consumed; entry replay forbidden")
    if not plan.get("actual_testnet_only"):
        reasons.append("plan must be actual_testnet_only")
    supervisor_ok, approval_ok, bridge_ok, source_reasons = source_matches(plan, args.supervisor_result_path, args.approval_request_path, args.bridge_result_path)
    reasons.extend(source_reasons)
    result = build_result(mode, BLOCKED if reasons else PREPARED, plan, reasons)
    result.update({
        "plan_id_matches": plan_id_matches,
        "payload_sha256_matches": sha_matches,
        "source_supervisor_matches": supervisor_ok,
        "source_approval_matches": approval_ok,
        "source_bridge_matches": bridge_ok,
    })
    return plan, result, reasons


def revalidate_before_entry(plan: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    payload = plan.get("actual_roundtrip_payload", {})
    symbol = payload.get("symbol")
    reasons = base_environment_reasons(require_testnet_order=True)
    if execution_halt_active():
        reasons.append("execution halt active before entry")
    if plan_expired(plan):
        reasons.append("actual roundtrip plan expired")
    if plan.get("consumed") or plan.get("execution_started"):
        reasons.append("actual roundtrip plan consumed; entry replay forbidden")
    supervisor = read_json(SUPERVISOR_RESULT_PATH)
    daily_before, _, remaining = daily_capacity(supervisor)
    if remaining < REQUIRED_DAILY_ORDER_SLOTS:
        reasons.append("remaining_daily_order_slots must be at least 3")
    try:
        snapshot = get_live_snapshot(symbol)
        live_reasons, notional = live_readiness_reasons(snapshot, decimal_value(payload.get("entry_quantity")) or Decimal("0"))
        reasons.extend(live_reasons)
        max_exp = decimal_value(payload.get("maximum_total_exposure")) or MAX_NOTIONAL_USDT
        if notional is not None and notional > max_exp:
            reasons.append("current notional exceeds configured exposure cap")
        snapshot["daily_actual_order_count_before"] = daily_before
        snapshot["remaining_daily_order_slots"] = remaining
    except (BinanceFuturesTestnetClientError, OSError, ValueError) as exc:
        snapshot = {}
        reasons.append(f"read-only revalidation failed: {exc}")
    return reasons, snapshot


def safe_subprocess_run(command: List[str]) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    result_json = read_json(EXECUTOR_RESULT_PATH)
    captured = {
        "args": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
        "executor_result": result_json,
    }
    return completed.returncode, result_json, redact(captured)


def persist_plan_and_state(plan: Dict[str, Any], state: str, updates: Optional[Dict[str, Any]] = None, event: str = "crash/recovery state") -> None:
    if updates:
        plan.update(updates)
    write_json(PLAN_PATH, plan)
    write_json(STATE_PATH, {"state": state, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id"), **(updates or {})})
    append_audit(event, {"state": state, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id")})


def verify_entry_position(symbol: str, entry_side: str) -> Tuple[bool, str, Dict[str, Any]]:
    last_snapshot: Dict[str, Any] = {}
    for index in range(MAX_POSITION_POLLS):
        last_snapshot = get_live_snapshot(symbol)
        amt = decimal_value(last_snapshot.get("symbol_position_amt")) or Decimal("0")
        sign_ok = (entry_side == "BUY" and amt > 0) or (entry_side == "SELL" and amt < 0)
        no_second = not last_snapshot.get("other_nonzero_position_symbols")
        if amt != 0 and sign_ok and no_second:
            return True, decimal_text(amt), last_snapshot
        if index < MAX_POSITION_POLLS - 1:
            time.sleep(MAX_TOTAL_WAIT_SECONDS / MAX_POSITION_POLLS)
    return False, decimal_text(decimal_value(last_snapshot.get("symbol_position_amt")) or Decimal("0")), last_snapshot


def verify_flat(symbol: str) -> Tuple[bool, str, int, Dict[str, Any]]:
    last_snapshot: Dict[str, Any] = {}
    for index in range(MAX_POSITION_POLLS):
        last_snapshot = get_live_snapshot(symbol)
        amt = decimal_value(last_snapshot.get("symbol_position_amt")) or Decimal("0")
        orders = int(last_snapshot.get("open_order_count") or 0)
        no_second = not last_snapshot.get("other_nonzero_position_symbols")
        if amt == 0 and orders == 0 and no_second:
            return True, "0", orders, last_snapshot
        if index < MAX_POSITION_POLLS - 1:
            time.sleep(MAX_TOTAL_WAIT_SECONDS / MAX_POSITION_POLLS)
    amt_text = decimal_text(decimal_value(last_snapshot.get("symbol_position_amt")) or Decimal("0"))
    return False, amt_text, int(last_snapshot.get("open_order_count") or 0), last_snapshot


def execute_close(plan: Dict[str, Any], result: Dict[str, Any], emergency: bool = False) -> bool:
    payload = plan.get("actual_roundtrip_payload", {})
    symbol = payload.get("symbol")
    snapshot = get_live_snapshot(symbol)
    position_amt = decimal_value(snapshot.get("symbol_position_amt")) or Decimal("0")
    close_side = close_side_for_position(position_amt)
    if position_amt == 0:
        flat_ok, flat_amt, open_orders, _ = verify_flat(symbol)
        result.update({"live_position_after_close": flat_amt, "open_orders_after_close": open_orders, "final_flat_verified": flat_ok})
        return flat_ok
    if close_side is None:
        return False
    intent_state = EMERGENCY_CLOSE_INTENT_RECORDED if emergency else PRIMARY_CLOSE_INTENT_RECORDED
    sent_state = EMERGENCY_CLOSE_SENT if emergency else PRIMARY_CLOSE_SENT
    count_key = "emergency_close_attempt_count" if emergency else "primary_close_attempt_count"
    rc_key = "primary_close_executor_return_code" if not emergency else "emergency_close_executor_return_code"
    persist_plan_and_state(plan, intent_state, {"last_live_close_quantity": decimal_text(abs(position_amt)), "last_live_close_side": close_side}, "emergency close intent" if emergency else "close intent")
    command = [sys.executable, "binance_testnet_executor.py", "--symbol", symbol, "--close-position", "--send"]
    result[count_key] = 1
    return_code, executor_result, captured = safe_subprocess_run(command)
    result[rc_key] = return_code
    result[f"{'emergency' if emergency else 'primary'}_close_command_redacted"] = captured
    persist_plan_and_state(plan, sent_state, {"last_close_return_code": return_code}, "emergency close result" if emergency else "close result")
    append_audit("close result" if not emergency else "emergency close result", {"return_code": return_code, "executor_result": executor_result})
    if return_code != 0 or executor_result.get("order_success") is False:
        return False
    flat_ok, flat_amt, open_orders, _ = verify_flat(symbol)
    result.update({"live_position_after_close": flat_amt, "open_orders_after_close": open_orders, "final_flat_verified": flat_ok})
    append_audit("flat verification", {"flat": flat_ok, "position": flat_amt, "open_orders": open_orders})
    return flat_ok


def finish_completion(plan: Dict[str, Any], result: Dict[str, Any], state: str = COMPLETED) -> int:
    plan.update({"completed": True, "completed_at": utc_now()})
    write_json(PLAN_PATH, plan)
    result.update({"status": "COMPLETED", "state": state, "completed": True, "next_action": "Operator review actual Testnet audit/result files."})
    if state in {EMERGENCY_FLAT_VERIFIED}:
        result["operator_review_required"] = True
    write_outputs(result, {"state": state, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id"), "completed": True}, "completion")
    print("COMPLETED")
    return 0


def handle_close_failure(plan: Dict[str, Any], result: Dict[str, Any]) -> int:
    payload = plan.get("actual_roundtrip_payload", {})
    symbol = payload.get("symbol")
    snapshot = get_live_snapshot(symbol)
    current_amt = decimal_value(snapshot.get("symbol_position_amt")) or Decimal("0")
    if current_amt == 0:
        flat_ok, flat_amt, open_orders, _ = verify_flat(symbol)
        result.update({"live_position_after_close": flat_amt, "open_orders_after_close": open_orders, "final_flat_verified": flat_ok})
        if flat_ok:
            return finish_completion(plan, result)
    ok = execute_close(plan, result, emergency=True)
    activate_halt("manual actual testnet emergency close attempted; operator review required")
    result["operator_review_required"] = True
    if ok:
        persist_plan_and_state(plan, EMERGENCY_FLAT_VERIFIED, {"emergency_close_used": True}, "flat verification")
        result["state"] = EMERGENCY_FLAT_VERIFIED
        return finish_completion(plan, result, state=EMERGENCY_FLAT_VERIFIED)
    flat_ok, flat_amt, open_orders, _ = verify_flat(symbol)
    result.update({
        "status": "FAILED",
        "state": EMERGENCY_MANUAL_ACTION_REQUIRED,
        "final_flat_verified": flat_ok,
        "live_position_after_close": flat_amt,
        "open_orders_after_close": open_orders,
        "operator_review_required": True,
        "next_action": f"Manual recovery required. Inspect read-only status, then run --recover-close --approve {plan.get('actual_roundtrip_plan_id')} --confirm-sha256 {plan.get('actual_roundtrip_payload_sha256')} --confirm-action REDUCE_ONLY_EMERGENCY_CLOSE only after review.",
    })
    persist_plan_and_state(plan, EMERGENCY_MANUAL_ACTION_REQUIRED, {"emergency_close_used": True}, "blocked execution")
    write_outputs(result, {"state": EMERGENCY_MANUAL_ACTION_REQUIRED, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id")}, "blocked execution")
    print("EMERGENCY_MANUAL_ACTION_REQUIRED")
    return 1


def execute_roundtrip(args: argparse.Namespace) -> int:
    reasons: List[str] = []
    if not env_bool("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP", False):
        reasons.append("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP=1 is required")
    if args.confirm_action != "OPEN_AND_CLOSE_BINANCE_FUTURES_DEMO_POSITION":
        reasons.append("confirm-action must be OPEN_AND_CLOSE_BINANCE_FUTURES_DEMO_POSITION")
    plan, result, plan_reasons = validate_plan_for_action(args, "execute-roundtrip")
    reasons.extend(plan_reasons)
    reasons.extend(base_environment_reasons(require_testnet_order=True))
    if reasons:
        result.update({"state": BLOCKED, "status": "BLOCKED", "blocked_reasons": reasons, "next_action": "Do not execute; resolve blocked_reasons or prepare a fresh plan."})
        write_outputs(result, {"state": BLOCKED}, "blocked execution")
        print("BLOCKED")
        return 1
    with LockHandle(LOCK_FILE_PATH) as lock:
        result["execution_lock_acquired"] = lock.acquired
        if not lock.acquired:
            result.update({"state": BLOCKED, "status": "BLOCKED", "blocked_reasons": ["another actual roundtrip controller is running"]})
            write_outputs(result, {"state": BLOCKED}, "blocked execution")
            print("BLOCKED")
            return 1
        append_audit("execution locked", {"actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id")})
        latest_plan = read_json(PLAN_PATH)
        entry_reasons, snapshot = revalidate_before_entry(latest_plan)
        if entry_reasons:
            result.update({"state": BLOCKED, "status": "BLOCKED", "blocked_reasons": entry_reasons, "remaining_daily_order_slots": snapshot.get("remaining_daily_order_slots"), "daily_actual_order_count_before": snapshot.get("daily_actual_order_count_before"), "next_action": "Entry blocked; no order sent."})
            write_outputs(result, {"state": BLOCKED}, "blocked execution")
            print("BLOCKED")
            return 1
        plan = latest_plan
        persist_plan_and_state(plan, ENTRY_INTENT_RECORDED, {"execution_started": True, "consumed": True, "entry_intent_recorded_at": utc_now()}, "entry intent")
        payload = plan.get("actual_roundtrip_payload", {})
        command = [sys.executable, "binance_testnet_executor.py", "--symbol", payload.get("symbol"), "--side", payload.get("entry_side"), "--quantity", payload.get("entry_quantity"), "--order-type", "MARKET", "--send"]
        result.update({"state": ENTRY_SENT, "entry_attempt_count": 1, "plan_consumed": True, "execution_started": True})
        return_code, executor_result, captured = safe_subprocess_run(command)
        result.update({"entry_executor_return_code": return_code, "entry_order_success": bool(executor_result.get("order_success")), "entry_command_redacted": captured})
        persist_plan_and_state(plan, ENTRY_SENT, {"entry_committed": return_code == 0, "entry_return_code": return_code}, "entry result")
        verified, live_amt, _ = verify_entry_position(payload.get("symbol"), payload.get("entry_side"))
        result.update({"entry_position_verified": verified, "live_position_after_entry": live_amt})
        append_audit("entry verification", {"verified": verified, "live_position_after_entry": live_amt})
        if return_code != 0 and decimal_value(live_amt) == Decimal("0"):
            result.update({"status": "FAILED", "state": ENTRY_FAILED, "next_action": "Entry failed with no live position; prepare a fresh plan only after review."})
            persist_plan_and_state(plan, ENTRY_FAILED, {}, "blocked execution")
            write_outputs(result, {"state": ENTRY_FAILED, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id")}, "blocked execution")
            print("ENTRY_FAILED")
            return 1
        if not verified:
            if decimal_value(live_amt) == Decimal("0"):
                result.update({"status": "FAILED", "state": ENTRY_FAILED, "next_action": "Entry verification failed and position is zero; no replay allowed."})
                persist_plan_and_state(plan, ENTRY_FAILED, {}, "blocked execution")
                write_outputs(result, {"state": ENTRY_FAILED, "actual_roundtrip_plan_id": plan.get("actual_roundtrip_plan_id")}, "blocked execution")
                print("ENTRY_FAILED")
                return 1
            result.update({"state": ENTRY_STATE_UNKNOWN, "status": "FAILED"})
            persist_plan_and_state(plan, ENTRY_STATE_UNKNOWN, {}, "crash/recovery state")
        else:
            persist_plan_and_state(plan, ENTRY_CONFIRMED, {"actual_live_position_amount": live_amt}, "entry verification")
        close_ok = execute_close(plan, result, emergency=False)
        if close_ok:
            persist_plan_and_state(plan, FINAL_FLAT_VERIFIED, {}, "flat verification")
            result.update({"state": COMPLETED, "final_flat_verified": True})
            return finish_completion(plan, result)
        result["state"] = CLOSE_FAILED
        persist_plan_and_state(plan, CLOSE_FAILED, {}, "close result")
        return handle_close_failure(plan, result)


def recover_close(args: argparse.Namespace) -> int:
    reasons: List[str] = []
    if not env_bool("ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE", False):
        reasons.append("ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE=1 is required")
    if args.confirm_action != "REDUCE_ONLY_EMERGENCY_CLOSE":
        reasons.append("confirm-action must be REDUCE_ONLY_EMERGENCY_CLOSE")
    plan, result, plan_reasons = validate_plan_for_action(args, "recover-close", allow_consumed_for_recovery=True)
    reasons.extend(plan_reasons)
    reasons.extend(base_environment_reasons(require_testnet_order=True))
    if reasons:
        result.update({"state": BLOCKED, "status": "BLOCKED", "blocked_reasons": reasons, "next_action": "Recovery close blocked; resolve blocked_reasons."})
        write_outputs(result, {"state": BLOCKED}, "blocked execution")
        print("BLOCKED")
        return 1
    with LockHandle(LOCK_FILE_PATH) as lock:
        result["execution_lock_acquired"] = lock.acquired
        if not lock.acquired:
            result.update({"state": BLOCKED, "status": "BLOCKED", "blocked_reasons": ["another actual roundtrip controller is running"]})
            write_outputs(result, {"state": BLOCKED}, "blocked execution")
            print("BLOCKED")
            return 1
        snapshot = get_live_snapshot(plan["actual_roundtrip_payload"]["symbol"])
        if decimal_value(snapshot.get("symbol_position_amt")) == Decimal("0"):
            flat_ok, flat_amt, open_orders, _ = verify_flat(plan["actual_roundtrip_payload"]["symbol"])
            result.update({"live_position_after_close": flat_amt, "open_orders_after_close": open_orders, "final_flat_verified": flat_ok})
            if flat_ok:
                return finish_completion(plan, result)
        ok = execute_close(plan, result, emergency=True)
        activate_halt("manual actual testnet recovery close attempted; operator review required")
        if ok:
            result["operator_review_required"] = True
            return finish_completion(plan, result, state=EMERGENCY_FLAT_VERIFIED)
        return handle_close_failure(plan, result)


def abbreviated_identifier(value: Any, prefix: int = 8, suffix: int = 4) -> str:
    if not isinstance(value, str) or not value:
        return "none"
    if len(value) <= prefix + suffix + 3:
        return "REDACTED"
    return f"{value[:prefix]}...{value[-suffix:]}"


def status() -> int:
    plan = read_json(PLAN_PATH)
    state = read_json(STATE_PATH)
    execution_result = read_json(RESULT_PATH)
    execution_result_available = bool(execution_result)
    payload = plan.get("actual_roundtrip_payload", {}) if plan else {}
    persistent_state = state.get("state") or (PREPARED if plan else NO_PLAN)
    current = posture()
    status_payload = {
        "generated_at": utc_now(),
        "mode": "status",
        "persistent_state": persistent_state,
        "plan_available": bool(plan),
        "plan_consumed": bool(plan.get("consumed")) if plan else False,
        "plan_completed": bool(plan.get("completed")) if plan else False,
        "plan_expired": plan_expired(plan) if plan else False,
        "actual_testnet_only": bool(plan.get("actual_testnet_only", True)) if plan else True,
        "symbol": payload.get("symbol") or execution_result.get("symbol"),
        "planned_entry_quantity": payload.get("entry_quantity") or execution_result.get("planned_entry_quantity"),
        "last_execution_status": execution_result.get("status") if execution_result_available else None,
        "last_execution_state": execution_result.get("state") if execution_result_available else None,
        "last_entry_attempt_count": execution_result.get("entry_attempt_count") if execution_result_available else None,
        "last_entry_order_success": execution_result.get("entry_order_success") if execution_result_available else None,
        "last_entry_position_verified": execution_result.get("entry_position_verified") if execution_result_available else None,
        "last_live_position_after_entry": execution_result.get("live_position_after_entry") if execution_result_available else None,
        "last_primary_close_attempt_count": execution_result.get("primary_close_attempt_count") if execution_result_available else None,
        "last_primary_close_reduce_only": execution_result.get("primary_close_reduce_only") if execution_result_available else None,
        "last_emergency_close_attempt_count": execution_result.get("emergency_close_attempt_count") if execution_result_available else None,
        "last_live_position_after_close": execution_result.get("live_position_after_close") if execution_result_available else None,
        "last_final_flat_verified": execution_result.get("final_flat_verified") if execution_result_available else None,
        "operator_review_required": bool(execution_result.get("operator_review_required")) if execution_result_available else False,
        "execution_result_available": execution_result_available,
        "execution_result_preserved": True,
        "current_read_only_status": {
            "real_binance_enabled": current["real_binance_enabled"],
            "auto_execution_enabled": current["allow_auto_testnet_order"],
            "execution_halt_active": current["execution_halt_active"],
        },
        "plan_lifecycle": {
            "available": bool(plan),
            "consumed": bool(plan.get("consumed")) if plan else False,
            "completed": bool(plan.get("completed")) if plan else False,
            "expired": plan_expired(plan) if plan else False,
        },
        "last_execution_result": {
            "available": execution_result_available,
            "status": execution_result.get("status") if execution_result_available else None,
            "state": execution_result.get("state") if execution_result_available else None,
        },
        "next_action": "No actual roundtrip plan available." if not plan else "Review status/result state; execute only with explicit manual gates.",
    }
    write_json(STATUS_PATH, redact(status_payload))
    print(f"persistent_state={status_payload['persistent_state']}")
    print(f"plan={abbreviated_identifier(plan.get('actual_roundtrip_plan_id')) if plan else 'none'}")
    print(f"sha={abbreviated_identifier(plan.get('actual_roundtrip_payload_sha256')) if plan else 'none'}")
    print(f"consumed={status_payload['plan_consumed']}")
    print(f"completed={status_payload['plan_completed']}")
    print(f"expired={status_payload['plan_expired']}")
    print(f"execution_result_available={status_payload['execution_result_available']}")
    print(f"last_execution_state={status_payload['last_execution_state']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2.97B1 manual actual Testnet roundtrip controller")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--execute-roundtrip", action="store_true")
    mode.add_argument("--recover-close", action="store_true")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--approve")
    parser.add_argument("--confirm-sha256")
    parser.add_argument("--confirm-action")
    parser.add_argument("--supervisor-result-path", default=SUPERVISOR_RESULT_PATH)
    parser.add_argument("--approval-request-path", default=APPROVAL_REQUEST_PATH)
    parser.add_argument("--bridge-result-path", default=BRIDGE_RESULT_PATH)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.execute_roundtrip or args.recover_close) and (not args.approve or not args.confirm_sha256 or not args.confirm_action):
        parser.error("execution/recovery requires --approve, --confirm-sha256, and --confirm-action")
    if args.prepare:
        return prepare(args)
    if args.execute_roundtrip:
        return execute_roundtrip(args)
    if args.recover_close:
        return recover_close(args)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
