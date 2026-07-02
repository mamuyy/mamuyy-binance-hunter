"""CP-043 read-only Phase 3 runtime status resolver.

Single source of truth for reporting layers (Telegram notifiers, reports,
dashboards) to describe the current execution governance mode. Reporting code
must derive Phase 3 / execution labels from here instead of hardcoding them.

Fail-closed: any missing, malformed, or ambiguous flag resolves to
PAPER_ONLY / NOT_UNLOCKED. This module never places orders, never mutates
state, and never contacts any broker. It only reads environment flags and the
execution halt marker file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    def load_dotenv() -> None:
        return None

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
HALT_FILE_PATH = BASE_DIR / "runtime" / "TESTNET_EXECUTION_HALT"

PHASE3_NOT_UNLOCKED = "NOT_UNLOCKED"
PHASE3_TESTNET_SEMI_MANUAL = "UNLOCKED_TESTNET_SEMI_MANUAL"

MODE_PAPER_ONLY = "PAPER_ONLY"
MODE_TESTNET_SEMI_MANUAL = "TESTNET_SEMI_MANUAL"

EXECUTION_NOT_ALLOWED = "NOT_ALLOWED"
EXECUTION_MANUAL_APPROVAL_ONLY = "MANUAL_APPROVAL_ONLY"

REAL_TRADING_LOCKED = "LOCKED"
REAL_TRADING_FLAG_DETECTED = "FLAG_DETECTED_REVIEW_REQUIRED"


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_phase3_status() -> Dict[str, Any]:
    allow_testnet_order = _env_true("ALLOW_TESTNET_ORDER")
    real_flag_detected = _env_true("ALLOW_REAL_BINANCE_ORDER") or _env_true("REAL_BINANCE_ENABLED")
    halt_active = HALT_FILE_PATH.exists()

    warnings: list[str] = []
    if real_flag_detected:
        warnings.append("Real-order flag detected in environment; review required.")
    if halt_active:
        warnings.append("TESTNET_EXECUTION_HALT is active.")

    testnet_semi_manual = allow_testnet_order and not real_flag_detected and not halt_active

    if testnet_semi_manual:
        phase3 = PHASE3_TESTNET_SEMI_MANUAL
        mode = MODE_TESTNET_SEMI_MANUAL
        execution = EXECUTION_MANUAL_APPROVAL_ONLY
    else:
        phase3 = PHASE3_NOT_UNLOCKED
        mode = MODE_PAPER_ONLY
        execution = EXECUTION_NOT_ALLOWED

    return {
        "phase3": phase3,
        "mode": mode,
        "execution": execution,
        "real_trading": REAL_TRADING_FLAG_DETECTED if real_flag_detected else REAL_TRADING_LOCKED,
        "allow_testnet_order": allow_testnet_order,
        "execution_halt_active": halt_active,
        "warnings": warnings,
    }
