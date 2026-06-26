"""Pure offline policy logic for Phase 3.00.

The functions in this module only read local JSON/JSONL evidence. They do not
communicate with an exchange and do not modify runtime configuration.
"""

import glob
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

PLAN_FILE = "manual_actual_testnet_roundtrip_plan.json"
STATE_FILE = "manual_actual_testnet_roundtrip_state.json"
AUDIT_FILE = "manual_actual_testnet_roundtrip_audit.jsonl"
ORDERS_FILE = "binance_testnet_orders.jsonl"
REQUIRED_FILES = [PLAN_FILE, STATE_FILE, AUDIT_FILE, ORDERS_FILE]

REQUIRED_EVENTS = {
    "entry intent",
    "entry result",
    "entry verification",
    "close intent",
    "close result",
    "flat verification",
    "completion",
}

TIER_POLICIES = {
    3: {"target": 5, "minimum_roundtrips": 3, "minimum_distinct_utc_days": 3},
    5: {"target": 10, "minimum_roundtrips": 10, "minimum_distinct_utc_days": 7},
}


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_jsonl(path: str) -> Tuple[List[Dict[str, Any]], int]:
    rows: List[Dict[str, Any]] = []
    malformed = 0
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return rows, malformed
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows, malformed


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


def decimal_value(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def is_zero(value: Any) -> bool:
    number = decimal_value(value)
    return number is not None and number == 0


def opposite(side: Any) -> Optional[str]:
    value = str(side or "").upper()
    return "SELL" if value == "BUY" else ("BUY" if value == "SELL" else None)


def verify_checksums(directory: str) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    path = os.path.join(directory, "SHA256SUMS")
    if not os.path.exists(path):
        return False, ["checksum file missing"]
    seen = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return False, ["checksum file unreadable"]
    for raw in lines:
        parts = raw.strip().split()
        if not parts:
            continue
        if len(parts) < 2:
            reasons.append("malformed checksum line")
            continue
        expected, filename = parts[0].lower(), parts[-1].lstrip("*")
        seen.add(filename)
        target = os.path.join(directory, filename)
        if not os.path.exists(target):
            reasons.append(f"checksum target missing: {filename}")
            continue
        with open(target, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest().lower()
        if actual != expected:
            reasons.append(f"checksum mismatch: {filename}")
    absent = sorted(set(REQUIRED_FILES) - seen)
    if absent:
        reasons.append("required checksum entries missing: " + ", ".join(absent))
    return not reasons, reasons


def _in_window(row: Dict[str, Any], start: datetime, end: datetime) -> bool:
    generated = parse_time(row.get("generated_at"))
    return generated is not None and start <= generated <= end


def _same_plan(row: Dict[str, Any], plan_id: Any) -> bool:
    for key in ("actual_roundtrip_plan_id", "plan_id", "roundtrip_plan_id"):
        if row.get(key) not in (None, ""):
            return str(row.get(key)) == str(plan_id)
    return True


def inspect_evidence(directory: str) -> Dict[str, Any]:
    name = os.path.basename(directory.rstrip(os.sep))
    reasons: List[str] = []
    missing = [item for item in REQUIRED_FILES if not os.path.exists(os.path.join(directory, item))]
    if missing:
        return {
            "evidence_directory": name,
            "valid": False,
            "utc_day": None,
            "checksum_passed": False,
            "duplicate_entry_count": 0,
            "emergency_recovery_count": 0,
            "reasons": ["missing required file: " + ", ".join(missing)],
        }

    plan = read_json(os.path.join(directory, PLAN_FILE))
    state = read_json(os.path.join(directory, STATE_FILE))
    orders, malformed_orders = read_jsonl(os.path.join(directory, ORDERS_FILE))
    audit, malformed_audit = read_jsonl(os.path.join(directory, AUDIT_FILE))
    checksum_passed, checksum_reasons = verify_checksums(directory)
    reasons.extend(checksum_reasons)
    if malformed_orders:
        reasons.append("malformed order evidence")
    if malformed_audit:
        reasons.append("malformed audit evidence")
    if not plan:
        reasons.append("plan unavailable")
        return {
            "evidence_directory": name,
            "valid": False,
            "utc_day": None,
            "checksum_passed": checksum_passed,
            "duplicate_entry_count": 0,
            "emergency_recovery_count": 0,
            "reasons": sorted(set(reasons)),
        }

    payload = plan.get("actual_roundtrip_payload", {})
    plan_id = plan.get("actual_roundtrip_plan_id")
    start = parse_time(plan.get("generated_at"))
    completed = parse_time(plan.get("completed_at"))
    if start is None or completed is None:
        reasons.append("plan execution window unavailable")
        start = start or datetime.min.replace(tzinfo=timezone.utc)
        completed = completed or start
    end = completed + timedelta(seconds=60)
    scoped_orders = [row for row in orders if _in_window(row, start, end)]
    scoped_audit = [row for row in audit if _in_window(row, start, end) and _same_plan(row, plan_id)]

    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("entry_side") or "").upper()
    quantity = decimal_value(payload.get("entry_quantity"))
    entries = [
        row for row in scoped_orders
        if row.get("mode") == "actual_order"
        and row.get("order_success") is True
        and row.get("order_test") is False
        and row.get("dry_run") is False
        and not row.get("reduce_only")
        and str(row.get("symbol") or "").upper() == symbol
        and str(row.get("side") or "").upper() == side
        and decimal_value(row.get("quantity")) == quantity
    ]
    closes = [
        row for row in scoped_orders
        if row.get("mode") == "actual_close_position"
        and row.get("order_success") is True
        and row.get("order_test") is False
        and row.get("dry_run") is False
        and row.get("reduce_only") is True
        and str(row.get("symbol") or "").upper() == symbol
        and str(row.get("side") or "").upper() == opposite(side)
        and decimal_value(row.get("quantity")) == quantity
        and not is_zero(row.get("position_before_amt"))
        and is_zero(row.get("position_after_amt"))
        and row.get("blocked_reason") is None
    ]
    events = {str(row.get("event") or "").strip().lower() for row in scoped_audit}
    emergency_events = [event for event in events if "emergency" in event or "recover" in event]

    if not plan.get("consumed"):
        reasons.append("plan not consumed")
    if not plan.get("completed"):
        reasons.append("plan not completed")
    if not state or state.get("state") not in {"COMPLETED", "FINAL_FLAT_VERIFIED"}:
        reasons.append("persistent state not completed")
    if len(entries) != 1:
        reasons.append("successful entry count is not exactly one")
    if len(closes) != 1:
        reasons.append("successful close count is not exactly one")
    if len(entries) > 1:
        reasons.append("duplicate entry detected")
    if emergency_events:
        reasons.append("emergency recovery was used")
    if not REQUIRED_EVENTS.issubset(events):
        reasons.append("audit lifecycle incomplete")
    if not checksum_passed:
        reasons.append("evidence checksum failed")

    return {
        "evidence_directory": name,
        "valid": not reasons,
        "utc_day": start.date().isoformat() if start.year > 1 else None,
        "checksum_passed": checksum_passed,
        "entry_count": len(entries),
        "close_count": len(closes),
        "duplicate_entry_count": max(0, len(entries) - 1),
        "emergency_recovery_count": len(emergency_events),
        "final_flat_verified": len(closes) == 1,
        "audit_lifecycle_passed": REQUIRED_EVENTS.issubset(events),
        "reasons": sorted(set(reasons)),
    }


def find_evidence(root: str) -> List[str]:
    return sorted(
        path for path in glob.glob(os.path.join(root, "*"))
        if os.path.isdir(path) and os.path.exists(os.path.join(path, PLAN_FILE))
    )


def inspect_current_safety(path: str) -> Tuple[bool, List[str], Optional[Dict[str, Any]]]:
    result = read_json(path)
    if not result:
        return False, ["operations supervisor result unavailable"], None
    # Hard safety: these block historical evidence evaluation regardless of context.
    hard_checks = [
        (result.get("real_binance_enabled") is False, "Real Binance is enabled"),
        (result.get("allow_auto_testnet_order") is False, "automatic execution is enabled"),
    ]
    # Soft state: reflects current session/live readiness, not historical evidence validity.
    # Kept for informational display but does NOT affect safety_passed.
    soft_checks = [
        (result.get("verdict") == "SAFE_IDLE", "operations verdict is not SAFE_IDLE"),
        (result.get("final_flat_live_verified") is True, "final flat state not verified"),
        (is_zero(result.get("symbol_position_amt")), "symbol position is not zero"),
        (result.get("symbol_open_order_count") == 0, "open orders remain"),
        (not result.get("other_nonzero_positions"), "other non-zero positions exist"),
        (result.get("execution_halt_active") is False, "execution HALT is active"),
        (result.get("execution_lock_active") is False, "execution lock is active"),
        (result.get("allow_testnet_order") is False, "Testnet gate is enabled"),
        (result.get("allow_manual_actual_roundtrip") is False, "manual roundtrip gate is enabled"),
    ]
    reasons = [reason for passed, reason in hard_checks if not passed]
    soft_warnings = [reason for passed, reason in soft_checks if not passed]
    # Attach soft warnings to result dict for downstream reporting
    result["_soft_safety_warnings"] = soft_warnings
    return not reasons, reasons, result


def evaluate_policy(current_limit: int, evidence_root: str, operations_path: str) -> Dict[str, Any]:
    safety_passed, safety_reasons, operations = inspect_current_safety(operations_path)
    records = [inspect_evidence(path) for path in find_evidence(evidence_root)]
    valid = [item for item in records if item.get("valid")]
    invalid = [item for item in records if not item.get("valid")]
    days = sorted({item["utc_day"] for item in valid if item.get("utc_day")})
    duplicates = sum(int(item.get("duplicate_entry_count") or 0) for item in records)
    recoveries = sum(int(item.get("emergency_recovery_count") or 0) for item in records)
    checksum_passes = sum(1 for item in records if item.get("checksum_passed"))

    failures = list(safety_reasons)
    if not records:
        failures.append("no roundtrip evidence snapshots found")
    if invalid:
        failures.append("one or more evidence snapshots are invalid")
    if duplicates:
        failures.append("duplicate entry evidence exists")
    if recoveries:
        failures.append("emergency recovery evidence exists")

    policy = TIER_POLICIES.get(current_limit)
    verdict = f"HOLD_AT_{current_limit}"
    recommended = current_limit
    human_review = False

    # safety_passed now reflects hard safety only (real_binance, auto_exec).
    if not safety_passed or duplicates or recoveries:
        verdict = "FREEZE_LIMIT"
    elif current_limit == 10:
        verdict = "HOLD_AT_10"
    elif policy is None:
        verdict = "FREEZE_LIMIT"
        failures.append("configured limit is outside the approved 3/5/10 policy")
    else:
        if len(valid) < policy["minimum_roundtrips"]:
            failures.append(
                f"valid roundtrips {len(valid)} below required {policy['minimum_roundtrips']}"
            )
        if len(days) < policy["minimum_distinct_utc_days"]:
            failures.append(
                f"distinct UTC days {len(days)} below required {policy['minimum_distinct_utc_days']}"
            )
        if not failures:
            recommended = policy["target"]
            verdict = f"ELIGIBLE_FOR_{recommended}_REVIEW"
            human_review = True

    return {
        "verdict": verdict,
        "current_daily_order_limit": current_limit,
        "recommended_daily_order_limit": recommended,
        "human_review_required": human_review,
        "current_safety_passed": safety_passed,
        "current_operations_verdict": operations.get("verdict") if operations else None,
        "evidence_sessions_discovered": len(records),
        "valid_roundtrips": len(valid),
        "invalid_roundtrips": len(invalid),
        "distinct_utc_days": len(days),
        "evaluated_utc_days": days,
        "checksum_pass_count": checksum_passes,
        "all_checksums_passed": bool(records) and checksum_passes == len(records),
        "duplicate_entry_count": duplicates,
        "emergency_recovery_count": recoveries,
        "final_flat_roundtrips": sum(1 for item in valid if item.get("final_flat_verified")),
        "roundtrip_records": records,
        "tier_policy": policy,
        "failed_requirements": sorted(set(failures)),
    }
