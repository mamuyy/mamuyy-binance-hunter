import csv
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
import requests


FLOW_FIELDS = [
    "timestamp",
    "symbol",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "flow_state",
    "whale_activity",
    "squeeze_risk",
    "funding_warning",
    "flow_adjustment",
    "final_score",
]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _zscore(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    latest = values[-1]
    mean = sum(values[:-1]) / max(len(values[:-1]), 1)
    variance = sum((value - mean) ** 2 for value in values[:-1]) / max(
        len(values[:-1]),
        1,
    )
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (latest - mean) / std


class AdvancedFlowEngine:
    def __init__(self, scanner: Any) -> None:
        self.scanner = scanner

    def analyze_symbol(
        self,
        symbol: str,
        candles: pd.DataFrame | None = None,
        funding_rate: float | None = None,
        open_interest: float | None = None,
    ) -> Dict[str, Any]:
        if candles is None:
            candles = self.scanner.get_klines(symbol, interval="15m", limit=80)
        if funding_rate is None:
            funding_rate = self.scanner.get_latest_funding_rate(symbol)
        if open_interest is None:
            open_interest = self.scanner.get_open_interest(symbol)

        funding_rates = self._get_funding_rate_series(symbol)
        funding_zscore = _zscore(funding_rates) if funding_rates else 0.0
        oi_expansion_rate = self._get_oi_expansion_rate(symbol, open_interest)

        latest = candles.iloc[-1]
        previous_20 = candles.iloc[-21:-1]
        average_volume = previous_20["volume"].mean()
        volume_spike = latest["volume"] / average_volume if average_volume else 0.0

        taker_buy = _safe_float(latest.get("taker_buy_base_asset_volume"))
        volume = _safe_float(latest.get("volume"))
        taker_sell = max(volume - taker_buy, 0.0)
        taker_delta = ((taker_buy - taker_sell) / volume) if volume else 0.0

        candle_return = (
            ((latest["close"] - latest["open"]) / latest["open"]) * 100
            if latest["open"]
            else 0.0
        )
        candle_range_percent = (
            ((latest["high"] - latest["low"]) / latest["close"]) * 100
            if latest["close"]
            else 0.0
        )

        pressure_score = self._calculate_pressure_score(
            taker_delta=taker_delta,
            oi_expansion_rate=oi_expansion_rate,
            volume_spike=volume_spike,
            candle_return=candle_return,
        )

        long_squeeze_probability = self._long_squeeze_probability(
            funding_rate=funding_rate,
            funding_zscore=funding_zscore,
            taker_delta=taker_delta,
            candle_return=candle_return,
            candle_range_percent=candle_range_percent,
            volume_spike=volume_spike,
            oi_expansion_rate=oi_expansion_rate,
        )
        short_squeeze_probability = self._short_squeeze_probability(
            funding_rate=funding_rate,
            funding_zscore=funding_zscore,
            taker_delta=taker_delta,
            candle_return=candle_return,
            candle_range_percent=candle_range_percent,
            volume_spike=volume_spike,
            oi_expansion_rate=oi_expansion_rate,
        )
        squeeze_probability = max(
            long_squeeze_probability,
            short_squeeze_probability,
        )

        flow_state = self._detect_flow_state(
            pressure_score=pressure_score,
            taker_delta=taker_delta,
            oi_expansion_rate=oi_expansion_rate,
            volume_spike=volume_spike,
            candle_return=candle_return,
            long_squeeze_probability=long_squeeze_probability,
            short_squeeze_probability=short_squeeze_probability,
        )
        whale_activity = self._detect_whale_activity(
            flow_state=flow_state,
            pressure_score=pressure_score,
            oi_expansion_rate=oi_expansion_rate,
            volume_spike=volume_spike,
        )
        squeeze_risk = self._detect_squeeze_risk(
            long_squeeze_probability=long_squeeze_probability,
            short_squeeze_probability=short_squeeze_probability,
        )
        funding_warning = self._funding_warning(funding_rate, funding_zscore)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "funding_zscore": float(funding_zscore),
            "oi_expansion_rate": float(oi_expansion_rate),
            "taker_delta": float(taker_delta),
            "volume_imbalance": float(taker_delta),
            "pressure_score": float(pressure_score),
            "squeeze_probability": float(squeeze_probability),
            "long_squeeze_probability": float(long_squeeze_probability),
            "short_squeeze_probability": float(short_squeeze_probability),
            "flow_state": flow_state,
            "whale_activity": whale_activity,
            "squeeze_risk": squeeze_risk,
            "funding_warning": funding_warning,
        }

    def _get_funding_rate_series(self, symbol: str) -> List[float]:
        try:
            rows = self.scanner.get_funding_rates(symbol, limit=24)
            return [_safe_float(row.get("fundingRate")) for row in rows]
        except (requests.RequestException, AttributeError):
            return []

    def _get_oi_expansion_rate(self, symbol: str, current_open_interest: float) -> float:
        try:
            rows = self.scanner.get_open_interest_history(
                symbol=symbol,
                period="15m",
                limit=12,
            )
        except (requests.RequestException, AttributeError):
            rows = []

        if len(rows) >= 2:
            first = _safe_float(rows[0].get("sumOpenInterest"))
            latest = _safe_float(rows[-1].get("sumOpenInterest"))
            if first:
                return ((latest - first) / first) * 100

        return 0.0 if current_open_interest else 0.0

    @staticmethod
    def _calculate_pressure_score(
        taker_delta: float,
        oi_expansion_rate: float,
        volume_spike: float,
        candle_return: float,
    ) -> float:
        score = 50 + (taker_delta * 35)
        score += _clamp(oi_expansion_rate, -10, 10) * 1.5
        score += _clamp(volume_spike - 1, 0, 3) * 5
        score += _clamp(candle_return, -4, 4) * 2
        return _clamp(score)

    @staticmethod
    def _long_squeeze_probability(
        funding_rate: float,
        funding_zscore: float,
        taker_delta: float,
        candle_return: float,
        candle_range_percent: float,
        volume_spike: float,
        oi_expansion_rate: float,
    ) -> float:
        score = 0.0
        if funding_rate > 0.0007:
            score += 25
        if funding_zscore > 1.5:
            score += 20
        if taker_delta < -0.18:
            score += 20
        if candle_return < -1.2:
            score += 15
        if candle_range_percent > 2.0:
            score += 10
        if volume_spike >= 2:
            score += 10
        if oi_expansion_rate >= 3:
            score += 10
        return _clamp(score)

    @staticmethod
    def _short_squeeze_probability(
        funding_rate: float,
        funding_zscore: float,
        taker_delta: float,
        candle_return: float,
        candle_range_percent: float,
        volume_spike: float,
        oi_expansion_rate: float,
    ) -> float:
        score = 0.0
        if funding_rate < -0.0004:
            score += 25
        if funding_zscore < -1.5:
            score += 20
        if taker_delta > 0.18:
            score += 20
        if candle_return > 1.2:
            score += 15
        if candle_range_percent > 2.0:
            score += 10
        if volume_spike >= 2:
            score += 10
        if oi_expansion_rate >= 3:
            score += 10
        return _clamp(score)

    @staticmethod
    def _detect_flow_state(
        pressure_score: float,
        taker_delta: float,
        oi_expansion_rate: float,
        volume_spike: float,
        candle_return: float,
        long_squeeze_probability: float,
        short_squeeze_probability: float,
    ) -> str:
        if long_squeeze_probability >= 70:
            return "LONG SQUEEZE RISK"
        if short_squeeze_probability >= 70:
            return "SHORT SQUEEZE RISK"
        if (
            pressure_score >= 65
            and taker_delta > 0.12
            and oi_expansion_rate >= 2
            and candle_return >= 0
        ):
            return "WHALE ACCUMULATION"
        if (
            pressure_score <= 35
            and taker_delta < -0.12
            and volume_spike >= 1.5
            and candle_return <= 0
        ):
            return "WHALE DISTRIBUTION"
        return "NEUTRAL FLOW"

    @staticmethod
    def _detect_whale_activity(
        flow_state: str,
        pressure_score: float,
        oi_expansion_rate: float,
        volume_spike: float,
    ) -> str:
        if flow_state in {"WHALE ACCUMULATION", "WHALE DISTRIBUTION"}:
            return flow_state
        if oi_expansion_rate >= 4 and volume_spike >= 2:
            return "HIGH OI + VOLUME ACTIVITY"
        if pressure_score >= 62:
            return "BUY PRESSURE"
        if pressure_score <= 38:
            return "SELL PRESSURE"
        return "NORMAL"

    @staticmethod
    def _detect_squeeze_risk(
        long_squeeze_probability: float,
        short_squeeze_probability: float,
    ) -> str:
        if long_squeeze_probability >= 70:
            return "HIGH LONG SQUEEZE RISK"
        if short_squeeze_probability >= 70:
            return "HIGH SHORT SQUEEZE RISK"
        if max(long_squeeze_probability, short_squeeze_probability) >= 45:
            return "MODERATE"
        return "LOW"

    @staticmethod
    def _funding_warning(funding_rate: float, funding_zscore: float) -> str:
        if funding_rate >= 0.001 or funding_zscore >= 2:
            return "LONGS CROWDED"
        if funding_rate <= -0.0007 or funding_zscore <= -2:
            return "SHORTS CROWDED"
        return ""


