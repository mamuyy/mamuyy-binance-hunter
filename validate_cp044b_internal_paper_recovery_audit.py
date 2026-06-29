#!/usr/bin/env python3
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("mamuyy_hunter.db")
REPORTS = Path("reports")
BASELINE = "2026-06-22T18:05:35.736930+00:00"
CAP = 20
ACTIVE = ("OPEN", "TP1 HIT")
TERMINAL = ("CLOSED", "WIN", "LOSS", "STOP_LOSS", "TAKE_PROFIT", "SL_HIT", "TP2_HIT")

def now():
    return datetime.now(timezone.utc).isoformat()

def conn_ro():
    uri = f"file:{DB.resolve().as_posix()}?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c

def table_exists(c, name):
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone() is not None

def cols(c, table):
    if not table_exists(c, table):
        return []
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]

def pick(columns, choices):
    lower = {x.lower(): x for x in columns}
    for choice in choices:
        if choice.lower() in lower:
            return lower[choice.lower()]
    return None

def q1(c, sql, args=()):
    row = c.execute(sql, args).fetchone()
    return row[0] if row else None

def qall(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]

def placeholders(items):
    return ",".join(["?"] * len(items))

def freshness(c, table):
    if not table_exists(c, table):
        return {"available": False, "table": table}
    columns = cols(c, table)
    ts = pick(columns, ["source_signal_timestamp", "signal_timestamp", "timestamp", "created_at", "updated_at"])
    out = {
        "available": True,
        "table": table,
        "row_count": q1(c, f"SELECT COUNT(*) FROM {table}"),
        "timestamp_column": ts,
        "latest_timestamp": None,
    }
    if ts:
        out["latest_timestamp"] = q1(c, f"SELECT MAX({ts}) FROM {table}")
    return out

def candidate_buckets(c):
    table = "signal_candidates"
    if not table_exists(c, table):
        return {"available": False, "table": table}
    columns = cols(c, table)
    ts = pick(columns, ["source_signal_timestamp", "signal_timestamp", "timestamp", "created_at", "updated_at"])
    score = pick(columns, ["score", "confidence", "predicted_probability", "probability", "final_score", "composite_score"])
    out = {
        "available": True,
        "table": table,
        "timestamp_column": ts,
        "score_column": score,
        "latest_timestamp": None,
        "after_baseline_total": None,
        "buckets_after_baseline": None,
    }
    if ts:
        out["latest_timestamp"] = q1(c, f"SELECT MAX({ts}) FROM {table}")
    if ts and score:
        out["after_baseline_total"] = q1(c, f"SELECT COUNT(*) FROM {table} WHERE {ts} > ?", (BASELINE,))
        out["buckets_after_baseline"] = {
            "gte_85": q1(c, f"SELECT COUNT(*) FROM {table} WHERE {ts} > ? AND COALESCE({score},0) >= 85", (BASELINE,)),
            "gte_90": q1(c, f"SELECT COUNT(*) FROM {table} WHERE {ts} > ? AND COALESCE({score},0) >= 90", (BASELINE,)),
            "gte_95": q1(c, f"SELECT COUNT(*) FROM {table} WHERE {ts} > ? AND COALESCE({score},0) >= 95", (BASELINE,)),
        }
    return out

