from typing import Any, Dict, List


PROFILES: Dict[str, Dict[str, Any]] = {
    "aggressive": {
        "min_confidence": 55.0,
        "exposure_multiplier": 1.0,
        "reject_macro": {"PANIC"},
        "markets": {"crypto", "forex", "stocks", "etf", "gold"},
    },
    "balanced": {
        "min_confidence": 65.0,
        "exposure_multiplier": 0.7,
        "reject_macro": {"PANIC"},
        "markets": {"crypto", "forex", "stocks", "etf", "gold"},
    },
    "defensive": {
        "min_confidence": 72.0,
        "exposure_multiplier": 0.45,
        "reject_macro": {"HIGH_STRESS", "PANIC"},
        "markets": {"crypto", "forex", "stocks", "etf", "gold"},
    },
    "ETF only": {
        "min_confidence": 60.0,
        "exposure_multiplier": 0.55,
        "reject_macro": {"HIGH_STRESS", "PANIC"},
        "markets": {"etf"},
    },
    "crypto only": {
        "min_confidence": 62.0,
        "exposure_multiplier": 0.75,
        "reject_macro": {"PANIC"},
        "markets": {"crypto"},
    },
}


def market_type(symbol: str, requested: str = "") -> str:
    market = str(requested or "").lower()
    if market:
        return market
    text = str(symbol or "").upper()
    if text.endswith("USDT"):
        return "crypto"
    if text in {"SPY", "QQQ", "DIA", "IWM"}:
        return "etf"
    if text in {"XAUUSD", "GOLD"}:
        return "gold"
    return "crypto"


def evaluate_profile(
    signal: Dict[str, Any],
    profile_name: str,
) -> Dict[str, Any]:
    profile = PROFILES.get(profile_name, PROFILES["balanced"])
    symbol = str(signal.get("symbol") or "")
    market = market_type(symbol, str(signal.get("market") or ""))
    confidence = float(signal.get("confidence") or 0.0)
    macro_state = str(signal.get("macro_state") or "UNKNOWN").upper()
    allocation_tier = str(signal.get("allocation_tier") or "WATCH").upper()
    reasons: List[str] = []
    allowed = True

    if market not in profile["markets"]:
        allowed = False
        reasons.append(f"market {market} rejected by profile")
    if macro_state in profile["reject_macro"]:
        allowed = False
        reasons.append(f"macro_state {macro_state} rejected")
    if confidence < profile["min_confidence"]:
        allowed = False
        reasons.append(f"confidence {confidence:.2f} below {profile['min_confidence']:.2f}")
    if allocation_tier == "AVOID":
        allowed = False
        reasons.append("allocation tier AVOID")

    if not reasons:
        reasons.append(f"profile {profile_name} accepted")
    return {
        "allowed": allowed,
        "profile": profile_name,
        "market": market,
        "exposure_multiplier": profile["exposure_multiplier"] if allowed else 0.0,
        "reason": "; ".join(reasons),
    }


def competition_status() -> Dict[str, Any]:
    return {
        "profiles": [
            {
                "profile": name,
                "min_confidence": config["min_confidence"],
                "exposure_multiplier": config["exposure_multiplier"],
                "reject_macro": sorted(config["reject_macro"]),
                "markets": sorted(config["markets"]),
            }
            for name, config in PROFILES.items()
        ],
        "paper_only": True,
        "broker_execution": False,
    }


def format_competition_status(status: Dict[str, Any]) -> str:
    lines = [
        "COMPETITION CONTROL STATUS",
        f"Paper Only: {status.get('paper_only')}",
        f"Broker Execution: {status.get('broker_execution')}",
        "Profiles:",
    ]
    for profile in status.get("profiles", []):
        lines.append(
            f"- {profile['profile']}: min_conf={profile['min_confidence']} "
            f"mult={profile['exposure_multiplier']} markets={','.join(profile['markets'])} "
            f"reject_macro={','.join(profile['reject_macro']) or '-'}"
        )
    return "\n".join(lines)
