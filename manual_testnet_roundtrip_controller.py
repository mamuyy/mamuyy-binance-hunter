"""Phase 2.97A manual Testnet dummy roundtrip controller.

Design/mock-simulation only. This module never imports a Binance client, never
invokes the executor, never launches subprocesses, and never sends/cancels any
order. It freezes a one-time roundtrip plan and simulates the intended lifecycle
offline.
"""

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from testnet_roundtrip_state import (
    BLOCKED,
    CLOSE_REDUCE_ONLY_SIMULATED,
    COMPLETED,
    ENTRY_SIMULATED,
    FAILED_CLOSE,
    FAILED_ENTRY,
    FAILED_FINAL_FLAT_VERIFICATION,
    FAILED_POSITION_VERIFICATION,
    FINAL_FLAT_VERIFIED_SIMULATED,
    NO_PLAN,
    POSITION_OPEN_VERIFIED_SIMULATED,
    PREPARED,
    SIMULATION_APPROVED,
    append_jsonl,
    read_json,
    write_json,
)

DEMO_FUTURES_BASE_URL = "https://demo-fapi.binance.com"
BROKER_MODE_REQUIRED = "BINANCE_FUTURES_TESTNET_ONLY"

SUPERVISOR_RESULT_PATH = "logs/testnet_execution_safety_supervisor_result.json"
APPROVAL_REQUEST_PATH = "logs/manual_testnet_approval_request.json"
BRIDGE_RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
PLAN_PATH = "logs/manual_testnet_roundtrip_plan.json"
RESULT_PATH = "logs/manual_testnet_roundtrip_result.json"
STATE_PATH = "logs/manual_testnet_roundtrip_state.json"
AUDIT_PATH = "logs/manual_testnet_roundtrip_audit.jsonl"
TELEGRAM_PREVIEW_PATH = "logs/manual_testnet_roundtrip_telegram_preview.json"
HALT_FILE_PATH = "runtime/TESTNET_EXECUTION_HALT"

ROUNDTRIP_TTL_MINUTES = 10
REQUIRED_DAILY_ORDER_SLOTS = 2
SECRET_KEY_FRAGMENTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "SIGNATURE", "CHAT_ID")


def utc_now_dt() -> datetime:
    override = os.getenv("MANUAL_TESTNET_ROUNDTRIP_NOW")
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


def short(value: Any, length: int = 12) -> Optional[str]:
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


def normalize_base_url() -> str:
    return os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL).strip().rstrip("/")


def execution_halt_active() -> bool:
    return env_bool("TESTNET_EXECUTION_HALT", False) or os.path.exists(HALT_FILE_PATH)


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


def signed_quantity(side: str, quantity: str) -> str:
    qty = decimal_value(quantity) or Decimal("0")
    return decimal_text(qty if side == "BUY" else -qty)


def opposite_side(side: str) -> Optional[str]:
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return None


def request_expired(request: Dict[str, Any]) -> bool:
    expires = parse_time(request.get("expires_at"))
    return expires is None or expires <= utc_now_dt()


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
    return payload


def posture() -> Dict[str, Any]:
    return {
        "broker_mode": os.getenv("BROKER_MODE", BROKER_MODE_REQUIRED),
        "base_url": normalize_base_url(),
        "real_binance_enabled": env_bool("REAL_BINANCE_ENABLED", False),
        "allow_real_binance_order": env_bool("ALLOW_REAL_BINANCE_ORDER", False),
        "allow_auto_testnet_order": env_bool("ALLOW_AUTO_TESTNET_ORDER", False),
        "allow_testnet_order": env_bool("ALLOW_TESTNET_ORDER", False),
        "allow_manual_testnet_approval": env_bool("ALLOW_MANUAL_TESTNET_APPROVAL", False),
        "execution_halt_active": execution_halt_active(),
    }


