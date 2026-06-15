#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

SRC = (
    ROOT
    / "data/ml_calibration_with_trade_quality_20260610.csv"
)

OUT_CSV = (
    ROOT
    / "data/ml_calibration_with_position_sizing_20260610.csv"
)

OUT_JSON = (
    ROOT
    / "logs/phase4c_position_sizing_engine_report_20260610.json"
)


size_map = {
    "A+": 1.5,
    "A": 1.25,
    "A-": 1.1,
    "B+": 1.0,
    "B": 0.75,
    "C": 0.5,
    "D": 0.0,
    "UNRANKED": 0.25,
}

risk_map = {
    "A+": 1.0,
    "A": 0.85,
    "A-": 0.75,
    "B+": 0.65,
    "B": 0.5,
    "C": 0.35,
    "D": 0.0,
    "UNRANKED": 0.2,
}

action_map = {
    "A+": "MAX_SIZE",
    "A": "LARGE_SIZE",
    "A-": "LARGE_SIZE",
    "B+": "NORMAL_SIZE",
    "B": "MEDIUM_SIZE",
    "C": "SMALL_SIZE",
    "D": "BLOCK",
    "UNRANKED": "MICRO_OBSERVE",
}

rank_order = [
    "A+",
    "A",
    "A-",
    "B+",
    "B",
    "C",
    "UNRANKED",
    "D",
]


df = pd.read_csv(SRC)

df["position_size_multiplier"] = (
    df["trade_quality_rank"]
    .map(size_map)
    .astype(float)
)

df["recommended_risk_pct"] = (
    df["trade_quality_rank"]
    .map(risk_map)
    .astype(float)
)

df["position_action"] = (
    df["trade_quality_rank"]
    .map(action_map)
    .astype(str)
)

known = df[
    df["win_loss"].isin(["WIN", "LOSS"])
].copy()

known["pnl_pct"] = pd.to_numeric(
    known["pnl_pct"],
    errors="coerce",
)

rank_summary = []

for rank in rank_order:
    group = known[
        known["trade_quality_rank"] == rank
    ]

    rows = len(group)
    wins = int(group["win_loss"].eq("WIN").sum())
    losses = int(group["win_loss"].eq("LOSS").sum())

    rank_summary.append(
        {
            "rank": rank,
            "rows": rows,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / rows, 4),
            "avg_pnl_pct": round(
                float(group["pnl_pct"].mean()),
                4,
            ),
            "position_size_multiplier": size_map[rank],
            "recommended_risk_pct": risk_map[rank],
        }
    )

report = {
    "phase": "Phase 4C Position Sizing Engine v1",
    "source_csv": str(SRC),
    "output_csv": str(OUT_CSV),
    "rows": len(df),
    "size_map": size_map,
    "risk_map": risk_map,
    "rank_summary": rank_summary,
    "safety": {
        "read_only_source": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
    "verdict": "POSITION_SIZING_DATASET_CREATED",
}

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

df.to_csv(OUT_CSV, index=False)

OUT_JSON.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
