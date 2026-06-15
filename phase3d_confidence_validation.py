#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score


ROOT = Path(__file__).resolve().parent

CSV = (
    ROOT
    / "data/ml_calibration_with_dynamic_confidence_20260610.csv"
)

OUT = (
    ROOT
    / "logs/phase3d_confidence_validation_report_20260610.json"
)


df = pd.read_csv(CSV)

df = df[df["win_loss"].isin(["WIN", "LOSS"])].copy()

target = df["win_loss"].map(
    {
        "WIN": 1,
        "LOSS": 0,
    }
)

raw_probability = pd.to_numeric(
    df["score"],
    errors="coerce",
) / 100.0

dynamic_probability = pd.to_numeric(
    df["dynamic_confidence"],
    errors="coerce",
)

valid = (
    target.notna()
    & raw_probability.notna()
    & dynamic_probability.notna()
)

target = target.loc[valid].astype(int)
raw_probability = raw_probability.loc[valid].astype(float)
dynamic_probability = dynamic_probability.loc[valid].astype(float)

raw_result = {
    "brier": round(
        float(
            brier_score_loss(
                target,
                raw_probability,
            )
        ),
        6,
    ),
    "auc": round(
        float(
            roc_auc_score(
                target,
                raw_probability,
            )
        ),
        6,
    ),
}

dynamic_result = {
    "brier": round(
        float(
            brier_score_loss(
                target,
                dynamic_probability,
            )
        ),
        6,
    ),
    "auc": round(
        float(
            roc_auc_score(
                target,
                dynamic_probability,
            )
        ),
        6,
    ),
}

winner = (
    "dynamic_confidence"
    if dynamic_result["brier"] < raw_result["brier"]
    else "raw_score"
)

report = {
    "phase": "Phase 3D Confidence Calibration Validation",
    "raw_score": raw_result,
    "dynamic_confidence": dynamic_result,
    "winner": winner,
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