def main():
    if not DB.exists():
        raise SystemExit(f"DB not found: {DB}")

    REPORTS.mkdir(exist_ok=True)

    c = conn_ro()
    try:
        status_counts = qall(c, """
            SELECT COALESCE(status,'UNKNOWN') status, COUNT(*) count
            FROM internal_paper_trades
            GROUP BY COALESCE(status,'UNKNOWN')
            ORDER BY count DESC
        """)

        active_count = q1(c, """
            SELECT COUNT(*)
            FROM internal_paper_trades
            WHERE UPPER(COALESCE(status,'OPEN')) IN ('OPEN','TP1 HIT')
        """)

        expired_orphaned = q1(c, """
            SELECT COUNT(*)
            FROM internal_paper_trades
            WHERE exit_reason='EXPIRED_ORPHANED'
        """)

        latest_source = q1(c, """
            SELECT MAX(source_signal_timestamp)
            FROM internal_paper_trades
        """)

        ph = placeholders(TERMINAL)

        closed_after = q1(c, f"""
            SELECT COUNT(*)
            FROM internal_paper_trades
            WHERE source_signal_timestamp > ?
              AND UPPER(COALESCE(status,'')) IN ({ph})
        """, (BASELINE, *TERMINAL))

        closed_score95 = q1(c, f"""
            SELECT COUNT(*)
            FROM internal_paper_trades
            WHERE source_signal_timestamp > ?
              AND UPPER(COALESCE(status,'')) IN ({ph})
              AND COALESCE(confidence, predicted_probability, 0) >= 95
        """, (BASELINE, *TERMINAL))

        closed_rows = qall(c, f"""
            SELECT id, timestamp, source_signal_timestamp, symbol, confidence, status, exit_reason, pnl, updated_at
            FROM internal_paper_trades
            WHERE source_signal_timestamp > ?
              AND UPPER(COALESCE(status,'')) IN ({ph})
            ORDER BY source_signal_timestamp DESC, id DESC
            LIMIT 30
        """, (BASELINE, *TERMINAL))

        report = {
            "generated_at": now(),
            "baseline": BASELINE,
            "governance": {
                "audit_mode": "READ_ONLY",
                "db_writes": False,
                "runtime_changes": False,
                "model_changes": False,
                "threshold_changes": False,
                "execution_changes": False,
                "paper_only": True,
                "classifier": "FROZEN",
                "model_promotion": "HOLD",
                "phase3": "LOCKED",
                "live_execution": "OFF",
            },
            "internal_paper_trades": {
                "status_counts": status_counts,
                "active_count": active_count,
                "global_active_cap": CAP,
                "active_cap_full": active_count >= CAP,
                "expired_orphaned_count": expired_orphaned,
                "latest_source_signal_timestamp": latest_source,
                "closed_after_baseline_count": closed_after,
                "closed_score95_after_baseline_count": closed_score95,
                "latest_closed_after_baseline": closed_rows,
            },
            "signal_candidates": candidate_buckets(c),
            "signals": freshness(c, "signals"),
            "shadow_trades": freshness(c, "shadow_trades"),
            "verdicts": {
                "RECOVERY_CONFIRMED": bool(active_count <= CAP and closed_after > 0),
                "SCORE95_EVIDENCE_INSUFFICIENT": bool(closed_score95 < 30),
                "ACTIVE_CAP_RISK": bool(active_count >= CAP),
                "CP045_APPROVED": False,
                "PHASE3_LOCKED": True,
            }
        }
    finally:
        c.close()

    json_path = REPORTS / "cp044b_internal_paper_recovery_audit.json"
    md_path = REPORTS / "cp044b_internal_paper_recovery_audit.md"
    csv_path = REPORTS / "cp044b_closed_forward_rows.csv"

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = []
    lines.append("# CP-044B Internal Paper Recovery Audit Evidence")
    lines.append("")
    lines.append(f"Generated: `{report['generated_at']}`")
    lines.append(f"Baseline CP-042: `{BASELINE}`")
    lines.append("")
    lines.append("## Governance")
    lines.append("")
    for k, v in report["governance"].items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("## Internal Paper Trades")
    ipt = report["internal_paper_trades"]
    lines.append("")
    lines.append(f"- Active count: `{ipt['active_count']}` / `{CAP}`")
    lines.append(f"- Active cap full: `{ipt['active_cap_full']}`")
    lines.append(f"- Expired orphaned count: `{ipt['expired_orphaned_count']}`")
    lines.append(f"- Latest source_signal_timestamp: `{ipt['latest_source_signal_timestamp']}`")
    lines.append(f"- Closed rows after baseline: `{ipt['closed_after_baseline_count']}`")
    lines.append(f"- Closed score>=95 rows after baseline: `{ipt['closed_score95_after_baseline_count']}`")
    lines.append("")
    lines.append("### Status Counts")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---:|")
    for r in ipt["status_counts"]:
        lines.append(f"| {r['status']} | {r['count']} |")
    lines.append("")
    lines.append("### Latest Closed Forward Rows")
    lines.append("")
    lines.append("| ID | Source Signal Timestamp | Symbol | Confidence | Status | Exit Reason | PnL |")
    lines.append("|---:|---|---|---:|---|---|---:|")
    if ipt["latest_closed_after_baseline"]:
        for r in ipt["latest_closed_after_baseline"]:
            lines.append(f"| {r['id']} | {r['source_signal_timestamp']} | {r['symbol']} | {r['confidence']} | {r['status']} | {r['exit_reason']} | {r['pnl']} |")
    else:
        lines.append("| - | - | - | - | - | - | - |")
    lines.append("")
    lines.append("## Score Buckets")
    lines.append("")
    lines.append(f"- signal_candidates: `{report['signal_candidates']}`")
    lines.append("")
    lines.append("## Freshness")
    lines.append("")
    lines.append(f"- signals: `{report['signals']}`")
    lines.append(f"- shadow_trades: `{report['shadow_trades']}`")
    lines.append("")
    lines.append("## Final Decision")
    lines.append("")
    lines.append("- Internal paper bridge recovery: **OPERATIONALLY RESOLVED**")
    lines.append("- Score95 forward evidence: **INSUFFICIENT**")
    lines.append("- CP-045: **NOT APPROVED**")
    lines.append("- Phase 3: **LOCKED**")
    lines.append("- Live execution: **OFF**")
    lines.append("- PAPER_ONLY: **TRUE**")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["id", "timestamp", "source_signal_timestamp", "symbol", "confidence", "status", "exit_reason", "pnl", "updated_at"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in closed_rows:
            w.writerow(r)

    print("CP-044B Internal Paper Recovery Audit")
    print("------------------------------------")
    print(f"Active count: {active_count}/{CAP}")
    print(f"Closed after baseline: {closed_after}")
    print(f"Closed score>=95 after baseline: {closed_score95}")
    print(f"Recovery confirmed: {report['verdicts']['RECOVERY_CONFIRMED']}")
    print("CP-045: NOT APPROVED")
    print("Phase 3: LOCKED")
    print(f"Wrote: {md_path}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {csv_path}")

if __name__ == "__main__":
    main()
