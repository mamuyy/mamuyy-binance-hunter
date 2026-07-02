import json
from pathlib import Path
from typing import Any

from config import config
from phase3_runtime_status import resolve_phase3_status
from telegram import send_telegram_message

QUEUE_JSON_PATH = Path("reports/binance_candidate_queue.json")
TOP_N = 5


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_candidate(item: dict[str, Any]) -> str:
    return (
        f"{item.get('rank')}. {item.get('symbol')} | "
        f"Score {item.get('score')} | "
        f"Pressure {_num(item.get('pressure_score')):.2f} | "
        f"Whale {item.get('whale_activity', '-')} | "
        f"Price {item.get('price')}"
    )


def build_message(data: dict[str, Any], status: dict[str, Any] | None = None) -> str:
    if status is None:
        status = resolve_phase3_status()
    candidates = data.get("candidates", [])
    lines = [
        "📋 BINANCE CANDIDATE QUEUE",
        "",
        f"Generated: {str(data.get('generated_at', '-'))[:19]} UTC",
        f"Mode: {data.get('mode', 'READ_ONLY_PROPOSAL')}",
        f"Freshness: {data.get('rules', {}).get('max_signal_age_hours', '-')}h",
        f"Candidates: {data.get('candidate_count', len(candidates))}",
        "",
        "Top Candidates:",
    ]

    if not candidates:
        lines.append("- No fresh candidates")
    else:
        for item in candidates[:TOP_N]:
            lines.append(_fmt_candidate(item))

    lines.extend([
        "",
        "Status: PROPOSAL_ONLY",
        f"Execution: {status.get('execution', 'NOT_ALLOWED')}",
        "Binance: NOT_CALLED",
        f"Phase 3: {status.get('phase3', 'NOT_UNLOCKED')}",
        f"Real Trading: {status.get('real_trading', 'LOCKED')}",
    ])

    for warning in status.get("warnings", []):
        lines.append(f"⚠️ {warning}")

    return "\n".join(lines)


def main() -> None:
    if not QUEUE_JSON_PATH.exists():
        raise SystemExit(
            f"Missing {QUEUE_JSON_PATH}. Run binance_candidate_queue_v1.py first."
        )

    data = json.loads(QUEUE_JSON_PATH.read_text(encoding="utf-8"))
    message = build_message(data)
    sent = send_telegram_message(config.telegram_bot_token, config.telegram_chat_id, message)

    if sent:
        print("Telegram queue notification sent.")
    else:
        print("Telegram queue notification not sent or failed.")


if __name__ == "__main__":
    main()
