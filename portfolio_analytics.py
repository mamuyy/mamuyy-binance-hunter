import math
import os
import sqlite3
from typing import Any, Dict, List

import pandas as pd


TIMESTAMP_CANDIDATES = ["timestamp", "entry_timestamp", "exit_timestamp", "closed_at"]
INTERNAL_COLUMNS = [
    "timestamp",
    "entry_timestamp",
    "exit_timestamp",
    "closed_at",
    "symbol",
    "side",
    "pnl",
    "confidence",
    "macro_state",
    "competition_profile",
    "allocation_tier",
    "status",
]
PAPER_COLUMNS = [
    "timestamp",
    "entry_timestamp",
    "exit_timestamp",
    "closed_at",
    "symbol",
    "pnl_percent",
    "status",
    "score",
    "macro_state",
    "competition_profile",
    "regime_name",
]


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame()


def _read_only_connection(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _read_table(connection: sqlite3.Connection, table: str, columns: List[str]) -> pd.DataFrame:
    if not _table_exists(connection, table):
        return _empty_frame()
    available = [
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    selected = [column for column in columns if column in available]
    if not selected:
        return _empty_frame()
    return pd.read_sql_query(
        f"SELECT {', '.join(selected)} FROM {table}",
        connection,
    )


def _detect_timestamp_column(df: pd.DataFrame) -> str | None:
    for column in TIMESTAMP_CANDIDATES:
        if column in df.columns:
            return column
    return None


def _normalize_internal(df: pd.DataFrame) -> tuple[pd.DataFrame, List[str]]:
    warnings: List[str] = []
    if df.empty:
        return _empty_frame(), warnings
    if "pnl" not in df.columns:
        return _empty_frame(), ["internal_paper_trades has no pnl column."]

    timestamp_column = _detect_timestamp_column(df)
    if not timestamp_column:
        warnings.append("internal_paper_trades has no timestamp-like column.")

    normalized = pd.DataFrame()
    normalized["timestamp"] = pd.to_datetime(
        df[timestamp_column] if timestamp_column else pd.NaT,
        errors="coerce",
        utc=True,
    )
    normalized["symbol"] = df.get("symbol", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    normalized["side"] = df.get("side", pd.Series(["-"] * len(df))).fillna("-").astype(str)
    normalized["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    normalized["macro_state"] = df.get("macro_state", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    normalized["competition_profile"] = df.get("competition_profile", pd.Series(["DEFAULT"] * len(df))).fillna("DEFAULT").astype(str)
    normalized["status"] = df.get("status", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    normalized["source"] = "internal_paper_trades"
    return normalized.dropna(subset=["pnl"]), warnings


def _normalize_paper(df: pd.DataFrame) -> tuple[pd.DataFrame, List[str]]:
    warnings: List[str] = []
    if df.empty:
        return _empty_frame(), warnings
    if "pnl_percent" not in df.columns:
        return _empty_frame(), ["paper_trades has no pnl_percent column."]

    timestamp_column = _detect_timestamp_column(df)
    if not timestamp_column:
        warnings.append("paper_trades has no timestamp-like column.")

    normalized = pd.DataFrame()
    normalized["timestamp"] = pd.to_datetime(
        df[timestamp_column] if timestamp_column else pd.NaT,
        errors="coerce",
        utc=True,
    )
    normalized["symbol"] = df.get("symbol", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    normalized["side"] = "-"
    normalized["pnl"] = pd.to_numeric(df["pnl_percent"], errors="coerce")
    normalized["macro_state"] = df.get("macro_state", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    if "regime_name" in df.columns:
        normalized["macro_state"] = normalized["macro_state"].where(
            normalized["macro_state"] != "UNKNOWN",
            df["regime_name"].fillna("UNKNOWN").astype(str),
        )
    normalized["competition_profile"] = df.get("competition_profile", pd.Series(["DEFAULT"] * len(df))).fillna("DEFAULT").astype(str)
    normalized["status"] = df.get("status", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)
    normalized["source"] = "paper_trades"
    return normalized.dropna(subset=["pnl"]), warnings


def load_portfolio_trades(db_path: str = "mamuyy_hunter.db") -> tuple[pd.DataFrame, List[str]]:
    warnings: List[str] = []
    try:
        with _read_only_connection(db_path) as connection:
            internal_raw = _read_table(connection, "internal_paper_trades", INTERNAL_COLUMNS)
            paper_raw = _read_table(connection, "paper_trades", PAPER_COLUMNS)
    except (FileNotFoundError, sqlite3.Error) as exc:
        return _empty_frame(), [f"Read-only SQLite load failed: {exc}"]

    internal, internal_warnings = _normalize_internal(internal_raw)
    paper, paper_warnings = _normalize_paper(paper_raw)
    warnings.extend(internal_warnings)
    warnings.extend(paper_warnings)

    frames = [frame for frame in [internal, paper] if not frame.empty]
    if not frames:
        return _empty_frame(), warnings
    trades = pd.concat(frames, ignore_index=True)
    trades = trades.sort_values(["timestamp", "symbol"], na_position="last").reset_index(drop=True)
    trades["trade_index"] = range(1, len(trades) + 1)
    return trades, warnings


def _profit_factor(pnl: pd.Series) -> float:
    pnl = pd.to_numeric(pnl, errors="coerce").fillna(0.0)
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _metrics(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty or "pnl" not in df.columns:
        return {
            "trade_count": 0,
            "total_pnl": 0.0,
            "winrate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "average_trade_pnl": 0.0,
        }
    pnl = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
    return {
        "trade_count": int(len(pnl)),
        "total_pnl": round(float(pnl.sum()), 4),
        "winrate": round(float((pnl > 0).mean() * 100), 2) if len(pnl) else 0.0,
        "profit_factor": round(_profit_factor(pnl), 4) if not math.isinf(_profit_factor(pnl)) else math.inf,
        "max_drawdown": round(float(drawdown.min()), 4) if not drawdown.empty else 0.0,
        "average_trade_pnl": round(float(pnl.mean()), 4) if len(pnl) else 0.0,
    }


def _group_performance(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if df.empty or column not in df.columns:
        return _empty_frame()
    rows = []
    for name, group in df.groupby(column, dropna=False):
        metric = _metrics(group)
        rows.append({column: name if pd.notna(name) else "UNKNOWN", **metric})
    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows).sort_values(["total_pnl", "trade_count"], ascending=[False, False])


def calculate_portfolio_analytics(db_path: str = "mamuyy_hunter.db") -> Dict[str, Any]:
    trades, warnings = load_portfolio_trades(db_path)
    if trades.empty:
        return {
            "ok": True,
            "warnings": warnings,
            "metrics": _metrics(trades),
            "trades": trades,
            "equity_curve": _empty_frame(),
            "macro_performance": _empty_frame(),
            "competition_performance": _empty_frame(),
            "macro_survival": _empty_frame(),
        }

    trades = trades.copy()
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0.0)
    trades["equity"] = trades["pnl"].cumsum()
    trades["running_max"] = trades["equity"].cummax()
    trades["drawdown"] = trades["equity"] - trades["running_max"]
    trades["rolling_pnl_10"] = trades["pnl"].rolling(10, min_periods=1).sum()
    trades["rolling_winrate_10"] = (
        (trades["pnl"] > 0).astype(float).rolling(10, min_periods=1).mean() * 100
    )

    equity_columns = [
        "trade_index",
        "timestamp",
        "symbol",
        "source",
        "pnl",
        "equity",
        "drawdown",
        "rolling_pnl_10",
        "rolling_winrate_10",
        "macro_state",
        "competition_profile",
    ]
    macro_performance = _group_performance(trades, "macro_state")
    competition_performance = _group_performance(trades, "competition_profile")
    macro_survival = macro_performance[
        macro_performance["macro_state"].astype(str).str.upper().isin(["HIGH_STRESS", "PANIC", "RISK_ON"])
    ].copy() if not macro_performance.empty else _empty_frame()

    return {
        "ok": True,
        "warnings": warnings,
        "metrics": _metrics(trades),
        "trades": trades,
        "equity_curve": trades[[column for column in equity_columns if column in trades.columns]],
        "macro_performance": macro_performance,
        "competition_performance": competition_performance,
        "macro_survival": macro_survival,
    }