def apply_flow_to_signal(signal: Dict[str, Any], flow: Dict[str, Any]) -> Dict[str, Any]:
    adjusted = dict(signal)
    score = int(adjusted.get("score", 0))
    adjustment = 0

    if flow.get("squeeze_probability", 0) >= 70 and adjusted.get("breakout"):
        adjustment -= 15
    elif flow.get("squeeze_probability", 0) >= 45 and adjusted.get("breakout"):
        adjustment -= 8

    if flow.get("flow_state") == "WHALE ACCUMULATION":
        adjustment += 15
    elif flow.get("flow_state") == "WHALE DISTRIBUTION":
        adjustment -= 15
    elif flow.get("flow_state") == "SHORT SQUEEZE RISK":
        adjustment += 8
    elif flow.get("flow_state") == "LONG SQUEEZE RISK":
        adjustment -= 20

    if flow.get("funding_warning") == "LONGS CROWDED":
        adjustment -= 10
    elif flow.get("funding_warning") == "SHORTS CROWDED":
        adjustment += 5

    final_score = int(_clamp(score + adjustment))
    adjusted["pre_flow_score"] = score
    adjusted["score"] = final_score
    adjusted["final_score"] = final_score
    adjusted["flow_adjustment"] = adjustment

    for key in [
        "funding_zscore",
        "oi_expansion_rate",
        "taker_delta",
        "volume_imbalance",
        "pressure_score",
        "squeeze_probability",
        "long_squeeze_probability",
        "short_squeeze_probability",
        "flow_state",
        "whale_activity",
        "squeeze_risk",
        "funding_warning",
    ]:
        adjusted[key] = flow.get(key)

    return adjusted


def log_flow(flow: Dict[str, Any], path: str = "flow_log.csv") -> None:
    file_exists = os.path.exists(path)
    row = dict(flow)
    row.setdefault("flow_adjustment", "")
    row.setdefault("final_score", "")

    with open(path, "a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FLOW_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in FLOW_FIELDS})