def environment_reasons() -> List[str]:
    current = posture()
    reasons: List[str] = []
    if current["broker_mode"] != BROKER_MODE_REQUIRED:
        reasons.append(f"broker mode must be exactly {BROKER_MODE_REQUIRED}")
    if current["base_url"] != DEMO_FUTURES_BASE_URL:
        reasons.append(f"base URL must be exactly {DEMO_FUTURES_BASE_URL}")
    if current["real_binance_enabled"]:
        reasons.append("Real Binance must be false")
    if current["allow_real_binance_order"]:
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be false")
    if not env_false_or_unset("ALLOW_AUTO_TESTNET_ORDER"):
        reasons.append("Auto testnet order must be false")
    if not env_false_or_unset("ALLOW_TESTNET_ORDER"):
        reasons.append("Testnet order gate must be false")
    if not env_false_or_unset("ALLOW_MANUAL_TESTNET_APPROVAL"):
        reasons.append("Manual testnet approval gate must be false")
    if current["execution_halt_active"]:
        reasons.append("execution halt active")
    return reasons


def supervisor_required_reasons(supervisor: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    required = {
        "status": "READY_FOR_MANUAL_DUMMY_ORDER",
        "read_only": True,
        "execution_permitted": False,
        "manual_execution_required": True,
        "order_attempted": False,
        "order_success": False,
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
        "daily_limit_passed": True,
        "execution_halt_active": False,
        "real_binance_enabled": False,
        "allow_auto_testnet_order": False,
        "allow_testnet_order": False,
        "allow_manual_testnet_approval": False,
        "base_url": DEMO_FUTURES_BASE_URL,
        "broker_mode": BROKER_MODE_REQUIRED,
    }
    for key, expected in required.items():
        if supervisor.get(key) != expected:
            reasons.append(f"supervisor {key} must be {expected!r}")
    if supervisor.get("blocked_reasons") != []:
        reasons.append("supervisor blocked_reasons must be []")
    if int(supervisor.get("open_position_count") or 0) != 0:
        reasons.append("open positions must be 0")
    if int(supervisor.get("open_order_count") or 0) != 0:
        reasons.append("open orders must be 0")
    daily_limit = int(supervisor.get("daily_order_limit") or 0)
    daily_count = int(supervisor.get("daily_actual_order_count") or 0)
    remaining = daily_limit - daily_count
    if remaining < REQUIRED_DAILY_ORDER_SLOTS:
        reasons.append("remaining_daily_order_slots must be at least 2")
    return reasons


def source_approval_reasons(request: Dict[str, Any], bridge: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not request:
        return ["source approval request missing"]
    if request_expired(request):
        reasons.append("source approval expired")
    if bool(request.get("used")):
        reasons.append("source approval used")
    payload = request.get("approval_payload") if isinstance(request.get("approval_payload"), dict) else {}
    if request.get("payload_sha256") != payload_sha256(payload):
        reasons.append("source approval payload sha256 mismatch")
    if bridge and payload.get("bridge_signal_metadata"):
        expected_metadata = {
            "bridge_status": bridge.get("status"),
            "signal_score": bridge.get("signal_score"),
            "overlay_decision": bridge.get("overlay_decision"),
            "trade_rank": bridge.get("trade_rank"),
            "suggested_risk": bridge.get("suggested_risk"),
            "source_report_path": bridge.get("overlay_report_path"),
        }
        if expected_metadata != payload.get("bridge_signal_metadata"):
            reasons.append("source approval bridge metadata mismatch")
    return reasons


def build_result(mode: str, state: str, plan: Optional[Dict[str, Any]], reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    payload = plan.get("roundtrip_payload", {}) if plan else {}
    current_posture = posture()
    daily_limit = plan.get("daily_order_limit") if plan else None
    daily_count = plan.get("daily_actual_order_count") if plan else None
    remaining = plan.get("remaining_daily_order_slots") if plan else None
    expires_at = plan.get("expires_at") if plan else None
    plan_expired = bool(plan and (parse_time(expires_at) is None or parse_time(expires_at) <= utc_now_dt()))
    return {
        "generated_at": utc_now(),
        "mode": mode,
        "status": "COMPLETED" if state == COMPLETED else ("FAILED" if state.startswith("FAILED") else ("PREPARED" if state == PREPARED else "BLOCKED")),
        "state": state,
        "simulation_only": True,
        "actual_execution_enabled": False,
        "roundtrip_plan_id_short": short(plan.get("roundtrip_plan_id")) if plan else None,
        "roundtrip_payload_sha256_short": short(plan.get("roundtrip_payload_sha256")) if plan else None,
        "plan_available": bool(plan),
        "plan_expired": plan_expired,
        "plan_used": bool(plan.get("used")) if plan else False,
        "plan_id_matches": None,
        "payload_sha256_matches": None,
        "source_supervisor_matches": None,
        "source_approval_matches": None,
        "symbol": payload.get("symbol"),
        "entry_side": payload.get("entry_side"),
        "entry_quantity": payload.get("entry_quantity"),
        "entry_reduce_only": payload.get("entry_reduce_only"),
        "close_side": payload.get("close_side"),
        "close_quantity": payload.get("close_quantity"),
        "close_reduce_only": payload.get("close_reduce_only"),
        "expected_entry_notional_usdt": payload.get("expected_entry_notional_usdt"),
        "initial_position_amt": "0",
        "simulated_position_after_entry": None,
        "simulated_position_after_close": None,
        "final_flat_verified": False,
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "remaining_daily_order_slots": remaining,
        "roundtrip_daily_capacity_passed": (remaining is not None and int(remaining) >= REQUIRED_DAILY_ORDER_SLOTS),
        "execution_halt_active": current_posture["execution_halt_active"],
        "halt_required": False,
        "emergency_close_required": False,
        "simulation_approved": False,
        "entry_simulated": False,
        "position_open_verified_simulated": False,
        "close_simulated": False,
        "close_reduce_only_verified": False,
        "order_attempted": False,
        "order_success": False,
        "actual_order_count_increment": 0,
        "daily_order_limit": daily_limit,
        "daily_actual_order_count": daily_count,
        "blocked_reasons": reasons or [],
        "next_action": None,
    }


def write_outputs(result: Dict[str, Any], state: Dict[str, Any], audit_event: str) -> None:
    write_json(RESULT_PATH, result)
    write_json(STATE_PATH, state)
    audit = dict(result)
    audit["event"] = audit_event
    append_jsonl(AUDIT_PATH, redact(audit))


def freeze_payload(symbol: str, supervisor: Dict[str, Any], request: Dict[str, Any], bridge: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    reasons: List[str] = []
    approval_payload = request.get("approval_payload") if isinstance(request.get("approval_payload"), dict) else {}
    entry_side = str(supervisor.get("side") or approval_payload.get("side") or bridge.get("side") or "").upper()
    entry_quantity = str(supervisor.get("approved_quantity") or approval_payload.get("approved_quantity") or approval_payload.get("quantity") or bridge.get("quantity") or "")
    quantity = decimal_value(entry_quantity)
    if entry_side not in {"BUY", "SELL"}:
        reasons.append("entry side must be BUY or SELL")
    if quantity is None or quantity <= 0:
        reasons.append("entry quantity must be positive")
    close = opposite_side(entry_side)
    if symbol.upper() != str(supervisor.get("symbol") or approval_payload.get("symbol") or bridge.get("symbol") or "").upper():
        reasons.append("symbol must match supervisor/source approval/bridge")
    payload = {
        "symbol": symbol.upper(),
        "entry_side": entry_side,
        "entry_quantity": decimal_text(quantity) if quantity is not None else entry_quantity,
        "entry_order_type": "MARKET",
        "entry_reduce_only": False,
        "expected_entry_notional_usdt": supervisor.get("live_proposed_notional_usdt") or supervisor.get("proposed_notional_usdt") or approval_payload.get("estimated_notional_usdt") or bridge.get("estimated_notional_usdt"),
        "close_side": close,
        "close_quantity": decimal_text(quantity) if quantity is not None else entry_quantity,
        "close_order_type": "MARKET",
        "close_reduce_only": True,
        "supervisor_generated_at": supervisor.get("generated_at"),
        "supervisor_dedupe_key": supervisor.get("dedupe_key"),
        "source_approval_request_id": request.get("request_id"),
        "source_approval_payload_sha256": request.get("payload_sha256"),
        "bridge_signal_metadata": approval_payload.get("bridge_signal_metadata") or {},
        "max_open_positions": supervisor.get("max_open_positions"),
        "max_total_exposure_usdt": supervisor.get("max_total_exposure_usdt"),
        "min_notional_usdt": supervisor.get("min_notional_usdt"),
        "max_notional_usdt": supervisor.get("max_notional_usdt"),
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "final_required_position_amt": "0",
    }
    return payload, reasons


def prepare(args: argparse.Namespace) -> int:
    supervisor = read_json(args.supervisor_result_path)
    request = read_json(args.approval_request_path)
    bridge = read_json(args.bridge_result_path)
    reasons = []
    if not supervisor:
        reasons.append("supervisor result missing")
    if not bridge:
        reasons.append("bridge result missing")
    reasons.extend(environment_reasons())
    if supervisor:
        reasons.extend(supervisor_required_reasons(supervisor))
    reasons.extend(source_approval_reasons(request, bridge))
    roundtrip_payload, payload_reasons = freeze_payload(args.symbol, supervisor, request, bridge)
    reasons.extend(payload_reasons)

    daily_limit = int(supervisor.get("daily_order_limit") or 0) if supervisor else 0
    daily_count = int(supervisor.get("daily_actual_order_count") or 0) if supervisor else 0
    remaining = daily_limit - daily_count
    roundtrip_daily_capacity_passed = remaining >= REQUIRED_DAILY_ORDER_SLOTS
    if not roundtrip_daily_capacity_passed and "remaining_daily_order_slots must be at least 2" not in reasons:
        reasons.append("remaining_daily_order_slots must be at least 2")

    if reasons:
        result = build_result("prepare", BLOCKED, None, reasons)
        result.update({
            "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
            "remaining_daily_order_slots": remaining,
            "roundtrip_daily_capacity_passed": roundtrip_daily_capacity_passed,
            "next_action": "Resolve blocked_reasons; no roundtrip plan created.",
        })
        write_outputs(result, {"state": BLOCKED, "last_result": result}, "roundtrip_prepare_blocked")
        print("BLOCKED")
        return 1

    digest = payload_sha256(roundtrip_payload)
    now = utc_now_dt()
    plan = {
        "roundtrip_plan_id": str(uuid.uuid4()),
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ROUNDTRIP_TTL_MINUTES)).isoformat(),
        "used": False,
        "simulation_only": True,
        "actual_execution_enabled": False,
        "roundtrip_payload_sha256": digest,
        "roundtrip_payload": roundtrip_payload,
        "source_supervisor_snapshot_sha256": payload_sha256(supervisor),
        "source_approval_snapshot_sha256": payload_sha256(request),
        "source_bridge_snapshot_sha256": payload_sha256(bridge),
        "daily_order_limit": daily_limit,
        "daily_actual_order_count": daily_count,
        "required_daily_order_slots": REQUIRED_DAILY_ORDER_SLOTS,
        "remaining_daily_order_slots": remaining,
        "roundtrip_daily_capacity_passed": True,
    }
    write_json(PLAN_PATH, plan)
    result = build_result("prepare", PREPARED, plan, [])
    result.update({"next_action": "Run --simulate with the roundtrip plan id and payload sha256 in simulation mode only."})
    write_outputs(result, {"state": PREPARED, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": False}, "roundtrip_prepared")
    print(f"roundtrip_plan_id={plan['roundtrip_plan_id']}")
    print(f"roundtrip_payload_sha256={digest}")
    print(f"roundtrip_expires_at={plan['expires_at']}")
    print("SIMULATION ONLY - NO ORDER SENT")
    return 0


def source_matches(plan: Dict[str, Any], supervisor_path: str, approval_path: str, bridge_path: str) -> Tuple[bool, bool, List[str]]:
    reasons: List[str] = []
    supervisor = read_json(supervisor_path)
    approval = read_json(approval_path)
    bridge = read_json(bridge_path)
    supervisor_ok = bool(supervisor) and payload_sha256(supervisor) == plan.get("source_supervisor_snapshot_sha256")
    approval_ok = bool(approval) and payload_sha256(approval) == plan.get("source_approval_snapshot_sha256") and not request_expired(approval) and not bool(approval.get("used"))
    bridge_ok = bool(bridge) and payload_sha256(bridge) == plan.get("source_bridge_snapshot_sha256")
    if not supervisor_ok:
        reasons.append("source supervisor result no longer matches frozen proposal")
    if not approval_ok:
        reasons.append("source approval request is changed, expired, or used")
    if not bridge_ok:
        reasons.append("source bridge result no longer matches frozen proposal")
    return supervisor_ok and bridge_ok, approval_ok, reasons


def simulate(args: argparse.Namespace) -> int:
    plan = read_json(PLAN_PATH)
    reasons: List[str] = []
    if not env_bool("ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION", False):
        reasons.append("ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION=1 is required")
    if not plan:
        result = build_result("simulate", BLOCKED, None, ["roundtrip plan missing"] + reasons)
        result["next_action"] = "Prepare a new roundtrip plan."
        write_outputs(result, {"state": BLOCKED}, "roundtrip_simulation_blocked")
        print("BLOCKED")
        return 1

    plan_expired = parse_time(plan.get("expires_at")) is None or parse_time(plan.get("expires_at")) <= utc_now_dt()
    plan_id_matches = args.approve == plan.get("roundtrip_plan_id")
    sha_matches = args.confirm_sha256 == plan.get("roundtrip_payload_sha256")
    if plan.get("used"):
        reasons.append("roundtrip plan already used")
    if plan_expired:
        reasons.append("roundtrip plan expired")
    if not plan_id_matches:
        reasons.append("roundtrip plan id mismatch")
    if not sha_matches:
        reasons.append("roundtrip payload sha256 mismatch")
    source_supervisor_ok, source_approval_ok, source_reasons = source_matches(plan, args.supervisor_result_path, args.approval_request_path, args.bridge_result_path)
    reasons.extend(source_reasons)
    reasons.extend(environment_reasons())

    if reasons:
        result = build_result("simulate", BLOCKED, plan, reasons)
        result.update({
            "plan_id_matches": plan_id_matches,
            "payload_sha256_matches": sha_matches,
            "source_supervisor_matches": source_supervisor_ok,
            "source_approval_matches": source_approval_ok,
            "next_action": "Do not simulate; resolve blocked_reasons or prepare a fresh plan.",
        })
        write_json(RESULT_PATH, result)
        append_jsonl(AUDIT_PATH, redact({**result, "event": "roundtrip_simulation_blocked"}))
        print("BLOCKED")
        return 1

    payload = plan["roundtrip_payload"]
    result = build_result("simulate", SIMULATION_APPROVED, plan, [])
    result.update({
        "plan_id_matches": True,
        "payload_sha256_matches": True,
        "source_supervisor_matches": True,
        "source_approval_matches": True,
        "simulation_approved": True,
        "next_action": "Simulating offline roundtrip lifecycle.",
    })
    qty = payload["entry_quantity"]
    after_entry = signed_quantity(payload["entry_side"], qty)

    if args.mock_entry_failure:
        result.update({
            "status": "FAILED",
            "state": FAILED_ENTRY,
            "simulated_position_after_entry": "0",
            "simulated_position_after_close": None,
            "final_flat_verified": False,
            "blocked_reasons": ["mock entry failure"],
            "next_action": "Entry simulation failed; no close simulated.",
        })
        write_outputs(result, {"state": FAILED_ENTRY, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": False}, "roundtrip_failed_entry")
        print("FAILED_ENTRY")
        return 1

    result.update({"state": ENTRY_SIMULATED, "entry_simulated": True, "simulated_position_after_entry": after_entry})

    if args.mock_position_verification_failure:
        result.update({
            "status": "FAILED",
            "state": FAILED_POSITION_VERIFICATION,
            "position_open_verified_simulated": False,
            "halt_required": True,
            "blocked_reasons": ["mock position verification failure"],
            "next_action": "Halt required; no close success claimed.",
        })
        write_outputs(result, {"state": FAILED_POSITION_VERIFICATION, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": False}, "roundtrip_failed_position_verification")
        print("FAILED_POSITION_VERIFICATION")
        return 1

    result.update({"state": POSITION_OPEN_VERIFIED_SIMULATED, "position_open_verified_simulated": True})

    close_valid = payload.get("close_side") == opposite_side(payload.get("entry_side")) and payload.get("close_quantity") == payload.get("entry_quantity") and payload.get("close_reduce_only") is True
    if args.mock_close_failure:
        result.update({
            "status": "FAILED",
            "state": FAILED_CLOSE,
            "simulated_position_after_close": after_entry,
            "final_flat_verified": False,
            "halt_required": True,
            "emergency_close_required": True,
            "close_reduce_only_verified": bool(close_valid),
            "blocked_reasons": ["mock close failure"],
            "next_action": "Emergency close required in a future execution-capable phase; no actual action taken here.",
        })
        write_outputs(result, {"state": FAILED_CLOSE, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": False}, "roundtrip_failed_close")
        print("FAILED_CLOSE")
        return 1

    result.update({
        "state": CLOSE_REDUCE_ONLY_SIMULATED,
        "close_simulated": True,
        "close_reduce_only_verified": bool(close_valid),
        "simulated_position_after_close": "0",
    })

    if args.mock_final_flat_failure:
        result.update({
            "status": "FAILED",
            "state": FAILED_FINAL_FLAT_VERIFICATION,
            "final_flat_verified": False,
            "halt_required": True,
            "blocked_reasons": ["mock final flat verification failure"],
            "next_action": "Halt required; final simulated flat verification failed.",
        })
        write_outputs(result, {"state": FAILED_FINAL_FLAT_VERIFICATION, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": False}, "roundtrip_failed_final_flat")
        print("FAILED_FINAL_FLAT_VERIFICATION")
        return 1

    result.update({
        "state": FINAL_FLAT_VERIFIED_SIMULATED,
        "final_flat_verified": True,
    })
    plan["used"] = True
    plan["used_at"] = utc_now()
    write_json(PLAN_PATH, plan)
    result.update({
        "status": "COMPLETED",
        "state": COMPLETED,
        "plan_used": True,
        "next_action": "Phase 2.97A mock simulation complete; do not execute actual orders in this phase.",
    })
    state = {"state": COMPLETED, "roundtrip_plan_id": plan["roundtrip_plan_id"], "used": True, "final_position_amt": "0"}
    write_outputs(result, state, "roundtrip_completed")
    if args.telegram_preview:
        write_telegram_preview(result)
    print("COMPLETED")
    return 0


def write_telegram_preview(result: Dict[str, Any]) -> None:
    entry_pos = result.get("simulated_position_after_entry") or "0"
    text = (
        "🧪 TESTNET DUMMY ROUNDTRIP SIMULATION\n\n"
        f"Status: {result.get('status')}\n"
        f"Symbol: {result.get('symbol')}\n"
        f"Entry: {result.get('entry_side')} {result.get('entry_quantity')} MARKET\n"
        f"Simulated Position: 0 → {entry_pos}\n"
        f"Close: {result.get('close_side')} {result.get('close_quantity')} MARKET reduceOnly\n"
        f"Final Simulated Position: {result.get('simulated_position_after_close')}\n\n"
        "Simulation only.\nNo Binance order sent.\nNo position opened.\nReal Binance OFF."
    )
    write_json(TELEGRAM_PREVIEW_PATH, {"generated_at": utc_now(), "send_attempted": False, "payload_text": text})


def status() -> int:
    plan = read_json(PLAN_PATH)
    state = read_json(STATE_PATH)
    result = build_result("status", state.get("state") or (PREPARED if plan else NO_PLAN), plan if plan else None, [])
    result["next_action"] = "No plan available." if not plan else "Use full plan credentials from prepare output only when intentionally simulating."
    write_json(RESULT_PATH, result)
    print(f"state={result['state']}")
    print(f"plan={result['roundtrip_plan_id_short'] or 'none'}")
    print(f"sha={result['roundtrip_payload_sha256_short'] or 'none'}")
    print(f"used={result['plan_used']}")
    print(f"expired={result['plan_expired']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2.97A mock-only manual testnet roundtrip controller")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--simulate", action="store_true")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--approve")
    parser.add_argument("--confirm-sha256")
    parser.add_argument("--supervisor-result-path", default=SUPERVISOR_RESULT_PATH)
    parser.add_argument("--approval-request-path", default=APPROVAL_REQUEST_PATH)
    parser.add_argument("--bridge-result-path", default=BRIDGE_RESULT_PATH)
    parser.add_argument("--mock-entry-failure", action="store_true")
    parser.add_argument("--mock-position-verification-failure", action="store_true")
    parser.add_argument("--mock-close-failure", action="store_true")
    parser.add_argument("--mock-final-flat-failure", action="store_true")
    parser.add_argument("--telegram-preview", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.simulate and (not args.approve or not args.confirm_sha256):
        parser.error("--simulate requires --approve ROUNDTRIP_PLAN_ID and --confirm-sha256 ROUNDTRIP_PAYLOAD_SHA256")
    if args.prepare:
        return prepare(args)
    if args.simulate:
        return simulate(args)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
