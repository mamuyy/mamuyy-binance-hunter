import json
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
from json_utils import atomic_write_json
from database import sqlite_path
from exchange_info_cache import get_exchange_info
from symbol_validation import validate_symbol, DEFAULT_POLICY_DENYLIST, SymbolValidationResult, policy_denylist

DB_PATH = Path("mamuyy_hunter.db")
REPORTS_DIR = Path("reports")
BATCH_DIR = REPORTS_DIR / "candidate_batches"
OUTPUT_PATH = REPORTS_DIR / "binance_candidate_queue.json"
SNAPSHOT_PATH = Path("tmp/mamuyy_hunter_candidate_queue_snapshot.db")
MIN_SCORE = 85
MAX_CANDIDATES = 20
MAX_SIGNAL_AGE_HOURS = 72
CANDIDATE_SOURCE = "LIVE_SCANNER"
EXCHANGE_INFO_CACHE_PATH = Path("reports/binance_futures_exchange_info_cache.json")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{sqlite_path(str(db_path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def fetch_exchange_info(base_url: str | None = None, cache_path: Path = EXCHANGE_INFO_CACHE_PATH):
    base = base_url or os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
    return get_exchange_info(base, cache_path=cache_path, timeout=15)


def fetch_candidates(db_path: Path = DB_PATH, exchange_info: dict | None = None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_SIGNAL_AGE_HOURS)).isoformat()
    diagnostics = {"live_rows_considered": 0, "historical_rows_excluded": 0, "legacy_rows_excluded": 0, "rejected_symbol_count": 0, "rejection_reasons": {}, "rejected_symbols": []}
    exchange_result = None
    exchange_reason = None
    if exchange_info is None:
        exchange_result = fetch_exchange_info()
        exchange_info = exchange_result.exchange_info
        exchange_reason = exchange_result.reason
    else:
        exchange_reason = None
    diagnostics["exchange_info"] = {
        "available": exchange_info is not None,
        "reason": exchange_reason,
        "cache_status": getattr(exchange_result, "cache_status", "PROVIDED"),
        "metadata": getattr(exchange_result, "metadata", {}),
    }
    with _connect_readonly(db_path) as conn:
        counts = conn.execute("SELECT data_source, COUNT(*) c FROM signals WHERE timestamp >= ? GROUP BY data_source", (cutoff,)).fetchall()
        for row in counts:
            source = row["data_source"] or "LEGACY_UNKNOWN"
            if source == CANDIDATE_SOURCE: diagnostics["live_rows_considered"] = int(row["c"])
            elif source == "HISTORICAL_BACKFILL": diagnostics["historical_rows_excluded"] += int(row["c"])
            else: diagnostics["legacy_rows_excluded"] += int(row["c"])
        rows = conn.execute("""
        WITH latest AS (
            SELECT MAX(id) AS latest_id FROM signals
            WHERE data_source = ? AND score >= ? AND squeeze_risk = 'LOW'
              AND (funding_warning IS NULL OR funding_warning = '') AND timestamp >= ?
            GROUP BY symbol
        )
        SELECT s.* FROM signals s JOIN latest l ON s.id = l.latest_id
        ORDER BY s.score DESC, s.pressure_score DESC, s.id DESC LIMIT ?
        """, (CANDIDATE_SOURCE, MIN_SCORE, cutoff, MAX_CANDIDATES * 2)).fetchall()
    candidates=[]
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        if exchange_info is None and symbol not in policy_denylist():
            validation = SymbolValidationResult(symbol, False, exchange_reason or "EXCHANGE_INFO_UNAVAILABLE")
        else:
            validation = validate_symbol(symbol, exchange_info)
        if not validation.valid:
            diagnostics["rejected_symbol_count"] += 1
            diagnostics["rejection_reasons"][validation.reason] = diagnostics["rejection_reasons"].get(validation.reason, 0) + 1
            diagnostics["rejected_symbols"].append(validation.as_dict())
            continue
        candidates.append({"rank": len(candidates)+1, "symbol": symbol, "timestamp": row["timestamp"], "score": row["score"], "price": row["price"], "regime_name": row["regime_name"], "pressure_score": row["pressure_score"], "oi_expansion_rate": row["oi_expansion_rate"], "taker_delta": row["taker_delta"], "squeeze_probability": row["squeeze_probability"], "whale_activity": row["whale_activity"], "squeeze_risk": row["squeeze_risk"], "funding_warning": row["funding_warning"], "data_source": CANDIDATE_SOURCE, "symbol_validation": validation.as_dict(), "status": "PROPOSAL_ONLY", "execution_allowed": False})
        if len(candidates) >= MAX_CANDIDATES: break
    return candidates, diagnostics


def build_report(candidates, diagnostics, db_path: Path = DB_PATH):
    generated_at = datetime.now(timezone.utc).isoformat()
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
    reason = None
    if not candidates:
        if diagnostics.get("live_rows_considered", 0) <= 0:
            reason = "NO_LIVE_SCANNER_CANDIDATES"
        elif diagnostics.get("exchange_info", {}).get("available") is False:
            reason = diagnostics.get("exchange_info", {}).get("reason") or "EXCHANGE_INFO_UNAVAILABLE"
        elif diagnostics.get("rejected_symbol_count", 0) > 0:
            reason = "SYMBOL_VALIDATION_BLOCKED"
        else:
            reason = "NO_QUALIFYING_LIVE_SCANNER_CANDIDATES"
    return {"batch_id": batch_id, "generated_at": generated_at, "phase": "Phase 9D.1A Candidate Queue", "mode": "READ_ONLY_PROPOSAL", "source_db": str(db_path), "candidate_source": CANDIDATE_SOURCE, "source": CANDIDATE_SOURCE, "status": "OPEN", "validation_horizons": [24,48,72], "empty_reason": reason, "rules": {"min_score": MIN_SCORE, "squeeze_risk": "LOW", "funding_warning": "empty_or_null", "excluded_symbols": sorted(DEFAULT_POLICY_DENYLIST), "max_candidates": MAX_CANDIDATES, "max_signal_age_hours": MAX_SIGNAL_AGE_HOURS}, "safety": {"paper_only": True, "real_binance_enabled": False, "testnet_order_enabled": False, "auto_execution_enabled": False, "manual_review_required": True, "writes_to_database": False, "writes_to_broker": False, "execution_allowed": False, "automatic_promotion_allowed": False}, "governance": {"paper_only": True, "writes_to_broker": False, "execution_allowed": False, "automatic_promotion_allowed": False}, "diagnostics": diagnostics, "candidate_count": len(candidates), "candidates": candidates}


def _update_batch_registry(report, batch_path: Path, state_path: Path) -> None:
    registry_path = BATCH_DIR / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        registry = {"batches": []}
    batches = registry.setdefault("batches", [])
    if not any(item.get("batch_id") == report["batch_id"] for item in batches):
        batches.append({
            "batch_id": report["batch_id"],
            "archive_path": str(batch_path),
            "state_path": str(state_path),
            "status": report.get("status", "OPEN"),
            "generated_at": report.get("generated_at"),
            "candidate_count": report.get("candidate_count", 0),
            "source": report.get("source"),
        })
    registry["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(registry_path, registry)


def write_reports(report):
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    batch_path = BATCH_DIR / f"{report['batch_id']}.json"
    state_path = BATCH_DIR / f"{report['batch_id']}.state.json"
    if not batch_path.exists():
        atomic_write_json(batch_path, report)
    if not state_path.exists():
        atomic_write_json(state_path, {
            "batch_id": report["batch_id"],
            "status": report.get("status", "OPEN"),
            "archive_path": str(batch_path),
            "generated_at": report.get("generated_at"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "validation_horizons": report.get("validation_horizons", []),
            "governance": report.get("governance", {}),
        })
    _update_batch_registry(report, batch_path, state_path)
    latest = dict(report); latest["archive_path"] = str(batch_path); latest["state_path"] = str(state_path)
    atomic_write_json(OUTPUT_PATH, latest)
    return batch_path


def main() -> None:
    candidates, diagnostics = fetch_candidates(DB_PATH)
    report = build_report(candidates, diagnostics, DB_PATH)
    path = write_reports(report)
    print(f"Candidate Queue generated: {OUTPUT_PATH}; archive: {path}; Candidates: {len(candidates)}")
    if SNAPSHOT_PATH.exists():
        raise RuntimeError(f"Forbidden stale snapshot remains: {SNAPSHOT_PATH}")

if __name__ == "__main__": main()
