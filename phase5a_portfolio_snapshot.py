#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

CSV = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
OUT = ROOT / "logs/phase5a_portfolio_snapshot_report_20260610.json"


df = pd.read_csv(CSV)

top10 = (
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

report = {
    "phase": "Phase 5A Portfolio Snapshot",
    "top10": top10.to_dict("records"),
}

OUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)

OUT.write_text(
    json.dumps(
        report,
        indent=2,
    ),
    encoding="utf-8",
)

print(
    json.dumps(
        report,
        indent=2,
    )
)
