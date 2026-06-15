#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

CSV = (
    ROOT
    / "data/ml_calibration_with_dynamic_confidence_20260610.csv"
)

OUT = (
    ROOT
    / "logs/phase4a_expected_value_audit_report_20260610.json"
)


df = pd.read_csv(CSV)

df = df[df["win_loss"].isin(["WIN", "LOSS"])].copy()

df["pnl_pct"] = pd.to_numeric(
    df["pnl_pct"],
    errors="coerce",
)

df = df[df["pnl_pct"].notna()].copy()

wins = df[df["win_loss"] == "WIN"]["pnl_pct"]
losses = df[df["win_loss"] == "LOSS"]["pnl_pct"]

rows = len(df)
winrate = len(wins) / rows

avg_win = wins.mean()
avg_loss = losses.mean()

expected_value = (
    winrate * avg_win
    + (1.0 - winrate) * avg_loss
)

report = {
    "phase": "Phase 4A EV Audit",
    "rows": int(rows),
    "overall_winrate": round(float(winrate), 4),
    "avg_win_pct": round(float(avg_win), 4),
    "avg_loss_pct": round(float(avg_loss), 4),
    "expected_value_pct": round(
        float(expected_value),
        4,
    ),
    "verdict": (
        "POSITIVE_EV"
        if expected_value > 0
        else "NON_POSITIVE_EV"
    ),
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
