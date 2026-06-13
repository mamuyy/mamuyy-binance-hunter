"""Phase 3.00 offline Testnet stability and daily-limit expansion gate.

This command reads local evidence and produces a policy recommendation only.
It never changes configuration and never communicates with an exchange.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from binance_futures_testnet_client import load_dotenv_file
from testnet_stability_policy import evaluate_policy

RESULT_PATH = "logs/testnet_stability_limit_expansion_gate_result.json"
TELEGRAM_PREVIEW_PATH = "logs/testnet_stability_limit_expansion_gate_telegram_preview.json"
OPERATIONS_RESULT_PATH = "logs/testnet_operations_evidence_supervisor_result.json"
EVIDENCE_ROOT = "evidence"
DEFAULT_POLICY_LIMIT = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def configured_daily_limit() -> int:
    raw = os.getenv("TESTNET_MAX_ORDERS_PER_DAY") or os.getenv("TESTNET_DAILY_ORDER_LIMIT")
    if raw in (None, ""):
        return DEFAULT_POLICY_LIMIT
    try:
        return int(raw)
    except ValueError:
        return -1


def evaluate(
    evidence_root: str = EVIDENCE_ROOT,
    operations_result_path: str = OPERATIONS_RESULT_PATH,
    configured_limit: int | None = None,
) -> Dict[str, Any]:
    load_dotenv_file()
    current_limit = configured_daily_limit() if configured_limit is None else configured_limit
    result = evaluate_policy(current_limit, evidence_root, operations_result_path)
    result.update({
        "generated_at": utc_now(),
        "mode": "read_only_limit_expansion_gate",
        "status": result["verdict"],
        "limit_expansion_authorized": False,
        "configuration_changed": False,
        "order_attempted": False,
        "order_success": False,
    })
    if result["verdict"] == "FREEZE_LIMIT":
        result["next_action"] = "Do not raise the limit; resolve safety or evidence failures first."
    elif result["verdict"].startswith("ELIGIBLE_FOR_"):
        result["next_action"] = (
            "Human review may approve a separate configuration change; this gate does not change the limit."
        )
    else:
        result["next_action"] = "Keep the current limit and collect additional clean roundtrip evidence."
    return result


def telegram_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    text = "\n".join([
        "🧪 MAMUYY TESTNET STABILITY GATE",
        "",
        f"Verdict: {result['verdict']}",
        f"Current Limit: {result['current_daily_order_limit']}",
        f"Recommended Limit: {result['recommended_daily_order_limit']}",
        f"Valid Roundtrips: {result['valid_roundtrips']}",
        f"Distinct UTC Days: {result['distinct_utc_days']}",
        f"Current Safety: {'PASS' if result['current_safety_passed'] else 'FAIL'}",
        f"Checksums: {'PASS' if result['all_checksums_passed'] else 'REVIEW'}",
        f"Duplicate Entries: {result['duplicate_entry_count']}",
        f"Emergency Recoveries: {result['emergency_recovery_count']}",
        "",
        "Read-only policy gate.",
        "No order sent.",
        "No limit changed.",
    ])
    return {"generated_at": utc_now(), "preview": text}


def print_summary(result: Dict[str, Any]) -> None:
    print(f"TESTNET STABILITY LIMIT EXPANSION GATE: {result['verdict']}")
    print(f"Current Limit: {result['current_daily_order_limit']}")
    print(f"Recommended Limit: {result['recommended_daily_order_limit']}")
    print(f"Valid Roundtrips: {result['valid_roundtrips']}")
    print(f"Distinct UTC Days: {result['distinct_utc_days']}")
    print(f"Current Safety: {'PASS' if result['current_safety_passed'] else 'FAIL'}")
    print(f"Checksums: {'PASS' if result['all_checksums_passed'] else 'REVIEW'}")
    print(f"Duplicate Entries: {result['duplicate_entry_count']}")
    print(f"Emergency Recoveries: {result['emergency_recovery_count']}")
    print("Limit Changed: NO")
    print("Order Sent: NO")


def run(make_preview: bool = False) -> Dict[str, Any]:
    result = evaluate()
    write_json(RESULT_PATH, result)
    if make_preview:
        write_json(TELEGRAM_PREVIEW_PATH, telegram_preview(result))
    print_summary(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline Testnet stability policy evaluator")
    parser.add_argument("--evaluate", action="store_true", required=True)
    parser.add_argument("--telegram-preview", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args.telegram_preview)
    return 2 if result["verdict"] == "FREEZE_LIMIT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
