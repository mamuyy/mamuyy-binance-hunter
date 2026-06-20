import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pandas as pd
import requests

from database import get_connection, init_db
from scanner import BinanceFuturesScanner


def _is_closed_candle(close_time: Any, now: datetime) -> bool:
    if hasattr(close_time, "to_pydatetime"):
        close_time = close_time.to_pydatetime()
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)
    return close_time <= now


KLINE_COLUMNS = [
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


@dataclass
class BackfillResult:
    symbols: int = 0
    candles_inserted: int = 0
    open_candles_skipped: int = 0
    funding_inserted: int = 0
    open_interest_inserted: int = 0
    signals_inserted: int = 0
    flow_logs_inserted: int = 0
    skipped_duplicates: int = 0
    errors: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbols": self.symbols,
            "candles_inserted": self.candles_inserted,
            "open_candles_skipped": self.open_candles_skipped,
            "funding_inserted": self.funding_inserted,
            "open_interest_inserted": self.open_interest_inserted,
            "signals_inserted": self.signals_inserted,
            "flow_logs_inserted": self.flow_logs_inserted,
            "skipped_duplicates": self.skipped_duplicates,
            "errors": self.errors,
        }


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso_from_ms(value: Any) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _request_with_retry(
    scanner: BinanceFuturesScanner,
    path: str,
    params: Dict[str, Any],
    retries: int = 3,
) -> Any:
    last_error = None
    for attempt in range(retries):
        try:
            return scanner._get(path, params=params)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1 + attempt)
    raise last_error


def _fetch_klines(
    scanner: BinanceFuturesScanner,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    rate_limit_seconds: float,
) -> pd.DataFrame:
    rows: List[List[Any]] = []
    cursor = _to_ms(start)
    end_ms = _to_ms(end)

    while cursor < end_ms:
        batch = _request_with_retry(
            scanner,
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(rate_limit_seconds)

    if not rows:
        return pd.DataFrame(columns=KLINE_COLUMNS)

    df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
    ]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.drop_duplicates(subset=["open_time"]).sort_values("open_time")


def _fetch_funding_history(
    scanner: BinanceFuturesScanner,
    symbol: str,
    start: datetime,
    end: datetime,
    rate_limit_seconds: float,
) -> List[Dict[str, Any]]:
    try:
        rows = _request_with_retry(
            scanner,
            "/fapi/v1/fundingRate",
            {
                "symbol": symbol,
                "startTime": _to_ms(start),
                "endTime": _to_ms(end),
                "limit": 1000,
            },
        )
        time.sleep(rate_limit_seconds)
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(f"  funding fallback OHLCV-only {symbol}: {exc}")
        return []


