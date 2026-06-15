#!/usr/bin/env python3
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent

SRC = (
    ROOT
    / "data/ml_calibration_with_survival_features_20260610.csv"
)

OUT_CSV = (
    ROOT
    / "data/ml_calibration_with_trade_quality_20260610.csv"
)

OUT_JSON = (
    ROOT
    / "logs/phase4b_trade_quality_ranking_engine_report_20260610.json"
)


df = pd.read_csv(SRC)

score = pd.to_numeric(
    df["score"],
    errors="coerce",
).fillna(0)

df["score_tier"] = np.select(
    [
        score >= 80,
        score >= 70,
        score >= 45,
    ],
    [
        "ELITE",
        "HIGH",
        "MID",
    ],
    default="LOW",
)

regime = (
    df["matched_regime"]
    .astype(str)
    .str.strip()
    .str.upper()
)

tier = df["score_tier"].astype(str)

df["trade_quality_rank"] = np.select(
    [
        regime.eq("TRENDING BULL")
        & tier.eq("ELITE"),

        regime.eq("SIDEWAYS / CHOPPY")
        & tier.eq("ELITE"),

        regime.eq("RISK OFF")
        & tier.eq("ELITE"),

        regime.eq("TRENDING BULL")
        & tier.eq("MID"),

        regime.eq("SIDEWAYS / CHOPPY")
        & tier.eq("HIGH"),

        regime.eq("TRENDING BULL")
        & tier.eq("LOW"),

        regime.eq("TRENDING BULL")
        & tier.eq("HIGH"),
    ],
    [
        "A+",
        "A",
        "A-",
        "B+",
        "B",
        "C",
        "D",
    ],
    default="UNRANKED",
)

quality_score_map = {
    "A+": 100,
    "A": 90,
    "A-": 85,
    "B+": 80,
    "B": 70,
    "C": 55,
    "D": 0,
    "UNRANKED": 40,
}

df["trade_quality_score"] = (
    df["trade_quality_rank"]
    .map(quality_score_map)
    .astype(int)
)

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

    average_pnl = round(
        float(group["pnl_pct"].mean()),
        4,
    )

    rank_summary.append(
        {
            "rank": rank,
            "rows": rows,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / rows, 4),
            "ev_pct": average_pnl,
            "avg_pnl": average_pnl,
        }
    )

report = {
    "phase": "Phase 4B Trade Quality Ranking Engine v1",
    "source_csv": str(SRC),
    "output_csv": str(OUT_CSV),
    "rows": len(df),
    "ranking_rules": {
        "A+": "TRENDING BULL + ELITE score tier",
        "A": "SIDEWAYS / CHOPPY + ELITE score tier",
        "A-": "RISK OFF + ELITE score tier",
        "B+": "TRENDING BULL + MID score tier",
        "B": "SIDEWAYS / CHOPPY + HIGH score tier",
        "C": "TRENDING BULL + LOW score tier",
        "D": "TRENDING BULL + HIGH score tier",
        "UNRANKED": "all other combinations",
    },
    "rank_summary": rank_summary,
    "safety": {
        "read_only_source": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
    "verdict": "TRADE_QUALITY_RANKING_DATASET_CREATED",
}

OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

df.to_csv(OUT_CSV, index=False)

OUT_JSON.write_text(
    json.dumps(report, indent=2),
    encoding="utf-8",
)

print(json.dumps(report, indent=2))
