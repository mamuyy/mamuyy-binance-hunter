from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from database import init_db

REPORT_PATH = "reports/portfolio_risk_budget.json"
EMERGENCY_BRAKE_SIMULATION_PATH = "reports/emergency_brake_simulation.json"
BRAKE_HIGH_TRIGGER_THRESHOLD = 50
DEFAULT_MAX_TOTAL_EXPOSURE = 50.0
REGIME_RISK_CAPS: Dict[str, Dict[str, float]] = {
    "RISK_OFF": {"max_total_exposure": 15.0, "defensive_multiplier": 0.75},
    "HIGH_VOLATILITY": {"max_total_exposure": 35.0, "defensive_multiplier": 0.75},
    "TRENDING_BEAR": {"max_total_exposure": DEFAULT_MAX_TOTAL_EXPOSURE, "defensive_multiplier": 0.50},
}
TERMINAL_SHADOW_STATES = {"TRADE CLOSED", "CLOSED", "WIN", "LOSS", "EXPIRED", "IGNORED", "PROFIT_MATURED"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default



def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as report_file:
            payload = json.load(report_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nested_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _brake_context(path: str = EMERGENCY_BRAKE_SIMULATION_PATH) -> Dict[str, Any]:
    brake = _read_json(path)
    trigger_count = int(_number(
        _nested_get(brake, "summary", "brake_trigger_count", default=None)
        or _nested_get(brake, "summary", "trigger_count", default=None)
        or brake.get("high_trigger_count")
        or brake.get("trigger_count"),
        0.0,
    ))
    if not brake:
        source = "NONE"
    elif "simulation" in os.path.basename(path).lower():
        source = "SIMULATION_RESEARCH"
    else:
        source = "LIVE_RUNTIME"
    brake_risk_level = "HIGH" if trigger_count >= BRAKE_HIGH_TRIGGER_THRESHOLD else "LOW" if trigger_count > 0 else "NONE"
    return {
        "trigger_count": trigger_count,
        "brake_risk_level": brake_risk_level,
        "source": source,
    }


def _apply_brake_recommendation_floor(recommendation: str, brake_context: Dict[str, Any]) -> str:
    if (
        int(_number(brake_context.get("trigger_count"), 0.0)) >= BRAKE_HIGH_TRIGGER_THRESHOLD
        and str(recommendation).upper() == "NORMAL"
    ):
        return "DEFENSIVE"
    return recommendation

def _normalize_regime(regime: Any) -> str:
    text = str(regime or "UNKNOWN").strip().upper()
    if not text:
        return "UNKNOWN"
    return text.replace(" / ", "_").replace("/", "_").replace(" ", "_").replace("-", "_")


def _display_regime(regime: Any) -> str:
    text = str(regime or "UNKNOWN").strip().upper()
    return text or "UNKNOWN"


def _read_sql(db_path: str, query: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(query, connection, params=params)
    except Exception:
        return pd.DataFrame()


def _latest_regime(db_path: str) -> str:
    regimes = _read_sql(
        db_path,
        "SELECT regime_name FROM regime_logs WHERE regime_name IS NOT NULL ORDER BY id DESC LIMIT 1",
    )
    if not regimes.empty:
        return _display_regime(regimes.iloc[0].get("regime_name"))
    signals = _read_sql(
        db_path,
        "SELECT regime_name FROM signals WHERE regime_name IS NOT NULL ORDER BY id DESC LIMIT 1",
    )
    if not signals.empty:
        return _display_regime(signals.iloc[0].get("regime_name"))
    return "UNKNOWN"


def _shadow_exposure_frame(db_path: str) -> pd.DataFrame:
    shadow = _read_sql(
        db_path,
        """
        SELECT id, timestamp, symbol, regime_name, lifecycle_status, exposure
        FROM shadow_trades
        WHERE symbol IS NOT NULL AND symbol != ''
        ORDER BY id DESC
        LIMIT 1000
        """,
    )
    if shadow.empty or not {"symbol", "exposure"}.issubset(shadow.columns):
        return pd.DataFrame()
    latest = shadow.sort_values("id").drop_duplicates("symbol", keep="last").copy()
    latest["lifecycle_status"] = latest.get("lifecycle_status", "").fillna("").astype(str).str.upper().str.strip()
    latest = latest[~latest["lifecycle_status"].isin(TERMINAL_SHADOW_STATES)].copy()
    latest["exposure"] = pd.to_numeric(latest["exposure"], errors="coerce").fillna(0.0).abs()
    latest = latest[latest["exposure"] > 0]
    return latest[["symbol", "regime_name", "exposure"]]


def _signal_exposure_frame(db_path: str) -> pd.DataFrame:
    signals = _read_sql(
        db_path,
        """
        SELECT id, symbol, regime_name, score, adaptive_confidence_score
        FROM signals
        WHERE symbol IS NOT NULL AND symbol != ''
        ORDER BY id DESC
        LIMIT 300
        """,
    )
    if signals.empty or not {"symbol", "score"}.issubset(signals.columns):
        return pd.DataFrame()
    latest = signals.sort_values("id").drop_duplicates("symbol", keep="last").tail(50).copy()
    score = pd.to_numeric(latest.get("adaptive_confidence_score"), errors="coerce")
    fallback = pd.to_numeric(latest["score"], errors="coerce")
    latest["exposure"] = (score.fillna(fallback).fillna(0.0).clip(lower=0.0) / 1000.0).clip(lower=0.0, upper=0.12)
    latest = latest[latest["exposure"] > 0]
    return latest[["symbol", "regime_name", "exposure"]]


def _load_exposure_frame(db_path: str) -> tuple[pd.DataFrame, str]:
    shadow = _shadow_exposure_frame(db_path)
    if not shadow.empty:
        return shadow, "shadow_trades_read_only"
    signals = _signal_exposure_frame(db_path)
    if not signals.empty:
        return signals, "signals_score_proxy_read_only"
    return pd.DataFrame(columns=["symbol", "regime_name", "exposure"]), "none"


def _breakdown(frame: pd.DataFrame, column: str, label: str, total_exposure: float) -> List[Dict[str, Any]]:
    if frame.empty or column not in frame.columns or total_exposure <= 0:
        return []
    grouped = (
        frame.groupby(column, dropna=False)
        .agg(exposure=("exposure", "sum"), rows=("symbol", "count"))
        .reset_index()
        .rename(columns={column: label})
        .sort_values("exposure", ascending=False)
    )
    grouped["exposure"] = (grouped["exposure"] * 100).round(2)
    grouped["portfolio_share"] = (grouped["exposure"] / total_exposure * 100).round(2)
    return grouped.to_dict("records")


def _regime_cap(regime: str) -> Dict[str, float]:
    key = _normalize_regime(regime)
    cap = REGIME_RISK_CAPS.get(key, {"max_total_exposure": DEFAULT_MAX_TOTAL_EXPOSURE, "defensive_multiplier": 1.0}).copy()
    raw_max = _number(cap.get("max_total_exposure"), DEFAULT_MAX_TOTAL_EXPOSURE)
    multiplier = _number(cap.get("defensive_multiplier"), 1.0)
    cap["raw_max_total_exposure"] = raw_max
    cap["defensive_multiplier"] = multiplier
    cap["max_allowed_exposure"] = round(raw_max * multiplier, 2)
    return cap


def _recommendation(total_exposure: float, max_allowed: float, utilization: float, concentration: float) -> str:
    if max_allowed <= 0 or total_exposure >= max_allowed * 1.25:
        return "FREEZE NEW ALLOCATION"
    if total_exposure > max_allowed:
        return "REDUCE EXPOSURE"
    if utilization >= 0.75 or concentration >= 50:
        return "DEFENSIVE"
    return "NORMAL"


def _concentration_label(concentration_score: float) -> str:
    if concentration_score >= 60:
        return "HIGH"
    if concentration_score >= 30:
        return "MEDIUM"
    return "LOW"


def calculate_portfolio_risk_budget(
    db_path: str = "mamuyy_hunter.db",
    output_path: str | None = REPORT_PATH,
    write_report: bool = True,
) -> Dict[str, Any]:
    frame, source = _load_exposure_frame(db_path)
    current_regime = _latest_regime(db_path)
    if not frame.empty:
        frame = frame.copy()
        frame["symbol"] = frame["symbol"].fillna("UNKNOWN").astype(str)
        frame["regime_name"] = frame["regime_name"].fillna(current_regime).astype(str).str.upper()
        frame["exposure"] = pd.to_numeric(frame["exposure"], errors="coerce").fillna(0.0).abs()
        frame = frame[frame["exposure"] > 0]

    total_exposure = round(float(frame["exposure"].sum() * 100) if not frame.empty else 0.0, 2)
    symbol_breakdown = _breakdown(frame, "symbol", "symbol", total_exposure)
    regime_breakdown = _breakdown(frame, "regime_name", "regime", total_exposure)
    max_symbol_exposure = round(max((_number(row.get("exposure")) for row in symbol_breakdown), default=0.0), 2)
    top_three_share = sum(_number(row.get("portfolio_share")) for row in symbol_breakdown[:3])
    hhi = sum((_number(row.get("portfolio_share")) / 100.0) ** 2 for row in symbol_breakdown) * 100
    concentration_score = round(min(100.0, max_symbol_exposure * 1.25 + max(0.0, top_three_share - 60.0) * 0.75 + hhi), 2)
    diversification_score = round(max(0.0, 100.0 - concentration_score), 2)
    cap = _regime_cap(current_regime)
    max_allowed_exposure = _number(cap.get("max_allowed_exposure"), DEFAULT_MAX_TOTAL_EXPOSURE)
    utilization_ratio = round(total_exposure / max_allowed_exposure, 4) if max_allowed_exposure > 0 else 0.0
    recommendation = _recommendation(total_exposure, max_allowed_exposure, utilization_ratio, concentration_score)
    brake_context = _brake_context()
    recommendation = _apply_brake_recommendation_floor(recommendation, brake_context)
    defensive_multiplier = _number(cap.get("defensive_multiplier"), 1.0)
    defensive_scaling_recommendation = (
        f"Scale new paper allocation by {defensive_multiplier:.2f}x in {current_regime}; analytics only."
        if defensive_multiplier < 1.0
        else "No defensive scaling from regime cap; analytics only."
    )
    warnings: List[str] = []
    if max_symbol_exposure >= 12.0:
        warnings.append(f"Single symbol exposure elevated: {max_symbol_exposure:.2f}%")
    if utilization_ratio >= 1.0:
        warnings.append(f"Risk budget utilization exceeds cap: {utilization_ratio:.2f}x")
    if concentration_score >= 60.0:
        warnings.append("Portfolio concentration is HIGH.")
    if brake_context.get("brake_risk_level") == "HIGH":
        warnings.append("Emergency brake simulation history is HIGH; review required before new paper allocation.")
    if not warnings:
        warnings.append("No major risk budget warning from available read-only data.")

    result: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "governance_layer": "PAPER_ONLY_READ_ONLY_PORTFOLIO_RISK_BUDGET",
        "execution_enabled": False,
        "live_trading_enabled": False,
        "regime": current_regime,
        "source": source,
        "total_exposure": total_exposure,
        "exposure_by_regime": regime_breakdown,
        "exposure_by_symbol": symbol_breakdown,
        "max_allowed_exposure": round(max_allowed_exposure, 2),
        "raw_max_allowed_exposure": round(_number(cap.get("raw_max_total_exposure"), max_allowed_exposure), 2),
        "defensive_multiplier": defensive_multiplier,
        "utilization_ratio": utilization_ratio,
        "risk_budget_utilization": round(utilization_ratio * 100, 2),
        "concentration_score": concentration_score,
        "concentration_label": _concentration_label(concentration_score),
        "max_symbol_exposure": max_symbol_exposure,
        "diversification_score": diversification_score,
        "defensive_scaling_recommendation": defensive_scaling_recommendation,
        "recommendation": recommendation,
        "brake_context": brake_context,
        "symbol_breakdown": symbol_breakdown,
        "warnings": warnings,
        "safety": [
            "PAPER_ONLY enforced",
            "Read-only governance intelligence only",
            "No broker routing",
            "No order placement",
            "No live trading",
            "No Phase 3 promotion",
        ],
    }
    if write_report and output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as report_file:
            json.dump(result, report_file, indent=2, sort_keys=True)
            report_file.write("\n")
    return result


def format_portfolio_risk_budget(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "PORTFOLIO RISK BUDGET (PAPER_ONLY)",
            f"Regime: {result.get('regime', 'UNKNOWN')}",
            f"Total Exposure: {_number(result.get('total_exposure')):.2f}%",
            f"Max Allowed Exposure: {_number(result.get('max_allowed_exposure')):.2f}%",
            f"Utilization: {_number(result.get('risk_budget_utilization')):.2f}%",
            f"Concentration: {result.get('concentration_label', 'UNKNOWN')} ({_number(result.get('concentration_score')):.2f}/100)",
            f"Diversification: {_number(result.get('diversification_score')):.2f}/100",
            f"Recommendation: {result.get('recommendation', 'NORMAL')}",
            f"Brake Context: {result.get('brake_context', {}).get('brake_risk_level', 'NONE')} "
            f"(triggers={int(_number(result.get('brake_context', {}).get('trigger_count')))}, "
            f"source={result.get('brake_context', {}).get('source', 'NONE')})",
            "Safety: PAPER_ONLY, read-only analytics, no order placement, no live trading.",
        ]
    )
