"""Manual-gated Telegram sender for semi-auto testnet bridge advisories.

This helper is intentionally advisory-only. It reads the dry-run bridge Telegram
preview/result JSON files, enforces manual and environment safety gates, and can
send the already-rendered advisory text to Telegram. It never imports or calls
Binance clients, never places testnet orders, and never requires order gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Tuple

import requests

from config import config

DEFAULT_PREVIEW_PATH = "logs/semi_auto_testnet_bridge_telegram_preview.json"
DEFAULT_BRIDGE_RESULT_PATH = "logs/semi_auto_testnet_bridge_result.json"
DEFAULT_RESULT_PATH = "logs/semi_auto_bridge_telegram_send_result.json"
DEFAULT_STATE_PATH = "logs/semi_auto_bridge_telegram_send_state.json"
ALLOW_SEND_ENV = "ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND"
DEFAULT_COOLDOWN_SECONDS = 5 * 60
VALID_BRIDGE_STATUSES = {"BLOCKED", "WOULD_ORDER"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or manually send semi-auto bridge dry-run advisories to Telegram."
    )
    parser.add_argument("--send", action="store_true", help="Request a Telegram send; manual gates are still required.")
    parser.add_argument("--dry-run", action="store_true", help="Never send Telegram, even when --send is supplied.")
    parser.add_argument(
        "--ignore-cooldown",
        action="store_true",
        help="Bypass duplicate/cooldown blocking for manual Telegram send testing.",
    )
    parser.add_argument(
        "--preview-path",
        default=DEFAULT_PREVIEW_PATH,
        help=f"Path to semi-auto bridge Telegram preview JSON (default: {DEFAULT_PREVIEW_PATH}).",
    )
    parser.add_argument(
        "--bridge-result-path",
        default=DEFAULT_BRIDGE_RESULT_PATH,
        help=f"Path to semi-auto bridge result JSON (default: {DEFAULT_BRIDGE_RESULT_PATH}).",
    )
    parser.add_argument(
        "--result-path",
        default=DEFAULT_RESULT_PATH,
        help=f"Path for sender result JSON (default: {DEFAULT_RESULT_PATH}).",
    )
    parser.add_argument(
        "--state-path",
        default=DEFAULT_STATE_PATH,
        help=f"Path for dedupe/cooldown state JSON (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=DEFAULT_COOLDOWN_SECONDS,
        help="Duplicate payload cooldown in seconds (default: 300).",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def read_json(path: str) -> Tuple[Dict[str, Any], str | None]:
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except FileNotFoundError:
        return {}, f"not_found: {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"read_failed: {path}: {exc}"
    if not isinstance(payload, dict):
        return {}, f"not_object: {path}"
    return payload, None


def payload_sha256(payload_text: Any) -> str:
    text = payload_text if isinstance(payload_text, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def env_raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value.strip()


def env_is_true(name: str) -> bool:
    value = env_raw(name)
    return value is not None and value.lower() in {"1", "true", "yes", "y", "on"}


def env_is_false(name: str) -> bool:
    value = env_raw(name)
    return value is not None and value.lower() in {"0", "false", "no", "n", "off"}


def bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
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


def load_state(path: str) -> Dict[str, Any]:
    state, error = read_json(path)
    if error:
        return {"sends": {}}
    sends = state.get("sends")
    if not isinstance(sends, dict):
        state["sends"] = {}
    return state


def cooldown_check(
    state: Dict[str, Any], dedupe_key: str, cooldown_seconds: int, ignore_cooldown: bool
) -> Tuple[bool, str | None]:
    if ignore_cooldown:
        return True, None
    if cooldown_seconds <= 0:
        return True, None
    sends = state.get("sends") if isinstance(state.get("sends"), dict) else {}
    entry = sends.get(dedupe_key) if isinstance(sends, dict) else None
    if not isinstance(entry, dict):
        return True, None
    last_sent = parse_iso_datetime(entry.get("last_sent_at"))
    if last_sent is None:
        return True, None
    elapsed = utc_now() - last_sent
    if elapsed < timedelta(seconds=cooldown_seconds):
        remaining = int(cooldown_seconds - elapsed.total_seconds())
        return False, f"duplicate payload within cooldown; {remaining}s remaining"
    return True, None


def record_successful_send(path: str, state: Dict[str, Any], dedupe_key: str, sha256: str, payload: Dict[str, Any]) -> None:
    sends = state.get("sends")
    if not isinstance(sends, dict):
        sends = {}
        state["sends"] = sends
    sends[dedupe_key] = {
        "last_sent_at": utc_now_iso(),
        "payload_sha256": sha256,
        "symbol": payload.get("symbol"),
        "bridge_status": payload.get("status"),
    }
    state["updated_at"] = utc_now_iso()
    write_json(path, state)


def validate_safety(preview: Dict[str, Any], bridge_result: Dict[str, Any], read_errors: Tuple[str | None, str | None]) -> Tuple[bool, str | None]:
    reasons = []
    preview_error, bridge_error = read_errors
    if preview_error:
        reasons.append(f"preview {preview_error}")
    if bridge_error:
        reasons.append(f"bridge_result {bridge_error}")

    bridge_status = str(bridge_result.get("status") or preview.get("status") or "").upper()
    if bridge_status not in VALID_BRIDGE_STATUSES:
        reasons.append("bridge status must be BLOCKED or WOULD_ORDER")

    if env_is_true("ALLOW_AUTO_TESTNET_ORDER"):
        reasons.append("ALLOW_AUTO_TESTNET_ORDER must not be true")
    if not env_is_false("REAL_BINANCE_ENABLED"):
        reasons.append("REAL_BINANCE_ENABLED must be explicitly false")
    if not env_is_false("ALLOW_REAL_BINANCE_ORDER"):
        reasons.append("ALLOW_REAL_BINANCE_ORDER must be explicitly false")

    broker_execution_enabled = bool_or_none(preview.get("broker_execution_enabled"))
    if "broker_execution_enabled" in preview and broker_execution_enabled is not False:
        reasons.append("broker_execution_enabled in preview must be false")

    if bool_or_none(bridge_result.get("order_attempted")) is not False:
        reasons.append("bridge result order_attempted must be false")
    if bool_or_none(bridge_result.get("dry_run")) is not True:
        reasons.append("bridge result dry_run must be true")

    payload_text = preview.get("payload_text")
    if not isinstance(payload_text, str) or not payload_text.strip():
        reasons.append("preview payload_text must be present")

    return not reasons, "; ".join(reasons) if reasons else None


def safe_send_telegram_message(message: str) -> bool:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        print("Semi-Auto Bridge Telegram Send: BLOCKED — Telegram credentials are not configured")
        return False

    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    data = {
        "chat_id": config.telegram_chat_id,
        "text": escape(message),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=data, timeout=config.request_timeout_seconds)
        response.raise_for_status()
        return True
    except requests.RequestException:
        # Do not print the exception: it may contain Telegram URLs/tokens.
        print("Semi-Auto Bridge Telegram Send: ERROR — Telegram request failed")
        return False


def build_result(
    *,
    args: argparse.Namespace,
    preview: Dict[str, Any],
    bridge_result: Dict[str, Any],
    sha256: str,
    dedupe_key: str,
    safety_passed: bool,
    blocked_reason: str | None,
    cooldown_passed: bool,
    status: str,
    send_attempted: bool,
    send_success: bool,
) -> Dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "status": status,
        "preview_path": args.preview_path,
        "bridge_result_path": args.bridge_result_path,
        "payload_text": preview.get("payload_text"),
        "payload_sha256": sha256,
        "symbol": bridge_result.get("symbol") or preview.get("symbol"),
        "bridge_status": bridge_result.get("status") or preview.get("status"),
        "broker_execution_enabled": bool_or_none(preview.get("broker_execution_enabled")),
        "order_attempted": bool_or_none(bridge_result.get("order_attempted")),
        "order_success": bool_or_none(bridge_result.get("order_success")),
        "dry_run": bool_or_none(bridge_result.get("dry_run")),
        "send_requested": bool(args.send),
        "send_attempted": send_attempted,
        "send_success": send_success,
        "telegram_enabled": bool(config.telegram_enabled),
        "safety_passed": safety_passed,
        "blocked_reason": blocked_reason,
        "cooldown_passed": cooldown_passed,
        "dedupe_key": dedupe_key,
    }


def main() -> int:
    args = parse_args()
    preview, preview_error = read_json(args.preview_path)
    bridge_result, bridge_error = read_json(args.bridge_result_path)
    text = preview.get("payload_text") if isinstance(preview.get("payload_text"), str) else ""
    sha256 = payload_sha256(text)
    dedupe_key = sha256
    state = load_state(args.state_path)

    safety_passed, safety_reason = validate_safety(preview, bridge_result, (preview_error, bridge_error))
    cooldown_passed, cooldown_reason = cooldown_check(
        state, dedupe_key, max(args.cooldown_seconds, 0), bool(args.ignore_cooldown)
    )

    status = "PREVIEW_ONLY"
    blocked_reason: str | None = "send flag not passed; preview only"
    send_attempted = False
    send_success = False

    if not args.send:
        print("Semi-Auto Bridge Telegram Send: PREVIEW ONLY")
    elif args.dry_run:
        status = "BLOCKED_DRY_RUN"
        blocked_reason = "--dry-run supplied; Telegram send disabled"
        print(f"Semi-Auto Bridge Telegram Send: BLOCKED — {blocked_reason}")
    elif os.getenv(ALLOW_SEND_ENV) != "1":
        status = "BLOCKED_MANUAL_GATE"
        blocked_reason = f"{ALLOW_SEND_ENV} must be 1 for manual send"
        print(f"Semi-Auto Bridge Telegram Send: BLOCKED — {blocked_reason}")
    elif not safety_passed:
        status = "BLOCKED_SAFETY"
        blocked_reason = safety_reason or "safety validation failed"
        print(f"Semi-Auto Bridge Telegram Send: BLOCKED — {blocked_reason}")
    elif not cooldown_passed:
        status = "BLOCKED_COOLDOWN"
        blocked_reason = cooldown_reason or "duplicate payload within cooldown"
        print(f"Semi-Auto Bridge Telegram Send: BLOCKED — {blocked_reason}")
    elif not config.telegram_enabled:
        status = "BLOCKED_SAFETY"
        blocked_reason = "Telegram is not enabled or credentials are missing"
        print(f"Semi-Auto Bridge Telegram Send: BLOCKED — {blocked_reason}")
    else:
        blocked_reason = None
        send_attempted = True
        send_success = safe_send_telegram_message(text)
        if send_success:
            status = "SENT"
            record_successful_send(args.state_path, state, dedupe_key, sha256, {**preview, **bridge_result})
            print("Semi-Auto Bridge Telegram Send: SENT")
        else:
            status = "ERROR"
            blocked_reason = "Telegram request failed"

    result = build_result(
        args=args,
        preview=preview,
        bridge_result=bridge_result,
        sha256=sha256,
        dedupe_key=dedupe_key,
        safety_passed=safety_passed,
        blocked_reason=blocked_reason,
        cooldown_passed=cooldown_passed,
        status=status,
        send_attempted=send_attempted,
        send_success=send_success,
    )
    write_json(args.result_path, result)

    if not args.send or args.dry_run:
        if text:
            print(text)

    return 0 if (send_success or not send_attempted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
