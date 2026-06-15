#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

CSV = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
OUT = ROOT / "logs/phase5d_portfolio_rebalancing_engine_report_20260610.json"


df = pd.read_csv(CSV)

buy = (
    df.sort_values(
        "capital_pct_v2",
        ascending=False,
    )
    .head(10)[
        [
            "symbol",
            "capital_pct_v2",
        ]
    ]
)

reduce = df[
    (df["capital_pct_v2"] > 0)
    & (df["capital_pct_v2"] < 2)
][
    [
        "symbol",
        "capital_pct_v2",
    ]
]

remove = df[
    df["capital_pct_v2"] == 0
][
    [
        "symbol",
        "capital_pct_v2",
    ]
]

report = {
    "phase": "Phase 5D Portfolio Rebalancing Engine",
    "buy_more": buy.to_dict("records"),
    "reduce": reduce.to_dict("records"),
    "remove": remove.to_dict("records"),
    "summary": {
        "buy_count": len(buy),
        "reduce_count": len(reduce),
        "remove_count": len(remove),
    },
    "safety": {
        "paper_only": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
}

OUT.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
