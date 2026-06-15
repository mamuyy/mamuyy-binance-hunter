#!/usr/bin/env python3
import json
import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

SRC = (
    ROOT
    / "data/ml_calibration_with_position_sizing_20260610.csv"
)

OUT_CSV = (
    ROOT
    / "data/ml_calibration_with_portfolio_allocation_20260610.csv"
)

OUT_JSON = (
    ROOT
    / "logs/phase4d_portfolio_allocation_engine_report_20260610.json"
)


df = pd.read_csv(SRC)

df = df[
    df["win_loss"].isin(["WIN", "LOSS"])
].copy()

df["pnl_numeric"] = pd.to_numeric(
    df["pnl_pct"],
    errors="coerce",
).fillna(0)

df["position_numeric"] = pd.to_numeric(
    df["position_size_multiplier"],
    errors="coerce",
).fillna(0)

rows_out = []

for symbol, group in df.groupby("symbol"):
    rows = len(group)

    if rows < 20:
        continue

    wins = group[
        group["pnl_numeric"] > 0
    ]["pnl_numeric"]

    losses = group[
        group["pnl_numeric"] <= 0
    ]["pnl_numeric"]

    if wins.empty or losses.empty:
        continue

    winrate_raw = len(wins) / rows

    average_win_raw = float(wins.mean())

    average_loss_raw = abs(
        float(losses.mean())
    )

    ev_raw = (
        winrate_raw * average_win_raw
        - (1.0 - winrate_raw) * average_loss_raw
    )

    position_raw = float(
        group["position_numeric"].mean()
    )

    allocation_score_raw = (
        ev_raw
        * position_raw
        * math.sqrt(rows)
    )

    rows_out.append(
        {
            "symbol": symbol,
            "rows": int(rows),
            "winrate": round(winrate_raw, 4),
            "ev_pct": round(ev_raw, 4),
            "position_multiplier": round(
                position_raw,
                4,
            ),
            "allocation_score": round(
                allocation_score_raw,
                4,
            ),
            "_allocation_score_raw": (
                allocation_score_raw
            ),
        }
    )

allocation = pd.DataFrame(rows_out)

positive_score = allocation[
    "_allocation_score_raw"
].clip(lower=0)

positive_total = float(
    positive_score.sum()
)

allocation["capital_pct"] = (
    positive_score
    / positive_total
    * 100
).round(2)

allocation = (
    allocation
    .drop(columns=["_allocation_score_raw"])
    .sort_values(
        "allocation_score",
        ascending=False,
    )
    .reset_index(drop=True)
)

report = {
    "phase": "Phase 4D Portfolio Allocation Engine v1",
    "rows": len(allocation),
    "top_allocations": (
        allocation
        .head(20)
        .to_dict("records")
    ),
    "verdict": "PORTFOLIO_ALLOCATION_CREATED",
}

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

allocation.to_csv(
    OUT_CSV,
    index=False,
)

OUT_JSON.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
