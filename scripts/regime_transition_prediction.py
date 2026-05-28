#!/usr/bin/env python3
"""Week 2E regime transition prediction & early warning (paper-only, read-only)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd


WARNING_BANDS = [
    (0, 30, "STABLE"),
    (31, 60, "WATCH"),
    (61, 80, "RISK_ELEVATED"),
    (81, 100, "BRAKE_CANDIDATE"),
]


def _to_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _normalize_win(value: object) -> float:
    if value is None:
        return float("nan")
    s = str(value).strip().upper()
    if s in {"WIN", "TRUE", "1"}:
        return 1.0
    if s in {"LOSS", "FALSE", "0"}:
        return 0.0
    if s == "FLAT":
        return float("nan")
    try:
        v = float(value)
        if v in (0.0, 1.0):
            return v
    except Exception:
        return float("nan")
    return float("nan")


def _find_col(columns, candidates):
    for c in candidates:
        if c in columns:
            return c
    return None


def _safe_norm(series: pd.Series, invert: bool = False) -> pd.Series:
    valid = series.replace([np.inf, -np.inf], np.nan)
    min_v = valid.min(skipna=True)
    max_v = valid.max(skipna=True)
    if pd.isna(min_v) or pd.isna(max_v):
        out = pd.Series(0.0, index=series.index)
    elif max_v - min_v < 1e-12:
        out = pd.Series(0.0, index=series.index)
    else:
        out = (valid - min_v) / (max_v - min_v)
    out = out.clip(0.0, 1.0).fillna(0.0)
    if invert:
        out = 1.0 - out
    return out


def _warning_label(score: float) -> str:
    score_int = int(round(score))
    for lo, hi, label in WARNING_BANDS:
        if lo <= score_int <= hi:
            return label
    return "BRAKE_CANDIDATE" if score_int > 100 else "STABLE"


def _load_optional_json(path: Path) -> Dict[str, object] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-csv", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--drift-report", default="reports/drift_detection_report.json")
    ap.add_argument("--emergency-report", default="reports/emergency_brake_simulation.json")
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--rolling-window", type=int, default=100)
    args = ap.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = pd.read_csv(input_csv)
    cols = set(df.columns)

    ts_col = _find_col(cols, ["signal_timestamp", "timestamp", "ts", "datetime"])
    regime_col = _find_col(cols, ["matched_regime", "regime", "market_regime"])
    pnl_col = _find_col(cols, ["pnl_pct", "pnl", "profit", "result_pnl", "realized_pnl"])
    hold_col = _find_col(cols, ["holding_candles", "hold_candles"])
    score_col = _find_col(cols, ["score", "raw_prob", "prob", "probability", "score_norm"])
    outcome_col = _find_col(cols, ["outcome", "result", "label", "win_loss", "target", "y", "win"])

    if not ts_col or not regime_col or not pnl_col or not hold_col or not score_col or not outcome_col:
        raise ValueError(f"Missing required columns in input CSV. Found columns: {sorted(cols)}")

    df = df.copy()
    df["signal_timestamp"] = _to_datetime(df[ts_col])
    df = df[df["signal_timestamp"].notna()].sort_values("signal_timestamp").reset_index(drop=True)

    if df.empty:
        raise RuntimeError("No valid rows with parseable signal timestamp.")

    df["matched_regime"] = df[regime_col].fillna("UNKNOWN").astype(str)
    df["pnl_pct"] = _num(df[pnl_col])
    df["holding_candles"] = _num(df[hold_col])
    df["score"] = _num(df[score_col])
    df["is_win"] = df[outcome_col].map(_normalize_win)

    w = max(20, int(args.rolling_window))
    minp = max(10, w // 3)

    df["prev_regime"] = df["matched_regime"].shift(1)
    df["is_transition"] = (df["matched_regime"] != df["prev_regime"]).astype(float)
    df.loc[df["prev_regime"].isna(), "is_transition"] = np.nan
    df["to_risk_off"] = df["matched_regime"].str.upper().eq("RISK OFF").astype(float)

    transition_df = df[df["prev_regime"].notna()].copy()
    transition_counts = (
        transition_df.groupby(["prev_regime", "matched_regime"]).size().rename("transition_count").reset_index()
    )
    total_by_prev = transition_counts.groupby("prev_regime")["transition_count"].transform("sum")
    transition_counts["transition_probability"] = (transition_counts["transition_count"] / total_by_prev).round(6)
    transition_counts = transition_counts.sort_values(["prev_regime", "transition_count"], ascending=[True, False])

    rolling_transition_freq = df["is_transition"].rolling(w, min_periods=minp).mean()
    regime_entropy = (
        df["matched_regime"].rolling(w, min_periods=minp).apply(
            lambda s: float(-(pd.Series(s).value_counts(normalize=True) * np.log2(pd.Series(s).value_counts(normalize=True))).sum()),
            raw=False,
        )
    )
    rolling_risk_off_share = df["to_risk_off"].rolling(w, min_periods=minp).mean()
    rolling_change_share = rolling_transition_freq.copy()

    # Predictive features
    vol_std = df["pnl_pct"].rolling(w, min_periods=minp).std()
    mean_abs_move = df["pnl_pct"].abs().rolling(w, min_periods=minp).mean()
    volatility_proxy = vol_std / (mean_abs_move.replace(0, np.nan))

    holding_mean = df["holding_candles"].rolling(w, min_periods=minp).mean()
    score_median = df["score"].rolling(w, min_periods=minp).median()
    score_shift = (score_median - score_median.shift(max(5, w // 5))).abs()

    winrate_roll = df["is_win"].rolling(w, min_periods=minp).mean()
    pnl_roll = df["pnl_pct"].rolling(w, min_periods=minp).mean()

    instability_score = (
        100.0
        * (
            0.35 * _safe_norm(rolling_transition_freq)
            + 0.25 * _safe_norm(regime_entropy)
            + 0.20 * _safe_norm(rolling_risk_off_share)
            + 0.20 * _safe_norm(rolling_change_share)
        )
    ).clip(0, 100)

    volatility_cluster = _safe_norm(volatility_proxy)
    holding_compression = _safe_norm(holding_mean, invert=True)
    score_shift_norm = _safe_norm(score_shift)
    performance_decay = 0.5 * _safe_norm(winrate_roll, invert=True) + 0.5 * _safe_norm(pnl_roll, invert=True)

    early_warning_score = (
        100.0
        * (
            0.30 * (instability_score / 100.0)
            + 0.20 * volatility_cluster
            + 0.15 * holding_compression
            + 0.15 * score_shift_norm
            + 0.20 * performance_decay
        )
    ).clip(0, 100)

    df_out = df[["signal_timestamp", "matched_regime", "prev_regime", "pnl_pct", "holding_candles", "score", "is_win"]].copy()
    df_out["rolling_transition_freq"] = rolling_transition_freq
    df_out["rolling_regime_entropy"] = regime_entropy
    df_out["rolling_risk_off_share"] = rolling_risk_off_share
    df_out["rolling_change_share"] = rolling_change_share
    df_out["transition_instability_score"] = instability_score
    df_out["volatility_proxy"] = volatility_proxy
    df_out["volatility_cluster_score"] = 100.0 * volatility_cluster
    df_out["holding_compression_score"] = 100.0 * holding_compression
    df_out["score_shift_score"] = 100.0 * score_shift_norm
    df_out["performance_decay_score"] = 100.0 * performance_decay
    df_out["early_warning_score"] = early_warning_score
    df_out["warning_label"] = df_out["early_warning_score"].apply(_warning_label)

    drift_report = _load_optional_json(Path(args.drift_report))
    emergency_report = _load_optional_json(Path(args.emergency_report))
    collapse_timestamp = None
    if drift_report:
        collapse_timestamp = (
            drift_report.get("collapse", {}).get("selected_collapse_timestamp")
            or drift_report.get("collapse", {}).get("rolling_winrate_collapse_timestamp")
        )

    validation = {"collapse_timestamp": collapse_timestamp, "status": "not_available"}
    if collapse_timestamp:
        cts = pd.to_datetime(collapse_timestamp, utc=True, errors="coerce")
        if pd.notna(cts):
            before = df_out[df_out["signal_timestamp"] < cts]
            after = df_out[df_out["signal_timestamp"] >= cts]
            pre_window = before.tail(w)
            post_window = after.head(w)
            pre_mean = float(pre_window["early_warning_score"].mean()) if len(pre_window) else None
            post_mean = float(post_window["early_warning_score"].mean()) if len(post_window) else None
            pre_trend = float(pre_window["early_warning_score"].diff().tail(max(3, w // 10)).mean()) if len(pre_window) else None
            validation = {
                "collapse_timestamp": cts.isoformat(),
                "status": "available",
                "before_rows": int(len(before)),
                "after_rows": int(len(after)),
                "pre_window_mean_early_warning": pre_mean,
                "post_window_mean_early_warning": post_mean,
                "pre_window_warning_trend": pre_trend,
                "warning_increased_before_collapse": bool(pre_trend is not None and pre_trend > 0),
                "post_vs_pre_delta": (None if pre_mean is None or post_mean is None else float(post_mean - pre_mean)),
            }

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    transition_counts.to_csv(reports_dir / "regime_transition_matrix.csv", index=False)
    df_out.to_csv(reports_dir / "transition_warning_timeseries.csv", index=False)

    latest = df_out.dropna(subset=["early_warning_score"]).tail(1)
    latest_score = float(latest["early_warning_score"].iloc[0]) if len(latest) else None
    latest_label = str(latest["warning_label"].iloc[0]) if len(latest) else "STABLE"

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_csv),
        "optional_inputs": {
            "drift_report": str(args.drift_report),
            "drift_report_loaded": drift_report is not None,
            "emergency_brake_report": str(args.emergency_report),
            "emergency_brake_report_loaded": emergency_report is not None,
        },
        "governance": {
            "PAPER_ONLY": True,
            "read_only": True,
            "strategy_mutation": False,
            "broker_order_execution_changes": False,
            "auto_promotion": False,
            "recommendation_only": True,
            "no_live_execution": True,
            "no_engine_changes": True,
            "no_strategy_deployment": True,
            "evidence_only_for_manual_review": True,
        },
        "rows_total": int(len(df_out)),
        "rolling_window": w,
        "transition_matrix": {
            "rows": int(len(transition_counts)),
            "top_transitions": transition_counts.sort_values("transition_count", ascending=False).head(10).to_dict(orient="records"),
        },
        "latest_early_warning": {
            "score": latest_score,
            "label": latest_label,
            "timestamp": (latest["signal_timestamp"].iloc[0].isoformat() if len(latest) else None),
        },
        "score_component_weights": {
            "transition_instability": 0.30,
            "volatility_cluster": 0.20,
            "holding_compression": 0.15,
            "score_shift": 0.15,
            "performance_decay": 0.20,
        },
        "collapse_validation": validation,
    }

    (reports_dir / "transition_prediction_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
