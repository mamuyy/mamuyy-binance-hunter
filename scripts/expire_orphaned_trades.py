#!/usr/bin/env python3
"""
expire_orphaned_trades.py — Mark stale orphaned paper trades as CLOSED.

Dry-run by default. Pass --confirm to execute the UPDATE.

Targets internal_paper_trades rows where:
  1. status is not a terminal status
  2. timestamp is older than STALE_DAYS days
  3. symbol has no entry in signals table in the last SIGNAL_WINDOW_HOURS hours

With --confirm:
  - Opens DB in read-write mode
  - Sets status='CLOSED', exit_reason='EXPIRED_ORPHANED', updated_at=<utcnow>
  - Logs each closed trade to logs/expire_orphaned.log

Zero writes in dry-run mode.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
DB_PATH = REPO / "mamuyy_hunter.db"
DRY_RUN_REPORT = REPO / "reports" / "expire_orphaned_dry_run.json"
CONFIRM_LOG = REPO / "logs" / "expire_orphaned.log"

STALE_DAYS = 7
SIGNAL_WINDOW_HOURS = 24
TERMINAL_STATUSES = {"CLOSED", "SL_HIT", "TP2_HIT"}
TARGET_TABLE = "internal_paper_trades"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_ts(ts_str: str) -> datetime:
    ts_str = ts_str.strip()
    try:
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        dt = datetime.fromisoformat(ts_str[:19])
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def setup_confirm_logger() -> logging.Logger:
    CONFIRM_LOG.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("expire_orphaned")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(CONFIRM_LOG)
    fh.setFormatter(logging.Formatter("%(asctime)s UTC %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))
    fh.formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
    logger.addHandler(fh)
    return logger


def find_candidates(cur: sqlite3.Cursor, stale_cutoff: datetime, signal_cutoff: datetime) -> list[dict]:
    cur.execute(
        "SELECT DISTINCT symbol FROM signals WHERE timestamp >= ?",
        (signal_cutoff.isoformat(),),
    )
    active_symbols = {row["symbol"] for row in cur.fetchall()}

    cur.execute(f"SELECT id, symbol, status, timestamp, sl, tp1, tp2 FROM {TARGET_TABLE}")
    candidates = []
    for row in cur.fetchall():
        d = dict(row)
        if d["status"] in TERMINAL_STATUSES:
            continue
        if d["symbol"] in active_symbols:
            continue
        ts_raw = d["timestamp"] or ""
        if not ts_raw:
            continue
        try:
            ts = parse_ts(ts_raw)
        except Exception:
            continue
        if ts >= stale_cutoff:
            continue
        age_days = (utcnow() - ts).total_seconds() / 86400
        candidates.append({
            "id": d["id"],
            "symbol": d["symbol"],
            "status": d["status"],
            "timestamp": ts_raw,
            "age_days": round(age_days, 2),
            "sl": d["sl"],
            "tp1": d["tp1"],
            "tp2": d["tp2"],
        })

    return sorted(candidates, key=lambda x: x["age_days"], reverse=True)


def print_summary(candidates: list[dict], confirm: bool, now: datetime) -> None:
    mode = "CONFIRM (WRITES ACTIVE)" if confirm else "DRY RUN (no writes)"
    print("=" * 65)
    print(f"EXPIRE ORPHANED TRADES — {mode}")
    print("=" * 65)
    print(f"Run at (UTC):      {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Stale threshold:   > {STALE_DAYS} days old")
    print(f"Signal window:     last {SIGNAL_WINDOW_HOURS} h")
    print(f"Target table:      {TARGET_TABLE}")
    print(f"Candidates found:  {len(candidates)}")
    print()

    if candidates:
        print(f"{'ID':>8}  {'Symbol':<16} {'Status':<12} {'Age(d)':>7}  Timestamp")
        print("-" * 72)
        for t in candidates:
            print(
                f"{t['id']:>8}  {t['symbol']:<16} {t['status']:<12} "
                f"{t['age_days']:>7.1f}  {t['timestamp'][:19]}"
            )
    else:
        print("No orphaned trades to expire.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Expire stale orphaned paper trades.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the UPDATE. Without this flag, only a dry run is performed.",
    )
    args = parser.parse_args()

    now = utcnow()
    stale_cutoff = now - timedelta(days=STALE_DAYS)
    signal_cutoff = now - timedelta(hours=SIGNAL_WINDOW_HOURS)

    # ── Phase 1: always read candidates in read-only mode ────────────────────
    ro_conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ro_conn.row_factory = sqlite3.Row
    candidates = find_candidates(ro_conn.cursor(), stale_cutoff, signal_cutoff)
    ro_conn.close()

    print_summary(candidates, confirm=args.confirm, now=now)

    # ── Dry-run report ────────────────────────────────────────────────────────
    if not args.confirm:
        DRY_RUN_REPORT.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "mode": "DRY_RUN",
            "generated_at": now.isoformat(),
            "stale_threshold_days": STALE_DAYS,
            "signal_window_hours": SIGNAL_WINDOW_HOURS,
            "target_table": TARGET_TABLE,
            "would_expire_count": len(candidates),
            "would_expire": candidates,
        }
        DRY_RUN_REPORT.write_text(json.dumps(report, indent=2))
        print()
        print(f"Dry-run report written to: {DRY_RUN_REPORT}")
        print()
        print("To execute, re-run with --confirm:")
        print(f"  .venv/bin/python {Path(__file__).name} --confirm")
        sys.exit(0)

    # ── Confirm: execute UPDATEs ──────────────────────────────────────────────
    if not candidates:
        print("\nNothing to expire. Exiting.")
        sys.exit(0)

    logger = setup_confirm_logger()
    logger.info("START expire_orphaned_trades.py --confirm | candidates=%d", len(candidates))

    rw_conn = sqlite3.connect(str(DB_PATH))
    rw_conn.row_factory = sqlite3.Row
    closed_at = now.isoformat()
    closed_ids = []

    try:
        with rw_conn:
            for t in candidates:
                rw_conn.execute(
                    f"""UPDATE {TARGET_TABLE}
                           SET status      = 'CLOSED',
                               exit_reason = 'EXPIRED_ORPHANED',
                               updated_at  = ?
                         WHERE id = ?
                           AND status NOT IN ('CLOSED','SL_HIT','TP2_HIT')""",
                    (closed_at, t["id"]),
                )
                logger.info(
                    "CLOSED id=%d symbol=%s age_days=%.1f original_status=%s",
                    t["id"], t["symbol"], t["age_days"], t["status"],
                )
                closed_ids.append(t["id"])
    finally:
        rw_conn.close()

    logger.info("DONE closed=%d", len(closed_ids))
    print(f"\nExpired {len(closed_ids)} trade(s). Log: {CONFIRM_LOG}")


if __name__ == "__main__":
    main()
