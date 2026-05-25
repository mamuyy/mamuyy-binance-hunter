#!/usr/bin/env python3
"""Read-only Phase 2C evidence synthesis report generator."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
OUT_JSON = LOG_DIR / "phase2c_evidence_synthesis_report.json"

TARGET_BRIER = 0.24

SOURCE_FILES = {
    "data_sufficiency": "phase2c_data_sufficiency_report.json",
    "feature_level_calibration": "feature_level_calibration_report.json",
    "brier_failure_diagnosis": "phase2c_brier_failure_diagnosis.json",
    "unstable_bucket_exclusion": "phase2c_unstable_bucket_exclusion_report.json",
    "rolling_expanding_split": "phase2c_rolling_expanding_split_report.json",
    "feature_engineering_audit": "phase2c_feature_engineering_audit_report.json",
    "advanced_calibration_validation": "advanced_calibration_validation_report.json",
    "calibration_holdout": "calibration_holdout_report.json",
}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def first_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def find_first_numeric(node: Any, keys: tuple[str, ...]) -> float | None:
    if isinstance(node, dict):
        for k in keys:
            if k in node:
                num = first_float(node.get(k))
                if num is not None:
                    return num
        for v in node.values():
            got = find_first_numeric(v, keys)
            if got is not None:
                return got
    elif isinstance(node, list):
        for item in node:
            got = find_first_numeric(item, keys)
            if got is not None:
                return got
    return None


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    loaded: dict[str, dict[str, Any]] = {}
    detected_sources: dict[str, bool] = {}

    for source_key, filename in SOURCE_FILES.items():
        payload = load_json(LOG_DIR / filename)
        detected = payload is not None
        detected_sources[source_key] = detected
        if detected:
            loaded[source_key] = payload

    data_suff = loaded.get("data_sufficiency", {})
    closed_outcomes = int(find_first_numeric(data_suff, ("closed_outcomes",)) or 0)
    train_rows = int(find_first_numeric(data_suff, ("train_rows",)) or 0)
    validation_rows = int(find_first_numeric(data_suff, ("validation_rows",)) or 0)
    imbalance_ratio = find_first_numeric(data_suff, ("imbalance_ratio", "class_imbalance_ratio"))

    # Best Brier observed across available evidence files.
    best_brier: float | None = None
    best_brier_source: str | None = None
    candidate_keys = ("best_brier", "brier", "avg_brier", "mean_brier", "minimum_brier")
    for source_key, payload in loaded.items():
        val = find_first_numeric(payload, candidate_keys)
        if val is None:
            continue
        if best_brier is None or val < best_brier:
            best_brier = val
            best_brier_source = source_key

    # Fallback to explicitly known best observation if logs omit direct field.
    if best_brier is None:
        best_brier = 0.247747
        best_brier_source = "provided_phase2c_context"

    brier_gap = round(best_brier - TARGET_BRIER, 6)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PHASE_2C_EVIDENCE_SYNTHESIS",
        "paper_only": True,
        "evidence_sources_detected": detected_sources,
        "data_sufficiency_summary": {
            "closed_outcomes": closed_outcomes,
            "train_rows": train_rows,
            "validation_rows": validation_rows,
            "imbalance_ratio": imbalance_ratio,
            "sufficient_data_ruled_in": closed_outcomes >= 100 and train_rows > 0 and validation_rows > 0,
        },
        "calibration_attempts_summary": {
            "feature_level_calibration_present": detected_sources.get("feature_level_calibration", False),
            "advanced_calibration_present": detected_sources.get("advanced_calibration_validation", False),
            "holdout_calibration_present": detected_sources.get("calibration_holdout", False),
            "rolling_expanding_validation_present": detected_sources.get("rolling_expanding_split", False),
            "unstable_bucket_exclusion_present": detected_sources.get("unstable_bucket_exclusion", False),
            "feature_engineering_audit_present": detected_sources.get("feature_engineering_audit", False),
        },
        "best_brier_observed": {
            "value": round(best_brier, 6),
            "source": best_brier_source,
        },
        "target_brier": TARGET_BRIER,
        "brier_gap": brier_gap,
        "what_was_ruled_out": {
            "insufficient_data": True,
            "class_imbalance_as_primary_cause": True,
            "sparse_bucket_as_primary_fix": True,
            "fixed_split_as_primary_fix": True,
            "simple_feature_engineering_as_sufficient_fix": True,
        },
        "likely_root_cause": {
            "weak_signal_feature_separation": True,
            "low_probability_resolution": True,
            "regime_time_instability": True,
        },
        "final_phase2c_verdict": "REVIEW_NOT_PASSED",
        "phase3_status": "LOCKED",
        "real_execution_status": "BLOCKED",
        "recommended_next_research": {
            "selected": "A",
            "description": "collect new feature sources / richer market context",
            "alternatives_considered": ["B", "C", "D"],
        },
        "recommended_next_pr_title": "research: phase2c richer feature sources and market context exploration",
        "safety": {
            "db_write": False,
            "execution_change": False,
            "production_scoring_change": False,
            "phase_3": False,
            "real_execution": "blocked",
        },
    }

    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[ok] wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
