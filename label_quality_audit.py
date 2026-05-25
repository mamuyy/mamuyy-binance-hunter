import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("/home/ubuntu/mamuyy-binance-hunter/mamuyy_hunter.db")
OUTPUT_PATH = Path("/home/ubuntu/mamuyy-binance-hunter/logs/label_quality_audit.json")


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_one(conn, query):
    row = conn.execute(query).fetchone()
    return dict(row) if row else {}


def fetch_all(conn, query):
    return [dict(row) for row in conn.execute(query).fetchall()]


def table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
    return {row["name"] for row in rows}


def safe_ratio(a, b):
    if b in (0, None):
        return None
    return a / b


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with connect_readonly(DB_PATH) as conn:
        cols = table_columns(conn, "historical_outcomes")

        freshness = fetch_one(conn, """
            SELECT
              COUNT(*) AS total_rows,
              MIN(signal_timestamp) AS first_signal,
              MAX(signal_timestamp) AS last_signal,
              MIN(close_timestamp) AS first_close,
              MAX(close_timestamp) AS last_close
            FROM historical_outcomes;
        """)

        distribution = fetch_all(conn, """
            SELECT
              status,
              win_loss,
              COUNT(*) AS rows,
              ROUND(AVG(pnl_pct), 6) AS avg_pnl_pct
            FROM historical_outcomes
            GROUP BY status, win_loss
            ORDER BY rows DESC;
        """)

        mismatch = fetch_one(conn, """
            SELECT
              COUNT(*) AS total_rows,
              SUM(CASE
                WHEN status = 'WIN' AND pnl_pct < 0 THEN 1
                WHEN status = 'LOSS' AND pnl_pct > 0 THEN 1
                WHEN status = 'TP1 HIT' AND pnl_pct <= 0 THEN 1
                ELSE 0
              END) AS mismatch_count,
              ROUND(
                100.0 * SUM(CASE
                  WHEN status = 'WIN' AND pnl_pct < 0 THEN 1
                  WHEN status = 'LOSS' AND pnl_pct > 0 THEN 1
                  WHEN status = 'TP1 HIT' AND pnl_pct <= 0 THEN 1
                  ELSE 0
                END) / COUNT(*),
              6) AS mismatch_rate_pct
            FROM historical_outcomes;
        """)

        last_24h = fetch_all(conn, """
            SELECT
              status,
              win_loss,
              COUNT(*) AS rows,
              ROUND(AVG(pnl_pct), 6) AS avg_pnl_pct
            FROM historical_outcomes
            WHERE close_timestamp >= datetime('now', '-1 day')
            GROUP BY status, win_loss
            ORDER BY rows DESC;
        """)

        exit_reasons = []
        if "exit_reason" in cols:
            exit_reasons = fetch_all(conn, """
                SELECT
                  exit_reason,
                  status,
                  COUNT(*) AS rows,
                  ROUND(AVG(pnl_pct), 6) AS avg_pnl_pct
                FROM historical_outcomes
                GROUP BY exit_reason, status
                ORDER BY rows DESC
                LIMIT 50;
            """)

        score_buckets = []
        if "score" in cols:
            score_buckets = fetch_all(conn, """
                SELECT
                  CASE
                    WHEN score >= 90 THEN '90-100'
                    WHEN score >= 80 THEN '80-89'
                    WHEN score >= 70 THEN '70-79'
                    WHEN score >= 60 THEN '60-69'
                    WHEN score >= 50 THEN '50-59'
                    ELSE '<50'
                  END AS score_bucket,
                  COUNT(*) AS rows,
                  ROUND(AVG(CASE WHEN win_loss = 'WIN' THEN 1.0 ELSE 0.0 END) * 100, 4) AS winrate_pct,
                  ROUND(AVG(pnl_pct), 6) AS avg_pnl_pct
                FROM historical_outcomes
                GROUP BY score_bucket
                ORDER BY score_bucket DESC;
            """)

    total_rows = int(freshness.get("total_rows") or 0)
    mismatch_count = int(mismatch.get("mismatch_count") or 0)
    mismatch_rate = safe_ratio(mismatch_count, total_rows) or 0.0

    label_counts = {row["status"]: int(row["rows"]) for row in distribution}
    loss_count = label_counts.get("LOSS", 0)
    win_count = label_counts.get("WIN", 0)
    tp1_count = label_counts.get("TP1 HIT", 0)

    loss_win_ratio = safe_ratio(loss_count, win_count)
    loss_positive_ratio = safe_ratio(loss_count, win_count + tp1_count)

    recommendations = []

    if mismatch_rate > 0.12:
        verdict = "FAIL"
        recommendations.append("STOP Phase 2B. Label/PnL mismatch is above 12%.")
    elif loss_win_ratio is not None and loss_win_ratio > 5:
        verdict = "REVIEW"
        recommendations.append("Proceed cautiously. LOSS/WIN imbalance is above 5.0.")
    else:
        verdict = "PASS"
        recommendations.append("Label/PnL consistency is acceptable for Phase 2B preparation.")

    recommendations.append("Keep PAPER_ONLY. Do not enable real execution or Freqtrade.")
    recommendations.append("Next priority: calibration, per-regime quality, and lock-safe research pipeline.")

    report = {
        "audit_date_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_PAPER_ONLY",
        "source_table": "historical_outcomes",
        "freshness": freshness,
        "label_distribution": distribution,
        "last_24h_distribution": last_24h,
        "mismatch": {
            "total_rows": total_rows,
            "mismatch_count": mismatch_count,
            "mismatch_rate": round(mismatch_rate, 8),
            "mismatch_rate_pct": mismatch.get("mismatch_rate_pct"),
        },
        "imbalance": {
            "loss_count": loss_count,
            "win_count": win_count,
            "tp1_hit_count": tp1_count,
            "loss_win_ratio": None if loss_win_ratio is None else round(loss_win_ratio, 6),
            "loss_positive_ratio": None if loss_positive_ratio is None else round(loss_positive_ratio, 6),
        },
        "exit_reasons": exit_reasons,
        "score_buckets": score_buckets,
        "overall_verdict": verdict,
        "recommendations": recommendations,
    }

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("====== MAMUYY HUNTER LABEL QUALITY AUDIT ======")
    print(f"Rows                 : {total_rows}")
    print(f"Last close           : {freshness.get('last_close')}")
    print(f"Mismatch count       : {mismatch_count}")
    print(f"Mismatch rate pct    : {mismatch.get('mismatch_rate_pct')}")
    print(f"LOSS/WIN ratio       : {report['imbalance']['loss_win_ratio']}")
    print(f"LOSS/POS ratio       : {report['imbalance']['loss_positive_ratio']}")
    print(f"Verdict              : {verdict}")
    print(f"Output               : {OUTPUT_PATH}")
    print("Recommendations:")
    for rec in recommendations:
        print(f"- {rec}")


if __name__ == "__main__":
    main()
