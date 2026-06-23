#!/usr/bin/env python3
"""
audit_stale_trades.py — Read-only diagnostic for non-CLOSED trades older than 7 days.

Groups stale trades as:
  STALE_ACTIVE   — symbol still appears in signals table (last 24 h)
  STALE_ORPHANED — symbol no longer in signals feed

Zero DB writes. Outputs reports/stale_trades_audit.json and prints summary.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "mamuyy_hunter.db"
REPORT_PATH = Path(__file__).parent.parent / "reports" / "stale_trades_audit.json"
STALE_DAYS = 7
SIGNAL_WINDOW_HOURS = 24

CLOSED_STATUSES = {"CLOSED", "SL_HIT", "TP2_HIT"}


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp (with or without timezone)."""
    ts_str = ts_str.strip()
    try:
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        dt = datetime.fromisoformat(ts_str[:19])
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> None:
    now = utcnow()
    stale_cutoff = now - timedelta(days=STALE_DAYS)
    signal_cutoff = now - timedelta(hours=SIGNAL_WINDOW_HOURS)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── Active symbols in signals feed (last 24 h) ───────────────────────────
    cur.execute(
        "SELECT DISTINCT symbol FROM signals WHERE timestamp >= ?",
        (signal_cutoff.isoformat(),),
    )
    active_symbols = {row["symbol"] for row in cur.fetchall()}

    stale_trades = []

    for table in ("paper_trades", "internal_paper_trades"):
        cur.execute(f"SELECT * FROM {table}")  # noqa: S608
        for row in cur.fetchall():
            d = dict(row)
            if d.get("status") in CLOSED_STATUSES:
                continue
            ts_raw = d.get("timestamp") or d.get("updated_at") or ""
            if not ts_raw:
                continue
            try:
                ts = parse_ts(ts_raw)
            except Exception:
                continue
            if ts >= stale_cutoff:
                continue  # not yet stale
            age_days = (now - ts).total_seconds() / 86400
            symbol = d.get("symbol", "UNKNOWN")
            stale_trades.append(
                {
                    "source_table": table,
                    "id": d.get("id"),
                    "symbol": symbol,
                    "status": d.get("status"),
                    "timestamp": ts_raw,
                    "age_days": round(age_days, 2),
                    "sl": d.get("sl"),
                    "tp1": d.get("tp1"),
                    "tp2": d.get("tp2"),
                    "classification": (
                        "STALE_ACTIVE" if symbol in active_symbols else "STALE_ORPHANED"
                    ),
                }
            )

    conn.close()

    stale_active = [t for t in stale_trades if t["classification"] == "STALE_ACTIVE"]
    stale_orphaned = [t for t in stale_trades if t["classification"] == "STALE_ORPHANED"]

    report = {
        "generated_at": now.isoformat(),
        "stale_threshold_days": STALE_DAYS,
        "signal_window_hours": SIGNAL_WINDOW_HOURS,
        "active_symbols_in_feed": len(active_symbols),
        "total_stale": len(stale_trades),
        "stale_active_count": len(stale_active),
        "stale_orphaned_count": len(stale_orphaned),
        "stale_active": stale_active,
        "stale_orphaned": stale_orphaned,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    # ── Console summary ───────────────────────────────────────────────────────
    print("=" * 60)
    print("STALE TRADES AUDIT")
    print("=" * 60)
    print(f"Run at (UTC):          {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Stale threshold:       > {STALE_DAYS} days")
    print(f"Signal feed window:    last {SIGNAL_WINDOW_HOURS} h")
    print(f"Active symbols in feed:{len(active_symbols)}")
    print(f"Total stale trades:    {len(stale_trades)}")
    print(f"  STALE_ACTIVE:        {len(stale_active)}")
    print(f"  STALE_ORPHANED:      {len(stale_orphaned)}")
    print()

    if stale_trades:
        print(f"{'Table':<25} {'ID':>6} {'Symbol':<14} {'Status':<12} {'Age(d)':>7}  {'Class'}")
        print("-" * 80)
        for t in sorted(stale_trades, key=lambda x: x["age_days"], reverse=True):
            print(
                f"{t['source_table']:<25} {t['id']:>6} {t['symbol']:<14} "
                f"{t['status']:<12} {t['age_days']:>7.1f}  {t['classification']}"
            )
    else:
        print("No stale trades found.")

    print()
    print(f"Report written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
