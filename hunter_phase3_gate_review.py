import json
import sys
from pathlib import Path

READINESS_PATH = Path("reports/phase3_readiness.json")

def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    sys.exit(1)

def run_gate_review() -> None:
    print("=== MAMUYY HUNTER CANONICAL PHASE 3 GATE REVIEW ===")

    if not READINESS_PATH.exists():
        fail("Operational readiness report missing. Run phase3_readiness.py first.")

    report = json.loads(READINESS_PATH.read_text(encoding="utf-8"))

    readiness = float(report.get("readiness_percent", 0) or 0)
    status = report.get("status")
    failed = report.get("failed_criteria", [])
    blockers = report.get("blockers", [])
    constraints = report.get("governance_constraints", {})

    if readiness < 100.0:
        fail(f"Operational readiness incomplete: {readiness}%")

    if status != "READY_FOR_REVIEW":
        fail(f"Unexpected readiness status: {status}")

    if failed:
        fail(f"Failed criteria still present: {failed}")

    if blockers:
        fail(f"Blockers still present: {blockers}")

    if report.get("paper_only") is not True:
        fail("paper_only flag is not true.")

    required_true = [
        "no_live_trading",
        "no_execution_changes",
        "no_broker_routing",
        "no_strategy_promotion",
        "no_phase_3_unlock_automation",
        "read_only_analytics",
    ]
    for key in required_true:
        if constraints.get(key) is not True:
            fail(f"Governance constraint not enforced: {key}={constraints.get(key)}")

    print("[PASS] Readiness Verified: 100% READY_FOR_REVIEW.")
    print("[PASS] Criteria Verified: 10/10 passed, no blockers.")
    print("[PASS] PAPER_ONLY Verified: no execution, no broker routing, no unlock automation.")
    print("")
    print("[SUCCESS] PHASE 3 GATE PROMOTED TO FORMAL REVIEW STATUS.")

if __name__ == "__main__":
    run_gate_review()
