import math
import os
import sqlite3
from typing import Any, Dict, List, Tuple

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


CLOSED_STATUSES = {"WIN", "LOSS"}
REGIME_LABELS = ["TRENDING BULL", "SIDEWAYS / CHOPPY", "PANIC SELLING"]


def _empty_trades_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "timestamp",
            "symbol",
            "entry",
            "current_price",
            "pnl_percent",
            "status",
            "sl",
            "tp1",
            "tp2",
            "score",
            "regime_name",
            "regime_score",
        ]
    )


def _load_historical_outcomes(database_path: str = "mamuyy_hunter.db") -> pd.DataFrame:
    if not os.path.exists(database_path):
        return _empty_trades_frame()
    try:
        with sqlite3.connect(database_path) as connection:
            df = pd.read_sql_query(
                """
                SELECT
                    o.signal_timestamp AS timestamp,
                    o.symbol,
                    o.entry,
                    o.exit_price AS current_price,
                    o.pnl_pct AS pnl_percent,
                    CASE
                        WHEN o.win_loss = 'WIN' THEN 'WIN'
                        WHEN o.win_loss = 'LOSS' THEN 'LOSS'
                        ELSE status
                    END AS status,
                    o.sl,
                    o.tp1,
                    o.tp2,
                    o.score,
                    COALESCE(NULLIF(s.regime_name, ''), 'HISTORICAL_BACKTEST') AS regime_name,
                    COALESCE(s.regime_score, 0) AS regime_score
                FROM historical_outcomes o
                LEFT JOIN signals s
                  ON s.symbol = o.symbol
                 AND s.timestamp = o.signal_timestamp
                ORDER BY o.signal_timestamp ASC
                """,
                connection,
            )
    except (sqlite3.Error, pd.errors.DatabaseError):
        return _empty_trades_frame()
    if df.empty:
        return _empty_trades_frame()
    return df


def load_trades(path: str = "paper_trades.csv", database_path: str = "mamuyy_hunter.db") -> pd.DataFrame:
    if not os.path.exists(path):
        df = _empty_trades_frame()
    else:
        df = pd.read_csv(path)

    if df.empty:
        df = _load_historical_outcomes(database_path)
    if df.empty:
        return _empty_trades_frame()

    for column in ["entry", "current_price", "pnl_percent", "sl", "tp1", "tp2", "score"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)

    if "regime_name" not in df.columns:
        df["regime_name"] = "UNKNOWN"
    if "regime_score" not in df.columns:
        df["regime_score"] = 0

    df["status"] = df.get("status", "").fillna("OPEN")
    df["symbol"] = df.get("symbol", "").fillna("")
    df["regime_name"] = df["regime_name"].fillna("UNKNOWN").replace("", "UNKNOWN")
    return df


def _longest_streak(statuses: List[str], target: str) -> int:
    longest = 0
    current = 0
    for status in statuses:
        if status == target:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdowns = equity - running_max
    return float(drawdowns.min())


def build_equity_curve(
    trades: pd.DataFrame,
    output_path: str = "equity_curve.csv",
) -> pd.DataFrame:
    if trades.empty:
        equity = pd.DataFrame(columns=["timestamp", "symbol", "pnl_percent", "equity"])
        equity.to_csv(output_path, index=False)
        return equity

    df = trades.sort_values("timestamp").copy()
    df["pnl_percent"] = pd.to_numeric(df["pnl_percent"], errors="coerce").fillna(0.0)
    df["equity"] = df["pnl_percent"].cumsum()
    equity = df[["timestamp", "symbol", "pnl_percent", "equity"]]
    equity.to_csv(output_path, index=False)
    return equity


def _coin_performance(trades: pd.DataFrame) -> Tuple[str, str, pd.DataFrame]:
    if trades.empty:
        empty = pd.DataFrame(columns=["symbol", "trades", "avg_pnl", "total_pnl"])
        return "-", "-", empty

    grouped = (
        trades.groupby("symbol")
        .agg(
            trades=("symbol", "count"),
            avg_pnl=("pnl_percent", "mean"),
            total_pnl=("pnl_percent", "sum"),
        )
        .reset_index()
        .sort_values("total_pnl", ascending=False)
    )
    best_coin = grouped.iloc[0]["symbol"] if not grouped.empty else "-"
    worst_coin = grouped.iloc[-1]["symbol"] if not grouped.empty else "-"
    return str(best_coin), str(worst_coin), grouped


