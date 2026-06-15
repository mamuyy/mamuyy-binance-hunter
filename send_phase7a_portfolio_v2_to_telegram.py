#!/usr/bin/env python3
"""Manual Telegram sender for the Phase 7A Portfolio V2 advisory report."""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
from typing import Any, Dict

from config import config
from telegram import send_telegram_message


ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_PATH = ROOT / "logs/phase7a_telegram_portfolio_v2_report_20260611.txt"
EVENT_TYPE = "PHASE7A_PORTFOLIO_V2_ADVISORY"
TELEGRAM_MESSAGE_LIMIT = 4096
REQUIRED_SAFETY_LINES = (
    "Mode: V2_ADVISORY_ONLY",
    "Runtime V1 Changed: NO",
    "Broker Routing: NO",
)


def load_v2_advisory_report(report_path: Path) -> str:
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found: {report_path}")

    report = report_path.read_text(encoding="utf-8").strip()
    if not report:
        raise ValueError(f"Report is empty: {report_path}")

    missing = [line for line in REQUIRED_SAFETY_LINES if line not in report]
    if missing:
        raise ValueError(
            "Refusing to send Phase 7A report because safety line(s) are missing: "
            + ", ".join(missing)
        )

    escaped_report = escape(report, quote=False)
    if len(escaped_report) > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError(
            "Refusing to send Phase 7A report because it exceeds Telegram's "
            f"{TELEGRAM_MESSAGE_LIMIT}-character message limit."
        )

    return escaped_report


def send_phase7a_portfolio_v2_report(
    report_path: Path = DEFAULT_REPORT_PATH,
    db_path: str | None = None,
) -> Dict[str, Any]:
    message = load_v2_advisory_report(report_path)
    if not config.telegram_enabled:
        return {
            "event_type": EVENT_TYPE,
            "enabled": False,
            "send_status": "PREVIEW_DISABLED",
            "error_message": "TELEGRAM_ENABLED=false or Telegram credentials are incomplete",
            "message": message,
            "db_path": db_path or config.database_path,
        }

    sent = send_telegram_message(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        message=message,
        timeout=config.request_timeout_seconds,
    )
    return {
        "event_type": EVENT_TYPE,
        "enabled": True,
        "send_status": "SENT" if sent else "FAILED",
        "error_message": "" if sent else "Telegram send failed; see stdout/stderr for request error.",
        "message": message,
        "db_path": db_path or config.database_path,
    }


def format_phase7a_send_result(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "PHASE 7A PORTFOLIO V2 TELEGRAM",
            f"Enabled: {result.get('enabled')}",
            f"Status: {result.get('send_status')}",
            f"Event Type: {result.get('event_type')}",
            "Mode: V2_ADVISORY_ONLY",
            "Runtime V1 Changed: NO",
            "Broker Routing: NO",
            f"Error: {result.get('error_message') or '-'}",
            "",
            "Preview:",
            str(result.get("message") or ""),
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send the Phase 7A Portfolio V2 advisory report to Telegram."
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Report text file to send. Default: {DEFAULT_REPORT_PATH}",
    )
    parser.add_argument(
        "--db-path",
        default=config.database_path,
        help=f"Reserved for CLI consistency. Default: {config.database_path}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = send_phase7a_portfolio_v2_report(args.report_path, args.db_path)
    print(format_phase7a_send_result(result))
    return 0 if result.get("send_status") in {"SENT", "PREVIEW_DISABLED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
