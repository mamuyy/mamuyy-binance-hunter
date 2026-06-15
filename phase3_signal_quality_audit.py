from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_SOURCE = Path(
    "data/ml_calibration_with_lifecycle_features_20260610.csv"
)

DEFAULT_REPORT = Path(
    "logs/phase3_signal_quality_audit_report_20260610.json"
)

TIER_ORDER = {
    "ELITE": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "AVOID": 4,
    "INSUFFICIENT_SAMPLE": 5,
}

TIER_RULES = {
    "ELITE": "rows >=100 and winrate >=54%",
    "HIGH": "rows >=100 and winrate >=52%",
    "MEDIUM": "rows >=100 and winrate >=49%",
    "LOW": "rows >=100 and winrate >=47%",
    "AVOID": "rows >=100 and winrate <47%",
    "INSUFFICIENT_SAMPLE": "rows <100",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 3.0 Signal Quality Audit"
    )

    parser.add_argument(
        "--source-csv",
        type=Path,
        default=DEFAULT_SOURCE,
    )

    parser.add_argument(
        "--report-json",
        type=Path,
        default=DEFAULT_REPORT,
    )

    return parser.parse_args()


def classify_tier(
    rows: int,
    winrate: float,
) -> str:
    if rows < 100:
        return "INSUFFICIENT_SAMPLE"

    if winrate >= 0.54:
        return "ELITE"

    if winrate >= 0.52:
        return "HIGH"

    if winrate >= 0.49:
        return "MEDIUM"

    if winrate >= 0.47:
        return "LOW"

    return "AVOID"


def load_known_rows(source_csv: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        source_csv,
        low_memory=False,
    )

    required_columns = {
        "matched_regime",
        "score",
        "win_loss",
    }

    missing = sorted(
        required_columns - set(frame.columns)
    )

    if missing:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(missing)
        )

    score_numeric = pd.to_numeric(
        frame["score"],
        errors="coerce",
    )

    invalid_scores = int(score_numeric.isna().sum())

    if invalid_scores:
        raise ValueError(
            f"Invalid/null score rows: {invalid_scores}"
        )

    frame["score_bucket"] = pd.cut(
        score_numeric,
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

    result = frame[
        frame["win_loss"]
        .astype("string")
        .isin(["WIN", "LOSS"])
    ].copy()

    result["_is_win"] = (
        result["win_loss"]
        .astype("string")
        .eq("WIN")
    )

    return result


def build_matrix(frame: pd.DataFrame) -> list[dict[str, Any]]:
    grouped = (
        frame
        .groupby(
            ["matched_regime", "score_bucket"],
            dropna=False,
            sort=False,
        )
        .agg(
            rows=("_is_win", "size"),
            wins=("_is_win", "sum"),
        )
        .reset_index()
    )

    grouped["losses"] = (
        grouped["rows"] - grouped["wins"]
    )

    grouped["winrate"] = (
        grouped["wins"]
        / grouped["rows"]
    ).round(4)

    grouped["tier"] = grouped.apply(
        lambda row: classify_tier(
            int(row["rows"]),
            float(row["winrate"]),
        ),
        axis=1,
    )

    grouped["_tier_order"] = (
        grouped["tier"]
        .map(TIER_ORDER)
    )

    grouped = grouped.sort_values(
        [
            "_tier_order",
            "winrate",
            "matched_regime",
            "score_bucket",
        ],
        ascending=[True, False, True, True],
        kind="mergesort",
    ).reset_index(drop=True)

    matrix: list[dict[str, Any]] = []

    for row in grouped.itertuples(index=False):
        matrix.append(
            {
                "matched_regime": str(row.matched_regime),
                "score_bucket": str(row.score_bucket),
                "rows": int(row.rows),
                "wins": int(row.wins),
                "losses": int(row.losses),
                "winrate": float(row.winrate),
                "tier": str(row.tier),
            }
        )

    return matrix


def main() -> None:
    args = parse_args()

    source_csv = args.source_csv.expanduser().resolve()
    report_json = args.report_json.expanduser().resolve()

    frame = load_known_rows(source_csv)
    matrix = build_matrix(frame)

    report = {
        "phase": "Phase 3.0 Signal Quality Audit",
        "source_csv": str(source_csv),
        "rows": int(len(frame)),
        "tier_rules": TIER_RULES,
        "score_regime_matrix": matrix,
        "safety": {
            "read_only": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
        "verdict": "SIGNAL_QUALITY_TIERS_READY_FOR_REVIEW",
    }

    report_json.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_json.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print("Phase: Phase 3.0 Signal Quality Audit")
    print(f"Rows: {len(frame)}")
    print(f"Matrix cells: {len(matrix)}")
    print(f"Report: {report_json}")
    print("Verdict: SIGNAL_QUALITY_TIERS_READY_FOR_REVIEW")


if __name__ == "__main__":
    main()