def _fetch_open_interest_history(
    scanner: BinanceFuturesScanner,
    symbol: str,
    period: str,
    start: datetime,
    end: datetime,
    rate_limit_seconds: float,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cursor = _to_ms(start)
    end_ms = _to_ms(end)
    try:
        while cursor < end_ms:
            batch = _request_with_retry(
                scanner,
                "/futures/data/openInterestHist",
                {
                    "symbol": symbol,
                    "period": period,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 500,
                },
            )
            if not batch:
                break
            rows.extend(batch)
            latest_time = int(batch[-1].get("timestamp", cursor))
            next_cursor = latest_time + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            time.sleep(rate_limit_seconds)
    except Exception as exc:
        print(f"  open interest fallback OHLCV-only {symbol}: {exc}")
    return rows


def _nearest_metric(rows: List[Dict[str, Any]], timestamp_ms: int, key: str) -> float:
    value = 0.0
    for row in rows:
        row_time = int(row.get("fundingTime") or row.get("timestamp") or 0)
        if row_time <= timestamp_ms:
            value = _safe_float(row.get(key))
        else:
            break
    return value


def _funding_zscore(rows: List[Dict[str, Any]], timestamp_ms: int) -> float:
    values = [
        _safe_float(row.get("fundingRate"))
        for row in rows
        if int(row.get("fundingTime") or 0) <= timestamp_ms
    ][-24:]
    if len(values) < 3:
        return 0.0
    latest = values[-1]
    prior = values[:-1]
    mean = sum(prior) / len(prior)
    variance = sum((value - mean) ** 2 for value in prior) / len(prior)
    std = variance ** 0.5
    return 0.0 if std == 0 else (latest - mean) / std


def _oi_expansion(rows: List[Dict[str, Any]], timestamp_ms: int) -> float:
    values = [
        _safe_float(row.get("sumOpenInterest"))
        for row in rows
        if int(row.get("timestamp") or 0) <= timestamp_ms
    ][-12:]
    if len(values) < 2 or not values[0]:
        return 0.0
    return ((values[-1] - values[0]) / values[0]) * 100


def _build_flow(signal: Dict[str, Any], candles: pd.DataFrame, funding_rows: List[Dict[str, Any]], oi_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = candles.iloc[-1]
    timestamp_ms = int(latest["close_time"].timestamp() * 1000)
    volume = _safe_float(latest.get("volume"))
    taker_buy = _safe_float(latest.get("taker_buy_base_asset_volume"))
    taker_sell = max(volume - taker_buy, 0.0)
    taker_delta = ((taker_buy - taker_sell) / volume) if volume else 0.0
    volume_spike = _safe_float(signal.get("volume_spike"))
    candle_return = ((latest["close"] - latest["open"]) / latest["open"] * 100) if latest["open"] else 0.0
    oi_expansion_rate = _oi_expansion(oi_rows, timestamp_ms)
    funding_zscore = _funding_zscore(funding_rows, timestamp_ms)
    pressure_score = max(0.0, min(100.0, 50 + taker_delta * 35 + max(min(oi_expansion_rate, 10), -10) * 1.5 + max(min(volume_spike - 1, 3), 0) * 5 + max(min(candle_return, 4), -4) * 2))
    squeeze_probability = 0.0
    if abs(funding_zscore) > 1.5:
        squeeze_probability += 25
    if abs(taker_delta) > 0.25:
        squeeze_probability += 25
    if volume_spike >= 2:
        squeeze_probability += 25
    if abs(oi_expansion_rate) >= 3:
        squeeze_probability += 25
    squeeze_probability = min(100.0, squeeze_probability)
    whale_activity = "WHALE ACCUMULATION" if pressure_score >= 70 and taker_delta > 0 else "WHALE DISTRIBUTION" if pressure_score <= 30 and taker_delta < 0 else "NORMAL"
    return {
        "timestamp": signal["timestamp"],
        "symbol": signal["symbol"],
        "funding_zscore": funding_zscore,
        "oi_expansion_rate": oi_expansion_rate,
        "taker_delta": taker_delta,
        "pressure_score": pressure_score,
        "squeeze_probability": squeeze_probability,
        "flow_state": whale_activity if whale_activity != "NORMAL" else "NEUTRAL FLOW",
        "whale_activity": whale_activity,
        "squeeze_risk": "HIGH" if squeeze_probability >= 70 else "MODERATE" if squeeze_probability >= 45 else "LOW",
        "funding_warning": "CROWDED" if abs(_safe_float(signal.get("funding"))) > 0.0007 else "",
        "flow_adjustment": 0,
        "final_score": signal.get("score", 0),
    }


def _insert_ignore(connection: Any, table: str, data: Dict[str, Any]) -> bool:
    columns = list(data.keys())
    placeholders = ", ".join(["?"] * len(columns))
    before = connection.total_changes
    connection.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [data[column] for column in columns],
    )
    return connection.total_changes > before


def _insert_signal_if_missing(connection: Any, signal: Dict[str, Any]) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM signals WHERE timestamp = ? AND symbol = ? LIMIT 1",
        (signal.get("timestamp"), signal.get("symbol")),
    ).fetchone()
    if exists:
        return False
    columns = [
        "timestamp",
        "symbol",
        "price",
        "score",
        "base_score",
        "volume_spike",
        "breakout",
        "liquidity_sweep",
        "taker_buy_ratio",
        "funding",
        "open_interest",
        "data_source",
    ]
    row = {
        "timestamp": signal.get("timestamp"),
        "symbol": signal.get("symbol"),
        "price": signal.get("price"),
        "score": signal.get("score"),
        "base_score": signal.get("score"),
        "volume_spike": signal.get("volume_spike"),
        "breakout": int(bool(signal.get("breakout"))),
        "liquidity_sweep": int(bool(signal.get("liquidity_sweep"))),
        "taker_buy_ratio": signal.get("taker_buy_ratio"),
        "funding": signal.get("funding"),
        "open_interest": signal.get("open_interest"),
        "data_source": "HISTORICAL_BACKFILL",
    }
    connection.execute(
        f"INSERT INTO signals ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
        [row[column] for column in columns],
    )
    return True


def _insert_flow_if_missing(connection: Any, flow: Dict[str, Any]) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM flow_logs WHERE timestamp = ? AND symbol = ? LIMIT 1",
        (flow.get("timestamp"), flow.get("symbol")),
    ).fetchone()
    if exists:
        return False
    flow = {**flow, "data_source": "HISTORICAL_BACKFILL"}
    columns = list(flow.keys())
    connection.execute(
        f"INSERT INTO flow_logs ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
        [flow[column] for column in columns],
    )
    return True


