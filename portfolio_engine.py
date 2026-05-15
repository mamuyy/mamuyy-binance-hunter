import json
import os
import sqlite3
from typing import Any, Dict, List

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


def _read_table(db_path: str, table: str) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(f"SELECT * FROM {table}", connection)
    except Exception:
        return pd.DataFrame()


def _load_tags(path: str) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as tag_file:
        return json.load(tag_file)


def _sector(symbol: str, tags: Dict[str, List[str]]) -> str:
    for sector, symbols in tags.items():
        if symbol in symbols:
            return sector
    return "Other"


def _drawdown(trades: pd.DataFrame) -> float:
    if trades.empty or "pnl_percent" not in trades.columns:
        return 0.0
    pnl = pd.to_numeric(trades["pnl_percent"], errors="coerce").fillna(0.0).cumsum()
    return float((pnl - pnl.cummax()).min()) if not pnl.empty else 0.0


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _save_bar(df: pd.DataFrame, x: str, y: str, path: str, title: str) -> None:
    if df.empty or x not in df.columns or y not in df.columns:
        _placeholder(path, title)
        return
    plt.figure(figsize=(9, 4))
    plt.bar(df[x], df[y])
    plt.title(title)
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _save_heatmap(matrix: pd.DataFrame, path: str, title: str) -> None:
    if matrix.empty:
        _placeholder(path, title)
        return
    plt.figure(figsize=(8, 6))
    plt.imshow(matrix.values, cmap="coolwarm", vmin=-1, vmax=1)
    plt.colorbar(label="Correlation")
    plt.xticks(range(len(matrix.columns)), matrix.columns, rotation=45, ha="right")
    plt.yticks(range(len(matrix.index)), matrix.index)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _size_signal(signal: pd.Series, drawdown: float, sector_weight: float, corr_penalty: float) -> float:
    confidence = float(signal.get("adaptive_confidence_score") or signal.get("score") or 0) / 100
    regime_confidence = float(signal.get("regime_score") or 0) / 100
    volatility = max(float(signal.get("volume_spike") or 1), 1)
    regime_name = str(signal.get("regime_name") or "").upper()
    base = 0.02 + (confidence * 0.06) + (regime_confidence * 0.02)
    if "PANIC" in regime_name or "RISK OFF" in regime_name:
        base *= 0.45
    if abs(drawdown) > 20:
        base *= 0.4
    elif abs(drawdown) > 10:
        base *= 0.65
    base /= min(volatility, 4)
    base *= max(0.35, 1 - sector_weight)
    base *= max(0.35, 1 - corr_penalty)
    return round(max(0.005, min(base, 0.12)), 4)


def build_portfolio(
    db_path: str = "mamuyy_hunter.db",
    tags_path: str = "symbol_tags.json",
    chart_dir: str = "charts",
) -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    tags = _load_tags(tags_path)
    signals = _read_table(db_path, "signals")
    trades = _read_table(db_path, "paper_trades")
    charts = {
        "portfolio_allocation": os.path.join(chart_dir, "portfolio_allocation.png"),
        "sector_exposure": os.path.join(chart_dir, "sector_exposure.png"),
        "correlation_heatmap": os.path.join(chart_dir, "correlation_heatmap_portfolio.png"),
        "risk_budget": os.path.join(chart_dir, "risk_budget_chart.png"),
    }
    if signals.empty:
        for path, title in [
            (charts["portfolio_allocation"], "Portfolio Allocation"),
            (charts["sector_exposure"], "Sector Exposure"),
            (charts["correlation_heatmap"], "Portfolio Correlation"),
            (charts["risk_budget"], "Risk Budget"),
        ]:
            _placeholder(path, title)
        return {
            "portfolio_health": "YELLOW",
            "portfolio_health_score": 50,
            "portfolio_risk_score": 0,
            "diversification_score": 0,
            "concentration_score": 0,
            "correlation_risk": 0,
            "sector_exposure": {},
            "largest_exposure": "-",
            "recommended_allocation": {},
            "charts": charts,
            "notes": ["No signal data yet. Portfolio simulation is empty."],
        }

    latest = signals.sort_values("id").drop_duplicates("symbol", keep="last").tail(20).copy()
    latest["sector"] = latest["symbol"].apply(lambda symbol: _sector(symbol, tags))
    drawdown = _drawdown(trades)
    sector_counts = latest["sector"].value_counts(normalize=True).to_dict()
    allocations = []
    for _, row in latest.iterrows():
        sector_weight = sector_counts.get(row["sector"], 0)
        corr_penalty = max(0, sector_weight - 0.25)
        allocations.append(_size_signal(row, drawdown, sector_weight, corr_penalty))
    latest["allocation"] = allocations
    total_allocation = latest["allocation"].sum()
    if total_allocation > 1:
        latest["allocation"] = latest["allocation"] / total_allocation

    sector_exposure = latest.groupby("sector")["allocation"].sum().sort_values(ascending=False)
    concentration_score = float(latest["allocation"].max() * 100) if not latest.empty else 0.0
    diversification_score = float(min(100, latest["sector"].nunique() / 7 * 100))
    correlation_risk = float(max(0, sector_exposure.max() - 0.35) * 100) if not sector_exposure.empty else 0.0
    portfolio_risk_score = min(100, concentration_score + correlation_risk + max(0, abs(drawdown) - 10))
    health_score = max(0, min(100, 100 - portfolio_risk_score + diversification_score * 0.25))
    health = "GREEN" if health_score >= 70 else "YELLOW" if health_score >= 45 else "RED"

    allocation_df = latest[["symbol", "allocation"]].sort_values("allocation", ascending=False)
    sector_df = sector_exposure.reset_index()
    sector_df.columns = ["sector", "allocation"]
    _save_bar(allocation_df, "symbol", "allocation", charts["portfolio_allocation"], "Portfolio Allocation")
    _save_bar(sector_df, "sector", "allocation", charts["sector_exposure"], "Sector Exposure")
    _save_bar(allocation_df, "symbol", "allocation", charts["risk_budget"], "Risk Budget Allocation")

    if not trades.empty and {"symbol", "pnl_percent"}.issubset(trades.columns):
        pivot = trades.pivot_table(index="timestamp", columns="symbol", values="pnl_percent", aggfunc="sum").fillna(0)
        corr = pivot.corr() if len(pivot) > 1 else pd.DataFrame()
    else:
        corr = pd.DataFrame()
    _save_heatmap(corr, charts["correlation_heatmap"], "Portfolio Correlation")

    recommended = dict(zip(allocation_df["symbol"], (allocation_df["allocation"] * 100).round(2)))
    largest = allocation_df.iloc[0]["symbol"] if not allocation_df.empty else "-"
    return {
        "portfolio_health": health,
        "portfolio_health_score": round(health_score, 2),
        "portfolio_risk_score": round(portfolio_risk_score, 2),
        "diversification_score": round(diversification_score, 2),
        "concentration_score": round(concentration_score, 2),
        "correlation_risk": round(correlation_risk, 2),
        "sector_exposure": {k: round(v * 100, 2) for k, v in sector_exposure.to_dict().items()},
        "largest_exposure": largest,
        "recommended_allocation": recommended,
        "charts": charts,
        "notes": [],
    }
