#!/usr/bin/env python3
"""Week 2D.1 emergency brake simulation (paper-only, read-only)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


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
    try:
        f = float(value)
        if f in (0.0, 1.0):
            return f
    except Exception:
        return float("nan")
    return float("nan")


def _find_col(columns: set[str], candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in columns:
            return c
    return None


def _max_drawdown_proxy(pnl: pd.Series) -> Optional[float]:
    s = pnl.dropna()
    if s.empty:
        return None
    equity = s.cumsum()
    drawdown = equity - equity.cummax()
    return float(drawdown.min())


def _metrics(df: pd.DataFrame) -> dict:
    pnl = df["pnl"].dropna()
    win = df["is_win"].dropna()
    return {
        "rows": int(len(df)),
        "total_pnl": (None if pnl.empty else float(pnl.sum())),
        "avg_pnl": (None if pnl.empty else float(pnl.mean())),
        "winrate": (None if win.empty else float(win.mean())),
        "max_drawdown_proxy": _max_drawdown_proxy(df["pnl"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-csv", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--drift-report", default="reports/drift_detection_report.json")
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--rolling-window", type=int, default=100)
    ap.add_argument("--cooldown-rows", type=int, default=100)
    args = ap.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = pd.read_csv(input_csv)
    cols = set(df.columns)
    ts_col = _find_col(cols, ["signal_timestamp", "timestamp", "ts", "datetime"])
    pnl_col = _find_col(cols, ["pnl", "pnl_pct", "realized_pnl", "pnl_usdt", "profit", "result_pnl"])
    hold_col = _find_col(cols, ["holding_candles", "hold_candles"])
    outcome_col = _find_col(cols, ["outcome", "result", "label", "win_loss", "target", "y", "win"])
    if not ts_col or not pnl_col or not hold_col or not outcome_col:
        raise ValueError(f"Missing required columns in {input_csv}. Found columns: {sorted(cols)}")

    df = df.copy()
    df["signal_timestamp"] = _to_datetime(df[ts_col])
    df = df[df["signal_timestamp"].notna()].sort_values("signal_timestamp").reset_index(drop=True)
    df["pnl"] = _num(df[pnl_col])
    df["holding_candles"] = _num(df[hold_col])
    df["is_win"] = df[outcome_col].map(_normalize_win)
    df = df[(df["pnl"].notna()) & (df["is_win"].notna()) & (df["holding_candles"].notna())].reset_index(drop=True)

    w = args.rolling_window
    minp = max(20, w // 2)
    df["rolling_winrate"] = df["is_win"].rolling(w, min_periods=minp).mean()
    df["rolling_avg_pnl"] = df["pnl"].rolling(w, min_periods=minp).mean()
    df["rolling_holding_candles_mean"] = df["holding_candles"].rolling(w, min_periods=minp).mean()

    cooldown = 0
    blocked = []
    brake_trigger_rows = []

    for i, row in df.iterrows():
        if cooldown > 0:
            blocked.append(True)
            cooldown -= 1
            continue

        cond = (
            (pd.notna(row["rolling_winrate"]) and row["rolling_winrate"] < 0.45)
            or (pd.notna(row["rolling_avg_pnl"]) and row["rolling_avg_pnl"] < 0.0)
            or (pd.notna(row["rolling_holding_candles_mean"]) and row["rolling_holding_candles_mean"] < 10.0)
        )
        if cond:
            brake_trigger_rows.append(i)
            blocked.append(True)
            cooldown = max(0, args.cooldown_rows - 1)
        else:
            blocked.append(False)

    df["brake_blocked"] = blocked
    kept_df = df[~df["brake_blocked"]].copy()

    collapse_ts = None
    drift_report_path = Path(args.drift_report)
    if drift_report_path.exists():
        drift_data = json.loads(drift_report_path.read_text(encoding="utf-8"))
        collapse_ts = drift_data.get("collapse", {}).get("selected_collapse_timestamp")
    collapse_ts_dt = pd.to_datetime(collapse_ts, utc=True, errors="coerce") if collapse_ts else pd.NaT

    after_before_df = df[df["signal_timestamp"] >= collapse_ts_dt] if pd.notna(collapse_ts_dt) else df.iloc[0:0]
    after_kept_df = kept_df[kept_df["signal_timestamp"] >= collapse_ts_dt] if pd.notna(collapse_ts_dt) else kept_df.iloc[0:0]

    baseline = _metrics(df)
    with_brake = _metrics(kept_df)
    after_baseline = _metrics(after_before_df)
    after_with_brake = _metrics(after_kept_df)

    degradation_reduced = None
    if after_baseline["avg_pnl"] is not None and after_with_brake["avg_pnl"] is not None:
        degradation_reduced = bool(after_with_brake["avg_pnl"] >= after_baseline["avg_pnl"])

    events = df.loc[df["brake_blocked"], ["signal_timestamp", "rolling_winrate", "rolling_avg_pnl", "rolling_holding_candles_mean"]].copy()
    events["event_type"] = "BRAKE_BLOCK"
    events["volatility_alert"] = (
        (events["rolling_winrate"] < 0.45)
        | (events["rolling_avg_pnl"] < 0.0)
        | (events["rolling_holding_candles_mean"] < 10.0)
    )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_csv),
        "drift_report": str(drift_report_path),
        "collapse_timestamp_used": (None if pd.isna(collapse_ts_dt) else collapse_ts_dt.isoformat()),
        "parameters": {"rolling_window": w, "cooldown_rows": args.cooldown_rows},
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
        "summary": {
            "total_rows": int(len(df)),
            "rows_blocked_by_brake": int(df["brake_blocked"].sum()),
            "rows_kept_after_brake": int((~df["brake_blocked"]).sum()),
            "brake_trigger_count": int(len(brake_trigger_rows)),
            "first_brake_timestamp": (None if not brake_trigger_rows else df.loc[brake_trigger_rows[0], "signal_timestamp"].isoformat()),
        },
        "before_vs_after": {
            "baseline": baseline,
            "with_brake": with_brake,
        },
        "after_collapse_before_vs_after": {
            "baseline": after_baseline,
            "with_brake": after_with_brake,
            "degradation_reduced": degradation_reduced,
        },
    }

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "emergency_brake_simulation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    events.to_csv(reports_dir / "emergency_brake_events.csv", index=False)
    print("[ok] wrote reports/emergency_brake_simulation.json")
    print("[ok] wrote reports/emergency_brake_events.csv")


if __name__ == "__main__":
    main()
