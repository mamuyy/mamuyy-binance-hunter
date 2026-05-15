import csv
import os
from typing import Dict, Any


FIELDNAMES = [
    "timestamp",
    "symbol",
    "price",
    "score",
    "base_score",
    "regime_name",
    "regime_score",
    "pre_flow_score",
    "flow_adjustment",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "flow_state",
    "whale_activity",
    "squeeze_risk",
    "funding_warning",
    "regime_model",
    "regime_model_adjustment",
    "adaptive_confidence_score",
    "model_confidence",
    "expected_behavior",
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "taker_buy_ratio",
    "funding",
    "open_interest",
]


def log_signal(signal: Dict[str, Any], path: str = "signals_log.csv") -> None:
    file_exists = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()

        writer.writerow({field: signal.get(field, "") for field in FIELDNAMES})
