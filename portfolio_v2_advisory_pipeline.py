"""Phase 3.01 fresh Portfolio Engine V2 advisory pipeline.

This module reads Portfolio V2 research artifacts, validates freshness and
allocation integrity, renders a separate advisory message, and optionally sends
that message through a manual Telegram gate. Portfolio Engine V1 and all broker
execution paths remain untouched.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

try:
    from config import config
except ImportError:  # pragma: no cover - standalone fallback
    config = SimpleNamespace(
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_enabled=False,
        request_timeout_seconds=15,
    )

REPORT_PATH = "logs/portfolio_v2_advisory_report.json"
PREVIEW_PATH = "logs/portfolio_v2_advisory_telegram_preview.json"
SEND_RESULT_PATH = "logs/portfolio_v2_advisory_send_result.json"
SEND_STATE_PATH = "logs/portfolio_v2_advisory_send_state.json"
ALLOW_SEND_ENV = "ALLOW_PORTFOLIO_V2_TELEGRAM_SEND"
DEFAULT_MAX_AGE_MINUTES = 36 * 60
DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60

ALLOCATION_PATTERNS = (
    "data/ml_portfolio_allocation_v2_*.csv",
    "data/ml_calibration_with_portfolio_allocation_*.csv",
    "logs/portfolio_v2_allocation*.csv",
    "logs/portfolio_v2_allocation*.json",
    "reports/portfolio_v2_allocation*.json",
)
HEALTH_PATTERNS = (
    "logs/*portfolio*health*.json",
    "reports/*portfolio*health*.json",
    "logs/phase5c*.json",
    "reports/phase5c*.json",
)
REBALANCING_PATTERNS = (
    "logs/*rebalanc*.json",
    "logs/*rebalanc*.csv",
    "reports/*rebalanc*.json",
    "reports/*rebalanc*.csv",
    "logs/phase5d*.json",
    "reports/phase5d*.json",
)

SYMBOL_COLUMNS = ("symbol", "ticker", "asset")
ALLOCATION_COLUMNS = (
    "capital_pct_v2",
    "allocation_pct_v2",
    "allocation_pct",
    "allocation",
    "weight_pct",
    "weight",
)
PERCENT_POINT_ALLOCATION_COLUMNS = {
    "capital_pct_v2",
    "allocation_pct_v2",
    "allocation_pct",
    "weight_pct",
}
FRACTION_ALLOCATION_COLUMNS = {"allocation", "weight"}
EV_COLUMNS = ("ev_pct", "expected_value", "expected_value_pct", "ev")
WINRATE_COLUMNS = ("winrate", "win_rate", "wr", "historical_winrate")
ACTION_COLUMNS = ("action", "recommendation", "rebalance_action", "bucket", "decision")
MIN_VALID_SYMBOLS = 5
MIN_ROWS_FOR_TOTAL_SANITY = 15
TOTAL_ALLOCATION_MIN = 95.0
TOTAL_ALLOCATION_MAX = 105.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def read_csv(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    for separator in ("/", "-", "_", " "):
        text = text.replace(separator, "")
    return text


def first_named_value(row: Dict[str, Any], names: Iterable[str]) -> Tuple[Optional[str], Any]:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return name.lower(), value
    return None, None


def first_value(row: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    _name, value = first_named_value(row, names)
    return default if value in (None, "") else value


def normalize_fraction_or_percent(value: Any) -> Optional[float]:
    number = safe_float(value)
    if number is None:
        return None
    if 0 < abs(number) <= 1:
        return number * 100
    return number


def allocation_percent_from_row(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    column, raw = first_named_value(row, ALLOCATION_COLUMNS)
    number = safe_float(raw)
    if column is None or number is None:
        return None, column
    if column in PERCENT_POINT_ALLOCATION_COLUMNS:
        return number, column
    if column in FRACTION_ALLOCATION_COLUMNS and 0 < abs(number) <= 1:
        return number * 100, column
    return number, column


def source_metadata(path: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or utc_now()
    if not path or not os.path.exists(path):
        return {"path": path, "available": False, "modified_at": None, "age_minutes": None}
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return {
        "path": path,
        "available": True,
        "modified_at": modified.replace(microsecond=0).isoformat(),
        "age_minutes": round(max(0.0, (current - modified).total_seconds() / 60.0), 2),
        "size_bytes": os.path.getsize(path),
    }


def discover_latest(patterns: Sequence[str]) -> Optional[str]:
    candidates: List[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    files = [path for path in candidates if os.path.isfile(path)]
    return max(files, key=lambda path: (os.path.getmtime(path), path)) if files else None


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "allocations",
        "allocation",
        "rows",
        "data",
        "symbols",
        "portfolio",
        "recommendations",
        "rebalancing",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if first_value(payload, SYMBOL_COLUMNS) not in (None, ""):
        return [payload]
    return []


def load_records(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    return read_csv(path) if Path(path).suffix.lower() == ".csv" else extract_records(read_json(path))


def canonicalize_allocations(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen: Dict[str, int] = {}
    for index, row in enumerate(rows, 1):
        symbol = normalize_symbol(first_value(row, SYMBOL_COLUMNS))
        allocation, allocation_column = allocation_percent_from_row(row)
        if not symbol:
            errors.append(f"row {index}: symbol missing")
            continue
        if allocation is None:
            errors.append(f"row {index} {symbol}: allocation missing or invalid")
            continue
        if allocation < 0 or allocation > 100:
            errors.append(f"row {index} {symbol}: allocation {allocation} outside 0..100")
            continue
        seen[symbol] = seen.get(symbol, 0) + 1
        records.append(
            {
                "symbol": symbol,
                "allocation_pct": round(allocation, 6),
                "allocation_source_column": allocation_column,
                "expected_value": safe_float(first_value(row, EV_COLUMNS)),
                "winrate_pct": normalize_fraction_or_percent(first_value(row, WINRATE_COLUMNS)),
                "position_multiplier": safe_float(first_value(row, ("position_multiplier", "size_multiplier"))),
                "allocation_score": safe_float(first_value(row, ("allocation_score_v2", "allocation_score"))),
            }
        )
    duplicates = sorted(symbol for symbol, count in seen.items() if count > 1)
    if duplicates:
        errors.append("duplicate normalized symbols: " + ", ".join(duplicates))
    records.sort(key=lambda item: (-item["allocation_pct"], item["symbol"]))
    return records, errors


def allocation_total(allocations: Sequence[Dict[str, Any]]) -> float:
    return round(sum(max(0.0, float(item["allocation_pct"])) for item in allocations), 6)


def derived_health(allocations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    positives = [max(0.0, float(item["allocation_pct"])) for item in allocations]
    total = sum(positives)
    largest = max(positives, default=0.0)
    normalized = [value / total for value in positives if total > 0 and value > 0]
    hhi = sum(weight * weight for weight in normalized)
    effective_assets = (1.0 / hhi) if hhi > 0 else 0.0
    target_assets = min(max(len(normalized), 1), 15)
    diversification = min(100.0, (effective_assets / target_assets) * 100.0) if normalized else 0.0
    healthy_total = TOTAL_ALLOCATION_MIN <= total <= TOTAL_ALLOCATION_MAX
    if allocations and healthy_total and largest <= 15.0 and diversification >= 55.0:
        status = "GREEN"
    elif allocations and total > 0 and largest <= 25.0:
        status = "YELLOW"
    else:
        status = "RED"
    largest_record = max(allocations, key=lambda item: item["allocation_pct"], default=None)
    return {
        "portfolio_health": status,
        "risk_score": round(largest, 2),
        "diversification_score": round(diversification, 2),
        "largest_exposure_symbol": largest_record["symbol"] if largest_record else None,
        "largest_exposure_pct": round(largest, 2),
        "total_allocation_pct": round(total, 2),
        "active_symbols": len(normalized),
        "health_source": "DERIVED_FROM_ALLOCATION",
    }


def source_is_fresh(meta: Dict[str, Any], max_age_minutes: int) -> bool:
    age = meta.get("age_minutes")
    return bool(meta.get("available") and age is not None and float(age) <= max_age_minutes)


def parse_health(
    path: Optional[str],
    allocations: Sequence[Dict[str, Any]],
    meta: Dict[str, Any],
    max_age_minutes: int,
) -> Dict[str, Any]:
    derived = derived_health(allocations)
    if not source_is_fresh(meta, max_age_minutes):
        return {**derived, "health_source": "DERIVED_FROM_ALLOCATION_STALE_OR_MISSING_EXPLICIT_SOURCE"}
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return derived
    status = str(
        first_value(payload, ("portfolio_health", "health", "health_status", "status"), derived["portfolio_health"])
    ).upper()
    if status not in {"GREEN", "YELLOW", "RED"}:
        status = derived["portfolio_health"]
    risk_value = safe_float(first_value(payload, ("risk_score", "portfolio_risk_score")))
    diversification_value = safe_float(first_value(payload, ("diversification", "diversification_score")))
    largest_value = safe_float(first_value(payload, ("largest_exposure_pct", "largest_weight_pct")))
    return {
        **derived,
        "portfolio_health": status,
        "risk_score": risk_value if risk_value is not None else derived["risk_score"],
        "diversification_score": (
            diversification_value if diversification_value is not None else derived["diversification_score"]
        ),
        "largest_exposure_symbol": normalize_symbol(
            first_value(payload, ("largest_exposure", "largest_exposure_symbol", "largest_symbol"))
        )
        or derived["largest_exposure_symbol"],
        "largest_exposure_pct": largest_value if largest_value is not None else derived["largest_exposure_pct"],
        "health_source": path,
    }


def action_name(value: Any) -> str:
    text = str(value or "").strip().upper().replace("_", " ")
    aliases = {
        "BUY": "BUY MORE",
        "ADD": "BUY MORE",
        "INCREASE": "BUY MORE",
        "ACCUMULATE": "BUY MORE",
        "TRIM": "REDUCE",
        "DECREASE": "REDUCE",
        "SELL": "REMOVE",
        "AVOID": "REMOVE",
        "DROP": "REMOVE",
    }
    return aliases.get(text, text)


def parse_explicit_rebalancing(path: Optional[str]) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    if not path:
        return None
    payload = read_json(path) if Path(path).suffix.lower() == ".json" else None
    buckets = {"BUY MORE": [], "REDUCE": [], "REMOVE": []}
    if isinstance(payload, dict):
        key_aliases = {
            "BUY MORE": ("buy_more", "buy more", "increase", "accumulate"),
            "REDUCE": ("reduce", "trim", "decrease"),
            "REMOVE": ("remove", "avoid", "sell"),
        }
        matched = False
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for target, aliases in key_aliases.items():
            for alias in aliases:
                values = lowered.get(alias)
                if not isinstance(values, list):
                    continue
                matched = True
                for value in values:
                    if isinstance(value, dict):
                        symbol = normalize_symbol(first_value(value, SYMBOL_COLUMNS))
                        allocation, _column = allocation_percent_from_row(value)
                    else:
                        symbol = normalize_symbol(value)
                        allocation = None
                    if symbol:
                        buckets[target].append({"symbol": symbol, "allocation_pct": allocation})
        if matched:
            return buckets
    rows = load_records(path)
    matched = False
    for row in rows:
        action = action_name(first_value(row, ACTION_COLUMNS))
        if action not in buckets:
            continue
        symbol = normalize_symbol(first_value(row, SYMBOL_COLUMNS))
        if not symbol:
            continue
        allocation, _column = allocation_percent_from_row(row)
        matched = True
        buckets[action].append({"symbol": symbol, "allocation_pct": allocation})
    return buckets if matched else None


def derived_rebalancing(allocations: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    positives = [item for item in allocations if item["allocation_pct"] > 0]
    zeros = [item for item in allocations if item["allocation_pct"] <= 0]
    buy_more = positives[:5]
    remaining = [item for item in positives if item not in buy_more]
    reduce = sorted(remaining, key=lambda item: (item["allocation_pct"], item["symbol"]))[:5]
    return {
        "BUY MORE": [dict(item) for item in buy_more],
        "REDUCE": [dict(item) for item in reduce],
        "REMOVE": [dict(item) for item in zeros],
    }


def build_rebalancing(
    path: Optional[str],
    allocations: Sequence[Dict[str, Any]],
    meta: Dict[str, Any],
    max_age_minutes: int,
) -> Tuple[Dict[str, Any], str]:
    if source_is_fresh(meta, max_age_minutes):
        explicit = parse_explicit_rebalancing(path)
        if explicit is not None:
            return explicit, str(path)
    return derived_rebalancing(allocations), "DERIVED_FROM_ALLOCATION_STALE_OR_MISSING_EXPLICIT_SOURCE"


def env_true(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "y", "on"}


def execution_gates_safe() -> Tuple[bool, List[str]]:
    forbidden = (
        "REAL_BINANCE_ENABLED",
        "ALLOW_REAL_BINANCE_ORDER",
        "ALLOW_AUTO_TESTNET_ORDER",
        "ALLOW_TESTNET_ORDER",
        "ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP",
        "ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE",
    )
    active = [name for name in forbidden if env_true(name)]
    return not active, active


def validate_sources(
    allocation_meta: Dict[str, Any],
    allocation_rows: Sequence[Dict[str, Any]],
    allocation_errors: Sequence[str],
    max_age_minutes: int,
) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    if not allocation_meta.get("available"):
        reasons.append("Portfolio V2 allocation source not found")
    if allocation_meta.get("available") and allocation_meta.get("age_minutes") is not None:
        if float(allocation_meta["age_minutes"]) > max_age_minutes:
            reasons.append(
                f"allocation source stale: {allocation_meta['age_minutes']} minutes old; limit {max_age_minutes}"
            )
    if len(allocation_rows) < MIN_VALID_SYMBOLS:
        reasons.append(
            f"allocation source has only {len(allocation_rows)} valid symbols; minimum {MIN_VALID_SYMBOLS}"
        )
    reasons.extend(allocation_errors)
    total = allocation_total(allocation_rows)
    if len(allocation_rows) >= MIN_ROWS_FOR_TOTAL_SANITY and not (
        TOTAL_ALLOCATION_MIN <= total <= TOTAL_ALLOCATION_MAX
    ):
        reasons.append(
            f"allocation total {total:.2f}% outside {TOTAL_ALLOCATION_MIN:.0f}..{TOTAL_ALLOCATION_MAX:.0f}%"
        )
    if any("stale:" in reason for reason in reasons):
        return "BLOCKED_STALE_DATA", reasons
    if reasons:
        return "BLOCKED_DATA_QUALITY", reasons
    return "READY", []


def format_pct(value: Any, digits: int = 2) -> str:
    number = safe_float(value)
    return "N/A" if number is None else f"{number:.{digits}f}%"


def format_number(value: Any, digits: int = 4) -> str:
    number = safe_float(value)
    return "N/A" if number is None else f"{number:.{digits}f}"


def bucket_lines(items: Sequence[Dict[str, Any]], limit: int = 10) -> List[str]:
    lines: List[str] = []
    for item in items[:limit]:
        allocation = item.get("allocation_pct")
        suffix = f" {format_pct(allocation)}" if allocation is not None else ""
        lines.append(f"- {item.get('symbol', 'UNKNOWN')}{suffix}")
    return lines or ["- none"]


def render_message(report: Dict[str, Any]) -> str:
    health = report["portfolio_health"]
    sources = report["sources"]
    lines = [
        "📦 PORTFOLIO ENGINE V2 — ADVISORY",
        "",
        f"Status: {report['status']}",
        f"Generated: {report['generated_at']}",
        f"Data Age: {sources['allocation'].get('age_minutes')} minutes",
        f"Rows Evaluated: {report['rows_evaluated']}",
        f"Allocation Total: {report['allocation_total_pct']:.2f}%",
        "",
        f"Portfolio Health: {health['portfolio_health']}",
        f"Risk Score: {health['risk_score']:.2f}/100",
        f"Diversification: {health['diversification_score']:.2f}/100",
        f"Largest Exposure: {health['largest_exposure_symbol'] or 'N/A'}",
    ]
    if report["status"] != "READY":
        lines.extend(["", "⛔ Advisory blocked:"])
        lines.extend(f"- {reason}" for reason in report["blocked_reasons"])
    else:
        lines.extend(["", "🟢 Top Allocation:"])
        for index, item in enumerate(report["top_allocations"], 1):
            lines.append(
                f"{index}. {item['symbol']} — {format_pct(item['allocation_pct'])} | "
                f"EV {format_number(item.get('expected_value'))} | WR {format_pct(item.get('winrate_pct'))}"
            )
        lines.extend(["", "🔄 Rebalancing:", "BUY MORE:"])
        lines.extend(bucket_lines(report["rebalancing"]["BUY MORE"], 5))
        lines.extend(["", "REDUCE:"])
        lines.extend(bucket_lines(report["rebalancing"]["REDUCE"], 5))
        lines.extend(["", "REMOVE:"])
        lines.extend(bucket_lines(report["rebalancing"]["REMOVE"], 10))
    lines.extend(
        [
            "",
            "Mode: V2_ADVISORY_ONLY",
            "Runtime V1 Changed: NO",
            "Broker Routing: NO",
            "Order Attempted: NO",
            f"Allocation Source: {sources['allocation'].get('path') or 'NONE'}",
            f"Health Source: {health.get('health_source')}",
            f"Rebalancing Source: {report['rebalancing_source']}",
        ]
    )
    return "\n".join(lines)


def build_report(
    *,
    allocation_path: Optional[str] = None,
    health_path: Optional[str] = None,
    rebalancing_path: Optional[str] = None,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or utc_now()
    max_age = max(0, int(max_age_minutes))
    selected_allocation = allocation_path or discover_latest(ALLOCATION_PATTERNS)
    selected_health = health_path or discover_latest(HEALTH_PATTERNS)
    selected_rebalancing = rebalancing_path or discover_latest(REBALANCING_PATTERNS)
    allocation_meta = source_metadata(selected_allocation, current)
    health_meta = source_metadata(selected_health, current)
    rebalancing_meta = source_metadata(selected_rebalancing, current)
    allocations, errors = canonicalize_allocations(load_records(selected_allocation))
    status, blocked_reasons = validate_sources(allocation_meta, allocations, errors, max_age)
    health = parse_health(selected_health, allocations, health_meta, max_age)
    rebalancing, rebalancing_source = build_rebalancing(
        selected_rebalancing, allocations, rebalancing_meta, max_age
    )
    gates_safe, active_gates = execution_gates_safe()
    if not gates_safe:
        status = "BLOCKED_EXECUTION_GATES_ACTIVE"
        blocked_reasons.append("execution-related environment gates active: " + ", ".join(active_gates))
    report: Dict[str, Any] = {
        "generated_at": current.replace(microsecond=0).isoformat(),
        "phase": "3.01",
        "status": status,
        "blocked_reasons": blocked_reasons,
        "rows_evaluated": len(allocations),
        "allocation_total_pct": round(allocation_total(allocations), 2),
        "top_allocations": allocations[:10],
        "portfolio_health": health,
        "rebalancing": rebalancing,
        "rebalancing_source": rebalancing_source,
        "sources": {
            "allocation": allocation_meta,
            "health": health_meta,
            "rebalancing": rebalancing_meta,
        },
        "freshness_limit_minutes": max_age,
        "execution_gates_safe": gates_safe,
        "active_execution_gates": active_gates,
        "mode": "V2_ADVISORY_ONLY",
        "runtime_v1_changed": False,
        "broker_routing_enabled": False,
        "order_attempted": False,
        "order_success": False,
        "telegram_send_attempted": False,
        "telegram_send_success": False,
    }
    report["payload_text"] = render_message(report)
    report["payload_sha256"] = hashlib.sha256(report["payload_text"].encode("utf-8")).hexdigest()
    return report


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_state(path: str) -> Dict[str, Any]:
    payload = read_json(path)
    return payload if isinstance(payload, dict) else {}


def cooldown_passed(
    state: Dict[str, Any], payload_sha256: str, cooldown_seconds: int, now: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    last_hash = state.get("payload_sha256")
    last_sent = parse_iso(state.get("last_sent_at"))
    current = now or utc_now()
    if last_hash != payload_sha256 or last_sent is None or cooldown_seconds <= 0:
        return True, None
    elapsed = current - last_sent
    if elapsed >= timedelta(seconds=cooldown_seconds):
        return True, None
    remaining = max(0, int(cooldown_seconds - elapsed.total_seconds()))
    return False, f"duplicate Portfolio V2 payload within cooldown; {remaining}s remaining"


def send_telegram(text: str) -> bool:
    if not getattr(config, "telegram_enabled", False):
        return False
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    data = {
        "chat_id": config.telegram_chat_id,
        "text": escape(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=data, timeout=config.request_timeout_seconds)
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def send_or_preview(
    report: Dict[str, Any],
    *,
    send_requested: bool,
    dry_run: bool,
    ignore_cooldown: bool,
    cooldown_seconds: int,
    state_path: str,
) -> Dict[str, Any]:
    state = load_state(state_path)
    cooldown_ok, cooldown_reason = cooldown_passed(
        state, report["payload_sha256"], max(0, cooldown_seconds)
    )
    status = "PREVIEW_ONLY"
    blocked_reason: Optional[str] = None
    attempted = False
    success = False
    if dry_run:
        status = "BLOCKED_DRY_RUN"
        blocked_reason = "--dry-run supplied"
    elif not send_requested:
        blocked_reason = "send flag not supplied"
    elif report["status"] != "READY":
        status = "BLOCKED_REPORT"
        blocked_reason = "; ".join(report["blocked_reasons"]) or "report is not READY"
    elif os.getenv(ALLOW_SEND_ENV) != "1":
        status = "BLOCKED_MANUAL_GATE"
        blocked_reason = f"{ALLOW_SEND_ENV} must be 1"
    elif not ignore_cooldown and not cooldown_ok:
        status = "BLOCKED_COOLDOWN"
        blocked_reason = cooldown_reason
    elif not getattr(config, "telegram_enabled", False):
        status = "BLOCKED_TELEGRAM_CONFIG"
        blocked_reason = "Telegram is disabled or credentials are missing"
    else:
        attempted = True
        success = send_telegram(report["payload_text"])
        if success:
            status = "SENT"
            write_json(
                state_path,
                {
                    "last_sent_at": utc_now_iso(),
                    "payload_sha256": report["payload_sha256"],
                    "allocation_source": report["sources"]["allocation"].get("path"),
                },
            )
        else:
            status = "ERROR"
            blocked_reason = "Telegram request failed"
    return {
        "generated_at": utc_now_iso(),
        "status": status,
        "blocked_reason": blocked_reason,
        "send_requested": send_requested,
        "send_attempted": attempted,
        "send_success": success,
        "cooldown_passed": cooldown_ok,
        "payload_sha256": report["payload_sha256"],
        "report_status": report["status"],
        "runtime_v1_changed": False,
        "broker_routing_enabled": False,
        "order_attempted": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fresh Portfolio V2 advisory pipeline")
    parser.add_argument("--allocation-path")
    parser.add_argument("--health-path")
    parser.add_argument("--rebalancing-path")
    parser.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    parser.add_argument("--report-path", default=REPORT_PATH)
    parser.add_argument("--preview-path", default=PREVIEW_PATH)
    parser.add_argument("--send-result-path", default=SEND_RESULT_PATH)
    parser.add_argument("--state-path", default=SEND_STATE_PATH)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-cooldown", action="store_true")
    parser.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(
        allocation_path=args.allocation_path,
        health_path=args.health_path,
        rebalancing_path=args.rebalancing_path,
        max_age_minutes=args.max_age_minutes,
    )
    write_json(args.report_path, report)
    write_json(
        args.preview_path,
        {
            "generated_at": report["generated_at"],
            "status": report["status"],
            "payload_text": report["payload_text"],
            "payload_sha256": report["payload_sha256"],
            "broker_execution_enabled": False,
            "order_attempted": False,
            "runtime_v1_changed": False,
        },
    )
    send_result = send_or_preview(
        report,
        send_requested=args.send,
        dry_run=args.dry_run,
        ignore_cooldown=args.ignore_cooldown,
        cooldown_seconds=args.cooldown_seconds,
        state_path=args.state_path,
    )
    write_json(args.send_result_path, send_result)
    print(report["payload_text"])
    print(f"Telegram Result: {send_result['status']}")
    return 0 if send_result["status"] != "ERROR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
