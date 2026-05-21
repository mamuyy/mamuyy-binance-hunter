#!/usr/bin/env python3
"""Audit paper trading drawdown and open-trade risk contributors.

Read-only utility for local SQLite telemetry. Does not mutate DB/schema.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd


TABLES = ["paper_trades", "shadow_trades", "internal_paper_trades"]


@dataclass
class SourceFrame:
    source: str
    df: pd.DataFrame
    available: bool
    reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit PAPER_ONLY trade telemetry from SQLite.")
    parser.add_argument("--db", default="mamuyy_hunter.db", help="Path to sqlite database.")
    parser.add_argument("--top", type=int, default=5, help="Top symbols count for open floating loss.")
    return parser.parse_args()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def choose_col(columns: List[str], candidates: List[str]) -> Optional[str]:
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def load_source(conn: sqlite3.Connection, table: str) -> SourceFrame:
    if not table_exists(conn, table):
        return SourceFrame(table, pd.DataFrame(), False, "table not found")

    df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
    if df.empty:
        return SourceFrame(table, df, True, "table available but empty")

    df.columns = [str(c) for c in df.columns]
    cols = list(df.columns)

    ts_col = choose_col(cols, ["timestamp", "created_at", "signal_timestamp", "entry_timestamp", "open_time"])
    symbol_col = choose_col(cols, ["symbol", "coin", "asset"])
    status_col = choose_col(cols, ["status", "lifecycle_status", "state"])
    pnl_col = choose_col(cols, ["pnl_percent", "pnl_pct", "pnl", "unrealized_pnl_percent"])
    score_col = choose_col(cols, ["score", "final_score", "signal_score"])
    regime_col = choose_col(cols, ["regime_name", "market_regime", "regime"])
    entry_col = choose_col(cols, ["entry", "entry_price", "entry_px"])
    price_col = choose_col(cols, ["current_price", "mark_price", "last_price", "price"])

    normalized = pd.DataFrame()
    normalized["source"] = table
    normalized["symbol"] = df[symbol_col] if symbol_col else "UNKNOWN"
    normalized["status_raw"] = df[status_col].astype(str) if status_col else ""

    if pnl_col:
        normalized["pnl_percent"] = pd.to_numeric(df[pnl_col], errors="coerce")
    elif entry_col and price_col:
        entry = pd.to_numeric(df[entry_col], errors="coerce")
        current = pd.to_numeric(df[price_col], errors="coerce")
        normalized["pnl_percent"] = ((current - entry) / entry) * 100.0
    else:
        normalized["pnl_percent"] = pd.NA

    normalized["score"] = pd.to_numeric(df[score_col], errors="coerce") if score_col else pd.NA
    normalized["regime_name"] = df[regime_col].astype(str) if regime_col else "UNKNOWN"

    if ts_col:
        normalized["timestamp"] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    else:
        normalized["timestamp"] = pd.NaT

    status_upper = normalized["status_raw"].str.upper()
    normalized["is_open"] = (
        status_upper.str.contains("OPEN", na=False)
        | status_upper.str.contains("TP1 HIT", na=False)
    ) & ~status_upper.str.contains("CLOSED|WIN|LOSS|REJECT", na=False)

    return SourceFrame(table, normalized, True)


def fmt_table(df: pd.DataFrame, index: bool = False) -> str:
    if df.empty:
        return "(no data)"
    return df.to_string(index=index)


def score_bucket(score: float) -> str:
    if pd.isna(score):
        return "UNKNOWN"
    if score >= 85:
        return "85+ (HIGH)"
    if score >= 75:
        return "75-84 (BREAKOUT)"
    if score >= 60:
        return "60-74 (MEDIUM)"
    return "<60 (LOW)"


def hostile_regime(regime: str) -> bool:
    token = str(regime).upper()
    hostile_tokens = ["RISK OFF", "PANIC", "TRENDING BEAR", "HIGH VOLATILITY", "SIDEWAYS / CHOPPY"]
    return any(t in token for t in hostile_tokens)


def main() -> int:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    try:
        sources = [load_source(conn, table) for table in TABLES]
    finally:
        conn.close()

    frames = [s.df for s in sources if s.available and not s.df.empty]

    print("=" * 88)
    print("MAMUYY PAPER TRADES AUDIT (READ-ONLY)")
    print(f"Database: {args.db}")
    print("=" * 88)

    availability = pd.DataFrame(
        [
            {
                "source": s.source,
                "available": s.available,
                "rows": int(len(s.df)) if s.available else 0,
                "note": s.reason,
            }
            for s in sources
        ]
    )
    print("\n[1] Source availability")
    print(fmt_table(availability))

    if not frames:
        print("\nNo trade rows found in the requested sources.")
        return 0

    all_df = pd.concat(frames, ignore_index=True)
    open_df = all_df[all_df["is_open"]].copy()

    # 6) OPEN count per source
    open_count = (
        open_df.groupby("source", dropna=False)
        .size()
        .rename("open_trades")
        .reset_index()
        .sort_values("open_trades", ascending=False)
    )
    print("\n[2] OPEN trades per source")
    print(fmt_table(open_count))

    # 2) top symbols floating loss open
    open_loss = open_df.copy()
    open_loss["pnl_percent"] = pd.to_numeric(open_loss["pnl_percent"], errors="coerce")
    open_loss = open_loss.dropna(subset=["pnl_percent"])
    open_loss = open_loss[open_loss["pnl_percent"] < 0]
    top_loss = (
        open_loss.groupby("symbol", dropna=False)["pnl_percent"]
        .agg(open_positions="count", total_floating_loss_pct="sum", avg_floating_loss_pct="mean", worst_trade_pct="min")
        .sort_values("total_floating_loss_pct")
        .head(max(args.top, 1))
        .reset_index()
    )
    print(f"\n[3] Top {max(args.top,1)} OPEN symbol contributors (floating loss)")
    print(fmt_table(top_loss))

    # 3) avg holding period
    now = datetime.now(timezone.utc)
    print("\n[4] OPEN holding period")
    if "timestamp" not in open_df.columns or open_df.empty or not open_df["timestamp"].notna().any():
        print("timestamp/holding-period unavailable (no open rows with parsable timestamps).")
    else:
        hold = open_df.dropna(subset=["timestamp"]).copy()
        hold["holding_hours"] = (now - hold["timestamp"]).dt.total_seconds() / 3600.0
        hold = hold[pd.to_numeric(hold["holding_hours"], errors="coerce").notna()]
        if hold.empty:
            print("timestamp/holding-period unavailable (holding hours could not be computed).")
        else:
            hold_summary = pd.DataFrame(
                [
                    {
                        "open_positions_with_timestamp": int(len(hold)),
                        "avg_holding_hours": round(float(hold["holding_hours"].mean()), 2),
                        "median_holding_hours": round(float(hold["holding_hours"].median()), 2),
                        "max_holding_hours": round(float(hold["holding_hours"].max()), 2),
                    }
                ]
            )
            print(fmt_table(hold_summary))

    # 4) loss grouped by regime, score bucket, symbol
    loss_df = all_df.copy()
    loss_df["pnl_percent"] = pd.to_numeric(loss_df["pnl_percent"], errors="coerce")
    loss_df = loss_df.dropna(subset=["pnl_percent"])
    loss_df = loss_df[loss_df["pnl_percent"] < 0].copy()
    loss_df["score_bucket"] = loss_df["score"].apply(score_bucket)
    grouped = (
        loss_df.groupby(["regime_name", "score_bucket", "symbol"], dropna=False)["pnl_percent"]
        .agg(loss_trades="count", total_loss_pct="sum", avg_loss_pct="mean")
        .sort_values("total_loss_pct")
        .head(20)
        .reset_index()
    )
    print("\n[5] Loss clusters (regime_name x score_bucket x symbol), top 20 worst")
    print(fmt_table(grouped))

    # 5) breakout/high-score vs hostile regime dominance
    print("\n[6] Loss attribution flags")
    if loss_df.empty:
        print("No negative-loss rows available for attribution analysis.")
    else:
        loss_df["is_breakout_or_high"] = (pd.to_numeric(loss_df.get("score"), errors="coerce").fillna(-1) >= 75)
        loss_df["is_hostile_regime"] = loss_df.get("regime_name", pd.Series(index=loss_df.index, dtype="object")).fillna("").apply(hostile_regime)

        breakout_flag = loss_df.get("is_breakout_or_high", pd.Series(False, index=loss_df.index)).fillna(False).astype(bool)
        hostile_flag = loss_df.get("is_hostile_regime", pd.Series(False, index=loss_df.index)).fillna(False).astype(bool)

        breakdown = pd.DataFrame(
            [
                {
                    "bucket": "breakout/high-score losses",
                    "loss_trades": int(breakout_flag.sum()),
                    "total_loss_pct": round(float(loss_df.loc[breakout_flag, "pnl_percent"].sum()), 2),
                },
                {
                    "bucket": "hostile-regime losses",
                    "loss_trades": int(hostile_flag.sum()),
                    "total_loss_pct": round(float(loss_df.loc[hostile_flag, "pnl_percent"].sum()), 2),
                },
            ]
        )
        print(fmt_table(breakdown))

    # 7) drawdown sanity analysis
    closed_like = all_df.copy()
    status = closed_like["status_raw"].str.upper()
    closed_like = closed_like[
        status.str.contains("WIN|LOSS|CLOSED|CLOSE", na=False) & ~status.str.contains("OPEN", na=False)
    ].copy()
    closed_like["pnl_percent"] = pd.to_numeric(closed_like["pnl_percent"], errors="coerce")
    closed_like = closed_like.dropna(subset=["pnl_percent"])

    def cum_dd(series: pd.Series) -> float:
        if series.empty:
            return float("nan")
        eq = series.cumsum()
        dd = (eq - eq.cummax()).min()
        return float(dd)

    dd_open = cum_dd(open_loss["pnl_percent"]) if not open_loss.empty else float("nan")
    dd_closed = cum_dd(closed_like["pnl_percent"]) if not closed_like.empty else float("nan")
    dd_all = cum_dd(loss_df["pnl_percent"]) if not loss_df.empty else float("nan")

    diagnosis: List[str] = []
    if not math.isnan(dd_open) and dd_open < -500:
        diagnosis.append("Open floating-loss aggregation alone can generate extreme DD.")
    if not math.isnan(dd_closed) and dd_closed > -100 and (math.isnan(dd_open) or dd_open < dd_closed * 5):
        diagnosis.append("Closed-trade DD far smaller than open DD => likely trade lifecycle backlog or telemetry aggregation mismatch.")
    if math.isnan(dd_closed):
        diagnosis.append("No closed-like trades detected; DD likely dominated by floating/open telemetry.")
    if not diagnosis:
        diagnosis.append("Need manual cross-check against risk_events/model_output telemetry sources.")

    dd_table = pd.DataFrame(
        [
            {"metric": "drawdown_from_open_negative_trades_pct", "value": None if math.isnan(dd_open) else round(dd_open, 2)},
            {"metric": "drawdown_from_closed_like_trades_pct", "value": None if math.isnan(dd_closed) else round(dd_closed, 2)},
            {"metric": "drawdown_from_all_negative_rows_pct", "value": None if math.isnan(dd_all) else round(dd_all, 2)},
            {"metric": "open_trade_rows", "value": int(len(open_df))},
            {"metric": "closed_like_rows", "value": int(len(closed_like))},
        ]
    )
    print("\n[7] Drawdown sanity check")
    print(fmt_table(dd_table))
    print("\n[8] Diagnosis hints")
    for item in diagnosis:
        print(f"- {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
