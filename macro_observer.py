import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
import requests

from database import init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_table(db_path: str, table: str, limit: int = 300) -> pd.DataFrame:
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


def _fetch_json(url: str, timeout: int = 5) -> Dict[str, Any] | None:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "mamuyy-hunter/1.0"})
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _component(name: str, value: float, risk: float, source: str, detail: str) -> Dict[str, Any]:
    return {
        "component": name,
        "value": round(float(value), 6),
        "risk": round(max(0.0, min(float(risk), 100.0)), 6),
        "source": source,
        "detail": detail,
    }


def _btc_dominance(regime_logs: pd.DataFrame) -> Dict[str, Any]:
    payload = _fetch_json("https://api.coingecko.com/api/v3/global")
    try:
        value = float(payload["data"]["market_cap_percentage"]["btc"]) if payload else None
    except (KeyError, TypeError, ValueError):
        value = None
    if value is None:
        if not regime_logs.empty and "btc_volume_dominance" in regime_logs.columns:
            value = _num(pd.to_numeric(regime_logs["btc_volume_dominance"], errors="coerce").dropna().head(20).mean(), 50.0)
        else:
            value = 50.0
        if 0 < value <= 1:
            value *= 100.0
        return _component("btc_dominance", value, abs(value - 50.0) * 2.0, "synthetic", "fallback from regime logs/default")
    return _component("btc_dominance", value, abs(value - 50.0) * 2.0, "live", "CoinGecko global market data")


def _fear_greed() -> Dict[str, Any]:
    payload = _fetch_json("https://api.alternative.me/fng/?limit=1")
    try:
        value = float(payload["data"][0]["value"]) if payload else None
    except (KeyError, TypeError, ValueError, IndexError):
        value = None
    if value is None:
        value = 50.0
        return _component("fear_greed", value, 25.0, "synthetic", "neutral fallback")
    risk = max(0.0, 50.0 - value) * 1.4 if value < 50 else max(0.0, value - 80.0) * 1.2
    return _component("fear_greed", value, risk, "live", "alternative.me Fear & Greed Index")


def _funding_stress(flow_logs: pd.DataFrame) -> Dict[str, Any]:
    if flow_logs.empty or "funding_zscore" not in flow_logs.columns:
        return _component("funding_rate_stress", 0.0, 15.0, "synthetic", "no flow logs; neutral fallback")
    values = pd.to_numeric(flow_logs["funding_zscore"], errors="coerce").abs().dropna().head(100)
    value = float(values.mean()) if not values.empty else 0.0
    return _component("funding_rate_stress", value, value * 28.0, "internal", "flow_logs funding_zscore abs mean")


def _oi_anomaly(flow_logs: pd.DataFrame) -> Dict[str, Any]:
    if flow_logs.empty or "oi_expansion_rate" not in flow_logs.columns:
        return _component("open_interest_anomaly", 0.0, 15.0, "synthetic", "no flow logs; neutral fallback")
    values = pd.to_numeric(flow_logs["oi_expansion_rate"], errors="coerce").dropna().head(100)
    value = float(values.clip(lower=0).mean()) if not values.empty else 0.0
    return _component("open_interest_anomaly", value, value * 120.0, "internal", "flow_logs positive oi_expansion_rate mean")


def _stablecoin_flow_proxy(flow_logs: pd.DataFrame) -> Dict[str, Any]:
    if flow_logs.empty:
        return _component("stablecoin_flow_proxy", 50.0, 20.0, "synthetic", "neutral fallback")
    taker = pd.to_numeric(flow_logs.get("taker_delta", pd.Series(dtype=float)), errors="coerce").dropna().head(100)
    pressure = pd.to_numeric(flow_logs.get("pressure_score", pd.Series(dtype=float)), errors="coerce").dropna().head(100)
    value = 50.0 + (float(taker.mean()) * 20.0 if not taker.empty else 0.0) + ((float(pressure.mean()) - 50.0) * 0.2 if not pressure.empty else 0.0)
    value = max(0.0, min(100.0, value))
    risk = max(0.0, 50.0 - value) * 1.2
    return _component("stablecoin_flow_proxy", value, risk, "synthetic", "deterministic proxy from taker_delta and pressure_score")


