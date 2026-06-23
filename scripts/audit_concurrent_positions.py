#!/usr/bin/env python3
"""
audit_concurrent_positions.py — Read-only diagnostic for concurrent open positions.

Checks internal_paper_trades and paper_trades for symbols with multiple
concurrent OPEN / TP1 HIT positions. Flags symbols exceeding per-symbol or
global limits.

Zero DB writes. Outputs reports/concurrent_positions_audit.json and prints summary.
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "mamuyy_hunter.db"
REPORT_PATH = (
    Path(__file__).parent.parent / "reports" / "concurrent_positions_audit.json"
)

MAX_PER_SYMBOL = 3
MAX_GLOBAL = 20
OPEN_STATUSES = {"OPEN", "TP1 HIT", "TP1_HIT"}


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def main() -> None:
    now = utcnow()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    positions = []
    for table in ("internal_paper_trades", "paper_trades"):
        cur.execute(f"SELECT * FROM {table}")  # noqa: S608
        for row in cur.fetchall():
            d = dict(row)
            if d.get("status") not in OPEN_STATUSES:
                continue
            positions.append(
                {
                    "source_table": table,
                    "id": d.get("id"),
                    "symbol": d.get("symbol", "UNKNOWN"),
                    "status": d.get("status"),
                    "timestamp": d.get("timestamp") or d.get("updated_at"),
                    "sl": d.get("sl"),
                    "tp1": d.get("tp1"),
                    "tp2": d.get("tp2"),
                    "entry_price": d.get("entry_price") or d.get("entry"),
                    "confidence": d.get("confidence"),
                }
            )

    conn.close()

    # ── Group by symbol ───────────────────────────────────────────────────────
    by_symbol: dict[str, list] = defaultdict(list)
    for p in positions:
        by_symbol[p["symbol"]].append(p)

    symbol_summaries = []
    over_limit_symbols = []

    for symbol, trades in sorted(by_symbol.items()):
        count = len(trades)
        flag = "OVER_LIMIT" if count > MAX_PER_SYMBOL else "OK"
        if flag == "OVER_LIMIT":
            over_limit_symbols.append(symbol)
        symbol_summaries.append(
            {
                "symbol": symbol,
                "concurrent_count": count,
                "flag": flag,
                "positions": trades,
            }
        )

    global_count = len(positions)
    global_flag = "OVER_LIMIT" if global_count > MAX_GLOBAL else "OK"

    report = {
        "generated_at": now.isoformat(),
        "policy": {
            "MAX_CONCURRENT_PER_SYMBOL": MAX_PER_SYMBOL,
            "MAX_CONCURRENT_GLOBAL": MAX_GLOBAL,
        },
        "global_open_count": global_count,
        "global_flag": global_flag,
        "symbols_over_limit": over_limit_symbols,
        "symbol_count": len(by_symbol),
        "by_symbol": symbol_summaries,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    # ── Console summary ───────────────────────────────────────────────────────
    print("=" * 60)
    print("CONCURRENT POSITIONS AUDIT")
    print("=" * 60)
    print(f"Run at (UTC):        {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Policy limits:       per-symbol={MAX_PER_SYMBOL}  global={MAX_GLOBAL}")
    print(f"Total open positions:{global_count}  [{global_flag}]")
    print(f"Unique symbols:      {len(by_symbol)}")
    print(f"Symbols over limit:  {len(over_limit_symbols)}")
    print()

    if symbol_summaries:
        print(f"{'Symbol':<14} {'Count':>6}  {'Flag':<12}  {'SL / TP1 / TP2 (first pos)'}")
        print("-" * 72)
        for s in sorted(symbol_summaries, key=lambda x: x["concurrent_count"], reverse=True):
            first = s["positions"][0]
            levels = (
                f"SL={first['sl']}  TP1={first['tp1']}  TP2={first['tp2']}"
                if first["sl"] is not None
                else "levels unavailable"
            )
            marker = " <<<" if s["flag"] == "OVER_LIMIT" else ""
            print(
                f"{s['symbol']:<14} {s['concurrent_count']:>6}  {s['flag']:<12}  {levels}{marker}"
            )
    else:
        print("No open positions found.")

    if over_limit_symbols:
        print()
        print(f"OVER_LIMIT symbols: {', '.join(over_limit_symbols)}")

    print()
    print(f"Report written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
