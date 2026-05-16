import csv
import itertools
import math
import os
import sqlite3
from typing import Any, Callable, Dict, List, Tuple

import pandas as pd


MIN_SCORE_VALUES = [70, 75, 80, 85, 90]
FUNDING_RANGES = [
    ("any", None),
    ("abs_lt_1", lambda df: df["funding_zscore"].abs() < 1),
    ("abs_lt_2", lambda df: df["funding_zscore"].abs() < 2),
    ("negative", lambda df: df["funding_zscore"] < 0),
    ("positive", lambda df: df["funding_zscore"] > 0),
]
TAKER_THRESHOLDS = [
    ("any", None),
    ("gt_0", lambda df: df["taker_delta"] > 0),
    ("gt_0_10", lambda df: df["taker_delta"] > 0.10),
    ("gt_0_20", lambda df: df["taker_delta"] > 0.20),
]
VOLUME_SPIKES = [
    ("any", None),
    ("gte_1_5", lambda df: df["volume_spike"] >= 1.5),
    ("gte_2", lambda df: df["volume_spike"] >= 2),
    ("gte_3", lambda df: df["volume_spike"] >= 3),
]
BOOLEAN_FILTERS = [
    ("any", None),
    ("true", True),
    ("false", False),
]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_dataset(database_path: str) -> pd.DataFrame:
    if not os.path.exists(database_path):
        return pd.DataFrame()
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.symbol,
            o.pnl_pct,
            o.win_loss,
            o.status,
            o.score AS outcome_score,
            s.score,
            s.volume_spike,
            s.breakout,
            s.liquidity_sweep,
            COALESCE(NULLIF(s.regime_name, ''), 'UNKNOWN') AS regime_name,
            f.flow_state,
            f.whale_activity,
            f.squeeze_risk,
            f.funding_zscore,
            f.oi_expansion_rate,
            f.taker_delta,
            f.pressure_score,
            f.squeeze_probability
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        LEFT JOIN flow_logs f
          ON f.symbol = o.symbol
         AND f.timestamp = o.signal_timestamp
        ORDER BY o.signal_timestamp ASC
    """
    try:
        with sqlite3.connect(database_path) as connection:
            df = pd.read_sql_query(query, connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()
    if df.empty:
        return df

    for column in [
        "pnl_pct",
        "outcome_score",
        "score",
        "volume_spike",
        "funding_zscore",
        "oi_expansion_rate",
        "taker_delta",
        "pressure_score",
        "squeeze_probability",
    ]:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    df["score"] = df["score"].where(df["score"] > 0, df["outcome_score"])
    for column in ["breakout", "liquidity_sweep"]:
        df[column] = df[column].astype(str).str.lower().isin(["true", "1", "yes"])
    for column in ["regime_name", "flow_state", "whale_activity", "squeeze_risk"]:
        df[column] = df[column].fillna("UNKNOWN").replace("", "UNKNOWN").astype(str)
    df["win_loss"] = df["win_loss"].fillna("").astype(str).str.upper()
    return df


def _max_drawdown(pnl: pd.Series) -> float:
    if pnl.empty:
        return 0.0
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    return float(drawdown.min())


def _profit_factor(pnl: pd.Series) -> float:
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _metrics(df: pd.DataFrame) -> Dict[str, Any]:
    pnl = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    trade_count = int(len(df))
    winrate = (wins / trade_count * 100) if trade_count else 0.0
    avg_pnl = float(pnl.mean()) if trade_count else 0.0
    avg_win = float(pnl[pnl > 0].mean()) if wins else 0.0
    avg_loss = float(pnl[pnl < 0].mean()) if losses else 0.0
    loss_rate = losses / trade_count if trade_count else 0.0
    expectancy = ((winrate / 100) * avg_win) - (loss_rate * abs(avg_loss))
    return {
        "trade_count": trade_count,
        "winrate": winrate,
        "avg_pnl": avg_pnl,
        "profit_factor": _profit_factor(pnl),
        "max_drawdown": _max_drawdown(pnl),
        "expectancy": expectancy,
        "total_pnl": float(pnl.sum()) if trade_count else 0.0,
    }


def _option_values(df: pd.DataFrame, column: str, minimum_count: int) -> List[Tuple[str, Callable[[pd.DataFrame], pd.Series] | None]]:
    values = [("any", None)]
    if column not in df.columns:
        return values
    counts = df[column].value_counts()
    for value, count in counts.items():
        if count >= minimum_count and value not in {"", "UNKNOWN"}:
            values.append((str(value), lambda data, v=value: data[column] == v))
    return values[:8]


def _apply_filter(df: pd.DataFrame, predicate: Callable[[pd.DataFrame], pd.Series] | None) -> pd.DataFrame:
    if predicate is None:
        return df
    try:
        return df[predicate(df)]
    except Exception:
        return df.iloc[0:0]


def _setup_name(parts: Dict[str, str]) -> str:
    active = [f"{key}={value}" for key, value in parts.items() if value != "any"]
    return " | ".join(active) if active else "all_historical_outcomes"


def _evaluate_setup(df: pd.DataFrame, parts: Dict[str, str], predicates: List[Callable[[pd.DataFrame], pd.Series] | None], min_trades: int) -> Dict[str, Any] | None:
    filtered = df
    for predicate in predicates:
        filtered = _apply_filter(filtered, predicate)
        if len(filtered) < min_trades:
            return None
    row = {"setup": _setup_name(parts), **parts, **_metrics(filtered)}
    return row


def _bool_predicate(column: str, value: bool | None) -> Callable[[pd.DataFrame], pd.Series] | None:
    if value is None:
        return None
    return lambda df: df[column] == value


def run_filter_optimizer(
    database_path: str = "mamuyy_hunter.db",
    output_path: str = "optimizer_results.csv",
    min_trades: int = 30,
) -> Dict[str, Any]:
    dataset = _load_dataset(database_path)
    if dataset.empty:
        pd.DataFrame().to_csv(output_path, index=False)
        result = {
            "rows": 0,
            "output_path": output_path,
            "top_setups": [],
            "recommended_conservative_setup": {},
            "notes": ["No historical_outcomes rows available."],
        }
        print(f"Filter optimizer selesai: {result}")
        return result

    rows: List[Dict[str, Any]] = []
    flow_options = _option_values(dataset, "flow_state", min_trades)
    whale_options = _option_values(dataset, "whale_activity", min_trades)
    regime_options = _option_values(dataset, "regime_name", min_trades)

    for min_score in MIN_SCORE_VALUES:
        for flow_name, flow_pred in flow_options:
            for whale_name, whale_pred in whale_options:
                for squeeze_low_only in [False, True]:
                    for funding_name, funding_pred in FUNDING_RANGES:
                        for oi_positive_only in [False, True]:
                            for taker_name, taker_pred in TAKER_THRESHOLDS:
                                for regime_name, regime_pred in regime_options:
                                    for volume_name, volume_pred in VOLUME_SPIKES:
                                        for breakout_name, breakout_value in BOOLEAN_FILTERS:
                                            for sweep_name, sweep_value in BOOLEAN_FILTERS:
                                                parts = {
                                                    "min_score": str(min_score),
                                                    "flow_state": flow_name,
                                                    "whale_activity": whale_name,
                                                    "squeeze_risk": "LOW" if squeeze_low_only else "any",
                                                    "funding_zscore": funding_name,
                                                    "oi_expansion": "positive" if oi_positive_only else "any",
                                                    "taker_delta": taker_name,
                                                    "regime": regime_name,
                                                    "volume_spike": volume_name,
                                                    "breakout": breakout_name,
                                                    "liquidity_sweep": sweep_name,
                                                }
                                                predicates = [
                                                    lambda df, score=min_score: df["score"] >= score,
                                                    flow_pred,
                                                    whale_pred,
                                                    (lambda df: df["squeeze_risk"] == "LOW") if squeeze_low_only else None,
                                                    funding_pred,
                                                    (lambda df: df["oi_expansion_rate"] > 0) if oi_positive_only else None,
                                                    taker_pred,
                                                    regime_pred,
                                                    volume_pred,
                                                    _bool_predicate("breakout", breakout_value),
                                                    _bool_predicate("liquidity_sweep", sweep_value),
                                                ]
                                                row = _evaluate_setup(dataset, parts, predicates, min_trades)
                                                if row:
                                                    rows.append(row)

    results = pd.DataFrame(rows)
    if results.empty:
        results.to_csv(output_path, index=False)
        top = []
        conservative = {}
    else:
        results = results.sort_values(
            ["profit_factor", "expectancy", "trade_count"],
            ascending=[False, False, False],
        ).reset_index(drop=True)
        results.to_csv(output_path, index=False, quoting=csv.QUOTE_MINIMAL)
        top = results.head(20).replace({math.inf: "inf"}).to_dict(orient="records")
        conservative_pool = results[
            (results["trade_count"] >= max(min_trades, 100))
            & (results["profit_factor"] >= 1.15)
            & (results["max_drawdown"] > -20)
        ]
        conservative = (
            conservative_pool.sort_values(
                ["max_drawdown", "profit_factor", "expectancy"],
                ascending=[False, False, False],
            )
            .head(1)
            .replace({math.inf: "inf"})
            .to_dict(orient="records")
        )
        conservative = conservative[0] if conservative else top[0]

    result = {
        "rows": int(len(dataset)),
        "setups_tested": int(len(rows)),
        "output_path": output_path,
        "top_setups": top,
        "recommended_conservative_setup": conservative,
        "notes": [],
    }
    print(f"Filter optimizer selesai. Rows={result['rows']} setups={result['setups_tested']}")
    print(f"Top 20 saved to {output_path}")
    if conservative:
        print(f"Recommended conservative setup: {conservative.get('setup')}")
    return result
