"""
CP-045: Entry Deduplication & Signal Re-scan Audit
READ-ONLY - no runtime/execution/db changes, no model promotion

Goal: Quantify how much of the production universe dataset (CP-039D)
consists of "duplicate cluster" entries vs genuine unique signals,
and assess whether this explains WF instability (CP-038D/CP-040A).
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

REPORT_JSON = "reports/cp045_entry_dedup_dataset_impact.json"
REPORT_CSV  = "reports/cp045_entry_dedup_clusters.csv"
CLUSTER_WINDOW_SECONDS = 300  # 5 minutes, same as exploratory audit

def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def find_clusters(rows, key_field="symbol", ts_field="source_signal_timestamp"):
    by_key = defaultdict(list)
    for r in rows:
        by_key[r[key_field]].append(r)
    for k in by_key:
        by_key[k].sort(key=lambda r: r[ts_field])

    clusters = []
    for key, items in by_key.items():
        i = 0
        while i < len(items):
            cluster = [items[i]]
            t0 = parse_ts(items[i][ts_field])
            j = i + 1
            while j < len(items):
                tj = parse_ts(items[j][ts_field])
                if (tj - t0).total_seconds() <= CLUSTER_WINDOW_SECONDS:
                    cluster.append(items[j])
                    j += 1
                else:
                    break
            if len(cluster) > 1:
                clusters.append((key, cluster))
            i = j if len(cluster) > 1 else i + 1
    return clusters

def run():
    print("=== CP-045: Entry Deduplication & Dataset Impact Audit ===\n")

    conn = sqlite3.connect("file:mamuyy_hunter.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- Section A: System-wide clustering (internal_paper_trades) ----
    print("[A] System-wide same-symbol clustering (internal_paper_trades)...")
    all_trades = cur.execute("""
        SELECT id, symbol, source_signal_timestamp, entry_price, confidence,
               status, target_timestamp, predicted_probability
        FROM internal_paper_trades
        WHERE source_signal_timestamp IS NOT NULL
        ORDER BY symbol, source_signal_timestamp
    """).fetchall()
    all_trades = [dict(r) for r in all_trades]

    clusters_all = find_clusters(all_trades)
    total_trades = len(all_trades)
    total_clustered = sum(len(c) for _, c in clusters_all)
    excess_all = sum(len(c) - 1 for _, c in clusters_all)

    print(f"   Total trades: {total_trades}")
    print(f"   Clusters found: {len(clusters_all)}")
    print(f"   Trades involved in clustering: {total_clustered} ({round(total_clustered/total_trades*100,2)}%)")
    print(f"   Excess entries (redundant slots): {excess_all}\n")

    # ---- Section B: Re-scan interval characterization ----
    print("[B] Re-scan interval characterization within clusters...")
    intervals = []
    for symbol, cluster in clusters_all:
        for k in range(1, len(cluster)):
            t_prev = parse_ts(cluster[k-1]["source_signal_timestamp"])
            t_curr = parse_ts(cluster[k]["source_signal_timestamp"])
            intervals.append((t_curr - t_prev).total_seconds())

    if intervals:
        intervals_sorted = sorted(intervals)
        n = len(intervals_sorted)
        median_interval = intervals_sorted[n // 2]
        mean_interval = sum(intervals) / n
        print(f"   Sample size: {n}")
        print(f"   Mean interval: {round(mean_interval, 1)}s")
        print(f"   Median interval: {round(median_interval, 1)}s")
        print(f"   Min: {round(min(intervals),1)}s | Max: {round(max(intervals),1)}s\n")
    else:
        mean_interval = median_interval = None
        print("   No intervals found.\n")

    # ---- Section C: Impact on CP-039D production universe dataset ----
    print("[C] Mapping clusters onto CP-039D production_universe_dataset()...")
    try:
        import sys
        sys.path.insert(0, ".")
        from ml_engine import _production_universe_dataset
        ds = _production_universe_dataset()
        ds_ids = set()
        if "id" in ds.columns:
            ds_ids = set(ds["id"].dropna().astype(int).tolist())
        elif "trade_id" in ds.columns:
            ds_ids = set(ds["trade_id"].dropna().astype(int).tolist())

        dataset_available = True
        ds_rows = len(ds)
    except Exception as e:
        print(f"   WARNING: could not load _production_universe_dataset(): {e}")
        dataset_available = False
        ds_ids = set()
        ds_rows = None

    cluster_ids = set()
    excess_ids = set()
    for symbol, cluster in clusters_all:
        for c in cluster:
            cluster_ids.add(c["id"])
        for c in cluster[1:]:
            excess_ids.add(c["id"])

    dataset_cluster_overlap = None
    dataset_excess_overlap = None
    if dataset_available and ds_ids:
        dataset_cluster_overlap = len(ds_ids & cluster_ids)
        dataset_excess_overlap = len(ds_ids & excess_ids)
        print(f"   Production universe dataset rows: {ds_rows}")
        print(f"   Dataset rows that are part of a cluster: {dataset_cluster_overlap} "
              f"({round(dataset_cluster_overlap/ds_rows*100,2) if ds_rows else 'N/A'}%)")
        print(f"   Dataset rows that are 'excess' (redundant) cluster members: {dataset_excess_overlap} "
              f"({round(dataset_excess_overlap/ds_rows*100,2) if ds_rows else 'N/A'}%)\n")
    else:
        print("   Could not compute dataset overlap (dataset unavailable or no id column).\n")

    # ---- Section D: Confidence=100 cap frequency ----
    print("[D] Confidence cap (=100.0) frequency...")
    cur.execute("SELECT COUNT(*) FROM internal_paper_trades WHERE confidence = 100.0")
    conf100_count = cur.fetchone()[0]
    print(f"   Trades with confidence exactly 100.0: {conf100_count} / {total_trades} "
          f"({round(conf100_count/total_trades*100,2)}%)\n")

    # ---- Verdict ----
    pct_clustered = round(total_clustered / total_trades * 100, 2) if total_trades else 0
    pct_excess = round(excess_all / total_trades * 100, 2) if total_trades else 0

    if pct_clustered >= 50:
        verdict = "FAIL"
        verdict_reason = "Majority of dataset consists of re-scan duplicate clusters; likely autocorrelation/leakage risk for ML training."
    elif pct_clustered >= 20:
        verdict = "REVIEW"
        verdict_reason = "Significant clustering present; dedup strategy should be designed before further ML promotion."
    else:
        verdict = "PASS"
        verdict_reason = "Clustering is limited; unlikely to materially affect dataset integrity."

    print(f"=== VERDICT: {verdict} ===")
    print(f"Reason: {verdict_reason}\n")

    report = {
        "cp_id": "CP-045",
        "title": "Entry Deduplication & Signal Re-scan Audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "paper_only": True,
        "total_trades": total_trades,
        "total_clusters": len(clusters_all),
        "trades_in_clusters": total_clustered,
        "pct_trades_in_clusters": pct_clustered,
        "excess_entries": excess_all,
        "pct_excess_entries": pct_excess,
        "rescan_interval_seconds": {
            "sample_size": len(intervals),
            "mean": round(mean_interval, 2) if mean_interval else None,
            "median": round(median_interval, 2) if median_interval else None,
            "min": round(min(intervals), 2) if intervals else None,
            "max": round(max(intervals), 2) if intervals else None,
        },
        "production_universe_dataset_impact": {
            "dataset_available": dataset_available,
            "dataset_rows": ds_rows,
            "dataset_rows_in_clusters": dataset_cluster_overlap,
            "dataset_rows_excess": dataset_excess_overlap,
        },
        "confidence_cap_100": {
            "count": conf100_count,
            "total": total_trades,
            "pct": round(conf100_count / total_trades * 100, 2) if total_trades else None,
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "verdict_logic": {
            "FAIL": ">= 50% trades in clusters",
            "REVIEW": ">= 20% and < 50%",
            "PASS": "< 20%",
        },
        "notes": [
            "CP-045 is READ-ONLY. No runtime, execution, or DB changes made.",
            "No model retraining or promotion performed.",
            "Recommendation: design dedup/cooldown logic before next ML training cycle (out of scope for this audit).",
        ],
    }

    import os
    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # CSV of clusters
    import csv
    with open(REPORT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "cluster_size", "trade_id", "timestamp", "entry_price", "confidence", "status", "is_excess"])
        for symbol, cluster in clusters_all:
            for idx, c in enumerate(cluster):
                writer.writerow([
                    symbol, len(cluster), c["id"], c["source_signal_timestamp"],
                    c["entry_price"], c["confidence"], c["status"], idx > 0
                ])

    print(f"Report: {REPORT_JSON}")
    print(f"Clusters CSV: {REPORT_CSV}")

    conn.close()
    return report

if __name__ == "__main__":
    run()
