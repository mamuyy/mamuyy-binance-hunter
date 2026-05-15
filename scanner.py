from datetime import datetime, timezone
import time
from typing import Any, Dict, List

import pandas as pd
import requests


class BinanceFuturesScanner:
    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        timeout: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 + attempt)
        raise last_error

    def get_top_usdt_symbols(
        self,
        limit: int = 30,
        min_quote_volume: float = 0.0,
    ) -> List[str]:
        tickers = self.get_24h_tickers()
        usdt_tickers = []

        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue

            quote_volume = float(ticker.get("quoteVolume", 0) or 0)
            if quote_volume < min_quote_volume:
                continue

            usdt_tickers.append(
                {
                    "symbol": symbol,
                    "quoteVolume": quote_volume,
                }
            )

        sorted_tickers = sorted(
            usdt_tickers,
            key=lambda item: item["quoteVolume"],
            reverse=True,
        )
        return [ticker["symbol"] for ticker in sorted_tickers[:limit]]

    def get_24h_tickers(self) -> List[Dict[str, Any]]:
        return self._get("/fapi/v1/ticker/24hr")

    def get_usdt_prices(self) -> Dict[str, float]:
        tickers = self.get_24h_tickers()
        prices = {}

        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue

            prices[symbol] = float(ticker.get("lastPrice", 0) or 0)

        return prices

    def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 60,
    ) -> pd.DataFrame:
        raw_klines = self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ]
        df = pd.DataFrame(raw_klines, columns=columns)

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_asset_volume",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
        ]
        for column in numeric_columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def get_open_interest(self, symbol: str) -> float:
        data = self._get("/fapi/v1/openInterest", params={"symbol": symbol})
        return float(data.get("openInterest", 0) or 0)

    def get_open_interest_history(
        self,
        symbol: str,
        period: str = "15m",
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        return self._get(
            "/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
        )

    def get_latest_funding_rate(self, symbol: str) -> float:
        data = self._get(
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
        )
        if not data:
            return 0.0
        return float(data[0].get("fundingRate", 0) or 0)

    def get_funding_rates(
        self,
        symbol: str,
        limit: int = 24,
    ) -> List[Dict[str, Any]]:
        return self._get(
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": limit},
        )

    @staticmethod
    def calculate_signal(
        symbol: str,
        candles: pd.DataFrame,
        funding_rate: float,
        open_interest: float,
    ) -> Dict[str, Any]:
        if len(candles) < 21:
            raise ValueError(f"Data candle {symbol} kurang dari 21")

        previous_20 = candles.iloc[-21:-1]
        latest = candles.iloc[-1]

        average_volume = previous_20["volume"].mean()
        volume_spike = latest["volume"] / average_volume if average_volume else 0.0

        previous_high_20 = previous_20["high"].max()
        previous_low_20 = previous_20["low"].min()

        breakout = bool(latest["close"] > previous_high_20)
        liquidity_sweep = bool(
            latest["low"] < previous_low_20 and latest["close"] > previous_low_20
        )

        taker_buy_ratio = 0.0
        if latest["volume"]:
            taker_buy_ratio = latest["taker_buy_base_asset_volume"] / latest["volume"]

        score = 0
        if volume_spike >= 3:
            score += 30
        elif volume_spike >= 2:
            score += 20

        if breakout:
            score += 25

        if liquidity_sweep:
            score += 25

        if 0.50 <= taker_buy_ratio <= 0.68:
            score += 15

        if abs(funding_rate) < 0.0005:
            score += 15

        score = min(score, 100)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "price": float(latest["close"]),
            "score": score,
            "volume_spike": float(volume_spike),
            "breakout": breakout,
            "liquidity_sweep": liquidity_sweep,
            "taker_buy_ratio": float(taker_buy_ratio),
            "funding": float(funding_rate),
            "open_interest": float(open_interest),
        }

    def scan_symbol(
        self,
        symbol: str,
        interval: str = "15m",
        candle_limit: int = 60,
    ) -> Dict[str, Any] | None:
        try:
            candles = self.get_klines(symbol, interval=interval, limit=candle_limit)
            funding_rate = self.get_latest_funding_rate(symbol)
            open_interest = self.get_open_interest(symbol)
            return self.calculate_signal(
                symbol=symbol,
                candles=candles,
                funding_rate=funding_rate,
                open_interest=open_interest,
            )
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            print(f"Gagal scan {symbol}: {exc}")
            return None

    def scan_market(
        self,
        top_symbols_limit: int = 30,
        min_quote_volume: float = 0.0,
        interval: str = "15m",
        candle_limit: int = 60,
    ) -> List[Dict[str, Any]]:
        try:
            symbols = self.get_top_usdt_symbols(
                limit=top_symbols_limit,
                min_quote_volume=min_quote_volume,
            )
        except requests.RequestException as exc:
            print(f"Gagal mengambil daftar ticker Binance Futures: {exc}")
            return []

        signals = []
        for symbol in symbols:
            signal = self.scan_symbol(
                symbol=symbol,
                interval=interval,
                candle_limit=candle_limit,
            )
            if signal:
                signals.append(signal)

        return sorted(signals, key=lambda item: item["score"], reverse=True)