def _dxy_proxy(regime_logs: pd.DataFrame, flow_logs: pd.DataFrame) -> Dict[str, Any]:
    regime_risk = 0.0
    if not regime_logs.empty and "regime_name" in regime_logs.columns:
        names = regime_logs["regime_name"].fillna("").astype(str).str.upper().head(20)
        regime_risk = 20.0 if names.str.contains("RISK OFF|PANIC|HIGH VOLATILITY", regex=True).any() else 0.0
    funding = 0.0
    if not flow_logs.empty and "funding_zscore" in flow_logs.columns:
        funding = float(pd.to_numeric(flow_logs["funding_zscore"], errors="coerce").abs().dropna().head(50).mean() or 0.0)
    value = 50.0 + regime_risk + min(20.0, funding * 8.0)
    return _component("dxy_proxy", value, max(0.0, value - 55.0) * 1.5, "synthetic", "synthetic macro pressure proxy")


def _volatility_proxy(regime_logs: pd.DataFrame, flow_logs: pd.DataFrame) -> Dict[str, Any]:
    atr = 0.0
    if not regime_logs.empty and "atr_percent" in regime_logs.columns:
        atr = float(pd.to_numeric(regime_logs["atr_percent"], errors="coerce").dropna().head(30).mean() or 0.0)
    squeeze = 0.0
    if not flow_logs.empty and "squeeze_probability" in flow_logs.columns:
        squeeze = float(pd.to_numeric(flow_logs["squeeze_probability"], errors="coerce").dropna().head(100).mean() or 0.0)
    value = max(atr * 20.0, squeeze)
    return _component("volatility_proxy", value, value, "internal", "regime atr_percent and flow squeeze_probability")


def _state(score: float) -> str:
    if score >= 80:
        return "PANIC"
    if score >= 60:
        return "HIGH_STRESS"
    if score >= 40:
        return "CAUTION"
    if score >= 20:
        return "RISK_ON"
    return "LOW_RISK"


def observe_macro(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = "logs/macro_observer.csv",
) -> Dict[str, Any]:
    flow_logs = _read_table(db_path, "flow_logs", limit=300)
    regime_logs = _read_table(db_path, "regime_logs", limit=100)
    components = [
        _btc_dominance(regime_logs),
        _fear_greed(),
        _funding_stress(flow_logs),
        _oi_anomaly(flow_logs),
        _stablecoin_flow_proxy(flow_logs),
        _dxy_proxy(regime_logs, flow_logs),
        _volatility_proxy(regime_logs, flow_logs),
    ]
    weights = {
        "btc_dominance": 0.10,
        "fear_greed": 0.15,
        "funding_rate_stress": 0.18,
        "open_interest_anomaly": 0.16,
        "stablecoin_flow_proxy": 0.12,
        "dxy_proxy": 0.12,
        "volatility_proxy": 0.17,
    }
    score = sum(item["risk"] * weights.get(item["component"], 0.10) for item in components)
    score = round(max(0.0, min(100.0, score)), 4)
    contributors = sorted(components, key=lambda item: item["risk"], reverse=True)[:3]
    source_labels = sorted({item["source"] for item in components})
    row = {
        "timestamp": _now(),
        "macro_risk_score": score,
        "macro_state": _state(score),
        "components_json": json.dumps(components, default=str),
        "stress_contributors": " | ".join(f"{item['component']}={item['risk']:.2f}" for item in contributors),
        "source_labels": ",".join(source_labels),
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    pd.DataFrame([row]).to_csv(output_path, index=False)
    return {"ok": True, **row, "components": components}


def latest_macro_state(path: str = "logs/macro_observer.csv") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"macro_state": "UNKNOWN", "macro_risk_score": 0.0, "source_labels": "missing"}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {"macro_state": "UNKNOWN", "macro_risk_score": 0.0, "source_labels": "unreadable"}
    if df.empty:
        return {"macro_state": "UNKNOWN", "macro_risk_score": 0.0, "source_labels": "empty"}
    row = df.iloc[-1].to_dict()
    row["macro_risk_score"] = _num(row.get("macro_risk_score"))
    return row


def format_macro_observer(result: Dict[str, Any]) -> str:
    components = pd.DataFrame(result.get("components", []))
    return "\n".join(
        [
            "REAL MACRO OBSERVER",
            f"OK: {result.get('ok')}",
            f"Macro State: {result.get('macro_state')}",
            f"Macro Risk Score: {result.get('macro_risk_score')}",
            f"Sources: {result.get('source_labels')}",
            f"Stress Contributors: {result.get('stress_contributors')}",
            "",
            "Components:",
            components.to_string(index=False) if not components.empty else "No components.",
        ]
    )
