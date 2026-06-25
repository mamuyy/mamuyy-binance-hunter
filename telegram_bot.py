"""Telegram inline approval keyboard for testnet order proposals.

Sends InlineKeyboardMarkup approve/reject buttons via raw Telegram Bot API
(no python-telegram-bot dependency — uses requests like telegram_notifier.py).
Polls getUpdates in a background thread to handle callback_query events.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

import testnet_approval_state as tas

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
_POLL_TIMEOUT = 30          # long-poll seconds per getUpdates call
_POLL_INTERVAL_IDLE = 1     # seconds between polls when active
_REQUEST_TIMEOUT = 20       # requests timeout

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_polling_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_last_update_id: int = 0


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _post(method: str, payload: Dict[str, Any]) -> Optional[Dict]:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Telegram %s failed: %s", method, exc)
        return None


def _get(method: str, params: Dict[str, Any]) -> Optional[Dict]:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT + _POLL_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Telegram %s failed: %s", method, exc)
        return None


def _format_proposal(proposal: dict) -> str:
    symbol = proposal.get("symbol", "—")
    side = proposal.get("side", proposal.get("direction", "—")).upper()
    score = proposal.get("score", "—")
    confidence = proposal.get("confidence", "—")
    notional = proposal.get("notional_usdt", proposal.get("notional", "—"))
    notional_str = f"${float(notional):,.2f} USDT" if notional not in ("—", None) else "—"

    return (
        "🎯 <b>TESTNET ORDER PROPOSAL</b>\n\n"
        f"Symbol:     <b>{symbol}</b>\n"
        f"Side:       <b>{side}</b>\n"
        f"Score:      <b>{score}</b>\n"
        f"Confidence: <b>{confidence}</b>\n"
        f"Notional:   <b>{notional_str}</b>\n\n"
        "⏱ Expires in 5 minutes\n"
        "Tap <b>APPROVE</b> to execute on Binance Demo.\n"
        "Tap <b>REJECT</b> to skip this signal."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_approval_request(proposal: dict) -> Optional[str]:
    """Create pending approval state and send inline keyboard to Telegram.

    Returns the Telegram message_id (str) on success, None on failure.
    """
    proposal_id = tas.create_pending_approval(proposal)
    text = _format_proposal(proposal)
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ APPROVE", "callback_data": f"approve_{proposal_id}"},
            {"text": "❌ REJECT",  "callback_data": f"reject_{proposal_id}"},
        ]]
    }
    result = _post("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
        "disable_web_page_preview": True,
    })
    if result and result.get("ok"):
        msg_id = str(result["result"]["message_id"])
        logger.info("Approval request sent: proposal_id=%s message_id=%s", proposal_id, msg_id)
        return msg_id
    logger.error("Failed to send approval request: %s", result)
    return None


def send_execution_result(success: bool, details: dict) -> None:
    """Send post-execution summary message."""
    if success:
        symbol   = details.get("symbol", "—")
        entry    = details.get("entry_price", details.get("entry", "—"))
        position = details.get("position", "—")
        fee      = details.get("fee", "—")
        entry_str = f"${float(entry):,.2f}" if entry not in ("—", None) else "—"
        fee_str   = f"-${abs(float(fee)):,.2f} USDT" if fee not in ("—", None) else "—"
        text = (
            "✅ <b>ORDER EXECUTED</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Entry: <b>{entry_str}</b>\n"
            f"Position: <b>{position}</b>\n"
            f"Fee: <b>{fee_str}</b>"
        )
    else:
        reason = details.get("reason", "unknown")
        text = (
            "❌ <b>ORDER REJECTED / FAILED</b>\n"
            f"Reason: {reason}"
        )
    _post("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

def handle_callback(update: dict) -> None:
    """Process a callback_query update from getUpdates polling."""
    cq = update.get("callback_query", {})
    if not cq:
        return

    callback_id   = cq.get("id", "")
    callback_data = cq.get("data", "")
    message       = cq.get("message", {})
    chat_id       = message.get("chat", {}).get("id", CHAT_ID)
    message_id    = message.get("message_id")

    # Always answer the callback query to remove the loading spinner
    _post("answerCallbackQuery", {"callback_query_id": callback_id})

    if callback_data.startswith("approve_"):
        proposal_id = callback_data[len("approve_"):]
        ok = tas.approve_proposal(proposal_id)
        if ok:
            new_text = "✅ <b>APPROVED</b> — waiting for execution..."
        else:
            state = tas.read_approval_state()
            status = state.get("status", "UNKNOWN") if state else "MISSING"
            new_text = f"⚠️ Could not approve — proposal is <b>{status}</b>."

    elif callback_data.startswith("reject_"):
        proposal_id = callback_data[len("reject_"):]
        ok = tas.reject_proposal(proposal_id)
        if ok:
            new_text = "❌ <b>REJECTED</b> — signal skipped."
        else:
            state = tas.read_approval_state()
            status = state.get("status", "UNKNOWN") if state else "MISSING"
            new_text = f"⚠️ Could not reject — proposal is <b>{status}</b>."

    else:
        return  # unknown callback, ignore

    if message_id:
        _post("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML",
        })


# ---------------------------------------------------------------------------
# Polling thread
# ---------------------------------------------------------------------------

def _poll_loop() -> None:
    global _last_update_id
    logger.info("Telegram callback polling started")
    while not _stop_event.is_set():
        result = _get("getUpdates", {
            "offset": _last_update_id + 1,
            "timeout": _POLL_TIMEOUT,
            "allowed_updates": ["callback_query"],
        })
        if result and result.get("ok"):
            for update in result.get("result", []):
                uid = update.get("update_id", 0)
                if uid > _last_update_id:
                    _last_update_id = uid
                try:
                    handle_callback(update)
                except Exception as exc:
                    logger.exception("Error handling callback: %s", exc)
        if not _stop_event.is_set():
            time.sleep(_POLL_INTERVAL_IDLE)
    logger.info("Telegram callback polling stopped")


def start_bot_polling() -> None:
    """Start background thread that polls for callback_query updates."""
    global _polling_thread
    _stop_event.clear()
    if _polling_thread and _polling_thread.is_alive():
        logger.warning("Polling thread already running")
        return
    _polling_thread = threading.Thread(target=_poll_loop, daemon=True, name="tg-approval-poller")
    _polling_thread.start()
    logger.info("Bot polling thread started (tid=%s)", _polling_thread.ident)


def stop_bot_polling() -> None:
    """Signal the polling thread to stop and wait for it."""
    _stop_event.set()
    if _polling_thread and _polling_thread.is_alive():
        _polling_thread.join(timeout=_POLL_TIMEOUT + 5)
    logger.info("Bot polling thread stopped")
