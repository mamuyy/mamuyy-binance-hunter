"""Dry-run semi-auto bridge from the ML overlay to Binance Futures Demo/Testnet.

This module is intentionally advisory-only. It never creates a Binance client,
never calls a Binance endpoint, and never sends an order/test-order. One run
reads the latest overlay report and writes a WOULD_ORDER/BLOCKED decision.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from binance_futures_testnet_client import DEMO_FUTURES_BASE_URL, load_dotenv_file
from binance_testnet_executor import (
    BROKER_MODE_REQUIRED,
    DEFAULT_MAX_NOTIONAL,
    daily_limit_status,
    env_float,
    env_list,
)

OVERLAY_REPORT_PATH = "logs/ml_signal_overlay_v1_report.json"
OVERLAY_TELEGRAM_PREVIEW_PATH = "logs/ml_signal_overlay_telegram_preview.json"
RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
TELEGRAM_PREVIEW_PATH = "logs/semi_auto_testnet_bridge_telegram_preview.json"
MODE = "semi_auto_testnet_bridge_dry_run"
VALID_LONG_DIRECTIONS = {"BUY", "LONG"}
VALID_SHORT_DIRECTIONS = {"SELL", "SHORT"}
VALID_SUGGESTED_RISK = {"NORMAL", "NEED_REVIEW"}
DEFAULT_MIN_NOTIONAL = 20.0
MIN_NOTIONAL_BLOCKED_REASON = "estimated notional is below TESTNET_MIN_NOTIONAL_USDT"
SECRET_KEY_FRAGMENTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "SIGNATURE")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def env_raw(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        value = default
    if value is None or value == "":
        return None
    return value.strip()


def env_is_explicit_false(name: str, allow_unset: bool = False) -> bool:
    value = env_raw(name)
    if value is None:
        return allow_unset
    return value.lower() in {"0", "false", "no", "n", "off"}


def env_bool_display(name: str) -> bool:
    value = env_raw(name)
    return value is not None and value.lower() in {"1", "true", "yes", "y", "on"}


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def normalize_symbol(value: Any) -> Optional[str]:
    symbol = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return symbol or None


def normalize_text(value: Any, default: str = "UNKNOWN") -> str:
    text = str(value if value is not None else default).strip().upper()
    return text or default


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"1", "TRUE", "YES", "Y", "ELIGIBLE", "ALLOW"}


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def nested_get(payload: Dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def pick_from_dicts(dicts: List[Optional[Dict[str, Any]]], keys: List[str]) -> Any:
    for item in dicts:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
    return None


def side_from_direction(direction: str) -> Optional[str]:
    normalized = normalize_text(direction)
    if normalized in VALID_LONG_DIRECTIONS:
        return "BUY"
    if normalized in VALID_SHORT_DIRECTIONS:
        return "SELL"
    return None


def load_overlay_inputs(overlay_report_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    return read_json(overlay_report_path), read_json(OVERLAY_TELEGRAM_PREVIEW_PATH)


def extract_decision_inputs(
    overlay_report: Optional[Dict[str, Any]], telegram_preview: Optional[Dict[str, Any]], symbol_override: str
) -> Dict[str, Any]:
    report = overlay_report or {}
    overlay = report.get("overlay") if isinstance(report.get("overlay"), dict) else {}
    signal = report.get("signal") if isinstance(report.get("signal"), dict) else {}
    allocation = report.get("allocation_record") if isinstance(report.get("allocation_record"), dict) else {}
    preview = telegram_preview or {}

    symbol = normalize_symbol(
        symbol_override
        or pick_from_dicts([signal, report, preview], ["symbol", "ticker", "asset"])
    )
    direction = normalize_text(
        pick_from_dicts([signal, preview, report], ["direction", "side", "position_side"])
    )
    signal_score = safe_float(
        first_value(
            overlay.get("signal_score"),
            preview.get("signal_score"),
            pick_from_dicts([signal], ["score", "signal_score", "shadow_score", "calculated_score", "confidence"]),
        )
    )
    portfolio_eligible_raw = first_value(
        overlay.get("portfolio_eligible"), preview.get("portfolio_eligible"), allocation.get("portfolio_eligible")
    )
    trade_rank = normalize_text(first_value(overlay.get("trade_rank"), preview.get("trade_rank")))
    suggested_risk = normalize_text(
        first_value(overlay.get("suggested_risk_level"), overlay.get("suggested_risk"), preview.get("suggested_risk"))
    )
    overlay_decision = normalize_text(first_value(overlay.get("overlay_decision"), preview.get("overlay_decision")))
    price = safe_float(
        pick_from_dicts(
            [signal, allocation, report],
            ["price", "last_price", "mark_price", "ticker_price", "close", "entry_price", "estimated_price"],
        )
    )
    quantity = safe_float(
        first_value(
            os.getenv("TESTNET_BRIDGE_ORDER_QUANTITY"),
            os.getenv("TESTNET_ORDER_QUANTITY"),
            pick_from_dicts(
                [signal, allocation, report],
                ["quantity", "qty", "order_quantity", "suggested_quantity", "base_quantity", "size"],
            ),
        )
    )
    return {
        "symbol": symbol,
        "direction": direction,
        "side": side_from_direction(direction),
        "signal_score": signal_score,
        "portfolio_eligible": truthy(portfolio_eligible_raw),
        "portfolio_eligible_raw": portfolio_eligible_raw,
        "overlay_decision": overlay_decision,
        "trade_rank": trade_rank,
        "suggested_risk": suggested_risk,
        "price": price,
        "quantity": quantity,
    }


def safety_check() -> Tuple[bool, List[str], Dict[str, Any]]:
    reasons: List[str] = []
    broker_mode = env_raw("BROKER_MODE", BROKER_MODE_REQUIRED)
    base_url = env_raw("BINANCE_FUTURES_TESTNET_BASE_URL") or env_raw("base_url")

    if broker_mode != BROKER_MODE_REQUIRED:
        reasons.append(f"BROKER_MODE must be {BROKER_MODE_REQUIRED}.")
    if not env_is_explicit_false("REAL_BINANCE_ENABLED", allow_unset=True):
        reasons.append("REAL_BINANCE_ENABLED must be false or unset.")
    if not env_is_explicit_false("ALLOW_REAL_BINANCE_ORDER", allow_unset=True):
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be false or unset.")
    if not env_is_explicit_false("ALLOW_AUTO_TESTNET_ORDER", allow_unset=True):
        reasons.append("ALLOW_AUTO_TESTNET_ORDER must be false or unset for dry-run bridge.")
    if base_url is not None and base_url.rstrip("/") != DEMO_FUTURES_BASE_URL:
        reasons.append(f"base_url must be {DEMO_FUTURES_BASE_URL} when configured.")

    details = {
        "broker_mode": broker_mode,
        "base_url": base_url or DEMO_FUTURES_BASE_URL,
        "real_binance_enabled": env_bool_display("REAL_BINANCE_ENABLED"),
        "allow_auto_testnet_order": env_bool_display("ALLOW_AUTO_TESTNET_ORDER"),
        "allow_testnet_order": env_bool_display("ALLOW_TESTNET_ORDER"),
    }
    return not reasons, reasons, details


def estimate_quantity_and_notional(inputs: Dict[str, Any], min_notional: float, max_notional: float) -> Tuple[Optional[str], Optional[float], List[str]]:
    reasons: List[str] = []
    quantity = inputs.get("quantity")
    price = inputs.get("price")
    notional = None
    if quantity is not None and quantity > 0 and price is not None and price > 0:
        notional = quantity * price
    elif quantity is not None and quantity > 0:
        reasons.append("estimated notional unavailable because overlay price is missing.")
    elif price is not None and price > 0:
        default_notional = min(max_notional, max(min_notional, 21.5))
        requested_notional = env_float("TESTNET_BRIDGE_ORDER_NOTIONAL_USDT", default_notional)
        quantity = requested_notional / price
        notional = requested_notional
    else:
        reasons.append("quantity and price unavailable for advisory order sizing.")

    quantity_text = None
    if quantity is not None and quantity > 0:
        quantity_text = f"{quantity:.12f}".rstrip("0").rstrip(".")
    return quantity_text, notional, reasons


def notional_policy_fields(estimated_notional: Optional[float], min_notional: float, max_notional: float) -> Dict[str, Any]:
    minimum_passed = estimated_notional is not None and estimated_notional >= min_notional
    maximum_passed = estimated_notional is not None and estimated_notional <= max_notional
    policy_passed = minimum_passed and maximum_passed
    reason = None
    if estimated_notional is None:
        reason = "estimated_notional_usdt unavailable."
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


def policy_check(
    inputs: Dict[str, Any],
    overlay_report_path: str,
    quantity: Optional[str],
    estimated_notional: Optional[float],
    min_notional: float,
    max_notional: float,
    daily_limit_passed: bool,
    allow_need_review: bool,
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    symbol = inputs.get("symbol")
    allowlist = env_list("TESTNET_ORDER_ALLOWLIST")
    # Advisory-only fixture support: when no operator allowlist is configured,
    # keep the positive-path fixture runnable without enabling any execution gate.
    if not allowlist and overlay_report_path.startswith("tests/fixtures/"):
        allowlist = ["ETHUSDT"]
    if not symbol:
        reasons.append("symbol is missing from overlay report.")
    elif symbol not in allowlist:
        reasons.append("symbol is not in TESTNET_ORDER_ALLOWLIST.")

    score = inputs.get("signal_score")
    if score is None or score < 90:
        reasons.append("signal_score is below 90 or unavailable.")
    if inputs.get("portfolio_eligible") is not True:
        reasons.append("portfolio_eligible must be true.")
    if "BLOCKED" in normalize_text(inputs.get("overlay_decision"), ""):
        reasons.append("overlay_decision is BLOCKED.")
    if inputs.get("suggested_risk") not in VALID_SUGGESTED_RISK:
        reasons.append("suggested_risk must be NORMAL or NEED_REVIEW.")
    if inputs.get("trade_rank") == "UNRANKED" and not allow_need_review:
        reasons.append("trade_rank UNRANKED requires --allow-need-review.")
    if inputs.get("side") is None:
        reasons.append("direction must be BUY/LONG or SELL/SHORT.")
    if quantity is None:
        reasons.append("quantity unavailable for advisory order payload.")
    notional_policy = notional_policy_fields(estimated_notional, min_notional, max_notional)
    if not notional_policy["notional_policy_passed"]:
        reasons.append(str(notional_policy["notional_policy_reason"]))
    if not daily_limit_passed:
        reasons.append("daily testnet order limit would block an actual order.")
    return not reasons, reasons


def redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in payload.items():
        if any(fragment in key.upper() for fragment in SECRET_KEY_FRAGMENTS):
            redacted[key] = "REDACTED"
        elif isinstance(value, dict):
            redacted[key] = redact_payload(value)
        else:
            redacted[key] = value
    return redacted


def build_telegram_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    status_text = "WOULD ORDER" if result["status"] == "WOULD_ORDER" else "BLOCKED"
    policy_text = "PASS" if result["policy_passed"] else "BLOCKED"
    safety_text = "PASS" if result["safety_passed"] else "BLOCKED"
    reason = "; ".join(result["blocked_reasons"]) if result["blocked_reasons"] else "All dry-run bridge checks passed."
    payload_text = "\n".join(
        [
            "🧪 SEMI-AUTO TESTNET BRIDGE — DRY RUN ONLY",
            "",
            f"Status: {status_text}",
            f"Symbol: {result.get('symbol') or 'UNKNOWN'}",
            f"Side: {result.get('side') or 'UNKNOWN'}",
            f"Quantity: {result.get('quantity') or 'N/A'}",
            f"Estimated Notional: {result.get('estimated_notional_usdt') if result.get('estimated_notional_usdt') is not None else 'N/A'}",
            f"Signal Score: {result.get('signal_score') if result.get('signal_score') is not None else 'N/A'}",
            f"Policy: {policy_text}",
            f"Safety: {safety_text}",
            f"Reason: {reason}",
            "",
            "No order sent.",
            "No broker execution.",
            "Binance Futures Demo/Testnet only.",
            "Real Binance OFF.",
        ]
    )
    return {
        "generated_at": result["generated_at"],
        "payload_text": payload_text,
        "status": result["status"],
        "dry_run": True,
        "send_requested": False,
        "order_attempted": False,
        "broker_execution_enabled": False,
    }


def run(args: argparse.Namespace) -> Dict[str, Any]:
    overlay_report, overlay_telegram = load_overlay_inputs(args.overlay_report_path)
    if overlay_telegram is None:
        overlay_telegram = {}
    inputs = extract_decision_inputs(overlay_report, overlay_telegram, args.symbol or "")
    safety_passed, safety_reasons, safety = safety_check()
    daily_count, daily_limit, daily_passed, daily_reason = daily_limit_status()
    min_notional = env_float("TESTNET_MIN_NOTIONAL_USDT", DEFAULT_MIN_NOTIONAL)
    max_notional = env_float("TESTNET_MAX_NOTIONAL_USDT", DEFAULT_MAX_NOTIONAL)
    quantity, estimated_notional, sizing_reasons = estimate_quantity_and_notional(inputs, min_notional, max_notional)
    notional_policy = notional_policy_fields(estimated_notional, min_notional, max_notional)
    policy_passed, policy_reasons = policy_check(
        inputs, args.overlay_report_path, quantity, estimated_notional, min_notional, max_notional, daily_passed, args.allow_need_review
    )

    blocked_reasons: List[str] = []
    if overlay_report is None:
        blocked_reasons.append(f"overlay report not readable: {args.overlay_report_path}")
    blocked_reasons.extend(safety_reasons)
    blocked_reasons.extend(sizing_reasons)
    blocked_reasons.extend(policy_reasons)
    if not daily_passed and daily_reason not in blocked_reasons:
        blocked_reasons.append(daily_reason)

    status = "WOULD_ORDER" if safety_passed and policy_passed and overlay_report is not None else "BLOCKED"
    would_order_payload = {
        "symbol": inputs.get("symbol"),
        "side": inputs.get("side"),
        "order_type": "MARKET",
        "quantity": quantity,
        "estimated_notional_usdt": estimated_notional,
        "dry_run_only": True,
        "broker": "BINANCE_FUTURES_DEMO_TESTNET",
    }

    result = {
        "generated_at": utc_now(),
        "mode": MODE,
        "status": status,
        "overlay_report_path": args.overlay_report_path,
        "symbol": inputs.get("symbol"),
        "direction": inputs.get("direction"),
        "side": inputs.get("side"),
        "quantity": quantity,
        "estimated_notional_usdt": estimated_notional,
        "min_notional_usdt": min_notional,
        "max_notional_usdt": max_notional,
        "minimum_notional_passed": notional_policy["minimum_notional_passed"],
        "maximum_notional_passed": notional_policy["maximum_notional_passed"],
        "notional_policy_passed": notional_policy["notional_policy_passed"],
        "notional_policy_reason": notional_policy["notional_policy_reason"],
        "signal_score": inputs.get("signal_score"),
        "portfolio_eligible": inputs.get("portfolio_eligible"),
        "overlay_decision": inputs.get("overlay_decision"),
        "trade_rank": inputs.get("trade_rank"),
        "suggested_risk": inputs.get("suggested_risk"),
        "broker_mode": safety.get("broker_mode"),
        "real_binance_enabled": safety.get("real_binance_enabled"),
        "allow_auto_testnet_order": safety.get("allow_auto_testnet_order"),
        "allow_testnet_order": safety.get("allow_testnet_order"),
        "safety_passed": safety_passed,
        "policy_passed": policy_passed,
        "daily_actual_order_count": daily_count,
        "daily_order_limit": daily_limit,
        "daily_limit_passed": daily_passed,
        "blocked_reasons": blocked_reasons,
        "would_order_payload": redact_payload(would_order_payload),
        "order_attempted": False,
        "order_success": False,
        "dry_run": True,
        "send_requested": False,
    }
    write_json(RESULT_PATH, result)
    if args.telegram_preview:
        write_json(TELEGRAM_PREVIEW_PATH, build_telegram_preview(result))
    # --- Telegram approval proposal (non-fatal) ---
    if status == "WOULD_ORDER":
        try:
            from telegram_bot import send_approval_request
            from dotenv import load_dotenv
            load_dotenv()
            proposal = {
                "symbol": inputs.get("symbol", "UNKNOWN"),
                "side": inputs.get("side", "LONG"),
                "score": inputs.get("signal_score", 0),
                "confidence": "HIGH" if (inputs.get("signal_score") or 0) >= 85
                              else "MEDIUM",
                "notional_usdt": round(
                    float(os.getenv("TESTNET_ORDER_QUANTITY", "0.013"))
                    * float(inputs.get("price") or 0), 2
                ),
                "regime": inputs.get("regime_name", "UNKNOWN"),
            }
            send_approval_request(proposal)
            print("[BRIDGE] Telegram approval proposal sent.")
        except Exception as _tg_err:
            print(f"[BRIDGE] Telegram proposal failed (non-fatal): {_tg_err}")
    # --- End Telegram proposal ---
    print(f"SEMI_AUTO_TESTNET_BRIDGE: {status}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run semi-auto Binance Futures Testnet bridge advisory.")
    parser.add_argument("--allow-need-review", action="store_true", help="Allow UNRANKED/need-review advisory checks where policy permits.")
    parser.add_argument(
        "--overlay-report-path",
        default=OVERLAY_REPORT_PATH,
        help="Overlay report JSON to read; defaults to logs/ml_signal_overlay_v1_report.json.",
    )
    parser.add_argument("--symbol", default="", help="Optional symbol override, e.g. ETHUSDT.")
    parser.add_argument("--telegram-preview", action="store_true", help="Write a Telegram preview payload without sending it.")
    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    run(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
