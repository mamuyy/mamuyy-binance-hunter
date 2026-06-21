"""Phase 9D.1C-B temporal guards for ML feature construction.

Read-only/fail-closed helpers. They do not train, score, route orders, write broker
state, or change thresholds.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

LABEL_LEAKAGE_TOKENS = (
    "target", "label", "outcome", "y_true", "actual", "win_loss", "status",
    "pnl", "profit", "return", "future", "direction_hit", "hit_tp", "hit_sl",
)
ALLOWED_PREDICTION_TOKENS = ("prediction", "predicted", "y_pred", "probability", "score")


def _parse_ts(value: Any) -> Optional[pd.Timestamp]:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(parsed) else parsed


def target_like_feature_columns(columns: Iterable[str]) -> List[str]:
    leaked: List[str] = []
    for column in columns:
        name = str(column).lower()
        if any(allowed in name for allowed in ALLOWED_PREDICTION_TOKENS):
            continue
        if any(token in name for token in LABEL_LEAKAGE_TOKENS):
            leaked.append(str(column))
    return sorted(set(leaked))


def validate_temporal_feature_rows(
    rows: Any,
    feature_columns: Optional[Sequence[str]] = None,
    require_target_future: bool = True,
    source_artifact: Optional[str] = None,
) -> Dict[str, Any]:
    frame = pd.DataFrame(rows)
    findings: List[Dict[str, Any]] = []
    if frame.empty:
        return {
            "status": "REVIEW",
            "asof_feature_join_status": "REVIEW",
            "future_feature_violation_count": 0,
            "missing_feature_timestamp_count": 0,
            "target_leakage_column_count": len(target_like_feature_columns(feature_columns or [])),
            "feature_timestamp_coverage": 0.0,
            "temporal_guard_findings": [{"reason": "no feature rows available", "source_artifact": source_artifact}],
        }
    feature_columns = list(feature_columns or [c for c in frame.columns if not str(c).startswith("__")])
    leaked = target_like_feature_columns(feature_columns)
    required = ["prediction_timestamp", "feature_timestamp_max"]
    missing_columns = [col for col in required if col not in frame.columns]
    if missing_columns:
        findings.append({"reason": "missing_required_timestamp_columns", "columns": missing_columns, "source_artifact": source_artifact})
    future_feature = 0
    missing_feature_ts = 0
    non_future_target = 0
    for idx, row in frame.iterrows():
        pred = _parse_ts(row.get("prediction_timestamp"))
        feature_ts = _parse_ts(row.get("feature_timestamp_max"))
        if pred is None or feature_ts is None:
            missing_feature_ts += 1
            findings.append({"row_index": int(idx), "reason": "missing_or_invalid_prediction_or_feature_timestamp"})
            continue
        if feature_ts > pred:
            future_feature += 1
            findings.append({"row_index": int(idx), "reason": "feature_timestamp_max_after_prediction_timestamp", "prediction_timestamp": str(pred), "feature_timestamp_max": str(feature_ts)})
        for col in ("target_timestamp", "label_timestamp", "outcome_timestamp", "target_maturity_timestamp"):
            if col in frame.columns and row.get(col) not in (None, ""):
                target = _parse_ts(row.get(col))
                if target is None:
                    findings.append({"row_index": int(idx), "reason": f"invalid_{col}"})
                elif require_target_future and target <= pred:
                    non_future_target += 1
                    findings.append({"row_index": int(idx), "reason": f"{col}_not_after_prediction_timestamp"})
    if leaked:
        findings.append({"reason": "label_or_outcome_columns_in_model_features", "columns": leaked})
    coverage = 0.0 if len(frame) == 0 else (len(frame) - missing_feature_ts) / len(frame)
    blocked = future_feature or non_future_target or leaked or missing_columns or missing_feature_ts
    status = "BLOCKED" if blocked else "PASS"
    return {
        "status": status,
        "asof_feature_join_status": "BLOCKED" if future_feature else ("REVIEW" if missing_feature_ts or missing_columns else "PASS"),
        "future_feature_violation_count": int(future_feature),
        "missing_feature_timestamp_count": int(missing_feature_ts),
        "target_leakage_column_count": len(leaked),
        "target_or_outcome_not_future_count": int(non_future_target),
        "feature_timestamp_coverage": coverage,
        "temporal_guard_findings": findings,
    }


def asof_feature_join(
    predictions: pd.DataFrame,
    features: pd.DataFrame,
    by: str = "symbol",
    prediction_time_col: str = "prediction_timestamp",
    feature_time_col: str = "timestamp",
) -> pd.DataFrame:
    """Join latest per-symbol feature row with timestamp <= prediction time.

    pandas.merge_asof requires its time key to be monotonic. Sorting a mixed-symbol
    frame by [symbol, timestamp] can make the timestamp key non-monotonic globally,
    so this helper executes merge_asof independently for each symbol and restores
    the original prediction row order afterward.
    """
    out = predictions.copy()
    if "feature_timestamp_max" not in out.columns:
        out["feature_timestamp_max"] = pd.NaT
    if predictions.empty or features.empty:
        return out

    left = predictions.copy()
    right = features.copy()
    left[prediction_time_col] = pd.to_datetime(left[prediction_time_col], errors="coerce", utc=True)
    right[feature_time_col] = pd.to_datetime(right[feature_time_col], errors="coerce", utc=True)
    left["__asof_original_order"] = range(len(left))

    unmatched_feature_columns = [column for column in right.columns if column != by and column not in left.columns]
    joined_groups: List[pd.DataFrame] = []
    for symbol, left_group in left.groupby(by, dropna=False, sort=False):
        right_group = right[right[by].eq(symbol)] if pd.notna(symbol) else right[right[by].isna()]
        valid_left = left_group[left_group[prediction_time_col].notna()].copy()
        invalid_left = left_group[left_group[prediction_time_col].isna()].copy()
        group_parts: List[pd.DataFrame] = []
        if not valid_left.empty and not right_group.empty:
            right_group = right_group[right_group[feature_time_col].notna()].copy()
            if not right_group.empty:
                merged = pd.merge_asof(
                    valid_left.sort_values(prediction_time_col),
                    right_group.drop(columns=[by]).sort_values(feature_time_col),
                    left_on=prediction_time_col,
                    right_on=feature_time_col,
                    direction="backward",
                    suffixes=("", "_feature"),
                )
                group_parts.append(merged)
            else:
                group_parts.append(valid_left)
        elif not valid_left.empty:
            group_parts.append(valid_left)
        if not invalid_left.empty:
            group_parts.append(invalid_left)
        group = pd.concat(group_parts, ignore_index=True, sort=False) if group_parts else left_group.copy()
        for column in unmatched_feature_columns:
            if column not in group.columns:
                group[column] = pd.NA
        if feature_time_col not in group.columns:
            group[feature_time_col] = pd.NaT
        joined_groups.append(group)

    joined = pd.concat(joined_groups, ignore_index=True, sort=False) if joined_groups else left.copy()
    joined = joined.sort_values("__asof_original_order").drop(columns=["__asof_original_order"]).reset_index(drop=True)
    joined["feature_timestamp_max"] = pd.to_datetime(joined.get(feature_time_col), errors="coerce", utc=True)
    return joined
