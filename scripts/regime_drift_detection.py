#!/usr/bin/env python3
"""Week 2D market drift & regime transition diagnosis (paper-only, read-only).

Outputs:
- reports/drift_detection_report.json
- reports/regime_transition_stats.csv
- reports/drift_rolling_metrics.csv (optional; enabled by default)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd


def _to_datetime(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series, utc=True, errors="coerce")
    return ts


def _normalize_win(value: object) -> float:
    if value is None:
        return float("nan")
    s = str(value).strip().upper()
    if s in {"WIN", "TRUE", "1"}:
        return 1.0
    if s in {"LOSS", "FALSE", "0"}:
        return 0.0
    try:
        f = float(value)
        if f in (0.0, 1.0):
            return f
    except Exception:
        return float("nan")
    return float("nan")


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _find_col(columns, candidates):
    for c in candidates:
        if c in columns:
            return c
    return None


def _collapse_idx(series: pd.Series, window: int) -> int:
    delta = series.diff(window)
    if delta.notna().any():
        return int(delta.idxmin())
    return int(series.first_valid_index())


def _segment_metrics(df: pd.DataFrame) -> Dict[str, object]:
    return {
        "rows": int(len(df)),
        "winrate": (None if df["is_win"].dropna().empty else float(df["is_win"].mean())),
        "avg_pnl": (None if df["pnl"].dropna().empty else float(df["pnl"].mean())),
        "total_pnl": (None if df["pnl"].dropna().empty else float(df["pnl"].sum())),
        "holding_candles_mean": (None if df["holding_candles"].dropna().empty else float(df["holding_candles"].mean())),
        "holding_candles_median": (None if df["holding_candles"].dropna().empty else float(df["holding_candles"].median())),
        "score_mean": (None if df["score"].dropna().empty else float(df["score"].mean())),
        "score_median": (None if df["score"].dropna().empty else float(df["score"].median())),
        "regime_distribution": (
            df["matched_regime"].fillna("UNKNOWN").value_counts(normalize=True).round(6).to_dict()
            if len(df) > 0
            else {}
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-csv", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--rolling-window", type=int, default=100)
    ap.add_argument("--min-transition-samples", type=int, default=5)
    ap.add_argument("--disable-rolling-csv", action="store_true")
    args = ap.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = pd.read_csv(input_csv)
    cols = set(df.columns)

    ts_col = _find_col(cols, ["signal_timestamp", "timestamp", "ts", "datetime"])
    regime_col = _find_col(cols, ["matched_regime", "regime", "market_regime"])
    pnl_col = _find_col(cols, ["pnl", "realized_pnl", "pnl_usdt", "profit", "result_pnl"])
    hold_col = _find_col(cols, ["holding_candles", "hold_candles"])
    score_col = _find_col(cols, ["score", "raw_prob", "prob", "probability", "score_norm"])
    outcome_col = _find_col(cols, ["outcome", "result", "label", "target", "y", "win"])

    if not ts_col or not regime_col or not pnl_col or not hold_col or not score_col or not outcome_col:
        raise ValueError(
            "Missing required columns. Need timestamp, matched_regime, pnl, holding_candles, score, outcome/label. "
            f"Found: {sorted(cols)}"
        )

    df = df.copy()
    df["signal_timestamp"] = _to_datetime(df[ts_col])
    df = df[df["signal_timestamp"].notna()].sort_values("signal_timestamp").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("No valid timestamp rows after parsing.")

    df["matched_regime"] = df[regime_col].fillna("UNKNOWN").astype(str)
    df["pnl"] = _num(df[pnl_col])
    df["holding_candles"] = _num(df[hold_col])
    df["score"] = _num(df[score_col])
    df["is_win"] = df[outcome_col].map(_normalize_win)

    flat_mask = df["matched_regime"].str.upper().eq("FLAT")
    excluded_flat_count = int(flat_mask.sum())
    metric_df = df[~flat_mask].copy()
    metric_df = metric_df[(metric_df["is_win"].notna()) & (metric_df["pnl"].notna())].reset_index(drop=True)
    if len(metric_df) < max(20, args.rolling_window):
        raise RuntimeError("Insufficient non-FLAT valid rows for drift diagnosis.")

    w = args.rolling_window
    metric_df["rolling_winrate_overall"] = metric_df["is_win"].rolling(w, min_periods=max(10, w // 3)).mean()
    metric_df["rolling_avg_pnl_overall"] = metric_df["pnl"].rolling(w, min_periods=max(10, w // 3)).mean()

    metric_df["rolling_winrate_regime"] = (
        metric_df.groupby("matched_regime")["is_win"].transform(lambda s: s.rolling(w, min_periods=max(5, w // 4)).mean())
    )
    metric_df["rolling_avg_pnl_regime"] = (
        metric_df.groupby("matched_regime")["pnl"].transform(lambda s: s.rolling(w, min_periods=max(5, w // 4)).mean())
    )

    wr_idx = _collapse_idx(metric_df["rolling_winrate_overall"], max(5, w // 5))
    pnl_idx = _collapse_idx(metric_df["rolling_avg_pnl_overall"], max(5, w // 5))
    collapse_idx = max(wr_idx, pnl_idx)
    collapse_ts = metric_df.loc[collapse_idx, "signal_timestamp"]

    lookback = metric_df.iloc[max(0, collapse_idx - w + 1): collapse_idx + 1]
    dominant_regime = lookback["matched_regime"].value_counts().idxmax() if not lookback.empty else "UNKNOWN"

    before_df = metric_df.iloc[:collapse_idx]
    after_df = metric_df.iloc[collapse_idx:]

    transitions = metric_df[["signal_timestamp", "matched_regime", "is_win", "pnl"]].copy()
    transitions["prev_regime"] = transitions["matched_regime"].shift(1)
    transitions = transitions[transitions["prev_regime"].notna()].copy()
    transitions["transition"] = transitions["prev_regime"] + " -> " + transitions["matched_regime"]

    trans_stats = (
        transitions.groupby(["prev_regime", "matched_regime", "transition"], as_index=False)
        .agg(
            transition_count=("transition", "size"),
            post_transition_winrate=("is_win", "mean"),
            post_transition_avg_pnl=("pnl", "mean"),
            post_transition_total_pnl=("pnl", "sum"),
        )
        .sort_values("transition_count", ascending=False)
    )

    risk_off_trans = transitions[transitions["matched_regime"].str.upper().eq("RISK OFF")]
    non_risk_off_trans = transitions[~transitions["matched_regime"].str.upper().eq("RISK OFF")]

    warning_features = {
        "risk_off_share_before": float(before_df["matched_regime"].str.upper().eq("RISK OFF").mean()) if len(before_df) else None,
        "risk_off_share_after": float(after_df["matched_regime"].str.upper().eq("RISK OFF").mean()) if len(after_df) else None,
        "rolling_winrate_last": float(metric_df["rolling_winrate_overall"].dropna().iloc[-1]),
        "rolling_avg_pnl_last": float(metric_df["rolling_avg_pnl_overall"].dropna().iloc[-1]),
        "holding_candles_mean_before": float(before_df["holding_candles"].mean()) if len(before_df) else None,
        "holding_candles_mean_after": float(after_df["holding_candles"].mean()) if len(after_df) else None,
        "score_median_before": float(before_df["score"].median()) if len(before_df) else None,
        "score_median_after": float(after_df["score"].median()) if len(after_df) else None,
        "transition_freq_per_100_before": float(len(transitions[transitions.index < collapse_idx]) / max(1, len(before_df)) * 100.0) if len(before_df) else None,
        "transition_freq_per_100_after": float(len(transitions[transitions.index >= collapse_idx]) / max(1, len(after_df)) * 100.0) if len(after_df) else None,
    }

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_csv),
        "governance": {
            "PAPER_ONLY": True,
            "read_only": True,
            "strategy_mutation": False,
            "broker_order_execution_changes": False,
            "auto_promotion": False,
            "recommendation_only": True,
            "live_execution": False,
            "engine_changes": False,
            "strategy_deployment": False,
        },
        "excluded_flat_count": excluded_flat_count,
        "rows_total": int(len(df)),
        "rows_metric": int(len(metric_df)),
        "rolling_window": w,
        "collapse": {
            "rolling_winrate_collapse_timestamp": metric_df.loc[wr_idx, "signal_timestamp"].isoformat(),
            "rolling_avg_pnl_collapse_timestamp": metric_df.loc[pnl_idx, "signal_timestamp"].isoformat(),
            "selected_collapse_timestamp": collapse_ts.isoformat(),
            "dominant_regime_at_collapse": dominant_regime,
        },
        "before_vs_after": {
            "before": _segment_metrics(before_df),
            "after": _segment_metrics(after_df),
        },
        "risk_off_transition_correlation": {
            "samples_to_risk_off": int(len(risk_off_trans)),
            "samples_to_non_risk_off": int(len(non_risk_off_trans)),
            "winrate_to_risk_off": (float(risk_off_trans["is_win"].mean()) if len(risk_off_trans) else None),
            "winrate_to_non_risk_off": (float(non_risk_off_trans["is_win"].mean()) if len(non_risk_off_trans) else None),
            "avg_pnl_to_risk_off": (float(risk_off_trans["pnl"].mean()) if len(risk_off_trans) else None),
            "avg_pnl_to_non_risk_off": (float(non_risk_off_trans["pnl"].mean()) if len(non_risk_off_trans) else None),
            "degradation_correlated": bool(
                len(risk_off_trans) >= args.min_transition_samples
                and len(non_risk_off_trans) >= args.min_transition_samples
                and risk_off_trans["pnl"].mean() < non_risk_off_trans["pnl"].mean()
                and risk_off_trans["is_win"].mean() < non_risk_off_trans["is_win"].mean()
            ),
        },
        "early_warning_candidates": warning_features,
    }

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "drift_detection_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    trans_stats.to_csv(reports_dir / "regime_transition_stats.csv", index=False)
    if not args.disable_rolling_csv:
        metric_df.to_csv(reports_dir / "drift_rolling_metrics.csv", index=False)


if __name__ == "__main__":
    main()
