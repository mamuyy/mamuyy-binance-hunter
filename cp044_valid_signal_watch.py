"""Read-only CP-044 valid signal watch report.

The watch evaluates the latest ML overlay candidate, the semi-auto bridge dry-run
verdict, and the read-only operations supervisor posture. It never sends orders
and never recommends automatic sending.
"""

import argparse
import json
import os
from typing import Any, Dict, List, Optional

import semi_auto_testnet_bridge as bridge
import testnet_operations_evidence_supervisor as supervisor

REPORT_PATH = "reports/cp044_valid_signal_watch.json"
ALLOWLIST = {"BTCUSDT", "ETHUSDT", "HYPEUSDT"}


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    overlay_report, overlay_preview = bridge.load_overlay_inputs(args.overlay_report_path)
    inputs = bridge.extract_decision_inputs(overlay_report, overlay_preview or {}, args.symbol or "")
    freshness_seconds = bridge.env_float("TESTNET_OVERLAY_FRESHNESS_SECONDS", bridge.DEFAULT_OVERLAY_FRESHNESS_SECONDS)
    freshness_passed, freshness_reasons, freshness = bridge.overlay_freshness_check(overlay_report, freshness_seconds)

    bridge_args = argparse.Namespace(
        overlay_report_path=args.overlay_report_path,
        symbol=args.symbol or "",
        telegram_preview=False,
        allow_need_review=False,
        suppress_approval_proposal=True,
    )
    bridge_result = bridge.run(bridge_args)

    supervisor_result = read_json(args.supervisor_result_path)
    if supervisor_result is None or args.refresh_supervisor:
        supervisor_result = supervisor.run(args.supervisor_mode, inputs.get("symbol") or args.symbol or "BTCUSDT", make_preview=False)

    score = inputs.get("signal_score")
    rank = inputs.get("trade_rank")
    direction = inputs.get("direction")
    allowlist_passed = inputs.get("symbol") in ALLOWLIST
    score_passed = score is not None and score >= 90
    direction_passed = inputs.get("side") is not None and direction != "UNKNOWN"
    rank_passed = bool(rank) and rank not in {"UNKNOWN", "UNRANKED"}
    supervisor_verdict = supervisor_result.get("verdict")
    phase3_armed = bool(supervisor_result.get("phase3_armed"))
    daily_capacity_available = bool(bridge_result.get("daily_limit_passed"))
    real_trading_locked = not bool(supervisor_result.get("real_binance_enabled")) and not bool(supervisor_result.get("allow_real_binance_order"))
    auto_execution_locked = not bool(supervisor_result.get("allow_auto_testnet_order"))

    blocked_reasons: List[str] = []
    checks = {
        "allowlist_passed": allowlist_passed,
        "score_passed": score_passed,
        "freshness_passed": freshness_passed,
        "direction_passed": direction_passed,
        "rank_passed": rank_passed,
        "daily_capacity_available": daily_capacity_available,
        "real_trading_locked": real_trading_locked,
        "auto_execution_locked": auto_execution_locked,
        "supervisor_safe_idle": supervisor_verdict == "SAFE_IDLE",
        "phase3_armed": phase3_armed,
        "bridge_would_order": bridge_result.get("status") == "WOULD_ORDER",
    }
    for name, passed in checks.items():
        if not passed:
            blocked_reasons.append(name)
    blocked_reasons.extend(freshness_reasons)
    blocked_reasons.extend(str(reason) for reason in bridge_result.get("blocked_reasons", []))
    blocked_reasons.extend(str(reason) for reason in supervisor_result.get("blocked_reasons", []))
    blocked_reasons = sorted(set(blocked_reasons))

    ready = not blocked_reasons
    verdict = "READY_FOR_PREPARE" if ready else "WAITING_FOR_VALID_SIGNAL"
    if not real_trading_locked or not auto_execution_locked or supervisor_verdict not in {"SAFE_IDLE", "REVIEW_REQUIRED"}:
        verdict = "BLOCKED"

    return {
        "generated_at": bridge.utc_now(),
        "verdict": verdict,
        "latest_candidate_symbol": inputs.get("symbol"),
        "latest_candidate_score": score,
        "latest_candidate_direction": direction,
        "latest_candidate_rank": rank,
        "allowlist_passed": allowlist_passed,
        "score_passed": score_passed,
        "freshness_passed": freshness_passed,
        **freshness,
        "direction_passed": direction_passed,
        "rank_passed": rank_passed,
        "supervisor_verdict": supervisor_verdict,
        "phase3_armed": phase3_armed,
        "bridge_dry_run_verdict": bridge_result.get("status"),
        "daily_capacity_available": daily_capacity_available,
        "blocked_reasons": blocked_reasons,
        "next_action": "RUN_CP044_STEP_1_TO_3_ONLY" if ready else "WAIT_FOR_TELEGRAM_CANDIDATE",
        "orders_placed": False,
        "broker_called": False,
        "real_trading_locked": real_trading_locked,
        "auto_execution_locked": auto_execution_locked,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the CP-044 valid signal watch report.")
    parser.add_argument("--overlay-report-path", default=bridge.OVERLAY_REPORT_PATH)
    parser.add_argument("--supervisor-result-path", default=supervisor.SUPERVISOR_RESULT_PATH)
    parser.add_argument("--supervisor-mode", default="status")
    parser.add_argument("--refresh-supervisor", action="store_true")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--output", default=REPORT_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = evaluate(args)
    write_json(args.output, result)
    print(f"CP044_VALID_SIGNAL_WATCH: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
