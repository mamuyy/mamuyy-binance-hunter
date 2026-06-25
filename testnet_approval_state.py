"""Telegram inline approval state manager for testnet trade proposals.

Provides atomic file-based state shared between telegram_bot.py (writer)
and manual_actual_testnet_roundtrip_controller.py (reader/poller).
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

APPROVAL_FILE = "testnet_telegram_approval.json"
APPROVAL_TTL_SECONDS = 300  # 5 minutes to approve
APPROVAL_STATUS_PENDING = "PENDING"
APPROVAL_STATUS_APPROVED = "APPROVED"
APPROVAL_STATUS_REJECTED = "REJECTED"
APPROVAL_STATUS_EXPIRED = "EXPIRED"

_TMP_FILE = APPROVAL_FILE + ".tmp"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_iso(s: str) -> datetime:
    s = s.rstrip("Z")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _atomic_write(data: dict) -> None:
    payload = json.dumps(data, indent=2)
    with open(_TMP_FILE, "w") as f:
        f.write(payload)
    os.replace(_TMP_FILE, APPROVAL_FILE)


def create_pending_approval(proposal: dict) -> str:
    """Write a new PENDING approval file and return the proposal_id."""
    now = _utc_now()
    proposal_id = str(uuid.uuid4())
    _atomic_write({
        "status": APPROVAL_STATUS_PENDING,
        "proposal_id": proposal_id,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=APPROVAL_TTL_SECONDS)),
        "proposal": proposal,
    })
    return proposal_id


def _load_and_maybe_expire() -> Optional[dict]:
    """Read file; if PENDING and past TTL, mark EXPIRED in-place and return updated state."""
    state = read_approval_state()
    if state is None:
        return None
    if state.get("status") == APPROVAL_STATUS_PENDING:
        expires_at = _parse_iso(state["expires_at"])
        if _utc_now() > expires_at:
            state["status"] = APPROVAL_STATUS_EXPIRED
            _atomic_write(state)
    return state


def approve_proposal(proposal_id: str) -> bool:
    """Set status to APPROVED. Returns False if id mismatch or expired."""
    state = _load_and_maybe_expire()
    if state is None:
        return False
    if state.get("proposal_id") != proposal_id:
        return False
    if state.get("status") not in (APPROVAL_STATUS_PENDING,):
        return False
    state["status"] = APPROVAL_STATUS_APPROVED
    _atomic_write(state)
    return True


def reject_proposal(proposal_id: str) -> bool:
    """Set status to REJECTED. Returns False if id mismatch or not pending."""
    state = _load_and_maybe_expire()
    if state is None:
        return False
    if state.get("proposal_id") != proposal_id:
        return False
    if state.get("status") not in (APPROVAL_STATUS_PENDING,):
        return False
    state["status"] = APPROVAL_STATUS_REJECTED
    _atomic_write(state)
    return True


def read_approval_state() -> Optional[dict]:
    """Return current file content, or None if missing/unreadable."""
    try:
        with open(APPROVAL_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_approval() -> None:
    """Delete the approval file if it exists."""
    try:
        os.remove(APPROVAL_FILE)
    except FileNotFoundError:
        pass


def is_approval_pending() -> bool:
    """True if file exists, status=PENDING, and not yet expired."""
    state = _load_and_maybe_expire()
    return state is not None and state.get("status") == APPROVAL_STATUS_PENDING


def is_approval_approved() -> bool:
    """True if current status is APPROVED."""
    state = read_approval_state()
    return state is not None and state.get("status") == APPROVAL_STATUS_APPROVED


def wait_for_approval(timeout_seconds: int = 240, poll_interval: int = 3) -> str:
    """Poll until APPROVED/REJECTED/EXPIRED or timeout. Returns final status string."""
    deadline = time.monotonic() + timeout_seconds
    last_print = time.monotonic()
    while time.monotonic() < deadline:
        state = _load_and_maybe_expire()
        if state is None:
            time.sleep(poll_interval)
            continue
        status = state.get("status", "")
        if status in (APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED, APPROVAL_STATUS_EXPIRED):
            return status
        if time.monotonic() - last_print >= 30:
            remaining = int(deadline - time.monotonic())
            print(f"[wait_for_approval] Still PENDING — {remaining}s remaining")
            last_print = time.monotonic()
        time.sleep(poll_interval)
    return "TIMEOUT"
