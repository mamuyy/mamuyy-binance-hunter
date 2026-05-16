import math
import os
import sqlite3
from typing import Any, Dict, List

import pandas as pd

from database import init_db
from regime_shadow import apply_adaptive_regime_shadow_penalty


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _profit_factor(pnls: pd.Series) -> float:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdown = equity - running_max
    return float(drawdown.min())


def _winrate(pnls: pd.Series) -> float:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    if pnl.empty:
        return 0.0
    return float((pnl > 0).mean() * 100)


def _curve_metrics(pnls: pd.Series) -> Dict[str, float]:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    return {
        "total_pnl": round(float(pnl.sum()), 6),
        "max_drawdown": round(_max_drawdown(equity), 6),
        "winrate": round(_winrate(pnl), 6),
        "profit_factor": round(_profit_factor(pnl), 6) if math.isfinite(_profit_factor(pnl)) else math.inf,
        "trade_count": int((pnl != 0).sum()),
    }


def _load_dataset(database_path: str) -> pd.DataFrame:
    init_db(database_path)
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.symbol,
            o.pnl_pct,
            o.status,
            o.win_loss,
            COALESCE(s.score, o.score) AS score,
            s.calculated_score,
            s.shadow_score,
            s.penalty_applied,
            COALESCE(s.regime_name, 'UNKNOWN') AS regime_name
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        ORDER BY o.signal_timestamp ASC, o.id ASC
    """
    try:
        with sqlite3.connect(database_path) as connection:
            return pd.read_sql_query(query, connection)
    except Exception:
        return pd.DataFrame()


def _ensure_shadow_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    enriched = df.copy()
    for column in ["score", "calculated_score", "shadow_score", "pnl_pct"]:
        if column in enriched.columns:
            enriched[column] = pd.to_numeric(enriched[column], errors="coerce")
    calculated_values: List[float] = []
    shadow_values: List[float] = []
    penalty_values: List[int] = []
    for _, row in enriched.iterrows():
        calculated_score = row.get("calculated_score")
        if pd.isna(calculated_score):
            calculated_score = row.get("score", 0)
        shadow_score = row.get("shadow_score")
        penalty_applied = row.get("penalty_applied")
        if pd.isna(shadow_score):
            shadow = apply_adaptive_regime_shadow_penalty(
                {
                    "score": _safe_number(calculated_score),
                    "regime_name": row.get("regime_name", "UNKNOWN"),
                }
            )
            shadow_score = shadow["shadow_score"]
            penalty_applied = shadow["penalty_applied"]
        calculated_values.append(round(_safe_number(calculated_score), 6))
        shadow_values.append(round(_safe_number(shadow_score), 6))
        penalty_values.append(int(_safe_number(penalty_applied)))
    enriched["calculated_score"] = calculated_values
    enriched["shadow_score"] = shadow_values
    enriched["penalty_applied"] = penalty_values
    enriched["pnl_pct"] = pd.to_numeric(enriched["pnl_pct"], errors="coerce").fillna(0.0)
    return enriched


def _comparison_rows(
    original: Dict[str, float],
    shadow: Dict[str, float],
    avoided_losses: int,
    skipped_winners: int,
) -> List[Dict[str, Any]]:
    original_dd = abs(float(original.get("max_drawdown", 0.0)))
    shadow_dd = abs(float(shadow.get("max_drawdown", 0.0)))
    original_pnl = float(original.get("total_pnl", 0.0))
    shadow_pnl = float(shadow.get("total_pnl", 0.0))
    dd_reduction = ((original_dd - shadow_dd) / original_dd * 100) if original_dd else 0.0
    pnl_difference = ((shadow_pnl - original_pnl) / abs(original_pnl) * 100) if original_pnl else 0.0
    trade_reduction = (
        (original.get("trade_count", 0) - shadow.get("trade_count", 0))
        / max(original.get("trade_count", 0), 1)
        * 100
    )
    rows: List[Dict[str, Any]] = []
    for metric in ["total_pnl", "max_drawdown", "winrate", "profit_factor", "trade_count"]:
        rows.append(
            {
                "section": "summary",
                "metric": metric,
                "original": original.get(metric),
                "shadow": shadow.get(metric),
                "delta": _safe_number(shadow.get(metric)) - _safe_number(original.get(metric)),
                "regime_name": "",
                "value": "",
            }
        )
    for metric, value in [
        ("avoided_losses", avoided_losses),
        ("skipped_winners", skipped_winners),
        ("drawdown_reduction_pct", round(dd_reduction, 6)),
        ("pnl_difference_pct", round(pnl_difference, 6)),
        ("trade_reduction_pct", round(trade_reduction, 6)),
    ]:
        rows.append(
            {
                "section": "derived",
                "metric": metric,
                "original": "",
                "shadow": "",
                "delta": "",
                "regime_name": "",
                "value": value,
            }
        )
    return rows


def run_shadow_equity_analysis(
    database_path: str = "mamuyy_hunter.db",
    threshold: float = 75.0,
    equity_output_path: str = "shadow_equity_curve.csv",
    comparison_output_path: str = "shadow_comparison.csv",
) -> Dict[str, Any]:
    df = _ensure_shadow_scores(_load_dataset(database_path))
    if df.empty:
        pd.DataFrame().to_csv(equity_output_path, index=False)
        pd.DataFrame(_comparison_rows(_curve_metrics(pd.Series(dtype=float)), _curve_metrics(pd.Series(dtype=float)), 0, 0)).to_csv(
            comparison_output_path,
            index=False,
        )
        return {
            "rows": 0,
            "threshold": threshold,
            "original": _curve_metrics(pd.Series(dtype=float)),
            "shadow": _curve_metrics(pd.Series(dtype=float)),
            "avoided_losses": 0,
            "skipped_winners": 0,
            "drawdown_reduction_pct": 0.0,
            "pnl_difference_pct": 0.0,
            "trade_reduction_pct": 0.0,
            "equity_output_path": equity_output_path,
            "comparison_output_path": comparison_output_path,
        }

    df["included_shadow"] = df["shadow_score"] >= threshold
    df["pnl_original"] = df["pnl_pct"]
    df["pnl_shadow"] = df["pnl_pct"].where(df["included_shadow"], 0.0)
    df["equity_original"] = df["pnl_original"].cumsum()
    df["equity_shadow"] = df["pnl_shadow"].cumsum()
    df["trade_index"] = range(1, len(df) + 1)
    skipped = df[~df["included_shadow"]]
    avoided_losses = int((skipped["pnl_pct"] < 0).sum())
    skipped_winners = int((skipped["pnl_pct"] > 0).sum())

    original = _curve_metrics(df["pnl_original"])
    shadow = _curve_metrics(df["pnl_shadow"])
    comparison = _comparison_rows(original, shadow, avoided_losses, skipped_winners)

    if "regime_name" in df.columns:
        regime_rows = []
        for regime_name, group in df.groupby("regime_name", dropna=False):
            group_skipped = group[~group["included_shadow"]]
            regime_rows.extend(
                [
                    {
                        "section": "regime",
                        "metric": "skipped_trades",
                        "original": "",
                        "shadow": "",
                        "delta": "",
                        "regime_name": regime_name or "UNKNOWN",
                        "value": int(len(group_skipped)),
                    },
                    {
                        "section": "regime",
                        "metric": "skipped_pnl",
                        "original": "",
                        "shadow": "",
                        "delta": "",
                        "regime_name": regime_name or "UNKNOWN",
                        "value": round(float(group_skipped["pnl_pct"].sum()), 6),
                    },
                    {
                        "section": "regime",
                        "metric": "avoided_losses",
                        "original": "",
                        "shadow": "",
                        "delta": "",
                        "regime_name": regime_name or "UNKNOWN",
                        "value": int((group_skipped["pnl_pct"] < 0).sum()),
                    },
                ]
            )
        comparison.extend(regime_rows)

    curve_columns = [
        "trade_index",
        "timestamp",
        "symbol",
        "regime_name",
        "calculated_score",
        "shadow_score",
        "included_shadow",
        "pnl_original",
        "pnl_shadow",
        "equity_original",
        "equity_shadow",
    ]
    df[[column for column in curve_columns if column in df.columns]].to_csv(equity_output_path, index=False)
    pd.DataFrame(comparison).to_csv(comparison_output_path, index=False)

    original_dd = abs(float(original.get("max_drawdown", 0.0)))
    shadow_dd = abs(float(shadow.get("max_drawdown", 0.0)))
    original_pnl = float(original.get("total_pnl", 0.0))
    shadow_pnl = float(shadow.get("total_pnl", 0.0))
    drawdown_reduction = ((original_dd - shadow_dd) / original_dd * 100) if original_dd else 0.0
    pnl_difference = ((shadow_pnl - original_pnl) / abs(original_pnl) * 100) if original_pnl else 0.0
    trade_reduction = (len(skipped) / max(len(df), 1) * 100)

    return {
        "rows": int(len(df)),
        "threshold": threshold,
        "original": original,
        "shadow": shadow,
        "avoided_losses": avoided_losses,
        "skipped_winners": skipped_winners,
        "drawdown_reduction_pct": round(drawdown_reduction, 6),
        "pnl_difference_pct": round(pnl_difference, 6),
        "trade_reduction_pct": round(trade_reduction, 6),
        "equity_output_path": equity_output_path,
        "comparison_output_path": comparison_output_path,
    }


def format_shadow_analysis_summary(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "SHADOW PENALTY ANALYSIS",
            f"Rows: {result.get('rows', 0)}",
            f"Threshold: {result.get('threshold', 0)}",
            f"Original: {result.get('original', {})}",
            f"Shadow: {result.get('shadow', {})}",
            f"Avoided Losses: {result.get('avoided_losses', 0)}",
            f"Skipped Winners: {result.get('skipped_winners', 0)}",
            f"DD Reduction: {result.get('drawdown_reduction_pct', 0)}%",
            f"PnL Difference: {result.get('pnl_difference_pct', 0)}%",
            f"Trade Reduction: {result.get('trade_reduction_pct', 0)}%",
            f"Equity CSV: {result.get('equity_output_path')}",
            f"Comparison CSV: {result.get('comparison_output_path')}",
        ]
    )
