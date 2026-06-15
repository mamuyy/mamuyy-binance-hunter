#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

CSV = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
OUT = ROOT / "logs/phase5c_portfolio_health_dashboard_report_20260610.json"


df = pd.read_csv(CSV)

active = df[df["capital_pct_v2"] > 0].copy()

symbol_count = len(active)

largest_exposure = float(
    active["capital_pct_v2"].max()
)

largest_symbol = (
    active.sort_values(
        "capital_pct_v2",
        ascending=False,
    )
    .iloc[0]["symbol"]
)

diversification_score = min(
    round(symbol_count / 30 * 100, 2),
    100,
)

risk_score = round(
    largest_exposure,
    2,
)

if diversification_score >= 80 and risk_score <= 15:
    health = "GREEN"
elif diversification_score >= 60 and risk_score <= 25:
    health = "YELLOW"
else:
    health = "RED"

report = {
    "phase": "Phase 5C Portfolio Health Dashboard",
    "active_symbols": symbol_count,
    "largest_exposure_symbol": largest_symbol,
    "largest_exposure_pct": largest_exposure,
    "diversification_score": diversification_score,
    "risk_score": risk_score,
    "portfolio_health": health,
    "safety": {
        "read_only": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
}

OUT.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
