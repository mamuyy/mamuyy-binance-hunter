"""Read-only Phase 3C dynamic-confidence dataset generator."""

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

DEFAULT_OUTPUT = Path(
    "data/ml_calibration_with_dynamic_confidence_20260610.csv"
)

DEFAULT_REPORT = Path(
    "logs/phase3_dynamic_confidence_engine_report_20260610.json"
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
            "Phase 3C Dynamic Confidence Engine "
            "(PAPER_ONLY read-only source)"
        )
    )
    parser.add_argument("--source-csv", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--quality-report-json",
        type=Path,
        default=DEFAULT_QUALITY_REPORT,
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--reported-output-csv",
        type=Path,
        default=None,
    )
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


def load_confidence_mapping(
    quality_report_path: Path,
) -> tuple[
    dict[tuple[str, str], tuple[str, float]],
    int,
]:
    payload = json.loads(
        quality_report_path.read_text(encoding="utf-8")
    )
    matrix = payload.get("score_regime_matrix")

    if not isinstance(matrix, list):
        raise ValueError("Quality report has no score_regime_matrix")

    mapping: dict[
        tuple[str, str],
        tuple[str, float],
    ] = {}
    eligible_cells = 0

    for item in matrix:
        regime = str(item["matched_regime"])
        score_bucket = str(item["score_bucket"])
        audit_tier = str(item["tier"])
        winrate = round(float(item["winrate"]), 4)

        if audit_tier == "INSUFFICIENT_SAMPLE":
            dynamic_tier = "UNCLASSIFIED"
        else:
            dynamic_tier = audit_tier
            eligible_cells += 1

        key = (regime, score_bucket)
        if key in mapping:
            raise ValueError(f"Duplicate confidence mapping: {key}")

        mapping[key] = (dynamic_tier, winrate)

    return mapping, eligible_cells


def summarize_tier(
    frame: pd.DataFrame,
    tier: str,
) -> dict[str, Any]:
    selected = frame[
        frame["dynamic_signal_tier"] == tier
    ]

    rows = int(len(selected))
    wins = int(selected["_is_win"].sum())
    losses = rows - wins
    actual_winrate = round(wins / rows, 4) if rows else 0.0
    avg_dynamic_confidence = (
        round(
            float(selected["dynamic_confidence"].mean()),
            4,
        )
        if rows
        else 0.0
    )

    return {
        "tier": tier,
        "rows": rows,
        "wins": wins,
        "losses": losses,
        "actual_winrate": actual_winrate,
        "avg_dynamic_confidence": avg_dynamic_confidence,
    }


def main() -> None:
    args = parse_args()

    source_path = args.source_csv.expanduser().resolve()
    quality_report_path = (
        args.quality_report_json.expanduser().resolve()
    )
    output_path = args.output_csv.expanduser().resolve()
    report_path = args.report_json.expanduser().resolve()

    if args.reported_output_csv is None:
        reported_output_path = output_path
    else:
        reported_output_path = (
            args.reported_output_csv.expanduser().resolve()
        )

    source = pd.read_csv(source_path, low_memory=False)

    required_columns = {
        "matched_regime",
        "score",
        "win_loss",
    }

    missing_columns = sorted(required_columns - set(source.columns))
    if missing_columns:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing_columns)
        )

    mapping, eligible_cells = load_confidence_mapping(
        quality_report_path
    )

    known_mask = (
        source["win_loss"]
        .astype("string")
        .isin(["WIN", "LOSS"])
    )
    win_mask = (
        source["win_loss"]
        .astype("string")
        .eq("WIN")
    )

    known_rows = int(known_mask.sum())
    known_wins = int((known_mask & win_mask).sum())

    if known_rows == 0:
        raise ValueError("No known WIN/LOSS rows available")

    global_baseline = round(known_wins / known_rows, 4)
    score_bucket = derive_score_bucket(source["score"])

    dynamic_tiers: list[str] = []
    dynamic_confidences: list[float] = []

    for regime, bucket in zip(
        source["matched_regime"],
        score_bucket,
    ):
        key = (str(regime), str(bucket))
        mapped = mapping.get(key)

        if mapped is None:
            raise ValueError(f"No confidence mapping for {key}")

        tier, cell_winrate = mapped
        confidence = (
            global_baseline
            if tier == "UNCLASSIFIED"
            else cell_winrate
        )

        dynamic_tiers.append(tier)
        dynamic_confidences.append(round(float(confidence), 4))

    output = source.copy()
    output["score_bucket"] = score_bucket.to_numpy()
    output["dynamic_signal_tier"] = dynamic_tiers
    output["dynamic_confidence"] = dynamic_confidences
    output["dynamic_confidence_pct"] = [
        round(confidence * 100.0, 2)
        for confidence in dynamic_confidences
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_path, index=False)

    known = output.loc[known_mask].copy()
    known["_is_win"] = (
        known["win_loss"].astype("string").eq("WIN")
    )

    tier_summary = [
        summarize_tier(known, tier)
        for tier in TIER_ORDER
    ]

    report = {
        "phase": "Phase 3C Dynamic Confidence Engine",
        "source_csv": str(source_path),
        "output_csv": str(reported_output_path),
        "rows": int(len(output)),
        "confidence_map_size": eligible_cells,
        "tier_summary": tier_summary,
        "safety": {
            "read_only_source": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
        "verdict": "DYNAMIC_CONFIDENCE_DATASET_CREATED",
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

    print("Phase: Phase 3C Dynamic Confidence Engine")
    print(f"Rows: {len(output)}")
    print(f"Confidence-map size: {eligible_cells}")
    print(f"Global baseline: {global_baseline}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    print("Verdict: DYNAMIC_CONFIDENCE_DATASET_CREATED")


if __name__ == "__main__":
    main()
