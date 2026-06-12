"""ML Signal Overlay V1.3 for PAPER_ONLY Telegram advisory previews.

This module is intentionally read-only for Hunter runtime inputs. It inspects the
latest signal candidate (or a requested symbol), enriches it with available
portfolio intelligence artifacts, prints a Telegram-style advisory message, and
writes a standalone JSON preview report under logs/.

Safety constraints:
- PAPER_ONLY advisory output only.
- No broker API calls.
- No live execution.
- No execution engine imports or mutations.
- SQLite is opened in read-only mode when present.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DB_PATH = "mamuyy_hunter.db"
REPORT_PATH = "logs/ml_signal_overlay_v1_report.json"
TELEGRAM_PREVIEW_PATH = "logs/ml_signal_overlay_telegram_preview.json"
SAFETY_TEXT = "PAPER_ONLY; read-only advisory; no broker API; no live execution; no order placement"

SEARCH_DIRS = (".", "logs", "reports", "data")
SOURCE_EXTENSIONS = {".csv", ".json"}
EXCLUDED_SOURCE_FILES = {REPORT_PATH, TELEGRAM_PREVIEW_PATH}
TIMESTAMP_COLUMNS = (
    "timestamp",
    "created_at",
    "generated_at",
    "updated_at",
    "closed_at",
    "entry_time",
    "exit_time",
    "time",
    "date",
)
SIGNAL_TABLES = ("signals", "internal_paper_trades", "shadow_trades", "paper_trades")
SIGNAL_FILE_PATTERNS = ("signal", "paper_trades")
RANKING_FILE_PATTERNS = ("trade_quality_ranking", "trade_quality", "quality_ranking", "ranking", "rank")
ALLOCATION_FILE_PATTERNS = (
    "portfolio_allocation",
    "allocation_v2",
    "portfolio_v2",
    "portfolio_snapshot",
    "portfolio_health",
    "phase4e",
    "phase5a",
    "phase7a",
    "allocation",
    "paper_portfolio",
)
HEALTH_FILE_PATTERNS = (
    "portfolio_health",
    "portfolio_v2_health",
    "portfolio_risk_budget",
    "phase3_readiness",
    "portfolio_snapshot",
    "phase4e",
    "phase5a",
    "phase7a",
)

SYMBOL_COLUMNS = ("symbol", "ticker", "asset")
RANK_COLUMNS = ("rank", "trade_rank", "trade_quality_rank", "quality_rank", "portfolio_rank")
ALLOCATION_COLUMNS = (
    "allocation",
    "allocation_pct",
    "suggested_allocation",
    "suggested_allocation_pct",
    "suggested_max_weight_pct",
    "weight",
    "weight_pct",
    "target_weight",
    "target_weight_pct",
    "max_weight_pct",
    "capital_pct_v2",
    "capital_pct",
)
ELIGIBLE_COLUMNS = ("eligible", "portfolio_eligible", "is_eligible")
EV_COLUMNS = (
    "ev",
    "ev_pct",
    "expected_value",
    "expected_value_pct",
    "expected_return_pct",
    "avg_pnl",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            value = value.strip().replace("%", "")
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _normalize_symbol(value: Any) -> str:
    """Normalize exchange symbols without guessing alternate assets."""
    return re.sub(r"[\s/_-]+", "", str(value or "").upper())


def _normalize_rank(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    aliases = {
        "APLUS": "A+",
        "A_PLUS": "A+",
        "A-PLUS": "A+",
        "AMINUS": "A-",
        "A_MINUS": "A-",
        "BPLUS": "B+",
        "B_PLUS": "B+",
        "B-PLUS": "B+",
    }
    return aliases.get(text, text)


def _rank_from_score(score: Optional[float]) -> str:
    if score is None:
        return "UNKNOWN"
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 72:
        return "A-"
    if score >= 65:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def _rank_is_bplus_or_better(rank: str) -> bool:
    return _normalize_rank(rank) in {"A+", "A", "A-", "B+"}


def _rank_from_position_multiplier(position_multiplier: Optional[float]) -> str:
    if position_multiplier is None or position_multiplier <= 0:
        return "UNKNOWN"
    if position_multiplier >= 1.50:
        return "A+"
    if position_multiplier >= 1.25:
        return "A"
    if position_multiplier >= 1.10:
        return "A-"
    if position_multiplier >= 1.00:
        return "B+"
    if position_multiplier >= 0.75:
        return "B"
    if position_multiplier >= 0.50:
        return "C"
    return "UNKNOWN"


def _mode_rank(values: Iterable[Any]) -> str:
    counts: Dict[str, int] = {}
    first_seen: Dict[str, int] = {}
    for index, value in enumerate(values):
        rank = _normalize_rank(value)
        if not rank:
            continue
        counts[rank] = counts.get(rank, 0) + 1
        first_seen.setdefault(rank, index)
    if not counts:
        return "UNKNOWN"
    return sorted(counts, key=lambda rank: (-counts[rank], first_seen[rank]))[0]


def _is_canonical_allocation_source(path: str) -> bool:
    return Path(path).name.startswith("ml_portfolio_allocation_v2_") and Path(path).suffix.lower() == ".csv"


def _is_trade_quality_source(path: str) -> bool:
    return Path(path).name.startswith("ml_calibration_with_trade_quality_") and Path(path).suffix.lower() == ".csv"


def _format_pct(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "N/A"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.2f}%"


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as input_file:
            return json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return None


def _read_csv_rows(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, newline="", encoding="utf-8") as input_file:
            return list(csv.DictReader(input_file))
    except OSError:
        return []


def _connect_read_only(db_path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(db_path):
        return None
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()]
    except sqlite3.Error:
        return []


def _table_row_count(connection: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _pick_first(row: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
            return lowered[name.lower()]
    return default


def _discover_db_path(db_path: str = DB_PATH) -> Optional[str]:
    candidates = [Path(db_path), Path("mamuyy_hunter.sqlite"), Path("mamuyy_hunter.sqlite3")]
    candidates.extend(sorted(Path(".").glob("*.db")))
    candidates.extend(sorted(Path(".").glob("*.sqlite")))
    candidates.extend(sorted(Path(".").glob("*.sqlite3")))
    seen = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.exists() and candidate.is_file():
            return normalized
    return str(Path(db_path)) if Path(db_path).exists() else None


def _filename_matches(path: Path, patterns: Sequence[str]) -> bool:
    name = path.name.lower()
    stem = path.stem.lower()
    return any(pattern in name or pattern in stem for pattern in patterns)


def _discover_files(patterns: Sequence[str]) -> List[str]:
    found: List[str] = []
    seen = set()
    for directory in SEARCH_DIRS:
        base = Path(directory)
        if not base.exists() or not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            text = str(path).lstrip("./")
            if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if text in EXCLUDED_SOURCE_FILES:
                continue
            if _filename_matches(path, patterns):
                if text not in seen:
                    seen.add(text)
                    found.append(text)
    return found


def _path_text(path: Path) -> str:
    text = str(path)
    if path.is_absolute():
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return text
    return text.lstrip("./")


def _existing_file(path: str) -> Optional[str]:
    candidate = Path(path)
    if candidate.exists() and candidate.is_file():
        return _path_text(candidate)
    return None


def _newest_matching_file(pattern: str) -> Optional[str]:
    candidates = [path for path in Path(".").glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return _path_text(sorted(candidates, key=lambda item: (item.name, item.stat().st_mtime))[-1])


def _json_output_csv(path: str) -> Optional[str]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None
    output_csv = payload.get("output_csv")
    if not output_csv:
        return None
    candidate = Path(str(output_csv))
    candidates = [candidate] if candidate.is_absolute() else [candidate, Path(path).parent / candidate]
    for option in candidates:
        if option.exists() and option.is_file():
            return _path_text(option)
    return None


def _newest_json_output_csv(pattern: str) -> Optional[str]:
    reports = [path for path in Path(".").glob(pattern) if path.is_file()]
    for report in sorted(reports, key=lambda item: (item.name, item.stat().st_mtime), reverse=True):
        output_csv = _json_output_csv(_path_text(report))
        if output_csv:
            return output_csv
    return None


def _dedupe_paths(paths: Iterable[Optional[str]]) -> List[str]:
    selected: List[str] = []
    seen = set()
    for path in paths:
        if not path:
            continue
        text = str(path).lstrip("./")
        if text in EXCLUDED_SOURCE_FILES or text in seen:
            continue
        seen.add(text)
        selected.append(text)
    return selected


def _allocation_priority_paths() -> List[str]:
    return _dedupe_paths(
        [
            _existing_file("data/ml_portfolio_allocation_v2_20260610.csv")
            or _newest_matching_file("data/ml_portfolio_allocation_v2_*.csv"),
            _newest_matching_file("data/ml_calibration_with_portfolio_allocation_*.csv"),
            _newest_json_output_csv("logs/phase4e_portfolio_allocation_v2_report_*.json"),
            _newest_json_output_csv("logs/phase4d_portfolio_allocation_engine_report_*.json"),
            _existing_file("logs/opportunity_allocation.csv"),
        ]
    )


def _ranking_priority_paths() -> List[str]:
    return _dedupe_paths(
        [
            _newest_matching_file("data/ml_calibration_with_trade_quality_*.csv"),
            _newest_json_output_csv("logs/phase4b_trade_quality_ranking_engine_report_*.json"),
        ]
    )


def _phase4b_rank_summary_source() -> Optional[str]:
    reports = [path for path in Path(".").glob("logs/phase4b_trade_quality_ranking_engine_report_*.json") if path.is_file()]
    if not reports:
        return None
    return _path_text(sorted(reports, key=lambda item: (item.name, item.stat().st_mtime))[-1])


def _health_priority_paths() -> List[str]:
    return _dedupe_paths(
        [
            _newest_matching_file("reports/portfolio_risk_budget.json"),
            _newest_matching_file("reports/paper_portfolio.json"),
            _newest_matching_file("reports/phase3_readiness.json"),
            *_discover_files(HEALTH_FILE_PATTERNS),
        ]
    )


def selected_canonical_sources() -> Dict[str, str]:
    allocation_paths = _allocation_priority_paths()
    ranking_paths = _ranking_priority_paths()
    health_paths = _health_priority_paths()
    fallback_ranking = _phase4b_rank_summary_source()
    return {
        "allocation": allocation_paths[0] if allocation_paths else "none",
        "ranking": ranking_paths[0] if ranking_paths else fallback_ranking or "none",
        "health": health_paths[0] if health_paths else "none",
    }


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "allocations",
        "allocation",
        "rankings",
        "ranking",
        "rank_summary",
        "candidates",
        "rows",
        "data",
        "symbols",
        "positions",
        "portfolio",
        "holdings",
        "assets",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_records(value)
            if nested:
                return nested
    if any(str(key).lower() in SYMBOL_COLUMNS for key in payload):
        return [payload]
    return []


def _source_rows(path: str) -> List[Dict[str, Any]]:
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return _read_csv_rows(path)
    if suffix == ".json":
        return _extract_records(_read_json(path))
    return []


def _source_columns(path: str) -> List[str]:
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        try:
            with open(path, newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                return list(reader.fieldnames or [])
        except OSError:
            return []
    if suffix == ".json":
        payload = _read_json(path)
        records = _extract_records(payload)
        columns = set()
        for row in records:
            columns.update(str(key) for key in row.keys())
        if not columns and isinstance(payload, dict):
            columns.update(str(key) for key in payload.keys())
        return sorted(columns)
    return []


def _latest_timestamp_from_rows(rows: Iterable[Dict[str, Any]]) -> Optional[str]:
    latest: Optional[str] = None
    for row in rows:
        value = _pick_first(row, TIMESTAMP_COLUMNS)
        if value in (None, ""):
            continue
        text = str(value)
        if latest is None or text > latest:
            latest = text
    return latest


def _latest_timestamp_from_payload(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    value = _pick_first(payload, TIMESTAMP_COLUMNS)
    return str(value) if value not in (None, "") else None


def _source_diagnostic(path: str) -> Dict[str, Any]:
    rows = _source_rows(path)
    payload = _read_json(path) if Path(path).suffix.lower() == ".json" else None
    row_count = len(rows)
    if row_count == 0 and isinstance(payload, dict):
        row_count = 1
    latest_timestamp = _latest_timestamp_from_rows(rows) or _latest_timestamp_from_payload(payload)
    return {
        "path": path,
        "type": Path(path).suffix.lower().lstrip("."),
        "columns": _source_columns(path),
        "row_count": row_count,
        "latest_timestamp": latest_timestamp,
    }


def _db_table_diagnostics(db_path: str = DB_PATH) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    detected_path = _discover_db_path(db_path)
    if not detected_path:
        return None, []
    connection = _connect_read_only(detected_path)
    if connection is None:
        return detected_path, []
    diagnostics: List[Dict[str, Any]] = []
    try:
        for table in SIGNAL_TABLES:
            if not _table_exists(connection, table):
                continue
            columns = _table_columns(connection, table)
            latest_timestamp = None
            timestamp_column = next((column for column in columns if column.lower() in TIMESTAMP_COLUMNS), None)
            if timestamp_column:
                try:
                    row = connection.execute(
                        f"SELECT MAX({_quote_identifier(timestamp_column)}) FROM {_quote_identifier(table)}"
                    ).fetchone()
                    latest_timestamp = str(row[0]) if row and row[0] is not None else None
                except sqlite3.Error:
                    latest_timestamp = None
            diagnostics.append(
                {
                    "table": table,
                    "columns": columns,
                    "row_count": _table_row_count(connection, table),
                    "latest_timestamp": latest_timestamp,
                }
            )
    finally:
        connection.close()
    return detected_path, diagnostics


def discover_sources(db_path: str = DB_PATH) -> Dict[str, Any]:
    database_path, signal_tables = _db_table_diagnostics(db_path)
    signal_files = _discover_files(SIGNAL_FILE_PATTERNS)
    ranking_files = _discover_files(RANKING_FILE_PATTERNS)
    allocation_files = _discover_files(ALLOCATION_FILE_PATTERNS)
    health_files = _discover_files(HEALTH_FILE_PATTERNS)
    selected = selected_canonical_sources()
    return {
        "database_path": database_path,
        "signal_tables": signal_tables,
        "signal_files": [_source_diagnostic(path) for path in signal_files],
        "trade_ranking_files": [_source_diagnostic(path) for path in ranking_files],
        "portfolio_allocation_files": [_source_diagnostic(path) for path in allocation_files],
        "portfolio_health_files": [_source_diagnostic(path) for path in health_files],
        "selected_allocation_source": selected["allocation"],
        "selected_ranking_source": selected["ranking"],
        "selected_health_source": selected["health"],
    }


def print_source_listing(db_path: str = DB_PATH) -> None:
    diagnostics = discover_sources(db_path)
    print("ML Signal Overlay V1.3 source diagnostics")
    print(f"Detected database path: {diagnostics['database_path'] or 'none'}")
    print("\nSelected canonical sources:")
    print(f"  - allocation: {diagnostics['selected_allocation_source']}")
    print(f"  - ranking: {diagnostics['selected_ranking_source']}")
    print(f"  - health: {diagnostics['selected_health_source']}")
    print("\nDetected signal tables:")
    _print_items(diagnostics["signal_tables"], label_key="table")
    print("\nDetected signal files:")
    _print_items(diagnostics["signal_files"])
    print("\nDetected trade ranking files:")
    _print_items(diagnostics["trade_ranking_files"])
    print("\nDetected portfolio allocation files:")
    _print_items(diagnostics["portfolio_allocation_files"])
    print("\nDetected portfolio health files:")
    _print_items(diagnostics["portfolio_health_files"])


def _print_items(items: Sequence[Dict[str, Any]], label_key: str = "path") -> None:
    if not items:
        print("  - none")
        return
    for item in items:
        label = item.get(label_key) or item.get("path") or "unknown"
        print(f"  - {label}")
        print(f"    columns: {item.get('columns') or []}")
        print(f"    row_count: {item.get('row_count')}")
        print(f"    latest_timestamp: {item.get('latest_timestamp') or 'unknown'}")


def _latest_signal_from_db(db_path: str, symbol: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    connection = _connect_read_only(db_path)
    meta: Dict[str, Any] = {"database": db_path, "database_available": bool(connection), "source": "none"}
    if connection is None:
        return {}, meta
    try:
        for table in SIGNAL_TABLES:
            if not _table_exists(connection, table):
                continue
            columns = _table_columns(connection, table)
            symbol_column = next((column for column in columns if column.lower() in SYMBOL_COLUMNS), None)
            if not symbol_column:
                continue
            order_column = "id" if "id" in columns else "timestamp" if "timestamp" in columns else columns[0]
            if symbol:
                query = (
                    f"SELECT * FROM {_quote_identifier(table)} "
                    f"WHERE UPPER(REPLACE(REPLACE(REPLACE(REPLACE({_quote_identifier(symbol_column)}, '/', ''), '-', ''), '_', ''), ' ', ''))=? "
                    f"ORDER BY {_quote_identifier(order_column)} DESC LIMIT 1"
                )
                row = connection.execute(query, (symbol,)).fetchone()
            else:
                query = f"SELECT * FROM {_quote_identifier(table)} ORDER BY {_quote_identifier(order_column)} DESC LIMIT 1"
                row = connection.execute(query).fetchone()
            if row is not None:
                meta["source"] = f"sqlite:{table}"
                return _row_to_dict(row), meta
        return {}, meta
    finally:
        connection.close()


def _latest_signal_from_csv(symbol: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    for path in _discover_files(SIGNAL_FILE_PATTERNS):
        rows = _read_csv_rows(path)
        if not rows:
            continue
        candidates = rows
        if symbol:
            candidates = [row for row in rows if _normalize_symbol(_pick_first(row, SYMBOL_COLUMNS)) == symbol]
        if not candidates:
            continue
        return candidates[-1], {"source": f"csv:{path}"}
    return {}, {"source": "none"}


def load_signal_candidate(symbol: str = "", db_path: str = DB_PATH) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    db_signal, db_meta = _latest_signal_from_db(db_path, symbol)
    if db_signal:
        return db_signal, db_meta
    csv_signal, csv_meta = _latest_signal_from_csv(symbol)
    if csv_signal:
        meta = {**db_meta, **csv_meta}
        return csv_signal, meta
    fallback_symbol = symbol if symbol else "UNKNOWN"
    return {"symbol": fallback_symbol, "direction": "UNKNOWN", "score": None}, {**db_meta, "source": "fallback:no_signal_found"}


def _source_kind(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".") or "file"
    return f"{suffix}:{path}"


def _records_conflict(records: Sequence[Dict[str, Any]]) -> bool:
    if len(records) <= 1:
        return False
    canonical = [json.dumps(record, sort_keys=True, default=str) for record in records]
    return len(set(canonical)) > 1


def _aggregate_trade_quality_records(
    symbol: str,
    path: str,
    matches: Sequence[Dict[str, Any]],
    raw_symbols: Sequence[str],
    searched: Sequence[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    target = _normalize_symbol(symbol)
    normalized_matches = sorted({_normalize_symbol(raw_symbol) for raw_symbol in raw_symbols})
    if len(normalized_matches) > 1:
        return {}, {
            "source": f"ambiguous:{path}",
            "found": False,
            "searched": list(searched),
            "selected_source": path,
            "match_status": "AMBIGUOUS",
            "match_reason": f"multiple different normalized symbols matched requested symbol variant {target}: {normalized_matches}",
            "ranking_aggregation_method": "not_applied_ambiguous_symbol_variants",
        }

    mode_rank = _mode_rank(_pick_first(row, ["trade_quality_rank", *RANK_COLUMNS]) for row in matches)
    quality_scores = [
        score
        for score in (_safe_float(_pick_first(row, ["trade_quality_score"])) for row in matches)
        if score is not None
    ]
    aggregated: Dict[str, Any] = {
        "symbol": target,
        "source_row_count": len(matches),
        "raw_symbol_variants": sorted(set(str(raw_symbol) for raw_symbol in raw_symbols)),
        "__rank_source": "ranking.trade_quality_rank_mode",
    }
    if mode_rank != "UNKNOWN":
        aggregated["trade_quality_rank"] = mode_rank
    if quality_scores:
        aggregated["trade_quality_score_avg"] = sum(quality_scores) / len(quality_scores)

    return aggregated, {
        "source": f"csv_aggregated:{path}",
        "found": True,
        "searched": list(searched),
        "selected_source": path,
        "match_status": "MATCHED",
        "match_reason": f"aggregated {len(matches)} trade-quality rows for normalized symbol {target}",
        "ranking_aggregation_method": "normalized_symbol_group_mode_trade_quality_rank_avg_trade_quality_score",
        "aggregated_row_count": len(matches),
        "trade_quality_score_avg": aggregated.get("trade_quality_score_avg"),
    }


def _find_symbol_record(symbol: str, paths: Sequence[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    target = _normalize_symbol(symbol)
    searched: List[str] = []
    for path in paths:
        searched.append(path)
        matches: List[Dict[str, Any]] = []
        raw_symbols: List[str] = []
        for row in _source_rows(path):
            raw_symbol = _pick_first(row, SYMBOL_COLUMNS)
            if _normalize_symbol(raw_symbol) == target:
                matches.append(row)
                raw_symbols.append(str(raw_symbol))
        if matches and _is_trade_quality_source(path):
            return _aggregate_trade_quality_records(target, path, matches, raw_symbols, searched)
        if len(matches) > 1 and _records_conflict(matches):
            distinct = sorted({_normalize_symbol(raw_symbol) for raw_symbol in raw_symbols})
            if len(distinct) <= 1:
                return matches[-1], {
                    "source": _source_kind(path),
                    "found": True,
                    "searched": searched,
                    "selected_source": path,
                    "match_status": "MATCHED",
                    "match_reason": f"matched {len(matches)} rows for normalized symbol {target}; using latest row",
                }
            return {}, {
                "source": f"ambiguous:{path}",
                "found": False,
                "searched": searched,
                "selected_source": path,
                "match_status": "AMBIGUOUS",
                "match_reason": f"multiple conflicting rows matched normalized symbol {target}: {distinct}",
            }
        if matches:
            return matches[-1], {
                "source": _source_kind(path),
                "found": True,
                "searched": searched,
                "selected_source": path,
                "match_status": "MATCHED",
                "match_reason": f"matched normalized symbol {target} from raw symbol {raw_symbols[-1] if raw_symbols else target}",
            }
    return {}, {
        "source": "none",
        "found": False,
        "searched": searched,
        "selected_source": paths[0] if paths else "none",
        "match_status": "NOT_FOUND",
        "match_reason": f"no source row matched normalized symbol {target}",
    }


def _phase4b_rank_summary_record(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    target = _normalize_symbol(symbol)
    searched: List[str] = []
    reports = sorted(Path(".").glob("logs/phase4b_trade_quality_ranking_engine_report_*.json"), key=lambda item: (item.name, item.stat().st_mtime), reverse=True)
    for report in reports:
        path = _path_text(report)
        searched.append(path)
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        summary = payload.get("rank_summary")
        if not isinstance(summary, dict):
            continue
        value = summary.get(target) or summary.get(symbol)
        if isinstance(value, dict):
            return {"symbol": target, "__rank_source": "phase4b.rank_summary", **value}, {
                "source": f"json_rank_summary:{path}",
                "found": True,
                "searched": searched,
                "selected_source": path,
                "match_status": "MATCHED",
                "match_reason": f"matched normalized symbol {target} in phase4b rank_summary fallback",
            }
        if value not in (None, ""):
            return {"symbol": target, "rank": value, "__rank_source": "phase4b.rank_summary"}, {
                "source": f"json_rank_summary:{path}",
                "found": True,
                "searched": searched,
                "selected_source": path,
                "match_status": "MATCHED",
                "match_reason": f"matched normalized symbol {target} in phase4b rank_summary fallback",
            }
    return {}, {
        "source": "none",
        "found": False,
        "searched": searched,
        "selected_source": reports[0].as_posix() if reports else "none",
        "match_status": "NOT_FOUND",
        "match_reason": f"no phase4b rank_summary entry matched normalized symbol {target}",
    }


def load_ranking_record(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    priority_paths = _ranking_priority_paths()
    record, meta = _find_symbol_record(symbol, priority_paths)
    if record or meta.get("match_status") == "AMBIGUOUS":
        return record, meta
    fallback_record, fallback_meta = _phase4b_rank_summary_record(symbol)
    if fallback_record or fallback_meta.get("match_status") == "AMBIGUOUS":
        fallback_meta["searched"] = meta.get("searched", []) + fallback_meta.get("searched", [])
        return fallback_record, fallback_meta
    fallback_meta["searched"] = meta.get("searched", []) + fallback_meta.get("searched", [])
    return {}, fallback_meta


def load_allocation_record(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    record, meta = _find_symbol_record(symbol, _allocation_priority_paths())
    if record and _is_canonical_allocation_source(str(meta.get("selected_source", ""))):
        allocation = _safe_float(record.get("capital_pct_v2"))
        mapped = {
            **record,
            "symbol": record.get("symbol"),
            "allocation": record.get("capital_pct_v2"),
            "expected_value": record.get("ev_pct"),
            "position_multiplier": record.get("position_multiplier"),
            "allocation_score": record.get("allocation_score_v2"),
            "eligible": "YES" if allocation is not None and allocation > 0 else "NO",
        }
        meta["allocation_aggregation_method"] = "canonical_symbol_level_ml_portfolio_allocation_v2"
        return mapped, meta
    return record, meta


def load_portfolio_health() -> Tuple[str, Dict[str, Any]]:
    searched: List[str] = []
    for path in _health_priority_paths():
        searched.append(path)
        payload = _read_json(path) if Path(path).suffix.lower() == ".json" else None
        if not isinstance(payload, dict):
            continue
        explicit = _pick_first(payload, ["portfolio_health", "health", "health_status", "status"])
        if explicit:
            text = str(explicit).upper()
            if text in {"GREEN", "YELLOW", "RED"}:
                return text, {"source": path, "selected_source": path, "raw": explicit, "searched": searched, "match_status": "MATCHED"}
        recommendation = str(payload.get("recommendation") or "").upper()
        if recommendation:
            if recommendation in {"NORMAL", "OK", "ALLOW"}:
                return "GREEN", {"source": path, "selected_source": path, "raw": recommendation, "searched": searched, "match_status": "MATCHED"}
            if recommendation in {"DEFENSIVE", "CAUTION", "WATCH"}:
                return "YELLOW", {"source": path, "selected_source": path, "raw": recommendation, "searched": searched, "match_status": "MATCHED"}
            if recommendation in {"FREEZE", "HALT", "BLOCK", "AVOID"}:
                return "RED", {"source": path, "selected_source": path, "raw": recommendation, "searched": searched, "match_status": "MATCHED"}
        heat = str(payload.get("portfolio_heat") or payload.get("concentration_label") or "").upper()
        if heat:
            if heat == "LOW":
                return "GREEN", {"source": path, "selected_source": path, "raw": heat, "searched": searched, "match_status": "MATCHED"}
            if heat == "MEDIUM":
                return "YELLOW", {"source": path, "selected_source": path, "raw": heat, "searched": searched, "match_status": "MATCHED"}
            if heat == "HIGH":
                return "RED", {"source": path, "selected_source": path, "raw": heat, "searched": searched, "match_status": "MATCHED"}
    return "UNKNOWN", {"source": "none", "selected_source": searched[0] if searched else "none", "raw": "UNKNOWN", "searched": searched, "match_status": "NOT_FOUND"}


def _expected_value_with_debug(record: Dict[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    lowered = {str(key).lower(): value for key, value in record.items()}
    source_column = next(
        (column for column in EV_COLUMNS if column.lower() in lowered and lowered[column.lower()] not in (None, "")),
        None,
    )
    if source_column:
        raw = lowered[source_column.lower()]
        explicit = _safe_float(raw)
        warning = None
        if source_column.lower() == "ev_pct" and explicit is not None and abs(explicit) > 20:
            warning = "EV value unusually large; verify whether ev_pct is percentage or score metric."
        if explicit is not None:
            value = explicit if source_column.lower() == "ev_pct" else explicit * 100 if -1 < explicit < 1 and explicit != 0 else explicit
            return value, {
                "expected_value_source_column": source_column,
                "expected_value_raw": raw,
                "expected_value_display": _format_pct(value, signed=True),
                "expected_value_scale_warning": warning,
            }
    opportunity = _safe_float(_pick_first(record, ["opportunity_score", "quality_score", "score"]))
    risk = _safe_float(_pick_first(record, ["risk_score"])) or 0.0
    if opportunity is None:
        return None, {
            "expected_value_source_column": None,
            "expected_value_raw": None,
            "expected_value_display": "N/A",
            "expected_value_scale_warning": None,
        }
    value = round((opportunity - risk) / 100.0, 2)
    return value, {
        "expected_value_source_column": "derived_opportunity_minus_risk",
        "expected_value_raw": {"opportunity_score": opportunity, "risk_score": risk},
        "expected_value_display": _format_pct(value, signed=True),
        "expected_value_scale_warning": None,
    }


def _expected_value_from_record(record: Dict[str, Any]) -> Optional[float]:
    value, _debug = _expected_value_with_debug(record)
    return value


def _allocation_from_record(record: Dict[str, Any]) -> Optional[float]:
    allocation = _safe_float(_pick_first(record, ALLOCATION_COLUMNS))
    if allocation is not None and 0 < allocation <= 1:
        return allocation * 100
    return allocation


def _allocation_score_from_record(record: Dict[str, Any]) -> Optional[float]:
    return _safe_float(_pick_first(record, ["allocation_score_v2", "allocation_score"]))


def _eligible_from_record(record: Dict[str, Any], allocation: Optional[float]) -> bool:
    eligible = _pick_first(record, ELIGIBLE_COLUMNS)
    if eligible not in (None, ""):
        return str(eligible).strip().upper() in {"1", "TRUE", "YES", "Y", "ELIGIBLE", "ALLOW"}
    return allocation is not None and allocation > 0


def build_overlay(
    signal: Dict[str, Any],
    ranking_record: Dict[str, Any],
    allocation_record: Dict[str, Any],
    portfolio_health: str,
    symbol_missing: bool,
) -> Dict[str, Any]:
    score = _safe_float(_pick_first(signal, ["score", "signal_score", "shadow_score", "calculated_score", "confidence"]))
    allocation = _allocation_from_record(allocation_record) if allocation_record else None
    allocation_score = _allocation_score_from_record(allocation_record) if allocation_record else None
    position_multiplier = _safe_float(_pick_first(allocation_record, ["position_multiplier"])) if allocation_record else None
    rank = _rank_from_position_multiplier(position_multiplier)
    inferred_rank_source = "allocation.position_multiplier" if rank != "UNKNOWN" else "none"
    if rank == "UNKNOWN":
        rank = _normalize_rank(_pick_first(ranking_record, ["trade_quality_rank", *RANK_COLUMNS]))
        inferred_rank_source = str(ranking_record.get("__rank_source") or "ranking.trade_quality_rank_mode") if rank else "none"
    if not rank or rank == "UNKNOWN":
        rank = _normalize_rank(_pick_first(ranking_record, RANK_COLUMNS))
        inferred_rank_source = "phase4b.rank_summary" if rank else "none"
    if not rank:
        rank = "UNKNOWN"
    ev_source = allocation_record if allocation_record else ranking_record
    ev, ev_debug = _expected_value_with_debug(ev_source) if ev_source else (
        None,
        {
            "expected_value_source_column": None,
            "expected_value_raw": None,
            "expected_value_display": "N/A",
            "expected_value_scale_warning": None,
        },
    )
    tier = str(_pick_first(allocation_record, ["allocation_tier", "tier", "eligibility"], "")).upper()
    if rank == "D" or (ev is not None and ev < 0):
        suggested_risk = "BLOCKED"
    elif rank in {"A+", "A", "A-", "B+"}:
        suggested_risk = "NORMAL"
    elif rank == "B":
        suggested_risk = "ELEVATED"
    elif rank == "C":
        suggested_risk = "CAUTION"
    else:
        suggested_risk = "NEED_REVIEW"
    eligible_bool = _eligible_from_record(allocation_record, allocation) if allocation_record else False
    if tier in {"AVOID", "BLOCK", "BLOCKED"} and allocation in (None, 0):
        eligible_bool = False

    if symbol_missing:
        decision = "UNKNOWN / NEED_REVIEW"
    elif rank == "D" or (ev is not None and ev < 0):
        decision = "AVOID / BLOCKED"
    elif rank == "C":
        decision = "LOW PRIORITY / PAPER_ONLY"
    elif portfolio_health == "GREEN" and _rank_is_bplus_or_better(rank) and allocation is not None and allocation > 0:
        decision = "WATCH / PAPER_ONLY"
    elif rank == "B" and allocation is not None and allocation > 0:
        decision = "WATCHLIST / CAUTION / PAPER_ONLY"
    elif rank == "UNKNOWN":
        decision = "UNKNOWN / NEED_REVIEW"
    else:
        decision = "UNKNOWN / NEED_REVIEW"

    return {
        "trade_rank": rank or "UNKNOWN",
        "expected_value": ev,
        "portfolio_eligible": "YES" if eligible_bool else "NO",
        "suggested_allocation_pct": allocation,
        "allocation_score": allocation_score,
        "suggested_risk_level": suggested_risk,
        "portfolio_health": portfolio_health,
        "overlay_decision": decision,
        "signal_score": score,
        "position_multiplier": position_multiplier,
        "inferred_rank_source": inferred_rank_source,
        **ev_debug,
    }


def _score_text(score: Any) -> str:
    if score is None:
        return "N/A"
    number = float(score)
    return f"{number:.0f}" if number.is_integer() else f"{number:.2f}"


def format_message(signal: Dict[str, Any], overlay: Dict[str, Any]) -> str:
    symbol = _normalize_symbol(_pick_first(signal, SYMBOL_COLUMNS)) or "UNKNOWN"
    direction = str(_pick_first(signal, ["direction", "side", "position_side"], "UNKNOWN")).upper()
    score_text = _score_text(overlay.get("signal_score"))
    return "\n".join(
        [
            "🦅 HUNTER SIGNAL V2 — PAPER ONLY",
            "",
            f"Symbol: {symbol}",
            f"Direction: {direction}",
            f"Score: {score_text}",
            "",
            f"Trade Rank: {overlay['trade_rank']}",
            f"Expected Value: {_format_pct(overlay['expected_value'], signed=True)}",
            f"Portfolio Eligible: {overlay['portfolio_eligible']}",
            f"Suggested Allocation: {_format_pct(overlay['suggested_allocation_pct'])}",
            f"Suggested Risk: {overlay['suggested_risk_level']}",
            "",
            f"Portfolio Health: {overlay['portfolio_health']}",
            f"Overlay Decision: {overlay['overlay_decision']}",
            "",
            "⚠ Advisory only. No broker API. No live execution.",
        ]
    )


def format_telegram_preview_message(signal: Dict[str, Any], overlay: Dict[str, Any]) -> str:
    symbol = _normalize_symbol(_pick_first(signal, SYMBOL_COLUMNS)) or "UNKNOWN"
    direction = str(_pick_first(signal, ["direction", "side", "position_side"], "UNKNOWN")).upper()
    return "\n".join(
        [
            "🦅 HUNTER SIGNAL OVERLAY — PAPER ONLY",
            "",
            f"Symbol: {symbol}",
            f"Direction: {direction}",
            f"Signal Score: {_score_text(overlay.get('signal_score'))}",
            "",
            f"Portfolio Eligible: {overlay['portfolio_eligible']}",
            f"Suggested Allocation: {_format_pct(overlay['suggested_allocation_pct'])}",
            f"Expected Value: {_format_pct(overlay['expected_value'], signed=True)}",
            f"Trade Rank: {overlay['trade_rank']}",
            f"Suggested Risk: {overlay['suggested_risk_level']}",
            "",
            f"Portfolio Health: {overlay['portfolio_health']}",
            f"Overlay Decision: {overlay['overlay_decision']}",
            "",
            "Reason:",
            "Allocation matched, but trade-quality rank is not confirmed yet.",
            "",
            "Safety:",
            "PAPER_ONLY=True",
            "Broker API=False",
            "Live Execution=False",
            "",
            "⚠ Advisory only. No broker API. No live execution.",
        ]
    )


def build_telegram_preview_payload(
    signal: Dict[str, Any],
    overlay: Dict[str, Any],
    generated_at: str,
    overlay_report_path: str,
    send_telegram: bool,
) -> Dict[str, Any]:
    telegram_send_enabled = bool(send_telegram and os.getenv("ALLOW_TELEGRAM_SEND") == "1")
    blocked_reason = None
    if send_telegram and not telegram_send_enabled:
        blocked_reason = "ALLOW_TELEGRAM_SEND not enabled"
    elif not send_telegram:
        blocked_reason = "send flag not passed; preview only"
    return {
        "generated_at": generated_at,
        "symbol": _normalize_symbol(_pick_first(signal, SYMBOL_COLUMNS)) or "UNKNOWN",
        "telegram_send_enabled": telegram_send_enabled,
        "telegram_send_blocked_reason": blocked_reason,
        "payload_text": format_telegram_preview_message(signal, overlay),
        "overlay_report_path": overlay_report_path,
        "paper_only": True,
        "broker_api_enabled": False,
        "live_execution_enabled": False,
    }


def write_telegram_preview(payload: Dict[str, Any], output_path: str = TELEGRAM_PREVIEW_PATH) -> None:
    write_report(payload, output_path)


def write_report(report: Dict[str, Any], output_path: str = REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def _artifact_paths(diagnostics: Dict[str, Any], key: str) -> List[str]:
    return [item["path"] for item in diagnostics.get(key, []) if item.get("path")]


def run(
    symbol: str = "",
    dry_run: bool = False,
    db_path: str = DB_PATH,
    output_path: str = REPORT_PATH,
    telegram_preview: bool = False,
    send_telegram: bool = False,
) -> Dict[str, Any]:
    requested_symbol = _normalize_symbol(symbol)
    diagnostics = discover_sources(db_path)
    signal, signal_meta = load_signal_candidate(requested_symbol, db_path=db_path)
    signal_symbol = _normalize_symbol(_pick_first(signal, SYMBOL_COLUMNS)) or requested_symbol or "UNKNOWN"
    ranking_record, ranking_meta = load_ranking_record(signal_symbol)
    allocation_record, allocation_meta = load_allocation_record(signal_symbol)
    portfolio_health, health_meta = load_portfolio_health()
    symbol_missing = not (ranking_meta.get("found") or allocation_meta.get("found"))
    overlay = build_overlay(signal, ranking_record, allocation_record, portfolio_health, symbol_missing=symbol_missing)
    message = format_message(signal, overlay)
    match_status = "MATCHED" if not symbol_missing else "NOT_FOUND"
    match_reason = "ranking or allocation artifact matched requested symbol"
    if ranking_meta.get("match_status") == "AMBIGUOUS" or allocation_meta.get("match_status") == "AMBIGUOUS":
        match_status = "AMBIGUOUS"
        match_reason = ranking_meta.get("match_reason") or allocation_meta.get("match_reason")
    elif symbol_missing:
        match_reason = f"no ranking or allocation artifact matched normalized symbol {signal_symbol}"
    generated_at = _now()
    report = {
        "generated_at": generated_at,
        "mode": "PAPER_ONLY",
        "dry_run": bool(dry_run),
        "safety": SAFETY_TEXT,
        "telegram_send_enabled": False,
        "signal_source": signal_meta,
        "ranking_source": ranking_meta,
        "allocation_source": allocation_meta,
        "portfolio_health_source": health_meta,
        "source_files_detected": _artifact_paths(diagnostics, "signal_files"),
        "ranking_files_detected": _artifact_paths(diagnostics, "trade_ranking_files"),
        "allocation_files_detected": _artifact_paths(diagnostics, "portfolio_allocation_files"),
        "health_files_detected": _artifact_paths(diagnostics, "portfolio_health_files"),
        "selected_allocation_source": diagnostics.get("selected_allocation_source", allocation_meta.get("selected_source", "none")),
        "selected_ranking_source": diagnostics.get("selected_ranking_source", ranking_meta.get("selected_source", "none")),
        "selected_health_source": diagnostics.get("selected_health_source", health_meta.get("selected_source", "none")),
        "ranking_match_source": ranking_meta.get("source", "none"),
        "allocation_match_source": allocation_meta.get("source", "none"),
        "health_match_source": health_meta.get("source", "none"),
        "ranking_aggregation_method": ranking_meta.get("ranking_aggregation_method", "not_applied"),
        "inferred_rank_source": overlay.get("inferred_rank_source"),
        "expected_value_source_column": overlay.get("expected_value_source_column"),
        "expected_value_raw": overlay.get("expected_value_raw"),
        "expected_value_display": overlay.get("expected_value_display"),
        "expected_value_scale_warning": overlay.get("expected_value_scale_warning"),
        "match_status": match_status,
        "match_reason": match_reason,
        "signal": signal,
        "ranking_record": ranking_record,
        "allocation_record": allocation_record,
        "overlay": overlay,
        "telegram_message": message,
        "output_path": output_path,
    }
    write_report(report, output_path)
    print(message)
    print(f"\nJSON report saved: {output_path}")
    if telegram_preview or send_telegram:
        preview_payload = build_telegram_preview_payload(
            signal=signal,
            overlay=overlay,
            generated_at=generated_at,
            overlay_report_path=output_path,
            send_telegram=send_telegram,
        )
        if send_telegram and not preview_payload["telegram_send_enabled"]:
            print("Telegram Send: BLOCKED — ALLOW_TELEGRAM_SEND not enabled")
        elif preview_payload["telegram_send_enabled"]:
            print("Telegram Send: ENABLED — gated by ALLOW_TELEGRAM_SEND=1")
        else:
            print("Telegram Send: DISABLED / PREVIEW ONLY")
        print("\nTelegram Preview Payload:")
        print(preview_payload["payload_text"])
        write_telegram_preview(preview_payload)
        print(f"\nTelegram preview saved: {TELEGRAM_PREVIEW_PATH}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build PAPER_ONLY ML signal overlay advisory preview.")
    parser.add_argument("--symbol", default="", help="Optional symbol to overlay, e.g. HYPEUSDT.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; never sends Telegram or touches broker APIs.")
    parser.add_argument("--telegram-preview", action="store_true", help="Print and save a Telegram-style preview payload without sending it.")
    parser.add_argument("--send-telegram", action="store_true", help="Request Telegram send; blocked unless ALLOW_TELEGRAM_SEND=1.")
    parser.add_argument("--db-path", default=DB_PATH, help="SQLite database path opened read-only if present.")
    parser.add_argument("--output", default=REPORT_PATH, help="JSON report output path.")
    parser.add_argument("--list-sources", action="store_true", help="Print detected source diagnostics and exit without writing output.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.list_sources:
        print_source_listing(args.db_path)
    else:
        run(
            symbol=args.symbol,
            dry_run=args.dry_run,
            db_path=args.db_path,
            output_path=args.output,
            telegram_preview=args.telegram_preview,
            send_telegram=args.send_telegram,
        )
