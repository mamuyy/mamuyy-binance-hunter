import math
import os
import sqlite3
from typing import Any, Dict, List

import pandas as pd

from database import init_db


def _read_table(db_path: str, table: str, limit: int = 1000) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _read_historical_outcomes(db_path: str, limit: int = 5000) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.symbol,
            o.pnl_pct,
            COALESCE(s.regime_name, 'UNKNOWN') AS regime_name
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        ORDER BY o.id DESC
        LIMIT ?
    """
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(query, connection, params=(limit,))
    except Exception:
        return pd.DataFrame()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _market_type(symbol: Any) -> str:
    text = str(symbol or "").upper()
    if text.endswith("USDT"):
        return "USDT Futures"
    if text.endswith("BUSD") or text.endswith("USDC"):
        return "Stablecoin Futures"
    if text:
        return "Other Futures"
    return "UNKNOWN"


def _normalize_exposure(frame: pd.DataFrame, value_column: str) -> pd.DataFrame:
    if frame.empty or value_column not in frame.columns:
        return pd.DataFrame(columns=["symbol", "regime_name", "market_type", "exposure", "exposure_pct"])
    df = frame.copy()
    df["symbol"] = df["symbol"].fillna("UNKNOWN").astype(str) if "symbol" in df.columns else "UNKNOWN"
    df["regime_name"] = df["regime_name"].fillna("UNKNOWN").astype(str) if "regime_name" in df.columns else "UNKNOWN"
    df["market_type"] = df["symbol"].apply(_market_type)
    df["exposure"] = pd.to_numeric(df[value_column], errors="coerce").fillna(0.0).abs()
    df = df[df["exposure"] > 0]
    total = float(df["exposure"].sum())
    if total <= 0:
        return pd.DataFrame(columns=["symbol", "regime_name", "market_type", "exposure", "exposure_pct"])
    df["exposure_pct"] = df["exposure"] / total * 100
    return df


def _build_exposure_frame(signals: pd.DataFrame, shadow_trades: pd.DataFrame, outcomes: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if not shadow_trades.empty and {"symbol", "exposure"}.issubset(shadow_trades.columns):
        latest_shadow = shadow_trades.sort_values("id").drop_duplicates("symbol", keep="last").copy()
        if "lifecycle_status" in latest_shadow.columns:
            active = latest_shadow[
                ~latest_shadow["lifecycle_status"]
                .fillna("")
                .astype(str)
                .str.upper()
                .isin({"TRADE CLOSED", "CLOSED", "WIN", "LOSS"})
            ]
        else:
            active = latest_shadow
        exposure = _normalize_exposure(active, "exposure")
        if not exposure.empty:
            return exposure, "shadow_trades"

    if not signals.empty and {"symbol", "score"}.issubset(signals.columns):
        latest_signals = signals.sort_values("id").drop_duplicates("symbol", keep="last").tail(50).copy()
        latest_signals["exposure_proxy"] = pd.to_numeric(
            latest_signals.get("adaptive_confidence_score", latest_signals["score"]),
            errors="coerce",
        ).fillna(pd.to_numeric(latest_signals["score"], errors="coerce")).clip(lower=0)
        exposure = _normalize_exposure(latest_signals, "exposure_proxy")
        if not exposure.empty:
            return exposure, "signals_score_proxy"

    if not outcomes.empty and {"symbol", "pnl_pct"}.issubset(outcomes.columns):
        fallback = outcomes.copy()
        fallback["historical_activity"] = 1.0
        exposure = _normalize_exposure(fallback, "historical_activity")
        if not exposure.empty:
            return exposure, "historical_outcome_activity"

    return pd.DataFrame(columns=["symbol", "regime_name", "market_type", "exposure", "exposure_pct"]), "none"


def _group_exposure(exposure: pd.DataFrame, column: str, label: str) -> List[Dict[str, Any]]:
    if exposure.empty or column not in exposure.columns:
        return []
    grouped = (
        exposure.groupby(column, dropna=False)
        .agg(exposure=("exposure", "sum"), exposure_pct=("exposure_pct", "sum"), rows=("symbol", "count"))
        .reset_index()
        .rename(columns={column: label})
        .sort_values("exposure_pct", ascending=False)
    )
    grouped["exposure"] = grouped["exposure"].round(6)
    grouped["exposure_pct"] = grouped["exposure_pct"].round(2)
    return grouped.to_dict("records")


def _top_correlations(outcomes: pd.DataFrame) -> List[Dict[str, Any]]:
    if outcomes.empty or not {"timestamp", "symbol", "pnl_pct"}.issubset(outcomes.columns):
        return []
    df = outcomes.copy()
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    pivot = df.pivot_table(index="timestamp", columns="symbol", values="pnl_pct", aggfunc="sum").fillna(0.0)
    if pivot.shape[0] < 5 or pivot.shape[1] < 2:
        return []
    corr = pivot.corr().replace([math.inf, -math.inf], pd.NA)
    rows: List[Dict[str, Any]] = []
    symbols = list(corr.columns)
    for idx, left in enumerate(symbols):
        for right in symbols[idx + 1 :]:
            value = corr.loc[left, right]
            if pd.isna(value):
                continue
            rows.append({"symbol_a": left, "symbol_b": right, "correlation": round(float(value), 4)})
    return sorted(rows, key=lambda row: abs(row["correlation"]), reverse=True)[:10]


def _risk_event_pressure(risk_events: pd.DataFrame) -> float:
    if risk_events.empty:
        return 0.0
    latest = risk_events.sort_values("id").tail(10).copy()
    statuses = latest.get("status", pd.Series(dtype=str)).fillna("").astype(str).str.upper()
    if (statuses == "HALT").any():
        return 25.0
    if (statuses == "WATCH").any():
        return 12.5
    risk_score = pd.to_numeric(latest.get("risk_score", pd.Series(dtype=float)), errors="coerce").dropna()
    return min(float(risk_score.mean()) * 0.25, 20.0) if not risk_score.empty else 0.0


def observe_portfolio(db_path: str = "mamuyy_hunter.db") -> Dict[str, Any]:
    signals = _read_table(db_path, "signals", limit=1000)
    shadow_trades = _read_table(db_path, "shadow_trades", limit=1000)
    risk_events = _read_table(db_path, "risk_events", limit=200)
    outcomes = _read_historical_outcomes(db_path, limit=5000)

    exposure, source = _build_exposure_frame(signals, shadow_trades, outcomes)
    symbol_exposure = _group_exposure(exposure, "symbol", "symbol")
    regime_exposure = _group_exposure(exposure, "regime_name", "regime_name")
    market_type_exposure = _group_exposure(exposure, "market_type", "market_type")
    correlations = _top_correlations(outcomes)

    top_symbol_pct = _number(symbol_exposure[0]["exposure_pct"]) if symbol_exposure else 0.0
    top_three_pct = sum(_number(row.get("exposure_pct")) for row in symbol_exposure[:3])
    top_regime_pct = _number(regime_exposure[0]["exposure_pct"]) if regime_exposure else 0.0
    hhi = sum((_number(row.get("exposure_pct")) / 100) ** 2 for row in symbol_exposure) * 100
    corr_risk = max((abs(_number(row.get("correlation"))) for row in correlations[:3]), default=0.0) * 20
    concentration_risk = min(100.0, top_symbol_pct * 0.75 + max(0.0, top_three_pct - 60.0) + hhi)
    heat_score = min(100.0, concentration_risk * 0.55 + max(0.0, top_regime_pct - 45.0) * 0.30 + corr_risk + _risk_event_pressure(risk_events))
    heat = "LOW" if heat_score < 35 else "MEDIUM" if heat_score < 70 else "HIGH"

    warnings = []
    if top_symbol_pct >= 35:
        warnings.append(f"High single-symbol concentration: {symbol_exposure[0]['symbol']} {top_symbol_pct:.2f}%")
    if top_three_pct >= 70:
        warnings.append(f"Top 3 symbols dominate exposure: {top_three_pct:.2f}%")
    if top_regime_pct >= 70 and regime_exposure:
        warnings.append(f"Regime concentration is high: {regime_exposure[0]['regime_name']} {top_regime_pct:.2f}%")
    if correlations and abs(_number(correlations[0].get("correlation"))) >= 0.75:
        warnings.append(
            "High historical correlation: "
            f"{correlations[0]['symbol_a']}/{correlations[0]['symbol_b']} {correlations[0]['correlation']}"
        )
    if not warnings:
        warnings.append("No major concentration warning from available data.")

    return {
        "ok": True,
        "source": source,
        "portfolio_heat": heat,
        "portfolio_heat_score": round(heat_score, 2),
        "concentration_risk": round(concentration_risk, 2),
        "top_symbol_exposure_pct": round(top_symbol_pct, 2),
        "top_three_exposure_pct": round(top_three_pct, 2),
        "top_regime_exposure_pct": round(top_regime_pct, 2),
        "symbol_exposure": symbol_exposure[:20],
        "regime_exposure": regime_exposure[:20],
        "market_type_exposure": market_type_exposure[:10],
        "top_correlated_symbols": correlations,
        "warnings": warnings,
        "rows": {
            "signals": int(len(signals)),
            "shadow_trades": int(len(shadow_trades)),
            "historical_outcomes": int(len(outcomes)),
            "risk_events": int(len(risk_events)),
        },
    }


def format_portfolio_observer(result: Dict[str, Any]) -> str:
    symbol_rows = pd.DataFrame(result.get("symbol_exposure", [])).head(10)
    regime_rows = pd.DataFrame(result.get("regime_exposure", [])).head(10)
    correlation_rows = pd.DataFrame(result.get("top_correlated_symbols", [])).head(5)
    return "\n".join(
        [
            "PORTFOLIO OBSERVABILITY",
            f"OK: {result.get('ok')}",
            f"Source: {result.get('source')}",
            f"Portfolio Heat: {result.get('portfolio_heat')} ({result.get('portfolio_heat_score')}/100)",
            f"Concentration Risk: {result.get('concentration_risk')}",
            f"Top Symbol Exposure: {result.get('top_symbol_exposure_pct')}%",
            f"Top 3 Exposure: {result.get('top_three_exposure_pct')}%",
            f"Top Regime Exposure: {result.get('top_regime_exposure_pct')}%",
            f"Warnings: {' | '.join(result.get('warnings', []))}",
            "",
            "Top Exposure Symbols:",
            symbol_rows.to_string(index=False) if not symbol_rows.empty else "No exposure data.",
            "",
            "Regime Exposure:",
            regime_rows.to_string(index=False) if not regime_rows.empty else "No regime exposure data.",
            "",
            "Top Correlated Symbols:",
            correlation_rows.to_string(index=False) if not correlation_rows.empty else "Not enough historical outcome data.",
        ]
    )
