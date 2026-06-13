"""Shared canonical identity metadata for manual testnet approvals.

Bridge signal identity intentionally covers only stable signal metadata. Execution
values such as symbol, side, quantity, order type, live price, and request hashes
are validated by the manual approval gate and safety supervisor separately.
"""


def canonical_bridge_signal_metadata(bridge: dict) -> dict:
    return {
        "bridge_status": bridge.get("status"),
        "signal_score": bridge.get("signal_score"),
        "overlay_decision": bridge.get("overlay_decision"),
        "trade_rank": bridge.get("trade_rank"),
        "suggested_risk": bridge.get("suggested_risk"),
        "source_report_path": bridge.get("overlay_report_path"),
    }
