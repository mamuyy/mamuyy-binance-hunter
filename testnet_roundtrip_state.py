"""Shared state helpers for Phase 2.97A manual testnet roundtrip simulation.

The helpers in this module are intentionally filesystem-only. They do not import
or instantiate any Binance client and never mutate account state.
"""

import json
import os
from typing import Any, Dict

NO_PLAN = "NO_PLAN"
PREPARED = "PREPARED"
SIMULATION_APPROVED = "SIMULATION_APPROVED"
ENTRY_SIMULATED = "ENTRY_SIMULATED"
POSITION_OPEN_VERIFIED_SIMULATED = "POSITION_OPEN_VERIFIED_SIMULATED"
CLOSE_REDUCE_ONLY_SIMULATED = "CLOSE_REDUCE_ONLY_SIMULATED"
FINAL_FLAT_VERIFIED_SIMULATED = "FINAL_FLAT_VERIFIED_SIMULATED"
COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
FAILED_ENTRY = "FAILED_ENTRY"
FAILED_POSITION_VERIFICATION = "FAILED_POSITION_VERIFICATION"
FAILED_CLOSE = "FAILED_CLOSE"
FAILED_FINAL_FLAT_VERIFICATION = "FAILED_FINAL_FLAT_VERIFICATION"
HALTED_SIMULATION = "HALTED_SIMULATION"

SUPPORTED_STATES = {
    NO_PLAN,
    PREPARED,
    SIMULATION_APPROVED,
    ENTRY_SIMULATED,
    POSITION_OPEN_VERIFIED_SIMULATED,
    CLOSE_REDUCE_ONLY_SIMULATED,
    FINAL_FLAT_VERIFIED_SIMULATED,
    COMPLETED,
    BLOCKED,
    FAILED_ENTRY,
    FAILED_POSITION_VERIFICATION,
    FAILED_CLOSE,
    FAILED_FINAL_FLAT_VERIFICATION,
    HALTED_SIMULATION,
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
