import csv
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd


REGIME_HISTORY_FIELDS = [
    "timestamp",
    "regime_name",
    "regime_score",
    "btc_price",
    "btc_change_24h",
    "btc_above_ema50",
    "btc_above_ema200",
    "ema_distance",
    "atr_percent",
    "volume_ratio",
    "btc_volume_dominance",
    "funding_rate",
]


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _calculate_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = candles["close"].shift(1)
    high_low = candles["high"] - candles["low"]
    high_close = (candles["high"] - previous_close).abs()
    low_close = (candles["low"] - previous_close).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def _confidence_from_points(points: int, max_points: int) -> int:
    if max_points <= 0:
        return 0
    return max(0, min(100, round((points / max_points) * 100)))


class MarketRegimeEngine:
    def __init__(self, scanner: Any) -> None:
        self.scanner = scanner

    def detect(self) -> Dict[str, Any]:
        btc_candles = self.scanner.get_klines("BTCUSDT", interval="15m", limit=220)
        btc_funding = self.scanner.get_latest_funding_rate("BTCUSDT")
        tickers = self.scanner.get_24h_tickers()

        btc_ticker = next(
            (ticker for ticker in tickers if ticker.get("symbol") == "BTCUSDT"),
            {},
        )
        total_quote_volume = sum(
            float(ticker.get("quoteVolume", 0) or 0)
            for ticker in tickers
            if ticker.get("symbol", "").endswith("USDT")
        )
        btc_quote_volume = float(btc_ticker.get("quoteVolume", 0) or 0)
        btc_volume_dominance = (
            btc_quote_volume / total_quote_volume if total_quote_volume else 0.0
        )

        features = self._build_features(
            candles=btc_candles,
            funding_rate=btc_funding,
            btc_ticker=btc_ticker,
            btc_volume_dominance=btc_volume_dominance,
        )
        regime = self._classify(features)
        return {**features, **regime}

    def _build_features(
        self,
        candles: pd.DataFrame,
        funding_rate: float,
        btc_ticker: Dict[str, Any],
        btc_volume_dominance: float,
    ) -> Dict[str, Any]:
        if len(candles) < 200:
            raise ValueError("BTCUSDT candle kurang dari 200 untuk regime engine")

        close = candles["close"]
        latest = candles.iloc[-1]
        ema50 = _ema(close, 50)
        ema200 = _ema(close, 200)
        atr = _calculate_atr(candles)

        btc_price = float(latest["close"])
        ema50_value = float(ema50.iloc[-1])
        ema200_value = float(ema200.iloc[-1])
        atr_percent = float((atr.iloc[-1] / btc_price) * 100) if btc_price else 0.0

        recent_volume = candles["volume"].tail(20).mean()
        baseline_volume = candles["volume"].tail(80).head(60).mean()
        volume_ratio = (
            float(recent_volume / baseline_volume) if baseline_volume else 0.0
        )

        candle_return = ((latest["close"] - latest["open"]) / latest["open"]) * 100
        recent_returns = close.pct_change().tail(16) * 100
        vertical_candles = int((recent_returns.abs() >= 1.2).sum())
        dump_4h = float(((close.iloc[-1] - close.iloc[-16]) / close.iloc[-16]) * 100)

        previous_20 = candles.iloc[-21:-1]
        breakout_failures = int(
            (
                (candles["high"].tail(20) > previous_20["high"].max())
                & (candles["close"].tail(20) < previous_20["high"].max())
            ).sum()
        )

        ema_distance = (
            abs(ema50_value - ema200_value) / btc_price * 100 if btc_price else 0.0
        )

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "btc_price": btc_price,
            "btc_change_24h": float(btc_ticker.get("priceChangePercent", 0) or 0),
            "btc_above_ema50": bool(btc_price > ema50_value),
            "btc_above_ema200": bool(btc_price > ema200_value),
            "ema50": ema50_value,
            "ema200": ema200_value,
            "ema_distance": float(ema_distance),
            "atr_percent": atr_percent,
            "volume_ratio": volume_ratio,
            "btc_volume_dominance": float(btc_volume_dominance),
            "funding_rate": float(funding_rate),
            "candle_return": float(candle_return),
            "dump_4h": dump_4h,
            "vertical_candles": vertical_candles,
            "breakout_failures": breakout_failures,
        }

    def _classify(self, features: Dict[str, Any]) -> Dict[str, Any]:
        panic_points = 0
        if features["dump_4h"] <= -4 or features["btc_change_24h"] <= -8:
            panic_points += 35
        if features["atr_percent"] >= 2.8:
            panic_points += 25
        if features["volume_ratio"] >= 2.5:
            panic_points += 20
        if features["candle_return"] <= -2:
            panic_points += 20

        euphoria_points = 0
        if features["funding_rate"] >= 0.001:
            euphoria_points += 35
        if features["vertical_candles"] >= 4:
            euphoria_points += 25
        if features["volume_ratio"] >= 2.2:
            euphoria_points += 20
        if features["btc_change_24h"] >= 7:
            euphoria_points += 20

        bull_points = 0
        if features["btc_above_ema50"]:
            bull_points += 25
        if features["btc_above_ema200"]:
            bull_points += 25
        if 0.4 <= features["atr_percent"] <= 2.2:
            bull_points += 20
        if features["volume_ratio"] >= 1.1:
            bull_points += 20
        if features["ema50"] > features["ema200"]:
            bull_points += 10

        sideways_points = 0
        if features["ema_distance"] <= 0.6:
            sideways_points += 35
        if features["atr_percent"] <= 0.8:
            sideways_points += 25
        if features["breakout_failures"] >= 2:
            sideways_points += 20
        if 0.7 <= features["volume_ratio"] <= 1.2:
            sideways_points += 20

        risk_off_points = 0
        if not features["btc_above_ema50"]:
            risk_off_points += 25
        if not features["btc_above_ema200"]:
            risk_off_points += 25
        if features["btc_change_24h"] <= -3:
            risk_off_points += 25
        if features["ema50"] < features["ema200"]:
            risk_off_points += 25

        candidates = [
            ("PANIC SELLING", panic_points),
            ("EUPHORIA", euphoria_points),
            ("TRENDING BULL", bull_points),
            ("SIDEWAYS / CHOPPY", sideways_points),
            ("RISK OFF", risk_off_points),
        ]
        regime_name, points = max(candidates, key=lambda item: item[1])

        return {
            "regime_name": regime_name,
            "regime_score": _confidence_from_points(points, 100),
            "regime_points": {
                "panic": panic_points,
                "euphoria": euphoria_points,
                "bull": bull_points,
                "sideways": sideways_points,
                "risk_off": risk_off_points,
            },
        }


def apply_regime_to_signal(
    signal: Dict[str, Any],
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    adjusted = dict(signal)
    base_score = int(adjusted.get("score", 0))
    score = base_score
    regime_name = regime.get("regime_name", "UNKNOWN")

    if regime_name == "PANIC SELLING":
        score -= 30
    elif regime_name == "SIDEWAYS / CHOPPY" and adjusted.get("breakout"):
        score -= 15
    elif regime_name == "TRENDING BULL":
        if adjusted.get("breakout") or adjusted.get("volume_spike", 0) >= 2:
            score += 10
        if adjusted.get("taker_buy_ratio", 0) >= 0.5:
            score += 5
    elif regime_name == "RISK OFF":
        score -= 20
    elif regime_name == "EUPHORIA":
        score -= 10

    adjusted["base_score"] = base_score
    adjusted["score"] = max(0, min(100, score))
    adjusted["regime_name"] = regime_name
    adjusted["regime_score"] = regime.get("regime_score", 0)
    return adjusted


def log_regime_history(
    regime: Dict[str, Any],
    path: str = "regime_history.csv",
) -> None:
    file_exists = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=REGIME_HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: regime.get(field, "") for field in REGIME_HISTORY_FIELDS})
