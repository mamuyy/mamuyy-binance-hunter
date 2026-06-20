import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from json_utils import atomic_write_json

DEFAULT_CACHE_PATH = Path("reports/binance_futures_exchange_info_cache.json")


@dataclass(frozen=True)
class ExchangeInfoResult:
    exchange_info: dict[str, Any] | None
    reason: str | None
    cache_status: str
    metadata: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _ttl_minutes(ttl_minutes: int | None = None) -> int:
    if ttl_minutes is not None:
        return ttl_minutes
    try:
        return int(os.getenv("EXCHANGE_INFO_CACHE_TTL_MINUTES", "1440"))
    except ValueError:
        return 1440


def _validate_exchange_info(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict) and isinstance(data.get("symbols"), list):
        return data
    return None


def _cache_payload(data: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "cached_at": _now().isoformat(),
        "source": source,
        "cache_schema": "binance_futures_exchange_info_v1",
        "exchange_info": data,
    }


def write_exchange_info_cache(data: dict[str, Any], cache_path: str | Path = DEFAULT_CACHE_PATH, source: str = "binance_futures_public_api") -> None:
    atomic_write_json(cache_path, _cache_payload(data, source))


def load_exchange_info_cache(cache_path: str | Path = DEFAULT_CACHE_PATH, ttl_minutes: int | None = None) -> ExchangeInfoResult:
    path = Path(cache_path)
    if not path.exists():
        return ExchangeInfoResult(None, "EXCHANGE_INFO_CACHE_MISSING", "MISSING", {})
    try:
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ExchangeInfoResult(None, "EXCHANGE_INFO_CACHE_MALFORMED", "MALFORMED", {})
    if not isinstance(payload, dict) or payload.get("cache_schema") != "binance_futures_exchange_info_v1":
        return ExchangeInfoResult(None, "EXCHANGE_INFO_CACHE_MALFORMED", "MALFORMED", {})
    cached_at = _parse_ts(payload.get("cached_at"))
    data = _validate_exchange_info(payload.get("exchange_info"))
    if cached_at is None or data is None:
        return ExchangeInfoResult(None, "EXCHANGE_INFO_CACHE_MALFORMED", "MALFORMED", {"cached_at": payload.get("cached_at")})
    age = (_now() - cached_at).total_seconds() / 60
    metadata = {"cached_at": cached_at.isoformat(), "age_minutes": round(age, 2), "ttl_minutes": _ttl_minutes(ttl_minutes), "source": payload.get("source")}
    if age > _ttl_minutes(ttl_minutes):
        return ExchangeInfoResult(None, "EXCHANGE_INFO_CACHE_STALE", "STALE", metadata)
    return ExchangeInfoResult(data, None, "HIT", metadata)


def fetch_exchange_info(base_url: str, timeout: int = 15) -> dict[str, Any]:
    response = requests.get(base_url.rstrip("/") + "/fapi/v1/exchangeInfo", timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP_{response.status_code}:{getattr(response, 'text', '')[:160]}")
    data = response.json()
    valid = _validate_exchange_info(data)
    if valid is None:
        raise RuntimeError("EXCHANGE_INFO_RESPONSE_MALFORMED")
    return valid


def get_exchange_info(
    base_url: str,
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    ttl_minutes: int | None = None,
    timeout: int = 15,
    allow_network: bool = True,
) -> ExchangeInfoResult:
    if allow_network:
        try:
            data = fetch_exchange_info(base_url, timeout=timeout)
            write_exchange_info_cache(data, cache_path)
            return ExchangeInfoResult(data, None, "REFRESHED", {"cached_at": _now().isoformat(), "ttl_minutes": _ttl_minutes(ttl_minutes), "source": "binance_futures_public_api"})
        except Exception as exc:
            cache = load_exchange_info_cache(cache_path, ttl_minutes=ttl_minutes)
            if cache.exchange_info is not None:
                return cache
            return ExchangeInfoResult(None, cache.reason or "EXCHANGE_INFO_UNAVAILABLE", cache.cache_status, {**cache.metadata, "fetch_error": str(exc)})
    cache = load_exchange_info_cache(cache_path, ttl_minutes=ttl_minutes)
    if cache.exchange_info is not None:
        return cache
    return ExchangeInfoResult(None, cache.reason or "EXCHANGE_INFO_UNAVAILABLE", cache.cache_status, cache.metadata)