def _regime_performance(trades: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    performance = {}
    regimes = list(REGIME_LABELS)
    if not trades.empty and "regime_name" in trades.columns:
        for regime in sorted(str(value) for value in trades["regime_name"].dropna().unique()):
            if regime and regime not in regimes:
                regimes.append(regime)
    for regime in regimes:
        regime_trades = trades[trades["regime_name"] == regime]
        wins = regime_trades[regime_trades["status"] == "WIN"]
        total = len(regime_trades)
        performance[regime] = {
            "trades": total,
            "winrate": (len(wins) / total * 100) if total else 0.0,
            "avg_pnl": float(regime_trades["pnl_percent"].mean()) if total else 0.0,
            "total_pnl": float(regime_trades["pnl_percent"].sum()) if total else 0.0,
        }
    return performance


def calculate_performance_metrics(
    paper_trades_path: str = "paper_trades.csv",
    equity_curve_path: str = "equity_curve.csv",
    database_path: str = "mamuyy_hunter.db",
) -> Dict[str, Any]:
    trades = load_trades(paper_trades_path, database_path=database_path)
    equity_curve = build_equity_curve(trades, output_path=equity_curve_path)

    total_trades = len(trades)
    wins = trades[trades["status"] == "WIN"] if not trades.empty else trades
    losses = trades[trades["status"] == "LOSS"] if not trades.empty else trades
    win_count = len(wins)
    loss_count = len(losses)

    pnl = trades["pnl_percent"] if not trades.empty else pd.Series(dtype=float)
    win_pnl = wins["pnl_percent"] if not wins.empty else pd.Series(dtype=float)
    loss_pnl = losses["pnl_percent"] if not losses.empty else pd.Series(dtype=float)

    gross_profit = float(win_pnl.sum()) if not win_pnl.empty else 0.0
    gross_loss = abs(float(loss_pnl.sum())) if not loss_pnl.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss else (math.inf if gross_profit > 0 else 0.0)

    avg_win = float(win_pnl.mean()) if not win_pnl.empty else 0.0
    avg_loss = float(loss_pnl.mean()) if not loss_pnl.empty else 0.0
    risk_reward_ratio = avg_win / abs(avg_loss) if avg_loss else 0.0

    winrate = (win_count / total_trades * 100) if total_trades else 0.0
    loss_rate = (loss_count / total_trades * 100) if total_trades else 0.0
    average_pnl = float(pnl.mean()) if total_trades else 0.0
    expectancy = ((winrate / 100) * avg_win) - ((loss_rate / 100) * abs(avg_loss))
    sharpe_ratio = (
        float((pnl.mean() / pnl.std(ddof=1)) * math.sqrt(total_trades))
        if total_trades > 1 and pnl.std(ddof=1) != 0
        else 0.0
    )

    statuses = trades.sort_values("timestamp")["status"].tolist() if not trades.empty else []
    consecutive_wins = _longest_streak(statuses, "WIN")
    consecutive_losses = _longest_streak(statuses, "LOSS")
    max_drawdown = _max_drawdown(equity_curve["equity"]) if not equity_curve.empty else 0.0

    if not trades.empty:
        monthly_return = (
            trades.set_index("timestamp")["pnl_percent"]
            .resample("ME")
            .sum()
            .dropna()
            .to_dict()
        )
        monthly_return = {
            key.strftime("%Y-%m"): float(value) for key, value in monthly_return.items()
        }
    else:
        monthly_return = {}

    best_coin, worst_coin, coin_performance = _coin_performance(trades)
    regime_performance = _regime_performance(trades)

    ranked_regimes = sorted(
        regime_performance.items(),
        key=lambda item: item[1]["total_pnl"],
        reverse=True,
    )
    best_regime = ranked_regimes[0][0] if ranked_regimes and total_trades else "-"
    worst_regime = ranked_regimes[-1][0] if ranked_regimes and total_trades else "-"

    unhealthy_reasons = []
    if winrate < 40:
        unhealthy_reasons.append("winrate < 40%")
    if abs(max_drawdown) > 20:
        unhealthy_reasons.append("max drawdown > 20%")
    if profit_factor < 1:
        unhealthy_reasons.append("profit factor < 1")

    return {
        "total_trades": total_trades,
        "winrate": winrate,
        "loss_rate": loss_rate,
        "average_pnl": average_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "risk_reward_ratio": risk_reward_ratio,
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "expectancy": expectancy,
        "sharpe_ratio": sharpe_ratio,
        "monthly_return": monthly_return,
        "best_coin": best_coin,
        "worst_coin": worst_coin,
        "best_regime": best_regime,
        "worst_regime": worst_regime,
        "regime_performance": regime_performance,
        "coin_performance": coin_performance,
        "latest_signals": trades.sort_values("timestamp", ascending=False).head(20),
        "equity_curve": equity_curve,
        "unhealthy": bool(unhealthy_reasons),
        "unhealthy_reasons": unhealthy_reasons,
    }


def generate_charts(
    metrics: Dict[str, Any],
    output_dir: str = "charts",
) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    equity_path = os.path.join(output_dir, "equity_curve.png")
    distribution_path = os.path.join(output_dir, "win_loss_distribution.png")

    equity_curve = metrics["equity_curve"]
    plt.figure(figsize=(10, 4))
    if equity_curve.empty:
        plt.plot([0], [0])
    else:
        plt.plot(range(len(equity_curve)), equity_curve["equity"], linewidth=2)
    plt.title("Equity Curve")
    plt.xlabel("Trade")
    plt.ylabel("Cumulative PnL (%)")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(equity_path, dpi=140)
    plt.close()

    latest_signals = metrics["latest_signals"]
    plt.figure(figsize=(8, 4))
    if latest_signals.empty:
        plt.bar(["WIN", "LOSS", "OPEN/TP1"], [0, 0, 0])
    else:
        counts = latest_signals["status"].value_counts()
        labels = ["WIN", "LOSS", "OPEN", "TP1 HIT"]
        plt.bar(labels, [int(counts.get(label, 0)) for label in labels])
    plt.title("Win/Loss Distribution")
    plt.xlabel("Status")
    plt.ylabel("Trades")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(distribution_path, dpi=140)
    plt.close()

    return {
        "equity_curve": equity_path,
        "win_loss_distribution": distribution_path,
    }
