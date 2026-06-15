"""Read-only Phase 3A signal-tier filtering simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_SOURCE = Path(
    "data/ml_calibration_with_lifecycle_features_20260610.csv"
)

DEFAULT_QUALITY_REPORT = Path(
    "logs/phase3_signal_quality_audit_report_20260610.json"
)

DEFAULT_REPORT = Path(
    "logs/phase3_signal_tier_filter_simulator_report_20260610.json"
)

TIER_ORDER = [
    "UNCLASSIFIED",
    "ELITE",
    "HIGH",
    "MEDIUM",
    "LOW",
    "AVOID",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3A Signal Tier Filter Simulator "
            "(PAPER_ONLY read-only analytics)"
        )
    )
    parser.add_argument("--source-csv", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--quality-report-json",
        type=Path,
        default=DEFAULT_QUALITY_REPORT,
    )
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def derive_score_bucket(score: pd.Series) -> pd.Series:
    numeric_score = pd.to_numeric(score, errors="coerce")
    invalid_count = int(numeric_score.isna().sum())

    if invalid_count:
        raise ValueError(f"Invalid/null score rows: {invalid_count}")

    return pd.cut(
        numeric_score,
        bins=[
            float("-inf"),
            40,
            50,
            60,
            70,
            80,
            float("inf"),
        ],
        labels=[
            "0-40",
            "40-50",
            "50-60",
            "60-70",
            "70-80",
            "80-100",
        ],
        right=True,
        include_lowest=True,
    ).astype("string")


def load_quality_mapping(
    quality_report_path: Path,
) -> dict[tuple[str, str], str]:
    payload = json.loads(
        quality_report_path.read_text(encoding="utf-8")
    )
    matrix = payload.get("score_regime_matrix")

    if not isinstance(matrix, list):
        raise ValueError("Quality report has no score_regime_matrix")

    mapping: dict[tuple[str, str], str] = {}

    for item in matrix:
        regime = str(item["matched_regime"])
        score_bucket = str(item["score_bucket"])
        audit_tier = str(item["tier"])
        dynamic_tier = (
            "UNCLASSIFIED"
            if audit_tier == "INSUFFICIENT_SAMPLE"
            else audit_tier
        )

        key = (regime, score_bucket)
        if key in mapping:
            raise ValueError(f"Duplicate quality mapping: {key}")

        mapping[key] = dynamic_tier

    return mapping


def summarize(
    frame: pd.DataFrame,
    scenario: str,
) -> dict[str, Any]:
    rows = int(len(frame))
    wins = int(frame["_is_win"].sum())
    losses = rows - wins

    winrate = round(wins / rows, 4) if rows else 0.0
    avg_pnl = round(float(frame["_pnl"].mean()), 4) if rows else 0.0
    median_pnl = (
        round(float(frame["_pnl"].median()), 4)
        if rows
        else 0.0
    )

    return {
        "scenario": scenario,
        "rows": rows,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "avg_pnl": avg_pnl,
        "median_pnl": median_pnl,
    }


def main() -> None:
    args = parse_args()

    source_path = args.source_csv.expanduser().resolve()
    quality_report_path = (
        args.quality_report_json.expanduser().resolve()
    )
    report_path = args.report_json.expanduser().resolve()

    frame = pd.read_csv(source_path, low_memory=False)

    required_columns = {
        "matched_regime",
        "score",
        "win_loss",
        "pnl_pct",
    }

    missing_columns = sorted(required_columns - set(frame.columns))
    if missing_columns:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing_columns)
        )

    mapping = load_quality_mapping(quality_report_path)
    frame["_score_bucket"] = derive_score_bucket(frame["score"])

    scenarios: list[str] = []

    for regime, score_bucket in zip(
        frame["matched_regime"],
        frame["_score_bucket"],
    ):
        key = (str(regime), str(score_bucket))
        scenario = mapping.get(key)

        if scenario is None:
            raise ValueError(f"No quality mapping for {key}")

        scenarios.append(scenario)

    frame["_scenario"] = scenarios

    known_mask = (
        frame["win_loss"]
        .astype("string")
        .isin(["WIN", "LOSS"])
    )
    known = frame.loc[known_mask].copy()
    known["_is_win"] = (
        known["win_loss"].astype("string").eq("WIN")
    )
    known["_pnl"] = pd.to_numeric(
        known["pnl_pct"],
        errors="coerce",
    )

    invalid_pnl = int(known["_pnl"].isna().sum())
    if invalid_pnl:
        raise ValueError(
            f"Invalid/null PnL among known rows: {invalid_pnl}"
        )

    tier_summary = [
        summarize(
            known[known["_scenario"] == tier],
            tier,
        )
        for tier in TIER_ORDER
    ]

    scenario_definitions = [
        (
            "ALL_BASELINE",
            pd.Series(True, index=known.index),
        ),
        (
            "ELITE_ONLY",
            known["_scenario"].eq("ELITE"),
        ),
        (
            "ELITE_PLUS_HIGH",
            known["_scenario"].isin(["ELITE", "HIGH"]),
        ),
        (
            "ELITE_HIGH_MEDIUM",
            known["_scenario"].isin(
                ["ELITE", "HIGH", "MEDIUM"]
            ),
        ),
        (
            "EXCLUDE_AVOID",
            ~known["_scenario"].eq("AVOID"),
        ),
    ]

    scenario_summary = [
        summarize(known.loc[mask], scenario_name)
        for scenario_name, mask in scenario_definitions
    ]

    report = {
        "phase": "Phase 3A Signal Tier Filter Simulator",
        "source_csv": str(source_path),
        "tier_summary": tier_summary,
        "scenario_summary": scenario_summary,
        "safety": {
            "read_only": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
        "verdict": "SIGNAL_TIER_FILTER_SIMULATION_COMPLETE",
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("Phase: Phase 3A Signal Tier Filter Simulator")
    print(f"Known WIN/LOSS rows: {len(known)}")
    print(f"Quality map cells: {len(mapping)}")
    print(f"Report: {report_path}")
    print("Verdict: SIGNAL_TIER_FILTER_SIMULATION_COMPLETE")


if __name__ == "__main__":
    main()
