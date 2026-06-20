import os


def operational_kline_interval() -> str:
    """Resolve the single operational kline interval shared by sync, freshness, and validation."""
    return (
        os.getenv("OPERATIONAL_KLINE_INTERVAL")
        or os.getenv("CANDLE_INTERVAL")
        or os.getenv("DATA_SYNC_INTERVAL")
        or "15m"
    ).strip()


def interval_minutes(interval: str | None = None) -> int:
    value = (interval or operational_kline_interval()).strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    if value.endswith("h"):
        return int(value[:-1]) * 60
    return 15
