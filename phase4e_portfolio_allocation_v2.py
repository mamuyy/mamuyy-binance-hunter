#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

SRC = ROOT / "data/ml_calibration_with_position_sizing_20260610.csv"
OUT_CSV = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
OUT_JSON = ROOT / "logs/phase4e_portfolio_allocation_v2_report_20260610.json"


df = pd.read_csv(SRC)

df = df[df["win_loss"].isin(["WIN", "LOSS"])].copy()

df["pnl_pct"] = pd.to_numeric(
    df["pnl_pct"],
    errors="coerce",
).fillna(0)

df["position_size_multiplier"] = pd.to_numeric(
    df["position_size_multiplier"],
    errors="coerce",
).fillna(0)

rows_out = []

for symbol, g in df.groupby("symbol"):
    rows = len(g)

    if rows < 20:
        continue

    wins = g[g["pnl_pct"] > 0]["pnl_pct"]
    losses = g[g["pnl_pct"] <= 0]["pnl_pct"]

    if len(wins) == 0 or len(losses) == 0:
        continue

    wr = len(wins) / rows
    avg_win = wins.mean()
    avg_loss = abs(losses.mean())

    ev = (wr * avg_win) - ((1 - wr) * avg_loss)

    position_mult = g["position_size_multiplier"].mean()

    sample_weight = min(rows / 300, 1.0)
    raw_score = ev * position_mult
    allocation_score_v2 = raw_score * sample_weight

    if allocation_score_v2 <= 0:
        allocation_score_v2 = 0

    rows_out.append(
        {
            "symbol": symbol,
            "rows": int(rows),
            "winrate": round(wr, 4),
            "ev_pct": round(ev, 4),
            "position_multiplier": round(position_mult, 4),
            "sample_weight": round(sample_weight, 4),
            "allocation_score_v2": round(
                allocation_score_v2,
                6,
            ),
        }
    )

alloc = pd.DataFrame(rows_out).sort_values(
    "allocation_score_v2",
    ascending=False,
)

total = alloc["allocation_score_v2"].sum()

if total > 0:
    alloc["capital_pct_v2"] = (
        alloc["allocation_score_v2"] / total * 100
    ).round(2)
else:
    alloc["capital_pct_v2"] = 0

report = {
    "phase": "Phase 4E Portfolio Allocation V2",
    "source_csv": str(SRC),
    "output_csv": str(OUT_CSV),
    "rows": len(alloc),
    "sample_penalty_rule": (
        "sample_weight = min(rows / 300, 1.0)"
    ),
    "top_allocations": (
        alloc.head(20).to_dict("records")
    ),
    "bottom_allocations": (
        alloc.tail(10).to_dict("records")
    ),
    "safety": {
        "read_only_source": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
    "verdict": "PORTFOLIO_ALLOCATION_V2_CREATED",
}

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

alloc.to_csv(OUT_CSV, index=False)

OUT_JSON.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
