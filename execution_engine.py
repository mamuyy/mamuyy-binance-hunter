import csv
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


EXECUTION_FIELDS = [
    "timestamp",
    "symbol",
    "execution_profile",
    "expected_fill_price",
    "slippage_percent",
    "execution_cost",
    "fill_probability",
    "latency_risk",
    "liquidity_risk",
    "execution_quality_score",
    "pnl_before_execution",
    "execution_adjusted_pnl",
]


def _read_table(db_path: str, table: str) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(f"SELECT * FROM {table}", connection)
    except Exception:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _execution_profile(row: pd.Series) -> str:
    regime = str(row.get("regime_name") or "").upper()
    volume_spike = _num(row.get("volume_spike"), 1)
    pressure = _num(row.get("pressure_score"), 50)
    squeeze = _num(row.get("squeeze_probability"), 0)
    if "PANIC" in regime or squeeze >= 75:
        return "PANIC"
    if volume_spike >= 3 or pressure >= 80 or pressure <= 20:
        return "STRESSED"
    if volume_spike <= 1.4 and 40 <= pressure <= 65:
        return "IDEAL"
    return "NORMAL"


def simulate_execution(row: pd.Series) -> Dict[str, Any]:
    price = _num(row.get("price") or row.get("entry") or row.get("current_price"), 0)
    volume_spike = max(_num(row.get("volume_spike"), 1), 0.1)
    pressure = _num(row.get("pressure_score"), 50)
    taker_delta = abs(_num(row.get("taker_delta"), 0))
    squeeze = _num(row.get("squeeze_probability"), 0)
    whale = str(row.get("whale_activity") or "").upper()
    profile = _execution_profile(row)

    spread_percent = 0.015 + min(volume_spike, 5) * 0.01
    slippage_percent = 0.03 + spread_percent + abs(pressure - 50) * 0.002 + taker_delta * 0.08
    if profile == "STRESSED":
        slippage_percent *= 1.8
    elif profile == "PANIC":
        slippage_percent *= 3.0
    elif profile == "IDEAL":
        slippage_percent *= 0.65
    if "WHALE" in whale:
        slippage_percent *= 1.25

    liquidity_risk = min(100, max(0, 45 - volume_spike * 10) + squeeze * 0.25)
    latency_risk = min(100, squeeze * 0.35 + abs(pressure - 50) * 0.7)
    fill_probability = max(5, min(100, 100 - liquidity_risk * 0.55 - latency_risk * 0.25))
    fee_percent = 0.04
    execution_cost = slippage_percent + fee_percent
    pnl_before = _num(row.get("pnl_percent"), 0)
    quality = max(0, min(100, 100 - execution_cost * 8 - liquidity_risk * 0.25 - latency_risk * 0.25))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": row.get("symbol", "-"),
        "execution_profile": profile,
        "expected_fill_price": round(price * (1 + slippage_percent / 100), 8),
        "slippage_percent": round(slippage_percent, 4),
        "execution_cost": round(execution_cost, 4),
        "fill_probability": round(fill_probability, 2),
        "latency_risk": round(latency_risk, 2),
        "liquidity_risk": round(liquidity_risk, 2),
        "execution_quality_score": round(quality, 2),
        "pnl_before_execution": round(pnl_before, 4),
        "execution_adjusted_pnl": round(pnl_before - execution_cost, 4),
    }


def _write_log(rows: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=EXECUTION_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in EXECUTION_FIELDS})


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _hist(values: pd.Series, path: str, title: str, xlabel: str) -> None:
    if values.empty:
        _placeholder(path, title)
        return
    plt.figure(figsize=(8, 4))
    plt.hist(values, bins=12, edgecolor="black")
    plt.title(title)
    plt.xlabel(xlabel)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _before_after(df: pd.DataFrame, path: str) -> None:
    if df.empty:
        _placeholder(path, "PnL Before/After Execution")
        return
    x_values = range(len(df))
    plt.figure(figsize=(9, 4))
    plt.plot(x_values, df["pnl_before_execution"], label="Before")
    plt.plot(x_values, df["execution_adjusted_pnl"], label="After")
    plt.legend()
    plt.title("PnL Before/After Execution")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def run_execution_simulation(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = "execution_log.csv",
    chart_dir: str = "charts",
) -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    charts = {
        "slippage_distribution": os.path.join(chart_dir, "slippage_distribution.png"),
        "execution_quality_distribution": os.path.join(chart_dir, "execution_quality_distribution.png"),
        "pnl_before_after": os.path.join(chart_dir, "pnl_before_after_execution.png"),
    }
    signals = _read_table(db_path, "signals")
    trades = _read_table(db_path, "paper_trades")
    if signals.empty and trades.empty:
        _write_log([], output_path)
        for path, title in [
            (charts["slippage_distribution"], "Slippage Distribution"),
            (charts["execution_quality_distribution"], "Execution Quality Distribution"),
            (charts["pnl_before_after"], "PnL Before/After Execution"),
        ]:
            _placeholder(path, title)
        return {
            "execution_profile": "NORMAL",
            "expected_slippage": 0.0,
            "fill_probability": 0.0,
            "execution_quality": 0.0,
            "adjusted_pnl_impact": 0.0,
            "charts": charts,
            "notes": ["No signals or paper trades available for execution simulation."],
        }

    base = signals.sort_values("id").drop_duplicates("symbol", keep="last") if not signals.empty else trades.copy()
    if not trades.empty and "symbol" in trades.columns and "symbol" in base.columns:
        latest_trades = trades.sort_values("id").drop_duplicates("symbol", keep="last")
        base = base.merge(
            latest_trades[["symbol", "entry", "current_price", "pnl_percent"]],
            on="symbol",
            how="left",
            suffixes=("", "_trade"),
        )
        if "pnl_percent_trade" in base.columns:
            if "pnl_percent" not in base.columns:
                base["pnl_percent"] = base["pnl_percent_trade"]
            else:
                base["pnl_percent"] = base["pnl_percent"].fillna(base["pnl_percent_trade"])

    rows = [simulate_execution(row) for _, row in base.iterrows()]
    _write_log(rows, output_path)
    df = pd.DataFrame(rows)
    _hist(df["slippage_percent"], charts["slippage_distribution"], "Slippage Distribution", "Slippage (%)")
    _hist(df["execution_quality_score"], charts["execution_quality_distribution"], "Execution Quality Distribution", "Quality")
    _before_after(df, charts["pnl_before_after"])

    profile = df["execution_profile"].value_counts().idxmax() if not df.empty else "NORMAL"
    return {
        "execution_profile": profile,
        "expected_slippage": round(float(df["slippage_percent"].mean()), 4),
        "fill_probability": round(float(df["fill_probability"].mean()), 2),
        "execution_quality": round(float(df["execution_quality_score"].mean()), 2),
        "adjusted_pnl_impact": round(float((df["execution_adjusted_pnl"] - df["pnl_before_execution"]).mean()), 4),
        "charts": charts,
        "notes": [],
    }
