#!/usr/bin/env python3
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent

TRADES_CSV = (
    ROOT
    / "data/ml_calibration_with_position_sizing_20260610.csv"
)

ALLOCATION_CSV = (
    ROOT
    / "data/ml_portfolio_allocation_v2_20260610.csv"
)

OUT_JSON = (
    ROOT
    / "logs/phase6a_shadow_portfolio_validator_report_20260610.json"
)

REPORTED_TRADES_PATH = (
    "/home/ubuntu/mamuyy-binance-hunter/"
    "data/ml_calibration_with_position_sizing_20260610.csv"
)

REPORTED_ALLOCATION_PATH = (
    "/home/ubuntu/mamuyy-binance-hunter/"
    "data/ml_portfolio_allocation_v2_20260610.csv"
)


trades = pd.read_csv(TRADES_CSV)
allocation = pd.read_csv(ALLOCATION_CSV)

baseline = trades[
    trades["win_loss"].isin(["WIN", "LOSS"])
].copy()

baseline["pnl_numeric"] = pd.to_numeric(
    baseline["pnl_pct"],
    errors="coerce",
)

baseline = baseline[
    baseline["pnl_numeric"].notna()
].copy()

baseline_result = {
    "rows": int(len(baseline)),
    "avg_pnl_pct": round(
        float(baseline["pnl_numeric"].mean()),
        4,
    ),
    "winrate": round(
        float(
            baseline["win_loss"]
            .eq("WIN")
            .mean()
        ),
        4,
    ),
}


active_allocation = allocation[
    pd.to_numeric(
        allocation["capital_pct_v2"],
        errors="coerce",
    ).fillna(0) > 0
][
    [
        "symbol",
        "capital_pct_v2",
    ]
].copy()

active_allocation["allocation_weight"] = (
    pd.to_numeric(
        active_allocation["capital_pct_v2"],
        errors="coerce",
    )
    / 100.0
)


shadow = baseline.merge(
    active_allocation[
        [
            "symbol",
            "allocation_weight",
        ]
    ],
    on="symbol",
    how="inner",
    validate="many_to_one",
)

shadow["weighted_pnl_row"] = (
    shadow["pnl_numeric"]
    * shadow["allocation_weight"]
)


contributors = (
    shadow
    .groupby(
        "symbol",
        as_index=False,
        sort=True,
    )
    .agg(
        rows=("symbol", "size"),
        avg_pnl_pct=("pnl_numeric", "mean"),
        allocation_weight=(
            "allocation_weight",
            "first",
        ),
        weighted_contribution=(
            "weighted_pnl_row",
            "sum",
        ),
    )
    .sort_values(
        "weighted_contribution",
        ascending=False,
    )
    .reset_index(drop=True)
)


weighted_exposure = float(
    (
        contributors["rows"]
        * contributors["allocation_weight"]
    ).sum()
)

weighted_contribution_total = float(
    contributors["weighted_contribution"].sum()
)

shadow_result = {
    "rows": int(len(shadow)),
    "active_symbols": int(
        shadow["symbol"].nunique()
    ),
    "avg_weighted_pnl_pct": round(
        weighted_contribution_total
        / weighted_exposure,
        4,
    ),
    "winrate": round(
        float(
            shadow["win_loss"]
            .eq("WIN")
            .mean()
        ),
        4,
    ),
}


report = {
    "phase": "Phase 6A Shadow Portfolio Validator",
    "source_trades": REPORTED_TRADES_PATH,
    "source_allocation": REPORTED_ALLOCATION_PATH,
    "baseline": baseline_result,
    "shadow_portfolio": shadow_result,
    "top_contributors": (
        contributors
        .head(10)
        .to_dict("records")
    ),
    "worst_contributors": (
        contributors
        .tail(10)
        .to_dict("records")
    ),
    "safety": {
        "paper_only": True,
        "production_runtime_changed": False,
        "execution_changed": False,
    },
    "verdict": (
        "SHADOW_PORTFOLIO_VALIDATION_COMPLETE"
    ),
}


OUT_JSON.parent.mkdir(
    parents=True,
    exist_ok=True,
)

OUT_JSON.write_text(
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
