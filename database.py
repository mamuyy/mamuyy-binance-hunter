import csv
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List


DEFAULT_DB_PATH = "mamuyy_hunter.db"


SCHEMAS = {
    "signals": """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            price REAL,
            score REAL,
            calculated_score REAL,
            shadow_score REAL,
            penalty_applied INTEGER,
            base_score REAL,
            regime_name TEXT,
            regime_score REAL,
            pre_flow_score REAL,
            flow_adjustment REAL,
            funding_zscore REAL,
            oi_expansion_rate REAL,
            taker_delta REAL,
            pressure_score REAL,
            squeeze_probability REAL,
            flow_state TEXT,
            whale_activity TEXT,
            squeeze_risk TEXT,
            funding_warning TEXT,
            regime_model TEXT,
            regime_model_adjustment REAL,
            adaptive_confidence_score REAL,
            model_confidence REAL,
            expected_behavior TEXT,
            volume_spike REAL,
            breakout INTEGER,
            liquidity_sweep INTEGER,
            taker_buy_ratio REAL,
            funding REAL,
            open_interest REAL,
            data_source TEXT DEFAULT 'LEGACY_UNKNOWN'
        )
    """,
    "paper_trades": """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            entry REAL,
            current_price REAL,
            pnl_percent REAL,
            status TEXT,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            score REAL,
            regime_name TEXT,
            regime_score REAL
        )
    """,
    "flow_logs": """
        CREATE TABLE IF NOT EXISTS flow_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            funding_zscore REAL,
            oi_expansion_rate REAL,
            taker_delta REAL,
            pressure_score REAL,
            squeeze_probability REAL,
            flow_state TEXT,
            whale_activity TEXT,
            squeeze_risk TEXT,
            funding_warning TEXT,
            flow_adjustment REAL,
            final_score REAL,
            data_source TEXT DEFAULT 'LEGACY_UNKNOWN'
        )
    """,
    "regime_logs": """
        CREATE TABLE IF NOT EXISTS regime_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            regime_name TEXT,
            regime_score REAL,
            btc_price REAL,
            btc_change_24h REAL,
            btc_above_ema50 INTEGER,
            btc_above_ema200 INTEGER,
            ema_distance REAL,
            atr_percent REAL,
            volume_ratio REAL,
            btc_volume_dominance REAL,
            funding_rate REAL
        )
    """,
    "ml_results": """
        CREATE TABLE IF NOT EXISTS ml_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model_ready INTEGER,
            accuracy REAL,
            precision REAL,
            recall REAL,
            ai_confidence_score REAL,
            setup_ranking TEXT,
            most_profitable_regime TEXT,
            worst_regime TEXT,
            payload_json TEXT
        )
    """,
    "walkforward_results": """
        CREATE TABLE IF NOT EXISTS walkforward_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            fold INTEGER,
            train_start TEXT,
            train_end TEXT,
            test_start TEXT,
            test_end TEXT,
            train_accuracy REAL,
            test_accuracy REAL,
            precision REAL,
            recall REAL,
            profit_factor REAL,
            winrate REAL,
            best_regime TEXT,
            worst_regime TEXT
        )
    """,
    "shadow_trades": """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            lifecycle_status TEXT,
            regime_name TEXT,
            signal_score REAL,
            expected_fill REAL,
            simulated_live_fill REAL,
            execution_drift REAL,
            latency_impact REAL,
            prediction_drift REAL,
            regime_drift REAL,
            exposure REAL,
            pnl_percent REAL,
            execution_quality_score REAL
        )
    """,
    "historical_klines": """
        CREATE TABLE IF NOT EXISTS historical_klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            interval TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            quote_asset_volume REAL,
            number_of_trades REAL,
            taker_buy_base_asset_volume REAL,
            taker_buy_quote_asset_volume REAL
        )
    """,
    "historical_funding": """
        CREATE TABLE IF NOT EXISTS historical_funding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            funding_rate REAL
        )
    """,
    "historical_open_interest": """
        CREATE TABLE IF NOT EXISTS historical_open_interest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            open_interest REAL
        )
    """,
    "historical_outcomes": """
        CREATE TABLE IF NOT EXISTS historical_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp TEXT,
            close_timestamp TEXT,
            symbol TEXT,
            entry REAL,
            exit_price REAL,
            pnl_pct REAL,
            status TEXT,
            win_loss TEXT,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            score REAL,
            holding_candles INTEGER,
            exit_reason TEXT
        )
    """,
    "risk_events": """
        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            status TEXT,
            safe INTEGER,
            risk_score REAL,
            position_multiplier REAL,
            reasons_json TEXT,
            ml_accuracy REAL,
            model_confidence REAL,
            drawdown REAL,
            regime_name TEXT,
            heartbeat_age_minutes REAL,
            open_trades INTEGER,
            consecutive_losses INTEGER,
            session_name TEXT,
            action TEXT,
            result TEXT,
            dry_run INTEGER,
            reason TEXT
        )
    """,
    "runtime_heartbeats": """
        CREATE TABLE IF NOT EXISTS runtime_heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            source TEXT,
            state TEXT,
            system_health_score REAL,
            scheduler TEXT,
            uptime_seconds REAL,
            message TEXT
        )
    """,
    "internal_paper_trades": """
        CREATE TABLE IF NOT EXISTS internal_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            source_signal_timestamp TEXT,
            symbol TEXT,
            market_type TEXT,
            side TEXT,
            entry_price REAL,
            current_price REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            exit_price REAL,
            pnl REAL,
            confidence REAL,
            regime TEXT,
            macro_state TEXT,
            allocation_tier TEXT,
            status TEXT,
            exit_reason TEXT,
            updated_at TEXT,
            payload_json TEXT,
            prediction_id TEXT,
            predicted_probability REAL,
            model_version TEXT,
            evaluation_contract TEXT,
            target_timestamp TEXT
        )
    """,
    "broadcast_events": """
        CREATE TABLE IF NOT EXISTS broadcast_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            confidence REAL,
            macro_state TEXT,
            allocation_tier TEXT,
            target_name TEXT,
            target_type TEXT,
            target_profile TEXT,
            route_status TEXT,
            route_reason TEXT,
            payload_hash TEXT
        )
    """,
    "telegram_events": """
        CREATE TABLE IF NOT EXISTS telegram_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            event_type TEXT,
            message TEXT,
            send_status TEXT,
            error_message TEXT
        )
    """,
}


INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_signals_source_timestamp ON signals(data_source, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_signals_source_symbol_timestamp ON signals(data_source, symbol, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_timestamp ON paper_trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_flow_logs_timestamp ON flow_logs(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_flow_logs_symbol ON flow_logs(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_flow_logs_source_timestamp ON flow_logs(data_source, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_regime_logs_timestamp ON regime_logs(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_ml_results_timestamp ON ml_results(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_walkforward_results_timestamp ON walkforward_results(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_trades_timestamp ON shadow_trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_trades_symbol ON shadow_trades(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_historical_klines_timestamp ON historical_klines(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_historical_klines_symbol ON historical_klines(symbol)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_klines_unique ON historical_klines(timestamp, symbol, interval)",
    "CREATE INDEX IF NOT EXISTS idx_historical_funding_timestamp ON historical_funding(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_historical_funding_symbol ON historical_funding(symbol)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_funding_unique ON historical_funding(timestamp, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_historical_open_interest_timestamp ON historical_open_interest(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_historical_open_interest_symbol ON historical_open_interest(symbol)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_open_interest_unique ON historical_open_interest(timestamp, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_historical_outcomes_signal_timestamp ON historical_outcomes(signal_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_historical_outcomes_symbol ON historical_outcomes(symbol)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_outcomes_unique ON historical_outcomes(symbol, signal_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_risk_events_timestamp ON risk_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_risk_events_status ON risk_events(status)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_timestamp ON runtime_heartbeats(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_source ON runtime_heartbeats(source)",
    "CREATE INDEX IF NOT EXISTS idx_internal_paper_trades_timestamp ON internal_paper_trades(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_internal_paper_trades_symbol ON internal_paper_trades(symbol)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_internal_paper_unique ON internal_paper_trades(symbol, source_signal_timestamp, side)",
    "CREATE INDEX IF NOT EXISTS idx_broadcast_events_timestamp ON broadcast_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_broadcast_events_symbol ON broadcast_events(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_broadcast_events_target ON broadcast_events(target_name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_payload_target_unique ON broadcast_events(payload_hash, target_name)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_events_timestamp ON telegram_events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_events_type ON telegram_events(event_type)",
]


CSV_MIGRATIONS = {
    "signals": "signals_log.csv",
    "paper_trades": "paper_trades.csv",
    "flow_logs": "flow_log.csv",
    "regime_logs": "regime_history.csv",
    "walkforward_results": "walkforward_results.csv",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _to_number(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return int(value.lower() == "true")
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def sqlite_path(database_url: str = "") -> str:
    if database_url and _is_postgres(database_url):
        raise NotImplementedError(
            "PostgreSQL is optional but no PostgreSQL driver is bundled. "
            "Use SQLite DATABASE_PATH by default."
        )
    if not database_url:
        return DEFAULT_DB_PATH
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "", 1)
    if database_url.startswith("sqlite://"):
        return database_url.replace("sqlite://", "", 1)
    return database_url


def get_connection(database_url: str = "") -> sqlite3.Connection:
    path = sqlite_path(database_url)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(database_url: str = "") -> None:
    with get_connection(database_url) as connection:
        for schema in SCHEMAS.values():
            connection.execute(schema)
        _ensure_columns(connection)
        for index_sql in INDEXES:
            connection.execute(index_sql)
        connection.commit()


def _ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(signals)")}
    columns = {
        "regime_model": "TEXT",
        "regime_model_adjustment": "REAL",
        "adaptive_confidence_score": "REAL",
        "model_confidence": "REAL",
        "expected_behavior": "TEXT",
        "calculated_score": "REAL",
        "shadow_score": "REAL",
        "penalty_applied": "INTEGER",
        "data_source": "TEXT DEFAULT 'LEGACY_UNKNOWN'",
    }
    for column, column_type in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE signals ADD COLUMN {column} {column_type}")

    flow_existing = {row["name"] for row in connection.execute("PRAGMA table_info(flow_logs)")}
    if "data_source" not in flow_existing:
        connection.execute("ALTER TABLE flow_logs ADD COLUMN data_source TEXT DEFAULT 'LEGACY_UNKNOWN'")

    internal_paper_existing = {row["name"] for row in connection.execute("PRAGMA table_info(internal_paper_trades)")}
    internal_paper_columns = {
        "current_price": "REAL",
        "sl": "REAL",
        "tp1": "REAL",
        "tp2": "REAL",
        "exit_reason": "TEXT",
        "updated_at": "TEXT",
        "prediction_id": "TEXT",
        "predicted_probability": "REAL",
        "model_version": "TEXT",
        "evaluation_contract": "TEXT",
        "target_timestamp": "TEXT",
    }
    for column, column_type in internal_paper_columns.items():
        if column not in internal_paper_existing:
            connection.execute(f"ALTER TABLE internal_paper_trades ADD COLUMN {column} {column_type}")

    risk_existing = {row["name"] for row in connection.execute("PRAGMA table_info(risk_events)")}
    risk_columns = {
        "session_name": "TEXT",
        "action": "TEXT",
        "result": "TEXT",
        "dry_run": "INTEGER",
        "reason": "TEXT",
    }
    for column, column_type in risk_columns.items():
        if column not in risk_existing:
            connection.execute(f"ALTER TABLE risk_events ADD COLUMN {column} {column_type}")


def _columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def insert_row(table: str, data: Dict[str, Any], database_url: str = "") -> None:
    init_db(database_url)
    with get_connection(database_url) as connection:
        columns = [column for column in _columns(connection, table) if column != "id"]
        row = {column: _to_number(data.get(column)) for column in columns}
        if "timestamp" in row and not row["timestamp"]:
            row["timestamp"] = _now()
        placeholders = ", ".join(["?"] * len(row))
        column_sql = ", ".join(row.keys())
        connection.execute(
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
            list(row.values()),
        )
        connection.commit()


def insert_signal(signal: Dict[str, Any], database_url: str = "") -> None:
    row = dict(signal)
    row.setdefault("data_source", "LIVE_SCANNER")
    insert_row("signals", row, database_url)


def insert_paper_trade(trade: Dict[str, Any], database_url: str = "") -> None:
    insert_row("paper_trades", trade, database_url)


def insert_flow_log(flow: Dict[str, Any], database_url: str = "") -> None:
    row = dict(flow)
    row.setdefault("data_source", "LIVE_SCANNER")
    insert_row("flow_logs", row, database_url)


def insert_regime_log(regime: Dict[str, Any], database_url: str = "") -> None:
    insert_row("regime_logs", regime, database_url)


def insert_ml_result(result: Dict[str, Any], database_url: str = "") -> None:
    insert_row(
        "ml_results",
        {
            "timestamp": _now(),
            "model_ready": int(bool(result.get("model_ready"))),
            "accuracy": result.get("accuracy"),
            "precision": result.get("precision"),
            "recall": result.get("recall"),
            "ai_confidence_score": result.get("ai_confidence_score"),
            "setup_ranking": result.get("setup_ranking"),
            "most_profitable_regime": result.get("most_profitable_regime"),
            "worst_regime": result.get("worst_regime"),
            "payload_json": json.dumps(result, default=str),
        },
        database_url,
    )


def insert_walkforward_rows(rows: Iterable[Dict[str, Any]], database_url: str = "") -> int:
    count = 0
    for row in rows:
        insert_row("walkforward_results", {"timestamp": _now(), **row}, database_url)
        count += 1
    return count


def insert_runtime_heartbeat(heartbeat: Dict[str, Any], database_url: str = "") -> None:
    init_db(database_url)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with get_connection(database_url) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                columns = [column for column in _columns(connection, "runtime_heartbeats") if column != "id"]
                row = {column: _to_number(heartbeat.get(column)) for column in columns}
                if not row.get("timestamp"):
                    row["timestamp"] = _now()
                placeholders = ", ".join(["?"] * len(row))
                column_sql = ", ".join(row.keys())
                connection.execute(
                    f"INSERT INTO runtime_heartbeats ({column_sql}) VALUES ({placeholders})",
                    list(row.values()),
                )
                connection.commit()
                return
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    if last_error:
        raise last_error


def query(table: str, limit: int = 50, database_url: str = "") -> List[Dict[str, Any]]:
    init_db(database_url)
    with get_connection(database_url) as connection:
        rows = connection.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def migrate_csv_to_db(csv_paths: Dict[str, str] | None = None, database_url: str = "") -> Dict[str, int]:
    init_db(database_url)
    paths = csv_paths or CSV_MIGRATIONS
    migrated = {}
    for table, path in paths.items():
        migrated[table] = 0
        if not os.path.exists(path):
            continue
        with get_connection(database_url) as connection:
            existing = connection.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()["total"]
        if existing:
            continue
        with open(path, newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                if not any(row.values()):
                    continue
                insert_row(table, row, database_url)
                migrated[table] += 1
    return migrated


def backup_database(db_path: str = DEFAULT_DB_PATH, backup_dir: str = "db_backups") -> str:
    if not os.path.exists(db_path):
        init_db(db_path)
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"mamuyy_hunter_{timestamp}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path


def db_health_check(database_url: str = "", migrate_csv: bool = True, backup: bool = True) -> Dict[str, Any]:
    health = {
        "ok": False,
        "database": database_url or DEFAULT_DB_PATH,
        "tables": {},
        "migrated": {},
        "backup_path": "",
        "errors": [],
    }
    try:
        init_db(database_url)
        if migrate_csv:
            health["migrated"] = migrate_csv_to_db(database_url=database_url)
        with get_connection(database_url) as connection:
            for table in SCHEMAS:
                count = connection.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()["total"]
                health["tables"][table] = int(count)
        if backup and not _is_postgres(database_url):
            db_path = sqlite_path(database_url)
            health["backup_path"] = backup_database(db_path)
        health["ok"] = True
    except Exception as exc:
        health["errors"].append(str(exc))
    return health


def top_profitable_setup(database_url: str = "", limit: int = 10) -> List[Dict[str, Any]]:
    init_db(database_url)
    sql = """
        SELECT s.symbol, s.regime_name, s.whale_activity, COUNT(*) AS trades,
               AVG(p.pnl_percent) AS avg_pnl, SUM(p.pnl_percent) AS total_pnl
        FROM paper_trades p
        LEFT JOIN signals s ON s.symbol = p.symbol
        GROUP BY s.symbol, s.regime_name, s.whale_activity
        ORDER BY total_pnl DESC
        LIMIT ?
    """
    with get_connection(database_url) as connection:
        return [dict(row) for row in connection.execute(sql, (limit,)).fetchall()]


def best_regime(database_url: str = "") -> Dict[str, Any]:
    return _best_group("regime_name", database_url)


def best_symbol(database_url: str = "") -> Dict[str, Any]:
    return _best_group("symbol", database_url)


def worst_symbol(database_url: str = "") -> Dict[str, Any]:
    return _best_group("symbol", database_url, ascending=True)


def _best_group(column: str, database_url: str = "", ascending: bool = False) -> Dict[str, Any]:
    init_db(database_url)
    order = "ASC" if ascending else "DESC"
    sql = f"""
        SELECT {column} AS name, COUNT(*) AS trades, AVG(pnl_percent) AS avg_pnl,
               SUM(pnl_percent) AS total_pnl
        FROM paper_trades
        GROUP BY {column}
        ORDER BY total_pnl {order}
        LIMIT 1
    """
    with get_connection(database_url) as connection:
        row = connection.execute(sql).fetchone()
        return dict(row) if row else {}


def feature_profitability(database_url: str = "") -> List[Dict[str, Any]]:
    init_db(database_url)
    features = ["regime_name", "score", "status"]
    rows = []
    with get_connection(database_url) as connection:
        for feature in features:
            sql = f"""
                SELECT '{feature}' AS feature, {feature} AS value, COUNT(*) AS trades,
                       AVG(pnl_percent) AS avg_pnl, SUM(pnl_percent) AS total_pnl
                FROM paper_trades
                GROUP BY {feature}
                ORDER BY total_pnl DESC
            """
            rows.extend(dict(row) for row in connection.execute(sql).fetchall())
    return rows
