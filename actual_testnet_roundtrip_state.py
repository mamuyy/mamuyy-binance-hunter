"""Filesystem state helpers for Phase 2.97B1 manual actual Testnet roundtrip.

This module is intentionally small and side-effect free except for JSON/JSONL
persistence. Network calls and subprocess execution live in the controller.
"""

import json
import os
from typing import Any, Dict

NO_PLAN = "NO_PLAN"
PREPARED = "PREPARED"
EXECUTION_LOCKED = "EXECUTION_LOCKED"
ENTRY_INTENT_RECORDED = "ENTRY_INTENT_RECORDED"
ENTRY_SENT = "ENTRY_SENT"
ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
PRIMARY_CLOSE_INTENT_RECORDED = "PRIMARY_CLOSE_INTENT_RECORDED"
PRIMARY_CLOSE_SENT = "PRIMARY_CLOSE_SENT"
FINAL_FLAT_VERIFIED = "FINAL_FLAT_VERIFIED"
COMPLETED = "COMPLETED"
ENTRY_FAILED = "ENTRY_FAILED"
ENTRY_STATE_UNKNOWN = "ENTRY_STATE_UNKNOWN"
CLOSE_FAILED = "CLOSE_FAILED"
EMERGENCY_CLOSE_INTENT_RECORDED = "EMERGENCY_CLOSE_INTENT_RECORDED"
EMERGENCY_CLOSE_SENT = "EMERGENCY_CLOSE_SENT"
EMERGENCY_FLAT_VERIFIED = "EMERGENCY_FLAT_VERIFIED"
EMERGENCY_MANUAL_ACTION_REQUIRED = "EMERGENCY_MANUAL_ACTION_REQUIRED"
BLOCKED = "BLOCKED"

SUPPORTED_STATES = {
    NO_PLAN,
    PREPARED,
    EXECUTION_LOCKED,
    ENTRY_INTENT_RECORDED,
    ENTRY_SENT,
    ENTRY_CONFIRMED,
    PRIMARY_CLOSE_INTENT_RECORDED,
    PRIMARY_CLOSE_SENT,
    FINAL_FLAT_VERIFIED,
    COMPLETED,
    ENTRY_FAILED,
    ENTRY_STATE_UNKNOWN,
    CLOSE_FAILED,
    EMERGENCY_CLOSE_INTENT_RECORDED,
    EMERGENCY_CLOSE_SENT,
    EMERGENCY_FLAT_VERIFIED,
    EMERGENCY_MANUAL_ACTION_REQUIRED,
    BLOCKED,
}


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(payload, sort_keys=True) + "\n")
