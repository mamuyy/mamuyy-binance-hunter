from typing import Any, Dict


def apply_adaptive_regime_shadow_penalty(signal: Dict[str, Any]) -> Dict[str, Any]:
    adjusted = dict(signal)
    calculated_score = float(adjusted.get("score", 0) or 0)
    regime_name = str(adjusted.get("regime_name", "UNKNOWN") or "UNKNOWN").upper()
    multiplier = 1.0

    if regime_name == "SIDEWAYS / CHOPPY":
        multiplier = 0.20
    elif regime_name == "RISK OFF":
        multiplier = 0.50

    adjusted["calculated_score"] = round(calculated_score, 2)
    adjusted["shadow_score"] = round(calculated_score * multiplier, 2)
    adjusted["penalty_applied"] = int(multiplier < 1.0)
    return adjusted
