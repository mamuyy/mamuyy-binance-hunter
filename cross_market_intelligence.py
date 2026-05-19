import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd


OUTPUT_PATH = "logs/cross_market_intelligence.csv"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if pd.isna(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _read_table(db_path: str, table: str, limit: int = 500) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            if not exists:
                return pd.DataFrame()
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _latest(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=object)
    return df.sort_values("id").iloc[-1] if "id" in df.columns else df.iloc[0]


def _mean(df: pd.DataFrame, column: str, default: float = 0.0, rows: int = 100) -> float:
    if df.empty or column not in df.columns:
        return default
    values = pd.to_numeric(df[column], errors="coerce").dropna().head(rows)
    return float(values.mean()) if not values.empty else default


def _component(name: str, value: float, source: str, detail: str) -> Dict[str, Any]:
    return {
        "component": name,
        "value": round(float(value), 6),
        "source": source,
        "detail": detail,
    }


def _btc_dominance(regimes: pd.DataFrame) -> Dict[str, Any]:
    value = _mean(regimes, "btc_volume_dominance", 50.0, rows=20)
    if 0 < value <= 1:
        value *= 100
    value = max(35.0, min(65.0, value or 50.0))
    return _component("btc_dominance", value, "internal" if not regimes.empty else "synthetic", "regime_logs btc_volume_dominance/default")


def _dxy_proxy(regimes: pd.DataFrame, flows: pd.DataFrame) -> Dict[str, Any]:
    latest_regime = str(_latest(regimes).get("regime_name") or "").upper()
    funding_abs = abs(_mean(flows, "funding_zscore", 0.0, rows=80))
    atr = _mean(regimes, "atr_percent", 0.0, rows=20)
    value = 50.0 + min(18.0, funding_abs * 7.0) + min(18.0, atr * 4.0)
    if any(key in latest_regime for key in ["RISK OFF", "PANIC", "HIGH VOLATILITY"]):
        value += 12.0
    return _component("dxy_proxy", max(0.0, min(100.0, value)), "synthetic", "macro pressure proxy from funding, ATR, and regime")


def _gold_proxy(regimes: pd.DataFrame, flows: pd.DataFrame) -> Dict[str, Any]:
    latest_regime = str(_latest(regimes).get("regime_name") or "").upper()
    squeeze = _mean(flows, "squeeze_probability", 0.0, rows=80)
    value = 45.0 + min(25.0, squeeze * 0.25)
    if any(key in latest_regime for key in ["RISK OFF", "PANIC", "HIGH VOLATILITY"]):
        value += 18.0
    return _component("gold_proxy", max(0.0, min(100.0, value)), "synthetic", "safe-haven proxy from squeeze and regime")


def _spx_proxy(regimes: pd.DataFrame, flows: pd.DataFrame) -> Dict[str, Any]:
    btc_change = _num(_latest(regimes).get("btc_change_24h"), 0.0)
    pressure = _mean(flows, "pressure_score", 50.0, rows=80)
    taker_delta = _mean(flows, "taker_delta", 0.0, rows=80)
    value = 50.0 + btc_change * 2.0 + (pressure - 50.0) * 0.25 + taker_delta * 18.0
    return _component("spx_proxy", max(0.0, min(100.0, value)), "synthetic", "risk sentiment proxy from BTC change and flow pressure")


def _stablecoin_flow_proxy(flows: pd.DataFrame) -> Dict[str, Any]:
    pressure = _mean(flows, "pressure_score", 50.0, rows=80)
    taker_delta = _mean(flows, "taker_delta", 0.0, rows=80)
    value = 50.0 + (pressure - 50.0) * 0.3 + taker_delta * 25.0
    return _component("stablecoin_flow_proxy", max(0.0, min(100.0, value)), "internal" if not flows.empty else "synthetic", "flow pressure and taker_delta proxy")


def _altseason_proxy(signals: pd.DataFrame, btc_dominance: float) -> Dict[str, Any]:
    if signals.empty or "symbol" not in signals.columns:
        breadth = 50.0
        source = "synthetic"
    else:
        latest = signals.sort_values("id").drop_duplicates("symbol", keep="last") if "id" in signals.columns else signals
        score = pd.to_numeric(latest.get("score", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        alt = latest[~latest["symbol"].astype(str).str.upper().eq("BTCUSDT")]
        alt_score = pd.to_numeric(alt.get("score", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        breadth = float((alt_score >= 70).mean() * 100) if len(alt_score) else float((score >= 70).mean() * 100) if len(score) else 50.0
        source = "internal"
    value = max(0.0, min(100.0, (100.0 - btc_dominance) * 0.9 + breadth * 0.55))
    return _component("altseason_proxy", value, source, "alt signal breadth adjusted by BTC dominance")


def _etf_risk_sentiment(spx_proxy: float, dxy_proxy: float) -> Dict[str, Any]:
    value = max(0.0, min(100.0, spx_proxy - max(0.0, dxy_proxy - 55.0) * 0.35))
    return _component("etf_risk_sentiment_proxy", value, "synthetic", "SPX proxy penalized by DXY pressure")


def _correlation_matrix(outcomes: pd.DataFrame, components: Dict[str, float]) -> List[Dict[str, Any]]:
    base_assets = ["crypto", "dxy_proxy", "gold_proxy", "spx_proxy"]
    fallback = pd.DataFrame(
        [
            {"asset": "crypto", "crypto": 1.0, "dxy_proxy": -0.45, "gold_proxy": -0.05, "spx_proxy": 0.55},
            {"asset": "dxy_proxy", "crypto": -0.45, "dxy_proxy": 1.0, "gold_proxy": 0.25, "spx_proxy": -0.40},
            {"asset": "gold_proxy", "crypto": -0.05, "dxy_proxy": 0.25, "gold_proxy": 1.0, "spx_proxy": -0.15},
            {"asset": "spx_proxy", "crypto": 0.55, "dxy_proxy": -0.40, "gold_proxy": -0.15, "spx_proxy": 1.0},
        ]
    )
    if outcomes.empty or "pnl_pct" not in outcomes.columns:
        return fallback.to_dict("records")
    pnl = pd.to_numeric(outcomes["pnl_pct"], errors="coerce").dropna().head(250)
    if len(pnl) < 10:
        return fallback.to_dict("records")
    crypto = pnl.reset_index(drop=True)
    synthetic = pd.DataFrame(
        {
            "crypto": crypto,
            "dxy_proxy": -crypto.rolling(5, min_periods=1).mean() + (components["dxy_proxy"] - 50.0) / 50.0,
            "gold_proxy": -crypto.abs().rolling(5, min_periods=1).mean() + (components["gold_proxy"] - 50.0) / 80.0,
            "spx_proxy": crypto.rolling(5, min_periods=1).mean() + (components["spx_proxy"] - 50.0) / 50.0,
        }
    )
    corr = synthetic[base_assets].corr().fillna(0.0).round(4)
    corr.insert(0, "asset", corr.index)
    return corr.reset_index(drop=True).to_dict("records")


def _state(stress_score: float, altseason_probability: float, safe_haven: bool) -> str:
    if safe_haven:
        return "SAFE_HAVEN_ROTATION"
    if stress_score >= 70:
        return "CROSS_MARKET_STRESS"
    if stress_score >= 45:
        return "CAUTION"
    if altseason_probability >= 65:
        return "ALTSEASON_RISK_ON"
    return "RISK_ON"


def run_cross_market_intelligence(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = OUTPUT_PATH,
) -> Dict[str, Any]:
    regimes = _read_table(db_path, "regime_logs", limit=120)
    flows = _read_table(db_path, "flow_logs", limit=300)
    signals = _read_table(db_path, "signals", limit=300)
    outcomes = _read_table(db_path, "historical_outcomes", limit=1000)

    btc = _btc_dominance(regimes)
    dxy = _dxy_proxy(regimes, flows)
    gold = _gold_proxy(regimes, flows)
    spx = _spx_proxy(regimes, flows)
    stable = _stablecoin_flow_proxy(flows)
    altseason = _altseason_proxy(signals, btc["value"])
    etf = _etf_risk_sentiment(spx["value"], dxy["value"])

    components = [dxy, gold, spx, btc, altseason, stable, etf]
    values = {item["component"]: float(item["value"]) for item in components}
    dxy_pressure = max(0.0, values["dxy_proxy"] - 55.0)
    safe_haven_rotation = values["gold_proxy"] >= 65.0 and values["spx_proxy"] <= 45.0
    macro_divergence = values["spx_proxy"] >= 60.0 and values["dxy_proxy"] >= 65.0
    crypto_dxy_relationship = "INVERSE_PRESSURE" if dxy_pressure >= 10 else "NEUTRAL"
    crypto_gold_relationship = "SAFE_HAVEN_DIVERGENCE" if safe_haven_rotation else "NEUTRAL"
    risk_alignment = "RISK_ON_ALIGNED" if values["spx_proxy"] >= 55 and values["dxy_proxy"] < 65 else "RISK_OFF_ALIGNED" if values["spx_proxy"] < 45 and values["dxy_proxy"] >= 65 else "DIVERGENT" if macro_divergence else "MIXED"
    stress_score = (
        dxy_pressure * 1.35
        + max(0.0, values["gold_proxy"] - 55.0) * 0.80
        + max(0.0, 50.0 - values["spx_proxy"]) * 0.85
        + max(0.0, 50.0 - values["stablecoin_flow_proxy"]) * 0.75
        + (18.0 if macro_divergence else 0.0)
        + (20.0 if safe_haven_rotation else 0.0)
    )
    stress_score = round(max(0.0, min(100.0, stress_score)), 4)
    contributors = sorted(
        [
            ("dxy_pressure", dxy_pressure),
            ("gold_rotation", max(0.0, values["gold_proxy"] - 55.0)),
            ("spx_weakness", max(0.0, 50.0 - values["spx_proxy"])),
            ("stablecoin_outflow", max(0.0, 50.0 - values["stablecoin_flow_proxy"])),
            ("macro_divergence", 18.0 if macro_divergence else 0.0),
            ("safe_haven_rotation", 20.0 if safe_haven_rotation else 0.0),
        ],
        key=lambda item: item[1],
        reverse=True,
    )[:4]
    source_labels = sorted({item["source"] for item in components})
    correlation = _correlation_matrix(outcomes, values)
    row = {
        "timestamp": _now(),
        "cross_market_state": _state(stress_score, values["altseason_proxy"], safe_haven_rotation),
        "risk_alignment": risk_alignment,
        "altseason_probability": round(values["altseason_proxy"], 4),
        "dxy_pressure": round(dxy_pressure, 4),
        "safe_haven_rotation": bool(safe_haven_rotation),
        "cross_market_stress_score": stress_score,
        "macro_divergence": bool(macro_divergence),
        "crypto_dxy_relationship": crypto_dxy_relationship,
        "crypto_gold_relationship": crypto_gold_relationship,
        "components_json": json.dumps(components, default=str),
        "correlation_matrix_json": json.dumps(correlation, default=str),
        "stress_contributors": " | ".join(f"{name}={value:.2f}" for name, value in contributors if value > 0) or "none",
        "source_labels": ",".join(source_labels),
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    pd.DataFrame([row]).to_csv(output_path, index=False)
    return {"ok": True, **row, "components": components, "correlation_matrix": correlation}


def latest_cross_market_state(path: str = OUTPUT_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"cross_market_state": "UNKNOWN", "cross_market_stress_score": 0.0, "source_labels": "missing"}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {"cross_market_state": "UNKNOWN", "cross_market_stress_score": 0.0, "source_labels": "unreadable"}
    if df.empty:
        return {"cross_market_state": "UNKNOWN", "cross_market_stress_score": 0.0, "source_labels": "empty"}
    row = df.iloc[-1].to_dict()
    row["cross_market_stress_score"] = _num(row.get("cross_market_stress_score"))
    row["altseason_probability"] = _num(row.get("altseason_probability"))
    row["dxy_pressure"] = _num(row.get("dxy_pressure"))
    row["safe_haven_rotation"] = str(row.get("safe_haven_rotation")).lower() in {"true", "1", "yes"}
    row["macro_divergence"] = str(row.get("macro_divergence")).lower() in {"true", "1", "yes"}
    return row


def format_cross_market_report(result: Dict[str, Any]) -> str:
    corr = pd.DataFrame(result.get("correlation_matrix", []))
    return "\n".join(
        [
            "CROSS MARKET INTELLIGENCE",
            f"OK: {result.get('ok')}",
            f"State: {result.get('cross_market_state')}",
            f"Risk Alignment: {result.get('risk_alignment')}",
            f"Stress Score: {result.get('cross_market_stress_score')}",
            f"Altseason Probability: {result.get('altseason_probability')}",
            f"DXY Pressure: {result.get('dxy_pressure')}",
            f"Safe Haven Rotation: {result.get('safe_haven_rotation')}",
            f"Macro Divergence: {result.get('macro_divergence')}",
            f"Sources: {result.get('source_labels')}",
            f"Stress Contributors: {result.get('stress_contributors')}",
            "",
            "Correlation Matrix:",
            corr.to_string(index=False) if not corr.empty else "No correlation matrix.",
        ]
    )
