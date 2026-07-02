"""Phase 2.98 read-only Testnet operations evidence supervisor."""

import argparse
import glob
import hashlib
import json
import os
import fcntl
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from binance_futures_testnet_client import DEMO_FUTURES_BASE_URL, BinanceFuturesTestnetClient, BinanceFuturesTestnetClientError, assert_testnet_base_url, load_dotenv_file
from binance_testnet_executor import DEFAULT_DAILY_ORDER_LIMIT, ORDERS_PATH, daily_order_limit, today_actual_order_count

PLAN_PATH = "logs/manual_actual_testnet_roundtrip_plan.json"
STATE_PATH = "logs/manual_actual_testnet_roundtrip_state.json"
RESULT_PATH = "logs/manual_actual_testnet_roundtrip_result.json"
STATUS_PATH = "logs/manual_actual_testnet_roundtrip_status.json"
AUDIT_PATH = "logs/manual_actual_testnet_roundtrip_audit.jsonl"
SUPERVISOR_RESULT_PATH = "logs/testnet_operations_evidence_supervisor_result.json"
TELEGRAM_PREVIEW_PATH = "logs/testnet_operations_evidence_supervisor_telegram_preview.json"
HALT_FILE_PATH = "runtime/TESTNET_EXECUTION_HALT"
LOCK_FILE_PATH = "runtime/MANUAL_ACTUAL_TESTNET_ROUNDTRIP.lock"
BROKER_MODE_REQUIRED = "BINANCE_FUTURES_TESTNET_ONLY"
PRODUCTION_URL_FRAGMENTS = ("https://fapi.binance.com", "api.binance.com")
REQUIRED_EVIDENCE_FILES = [
    "manual_actual_testnet_roundtrip_plan.json",
    "manual_actual_testnet_roundtrip_state.json",
    "manual_actual_testnet_roundtrip_audit.jsonl",
    "binance_testnet_orders.jsonl",
]
LIFECYCLE_EVENTS = {"entry intent", "entry result", "entry verification", "close intent", "close result", "flat verification", "completion"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def dec(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def dec_text(value: Any) -> Optional[str]:
    number = dec(value)
    if number is None:
        return None
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def zero(value: Any) -> bool:
    number = dec(value)
    return number is not None and number == 0


def read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: str, malformed: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(f"malformed JSONL in {path}:{line_number}")
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def inspect_execution_lock(path: str = LOCK_FILE_PATH) -> Dict[str, bool]:
    present = os.path.exists(path)
    state = {
        "execution_lock_file_present": present,
        "execution_lock_active": False,
        "execution_lock_stale_or_free": False,
    }
    if not present:
        return state
    with open(path, "rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state["execution_lock_active"] = True
            return state
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    state["execution_lock_stale_or_free"] = True
    return state


def posture() -> Dict[str, Any]:
    base_url = os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL).strip().rstrip("/")
    lock = inspect_execution_lock()
    return {
        "broker_mode": os.getenv("BROKER_MODE", BROKER_MODE_REQUIRED),
        "base_url": base_url,
        "actual_testnet_only": True,
        "real_binance_enabled": env_bool("REAL_BINANCE_ENABLED", False),
        "allow_real_binance_order": env_bool("ALLOW_REAL_BINANCE_ORDER", False),
        "allow_auto_testnet_order": env_bool("ALLOW_AUTO_TESTNET_ORDER", False),
        "allow_testnet_order": env_bool("ALLOW_TESTNET_ORDER", False),
        "allow_manual_actual_roundtrip": env_bool("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP", False),
        "execution_halt_active": env_bool("TESTNET_EXECUTION_HALT", False) or os.path.exists(HALT_FILE_PATH),
        "execution_halt_file": HALT_FILE_PATH if os.path.exists(HALT_FILE_PATH) else None,
        "execution_lock_present": lock["execution_lock_file_present"],
        **lock,
    }


def latest_evidence_dir() -> Optional[str]:
    matches = [path for path in glob.glob("evidence/phase2_97b_*") if os.path.isdir(path)]
    if not matches:
        return None
    return max(matches, key=lambda path: (os.path.getmtime(path), path))


def verify_evidence_dir(result: Dict[str, Any], review: List[str]) -> None:
    directory = latest_evidence_dir()
    result["evidence_directory"] = directory
    result["evidence_required_files_present"] = False
    result["checksum_status"] = "NOT_CHECKED"
    result["evidence_checksum_passed"] = False
    if not directory:
        review.append("evidence directory missing")
        return
    missing = [name for name in REQUIRED_EVIDENCE_FILES if not os.path.exists(os.path.join(directory, name))]
    if missing:
        review.append("missing required evidence file: " + ", ".join(missing))
        return
    result["evidence_required_files_present"] = True
    sums_path = os.path.join(directory, "SHA256SUMS")
    if not os.path.exists(sums_path):
        result["checksum_status"] = "MISSING"
        review.append("checksum file missing")
        return
    details = []
    passed = True
    with open(sums_path, "r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            parts = raw.strip().split()
            if not parts:
                continue
            if len(parts) < 2:
                passed = False
                details.append({"line": line_number, "passed": False})
                continue
            expected, filename = parts[0], parts[-1].lstrip("*")
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                passed = False
                details.append({"file": filename, "passed": False, "reason": "missing"})
                continue
            with open(path, "rb") as evidence_file:
                actual = hashlib.sha256(evidence_file.read()).hexdigest()
            ok = actual.lower() == expected.lower()
            passed = passed and ok
            details.append({"file": filename, "passed": ok})
    result["checksum_status"] = "PASS" if passed else "MISMATCH"
    result["evidence_checksum_passed"] = passed
    result["evidence_checksum_results"] = details
    if not passed:
        review.append("checksum mismatch")


def opposite(side: Any) -> Optional[str]:
    value = str(side or "").upper()
    return "SELL" if value == "BUY" else ("BUY" if value == "SELL" else None)


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evidence_path(result: Dict[str, Any], filename: str) -> str:
    directory = result.get("evidence_directory")
    return os.path.join(directory, filename) if directory else filename


def in_window(row: Dict[str, Any], start: Optional[datetime], end: Optional[datetime]) -> bool:
    when = parse_time(row.get("generated_at"))
    if when is None or start is None or end is None:
        return False
    return start <= when <= end


def plan_id_matches(row: Dict[str, Any], plan_id: Any) -> bool:
    if not plan_id:
        return True
    for key in ("actual_roundtrip_plan_id", "plan_id", "roundtrip_plan_id"):
        if key in row and row.get(key) not in (None, ""):
            return str(row.get(key)) == str(plan_id)
    return True


def roundtrip_evidence(result: Dict[str, Any], review: List[str], blocked: List[str]) -> None:
    malformed: List[str] = []
    result["roundtrip_evidence_source"] = "EVIDENCE_DIRECTORY"
    plan = read_json(evidence_path(result, "manual_actual_testnet_roundtrip_plan.json"))
    state = read_json(evidence_path(result, "manual_actual_testnet_roundtrip_state.json"))
    execution_result = read_json(RESULT_PATH)
    status = read_json(STATUS_PATH)
    orders = read_jsonl(evidence_path(result, "binance_testnet_orders.jsonl"), malformed)
    audit = read_jsonl(evidence_path(result, "manual_actual_testnet_roundtrip_audit.jsonl"), malformed)
    review.extend(malformed)
    payload = plan.get("actual_roundtrip_payload", {}) if plan else {}
    symbol = str(result.get("symbol") or payload.get("symbol") or "").upper()
    entry_side = str(payload.get("entry_side") or "").upper()
    entry_qty = dec(payload.get("entry_quantity"))
    plan_id = plan.get("actual_roundtrip_plan_id") if plan else None
    start = parse_time(plan.get("generated_at") if plan else None)
    completed = parse_time(plan.get("completed_at") if plan else None)
    end = completed.replace() if completed else None
    if end:
        end = end + timedelta(seconds=60)
    orders_for_plan = [o for o in orders if in_window(o, start, end)]
    audit_for_plan = [a for a in audit if in_window(a, start, end) and plan_id_matches(a, plan_id)]
    result.update({
        "plan_available": bool(plan),
        "plan_consumed": bool(plan and plan.get("consumed")),
        "plan_completed": bool(plan and plan.get("completed")),
        "plan_expired": False,
        "persistent_state": state.get("state") if state else None,
        "execution_result_available": bool(execution_result),
        "audit_event_count": len(audit_for_plan),
        "audit_total_rows_in_snapshot": len(audit),
        "audit_rows_for_current_plan": len(audit_for_plan),
        "audit_event_names_for_current_plan": sorted({str(a.get("event") or "") for a in audit_for_plan if a.get("event")}),
    })
    if plan and plan.get("expires_at"):
        expires = parse_time(plan["expires_at"])
        if expires is None:
            review.append("plan expiry malformed")
        else:
            result["plan_expired"] = expires <= datetime.now(timezone.utc)
    if not plan:
        review.append("plan missing")
    if start is None or completed is None:
        review.append("plan execution window unavailable")
    if not execution_result:
        review.append("result file unavailable")
    if not result["plan_consumed"]:
        review.append("plan not consumed")
    if not result["plan_completed"]:
        review.append("plan not completed")
    if result["persistent_state"] not in {"COMPLETED", "FINAL_FLAT_VERIFIED"}:
        review.append("persistent state not completed")
    if status and execution_result and status.get("state") and execution_result.get("state") and status.get("state") != execution_result.get("state"):
        review.append("status/result evidence inconsistency")

    entries = [o for o in orders_for_plan if o.get("mode") == "actual_order" and o.get("order_success") is True and o.get("order_test") is False and o.get("dry_run") is False and not o.get("reduce_only") and (not symbol or str(o.get("symbol", "")).upper() == symbol) and (not entry_side or str(o.get("side", "")).upper() == entry_side) and (entry_qty is None or dec(o.get("quantity")) == entry_qty)]
    close_candidates = [o for o in orders_for_plan if o.get("mode") == "actual_close_position" and o.get("order_success") is True and o.get("order_test") is False and o.get("dry_run") is False and o.get("reduce_only") is True and (not symbol or str(o.get("symbol", "")).upper() == symbol) and (not entry_side or str(o.get("side", "")).upper() == opposite(entry_side)) and not zero(o.get("position_before_amt")) and dec(o.get("quantity")) == abs(dec(o.get("position_before_amt")) or Decimal("0"))]
    closes = [o for o in close_candidates if zero(o.get("position_after_amt"))]
    result["successful_entry_count"] = len(entries)
    result["successful_reduce_only_close_count"] = len(closes)
    result["duplicate_entry_detected"] = len(entries) > 1
    entry = entries[-1] if entries else None
    close = closes[-1] if closes else None
    result["entry_close_symbol_match"] = bool(entry and close and str(entry.get("symbol", "")).upper() == str(close.get("symbol", "")).upper())
    result["entry_close_quantity_match"] = bool(entry and close and dec(entry.get("quantity")) == dec(close.get("quantity")))
    result["entry_close_side_match"] = bool(entry and close and opposite(entry.get("side")) == str(close.get("side", "")).upper())
    result["close_position_after_zero"] = bool(close and zero(close.get("position_after_amt")))
    result["close_position_before_nonzero"] = bool(close and not zero(close.get("position_before_amt")))
    result["close_blocked_reason_null"] = bool(close and close.get("blocked_reason") is None)
    events = {str(a.get("event") or "").strip().lower() for a in audit_for_plan}
    result["audit_lifecycle_passed"] = LIFECYCLE_EVENTS.issubset(events)
    if len(entries) != 1:
        review.append("missing successful entry" if len(entries) == 0 else "duplicate entry detected")
    if len(closes) != 1:
        review.append("missing reduce-only close" if len(closes) == 0 else "duplicate close detected")
    for key, reason in [("entry_close_symbol_match", "entry/close symbol mismatch"), ("entry_close_quantity_match", "entry/close quantity mismatch"), ("entry_close_side_match", "wrong close side"), ("close_position_before_nonzero", "close position_before is zero"), ("close_blocked_reason_null", "close blocked_reason is not null"), ("audit_lifecycle_passed", "audit evidence incomplete")]:
        if not result.get(key):
            review.append(reason)
    if any(not zero(c.get("position_after_amt")) for c in close_candidates):
        blocked.append("close position_after non-zero")
    elif not result["close_position_after_zero"]:
        review.append("close position_after not zero")

def daily_capacity(result: Dict[str, Any]) -> None:
    result["daily_capacity_source"] = "LIVE_ORDER_LOG"
    count = today_actual_order_count(ORDERS_PATH)
    limit = daily_order_limit()
    remaining = max(0, limit - count)
    result.update({
        "daily_actual_order_count": count,
        "daily_order_limit": limit,
        "remaining_daily_order_slots": remaining,
        "emergency_close_slot_available": remaining >= 1,
        "full_roundtrip_required_slots": 3,
        "full_roundtrip_capacity_passed": remaining >= 3,
    })


def normalize_positions(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return [payload] if isinstance(payload, dict) else []


def live_read(symbol: str, result: Dict[str, Any], blocked: List[str], review: List[str], client_class: Any = BinanceFuturesTestnetClient) -> None:
    base_url = result["base_url"]
    try:
        assert_testnet_base_url(base_url)
    except BinanceFuturesTestnetClientError as exc:
        blocked.append(str(exc))
        return
    api = client_class(base_url=base_url)
    try:
        account = api.get_account()
        positions = normalize_positions(api.get_position_risk())
        orders = api.get_open_orders(symbol)
        mark = api.get_mark_price(symbol)
        api.get_exchange_info()
    except BinanceFuturesTestnetClientError as exc:
        review.append(f"live read failed: {exc}")
        return
    result["live_check_performed"] = True
    result["account_read_passed"] = True
    result["can_trade"] = bool(account.get("canTrade"))
    result["wallet_balance"] = dec_text(account.get("totalWalletBalance"))
    result["available_balance"] = dec_text(account.get("availableBalance"))
    symbol_amt = Decimal("0")
    symbol_notional = Decimal("0")
    other: List[str] = []
    for item in positions:
        item_symbol = str(item.get("symbol", "")).upper()
        amt = dec(item.get("positionAmt")) or Decimal("0")
        if item_symbol == symbol:
            symbol_amt = amt
            symbol_notional = dec(item.get("notional")) or Decimal("0")
        elif amt != 0:
            other.append(item_symbol)
    live_orders = [o for o in orders if isinstance(o, dict)] if isinstance(orders, list) else []
    result["symbol_position_amt"] = dec_text(symbol_amt)
    result["symbol_position_notional"] = dec_text(symbol_notional)
    result["symbol_open_order_count"] = len(live_orders)
    result["other_nonzero_positions"] = other
    result["final_flat_live_verified"] = symbol_amt == 0 and not live_orders and not other
    result["live_mark_price"] = dec_text(mark)
    if result.get("plan_completed") and symbol_amt != 0:
        blocked.append("unexpected live non-zero position after completed plan")
    if result.get("plan_completed") and live_orders:
        blocked.append("open order remains after completed plan")
    if other:
        blocked.append("another non-zero symbol position exists")


def build_result(mode: str, symbol: str) -> Dict[str, Any]:
    p = posture()
    result = {
        "generated_at": utc_now(), "mode": mode, "verdict": "REVIEW_REQUIRED", "status": "REVIEW_REQUIRED", "symbol": symbol,
        **p,
        "live_check_performed": False, "account_read_passed": False, "can_trade": None, "symbol_position_amt": None,
        "symbol_position_notional": None, "symbol_open_order_count": None, "other_nonzero_positions": [], "final_flat_live_verified": False,
        "plan_available": False, "plan_consumed": False, "plan_completed": False, "plan_expired": False, "persistent_state": None,
        "execution_result_available": False, "successful_entry_count": 0, "successful_reduce_only_close_count": 0,
        "duplicate_entry_detected": False, "entry_close_symbol_match": False, "entry_close_quantity_match": False,
        "entry_close_side_match": False, "close_position_after_zero": False, "audit_event_count": 0, "audit_lifecycle_passed": False,
        "evidence_directory": None, "evidence_required_files_present": False, "checksum_status": "NOT_CHECKED", "evidence_checksum_passed": False,
        "daily_actual_order_count": 0, "daily_order_limit": DEFAULT_DAILY_ORDER_LIMIT, "remaining_daily_order_slots": 0,
        "emergency_close_slot_available": False, "full_roundtrip_required_slots": 3, "full_roundtrip_capacity_passed": False,
        "blocked_reasons": [], "review_reasons": [], "soft_safety_warnings": [], "phase3_armed": False, "next_action": None,
    }
    return result


def finalize(result: Dict[str, Any], blocked: List[str], review: List[str]) -> None:
    if result["real_binance_enabled"]:
        blocked.append("Real Binance is enabled")
    if result["allow_real_binance_order"]:
        blocked.append("Real Binance order gate is enabled")
    if result["allow_auto_testnet_order"]:
        blocked.append("automatic execution is enabled")
    if result["allow_testnet_order"]:
        # CP-044A: since the CP-042 Phase 3 unlock, ALLOW_TESTNET_ORDER=true is
        # the approved semi-manual steady state, not an anomaly. Report it as an
        # armed-state warning; hard blockers below are unchanged.
        result["phase3_armed"] = True
        result["soft_safety_warnings"].append(
            "Testnet order gate is enabled (Phase 3 semi-manual armed state)"
        )
    if result["allow_manual_actual_roundtrip"]:
        blocked.append("manual actual roundtrip gate is enabled")
    if result["execution_halt_active"]:
        blocked.append("emergency halt active")
    if result["execution_lock_active"]:
        blocked.append("execution lock active")
    if result["base_url"] != DEMO_FUTURES_BASE_URL:
        msg = "production Binance URL is configured" if any(f in result["base_url"] for f in PRODUCTION_URL_FRAGMENTS) else "non-demo Binance URL is configured"
        blocked.append(msg)
    if result["broker_mode"] != BROKER_MODE_REQUIRED:
        blocked.append("broker mode is not Testnet-only")
    if not result["live_check_performed"]:
        review.append("no live inspection was performed when live state is required for a definitive verdict")
    result["blocked_reasons"] = sorted(set(blocked))
    result["review_reasons"] = sorted(set(review))
    if result["blocked_reasons"]:
        result["verdict"] = result["status"] = "HALTED"
        result["next_action"] = "Resolve halt/blocking safety condition before any operation."
    elif result["review_reasons"]:
        result["verdict"] = result["status"] = "REVIEW_REQUIRED"
        result["next_action"] = "Review evidence artifacts before further testnet operations."
    else:
        result["verdict"] = result["status"] = "SAFE_IDLE"
        result["next_action"] = "Remain idle; do not start another full roundtrip without capacity and manual approval."


def telegram_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    position = "FLAT" if zero(result.get("symbol_position_amt")) or not result.get("live_check_performed") else "NON-FLAT"
    text = "\n".join([
        "🛡 MAMUYY TESTNET OPERATIONS STATUS", "", f"Verdict: {result['verdict']}", f"Symbol: {result['symbol']}",
        f"Position: {position}", f"Open Orders: {result.get('symbol_open_order_count') if result.get('symbol_open_order_count') is not None else 'UNKNOWN'}", "",
        f"Roundtrip Evidence: {'PASS' if not result['review_reasons'] and not result['blocked_reasons'] else 'REVIEW'}",
        f"Plan: {'COMPLETED' if result.get('plan_completed') else 'NOT COMPLETED'} / {'CONSUMED' if result.get('plan_consumed') else 'NOT CONSUMED'}",
        f"Daily Orders: {result['daily_actual_order_count']} / {result['daily_order_limit']}",
        f"Emergency Slot: {'AVAILABLE' if result['emergency_close_slot_available'] else 'UNAVAILABLE'}",
        f"New Roundtrip Capacity: {'PASS' if result['full_roundtrip_capacity_passed'] else 'BLOCKED'}", "",
        f"HALT: {'ON' if result['execution_halt_active'] else 'OFF'}", f"Real Binance: {'ON' if result['real_binance_enabled'] else 'OFF'}",
        f"Auto Execution: {'ON' if result['allow_auto_testnet_order'] else 'OFF'}",
        f"Phase 3 Armed: {'YES' if result.get('phase3_armed') else 'NO'}", "", "Read-only report.", "No order sent.",
    ])
    return {"generated_at": utc_now(), "preview": text}


def print_summary(result: Dict[str, Any]) -> None:
    position = "FLAT" if zero(result.get("symbol_position_amt")) or not result.get("live_check_performed") else "NON-FLAT"
    checksum = "PASS" if result.get("evidence_checksum_passed") else result.get("checksum_status")
    print(f"TESTNET OPERATIONS EVIDENCE SUPERVISOR: {result['verdict']}")
    print(f"Position: {position}")
    print(f"Open Orders: {result.get('symbol_open_order_count') if result.get('symbol_open_order_count') is not None else 'UNKNOWN'}")
    print(f"Roundtrip Evidence: {'PASS' if not result['review_reasons'] and not result['blocked_reasons'] else 'REVIEW'}")
    print(f"Evidence Checksums: {checksum}")
    print(f"Daily Capacity: {result['daily_actual_order_count']} / {result['daily_order_limit']} used")
    print(f"Emergency Slot: {'AVAILABLE' if result['emergency_close_slot_available'] else 'UNAVAILABLE'}")
    print(f"New Full Roundtrip: {'PERMITTED' if result['full_roundtrip_capacity_passed'] else 'NOT PERMITTED'}")
    print(f"Real Binance: {'ON' if result['real_binance_enabled'] else 'OFF'}")
    print(f"Auto Execution: {'ON' if result['allow_auto_testnet_order'] else 'OFF'}")
    print(f"Phase 3 Armed: {'YES' if result.get('phase3_armed') else 'NO'}")
    for warning in result.get("soft_safety_warnings", []):
        print(f"Soft Warning: {warning}")


def run(mode: str, symbol: str, make_preview: bool = False, client_factory: Any = None) -> Dict[str, Any]:
    load_dotenv_file()
    symbol = symbol.upper()
    result = build_result(mode, symbol)
    blocked: List[str] = []
    review: List[str] = []
    verify_evidence_dir(result, review)
    roundtrip_evidence(result, review, blocked)
    daily_capacity(result)
    live_needed = mode in {"live-read-only", "full"}
    if live_needed:
        live_read(symbol, result, blocked, review, client_factory or BinanceFuturesTestnetClient)
    finalize(result, blocked, review)
    write_json(SUPERVISOR_RESULT_PATH, result)
    if make_preview:
        write_json(TELEGRAM_PREVIEW_PATH, telegram_preview(result))
    print_summary(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Testnet operations evidence supervisor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--live-read-only", action="store_true")
    group.add_argument("--verify-evidence", action="store_true")
    group.add_argument("--full", action="store_true")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--telegram-preview", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = "status" if args.status else ("live-read-only" if args.live_read_only else ("verify-evidence" if args.verify_evidence else "full"))
    result = run(mode, args.symbol, args.telegram_preview)
    return 0 if result["verdict"] == "SAFE_IDLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
