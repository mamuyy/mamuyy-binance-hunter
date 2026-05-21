from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


_TERMINAL = {"TRADE CLOSED", "CLOSED", "WIN", "LOSS", "EXPIRED", "IGNORED"}


@dataclass(frozen=True)
class ShadowLifecycleConfig:
    max_shadow_age_minutes: int = 240
    inactivity_timeout_minutes: int = 90
    stale_regime_decay_minutes: int = 45
    negative_pnl_accelerated_expiry: bool = True
    negative_pnl_expiry_multiplier: float = 0.6



def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _evaluate_status(row: sqlite3.Row, latest_ts: Dict[str, datetime], now: datetime, cfg: ShadowLifecycleConfig) -> str:
    status = str(row["lifecycle_status"] or "").upper().strip()
    if status in _TERMINAL:
        return "IGNORED" if status == "IGNORED" else "EXPIRED"

    symbol = str(row["symbol"] or "").strip()
    created = _parse_ts(row["timestamp"])
    if created is None:
        return "STALE"

    age_min = (now - created).total_seconds() / 60
    if age_min < 0:
        age_min = 0

    latest_symbol_ts = latest_ts.get(symbol)
    inactive_min = (now - latest_symbol_ts).total_seconds() / 60 if latest_symbol_ts else age_min

    expiry_age = float(cfg.max_shadow_age_minutes)
    if cfg.negative_pnl_accelerated_expiry and _num(row["pnl_percent"], 0.0) < 0:
        expiry_age *= max(0.1, cfg.negative_pnl_expiry_multiplier)

    if age_min >= expiry_age or inactive_min >= cfg.inactivity_timeout_minutes:
        return "EXPIRED"
    if age_min >= cfg.stale_regime_decay_minutes:
        return "STALE"
    return "ACTIVE"


def load_shadow_lifecycle_config_from_env() -> ShadowLifecycleConfig:
    def _i(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default

    return ShadowLifecycleConfig(
        max_shadow_age_minutes=_i("SHADOW_MAX_AGE_MINUTES", 240),
        inactivity_timeout_minutes=_i("SHADOW_INACTIVITY_TIMEOUT_MINUTES", 90),
        stale_regime_decay_minutes=_i("SHADOW_STALE_REGIME_DECAY_MINUTES", 45),
        negative_pnl_accelerated_expiry=os.getenv("SHADOW_NEGATIVE_PNL_ACCELERATED_EXPIRY", "true").strip().lower() in {"1", "true", "yes", "on"},
        negative_pnl_expiry_multiplier=_f("SHADOW_NEGATIVE_PNL_EXPIRY_MULTIPLIER", 0.6),
    )


def shadow_lifecycle_audit(db_path: str = "mamuyy_hunter.db", cfg: ShadowLifecycleConfig | None = None) -> Dict[str, Any]:
    cfg = cfg or load_shadow_lifecycle_config_from_env()
    now = datetime.now(timezone.utc)
    result: Dict[str, Any] = {
        "active_count": 0,
        "stale_count": 0,
        "expired_count": 0,
        "oldest_shadow_age_minutes": 0.0,
        "stuck_symbols": [],
        "total_rows": 0,
    }
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id,symbol,timestamp,lifecycle_status,pnl_percent FROM shadow_trades ORDER BY id ASC").fetchall()
    except sqlite3.Error:
        return result
    if not rows:
        return result

    latest_by_symbol: Dict[str, datetime] = {}
    for r in rows:
        ts = _parse_ts(r["timestamp"])
        sym = str(r["symbol"] or "").strip()
        if ts and sym and (sym not in latest_by_symbol or ts > latest_by_symbol[sym]):
            latest_by_symbol[sym] = ts

    ages: List[tuple[str, float]] = []
    for r in rows:
        evaluated = _evaluate_status(r, latest_by_symbol, now, cfg)
        result[f"{evaluated.lower()}_count"] = int(result.get(f"{evaluated.lower()}_count", 0)) + 1
        ts = _parse_ts(r["timestamp"])
        if ts:
            age = max(0.0, (now - ts).total_seconds() / 60)
            result["oldest_shadow_age_minutes"] = max(result["oldest_shadow_age_minutes"], age)
            ages.append((str(r["symbol"] or "UNKNOWN"), age))

    result["total_rows"] = len(rows)
    ages.sort(key=lambda item: item[1], reverse=True)
    result["stuck_symbols"] = [f"{sym}:{age:.1f}m" for sym, age in ages[:5]]
    return result


def active_shadow_positions(db_path: str = "mamuyy_hunter.db", cfg: ShadowLifecycleConfig | None = None) -> List[Dict[str, Any]]:
    cfg = cfg or load_shadow_lifecycle_config_from_env()
    now = datetime.now(timezone.utc)
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT id,symbol,timestamp,lifecycle_status,pnl_percent
                FROM shadow_trades
                WHERE symbol IS NOT NULL AND symbol != ''
                ORDER BY id ASC
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    if not rows:
        return []

    latest_rows: Dict[str, sqlite3.Row] = {}
    latest_ts: Dict[str, datetime] = {}
    for r in rows:
        sym = str(r["symbol"])
        latest_rows[sym] = r
        ts = _parse_ts(r["timestamp"])
        if ts:
            latest_ts[sym] = ts

    active: List[Dict[str, Any]] = []
    for sym, r in latest_rows.items():
        state = _evaluate_status(r, latest_ts, now, cfg)
        if state != "ACTIVE":
            continue
        active.append({"symbol": sym, "pnl_percent": _num(r["pnl_percent"], 0.0), "lifecycle_status": "ACTIVE"})
    return active