def run_historical_backfill(
    days: int,
    database_url: str,
    base_url: str,
    interval: str = "15m",
    top_symbols_limit: int = 30,
    min_quote_volume: float = 0.0,
    timeout: int = 15,
    rate_limit_seconds: float = 0.2,
) -> Dict[str, Any]:
    from infrastructure_capacity import assert_heavy_job_allowed
    assert_heavy_job_allowed(db_path=database_url or "mamuyy_hunter.db")
    init_db(database_url)
    scanner = BinanceFuturesScanner(base_url=base_url, timeout=timeout)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(days, 1))
    result = BackfillResult()

    print(f"Backfill historical Binance Futures {interval}: {start.isoformat()} -> {end.isoformat()}")
    symbols = scanner.get_top_usdt_symbols(limit=top_symbols_limit, min_quote_volume=min_quote_volume)
    result.symbols = len(symbols)
    print(f"Symbols selected: {len(symbols)}")

    with get_connection(database_url) as connection:
        for index, symbol in enumerate(symbols, start=1):
            print(f"[{index}/{len(symbols)}] Backfill {symbol}")
            try:
                candles = _fetch_klines(scanner, symbol, interval, start, end, rate_limit_seconds)
                open_mask = candles["close_time"].apply(lambda close_time: not _is_closed_candle(close_time, end)) if not candles.empty else []
                skipped_open = int(open_mask.sum()) if len(open_mask) else 0
                if skipped_open:
                    result.open_candles_skipped += skipped_open
                    print(f"  skipped unfinished candles={skipped_open}")
                    candles = candles.loc[~open_mask].copy()
                funding_rows = _fetch_funding_history(scanner, symbol, start, end, rate_limit_seconds)
                oi_rows = _fetch_open_interest_history(scanner, symbol, interval, start, end, rate_limit_seconds)

                for _, candle in candles.iterrows():
                    inserted = _insert_ignore(
                        connection,
                        "historical_klines",
                        {
                            "timestamp": candle["close_time"].isoformat(),
                            "symbol": symbol,
                            "interval": interval,
                            "open": float(candle["open"]),
                            "high": float(candle["high"]),
                            "low": float(candle["low"]),
                            "close": float(candle["close"]),
                            "volume": float(candle["volume"]),
                            "quote_asset_volume": float(candle["quote_asset_volume"]),
                            "number_of_trades": float(candle["number_of_trades"]),
                            "taker_buy_base_asset_volume": float(candle["taker_buy_base_asset_volume"]),
                            "taker_buy_quote_asset_volume": float(candle["taker_buy_quote_asset_volume"]),
                        },
                    )
                    result.candles_inserted += int(inserted)
                    result.skipped_duplicates += int(not inserted)

                for row in funding_rows:
                    inserted = _insert_ignore(
                        connection,
                        "historical_funding",
                        {
                            "timestamp": _iso_from_ms(row.get("fundingTime")),
                            "symbol": symbol,
                            "funding_rate": _safe_float(row.get("fundingRate")),
                        },
                    )
                    result.funding_inserted += int(inserted)

                for row in oi_rows:
                    inserted = _insert_ignore(
                        connection,
                        "historical_open_interest",
                        {
                            "timestamp": _iso_from_ms(row.get("timestamp")),
                            "symbol": symbol,
                            "open_interest": _safe_float(row.get("sumOpenInterest")),
                        },
                    )
                    result.open_interest_inserted += int(inserted)

                for candle_index in range(20, len(candles)):
                    window = candles.iloc[candle_index - 20 : candle_index + 1].copy()
                    latest = window.iloc[-1]
                    timestamp_ms = int(latest["close_time"].timestamp() * 1000)
                    funding_rate = _nearest_metric(funding_rows, timestamp_ms, "fundingRate")
                    open_interest = _nearest_metric(oi_rows, timestamp_ms, "sumOpenInterest")
                    signal = scanner.calculate_signal(symbol, window, funding_rate, open_interest)
                    signal["timestamp"] = latest["close_time"].isoformat()
                    inserted_signal = _insert_signal_if_missing(connection, signal)
                    result.signals_inserted += int(inserted_signal)
                    result.skipped_duplicates += int(not inserted_signal)

                    flow = _build_flow(signal, window, funding_rows, oi_rows)
                    if _insert_flow_if_missing(connection, flow):
                        result.flow_logs_inserted += 1
                    else:
                        result.skipped_duplicates += 1

                connection.commit()
                print(
                    f"  candles={len(candles)} funding={len(funding_rows)} "
                    f"oi={len(oi_rows)} signals_inserted={result.signals_inserted}"
                )
                time.sleep(rate_limit_seconds)
            except Exception as exc:
                connection.rollback()
                result.errors += 1
                print(f"  gagal backfill {symbol}: {exc}")

    print(f"Backfill selesai: {result.as_dict()}")
    return result.as_dict()
