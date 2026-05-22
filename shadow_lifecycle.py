from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple


_TERMINAL = {"TRADE CLOSED", "CLOSED", "WIN", "LOSS", "EXPIRED", "IGNORED", "PROFIT_MATURED"}


@dataclass(frozen=True)
class ShadowLifecycleConfig:
    max_shadow_age_minutes: int = 240
    inactivity_timeout_minutes: int = 90
    stale_regime_decay_minutes: int = 45
    negative_pnl_accelerated_expiry: bool = True
    negative_pnl_expiry_multiplier: float = 0.6
    positive_exit_enabled: bool = True
    take_profit_pct: float = 1.5
    profit_maturity_minutes: int = 30



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

    pnl_percent = _num(row["pnl_percent"], 0.0)
    if cfg.positive_exit_enabled and pnl_percent >= cfg.take_profit_pct and age_min >= cfg.profit_maturity_minutes:
        return "PROFIT_MATURED"

    latest_symbol_ts = latest_ts.get(symbol)
    inactive_min = (now - latest_symbol_ts).total_seconds() / 60 if latest_symbol_ts else age_min

    expiry_age = float(cfg.max_shadow_age_minutes)
    if cfg.negative_pnl_accelerated_expiry and pnl_percent < 0:
        expiry_age *= max(0.1, cfg.negative_pnl_expiry_multiplier)

    if age_min >= expiry_age or inactive_min >= cfg.inactivity_timeout_minutes:
        return "EXPIRED"
    if age_min >= cfg.stale_regime_decay_minutes:
        return "STALE"
    return "ACTIVE"


def _row_sort_key(row: sqlite3.Row) -> Tuple[datetime, int]:
    ts = _parse_ts(row["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc)
    try:
        rid = int(row["id"])
    except (TypeError, ValueError):
        rid = -1
    return ts, rid


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
        positive_exit_enabled=os.getenv("SHADOW_POSITIVE_EXIT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        take_profit_pct=_f("SHADOW_TAKE_PROFIT_PCT", 1.5),
        profit_maturity_minutes=_i("SHADOW_PROFIT_MATURITY_MINUTES", 30),
    )


def shadow_lifecycle_audit(db_path: str = "mamuyy_hunter.db", cfg: ShadowLifecycleConfig | None = None) -> Dict[str, Any]:
    cfg = cfg or load_shadow_lifecycle_config_from_env()
    now = datetime.now(timezone.utc)
    result: Dict[str, Any] = {
        "active_count": 0,
        "stale_count": 0,
        "expired_count": 0,
        "profit_matured_count": 0,
        "oldest_shadow_age_minutes": 0.0,
        "stuck_symbols": [],
        "profit_matured_symbols": [],
        "total_rows": 0,
        "legacy_active_latest_row_count": 0,
        "active_after_profit_matured_filter_count": 0,
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
        if evaluated == "PROFIT_MATURED":
            sym = str(r["symbol"] or "UNKNOWN").strip() or "UNKNOWN"
            result["profit_matured_symbols"].append(sym)
        ts = _parse_ts(r["timestamp"])
        if ts:
            age = max(0.0, (now - ts).total_seconds() / 60)
            result["oldest_shadow_age_minutes"] = max(result["oldest_shadow_age_minutes"], age)
            ages.append((str(r["symbol"] or "UNKNOWN"), age))

    result["total_rows"] = len(rows)

    legacy_latest_rows: Dict[str, sqlite3.Row] = {}
    latest_rows_by_ts_id: Dict[str, sqlite3.Row] = {}
    latest_state_by_symbol: Dict[str, str] = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym:
            continue
        legacy_latest_rows[sym] = r
        state = _evaluate_status(r, latest_by_symbol, now, cfg)
        existing = latest_rows_by_ts_id.get(sym)
        if existing is None or _row_sort_key(r) >= _row_sort_key(existing):
            latest_rows_by_ts_id[sym] = r
            latest_state_by_symbol[sym] = state

    result["legacy_active_latest_row_count"] = sum(1 for r in legacy_latest_rows.values() if _evaluate_status(r, latest_by_symbol, now, cfg) == "ACTIVE")
    result["active_after_profit_matured_filter_count"] = sum(1 for st in latest_state_by_symbol.values() if st == "ACTIVE")

    ages.sort(key=lambda item: item[1], reverse=True)
    result["stuck_symbols"] = [f"{sym}:{age:.1f}m" for sym, age in ages[:5]]
    result["profit_matured_symbols"] = sorted(set(result["profit_matured_symbols"]))
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

    latest_ts_by_symbol: Dict[str, datetime] = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym:
            continue
        ts = _parse_ts(r["timestamp"])
        if ts and (sym not in latest_ts_by_symbol or ts > latest_ts_by_symbol[sym]):
            latest_ts_by_symbol[sym] = ts

    latest_row_by_symbol: Dict[str, sqlite3.Row] = {}
    latest_state_by_symbol: Dict[str, str] = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym:
            continue
        state = _evaluate_status(r, latest_ts_by_symbol, now, cfg)
        existing = latest_row_by_symbol.get(sym)
        if existing is None or _row_sort_key(r) >= _row_sort_key(existing):
            latest_row_by_symbol[sym] = r
            latest_state_by_symbol[sym] = state

    active: List[Dict[str, Any]] = []
    for sym, r in latest_row_by_symbol.items():
        if latest_state_by_symbol.get(sym) != "ACTIVE":
            continue
        active.append({"symbol": sym, "pnl_percent": _num(r["pnl_percent"], 0.0), "lifecycle_status": "ACTIVE"})
    return active
