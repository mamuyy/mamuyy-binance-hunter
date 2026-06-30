"""
CP-045 Section C (fixed): map clusters onto production_universe_dataset
using symbol + timestamp join (no id column available in dataset).
READ-ONLY.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, ".")
from ml_engine import _production_universe_dataset

CLUSTER_WINDOW_SECONDS = 300

def parse_ts(ts):
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts  # already a Timestamp

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

print("=== CP-045 Section C (FIXED): Dataset Overlap via symbol+timestamp ===\n")

conn = sqlite3.connect("file:mamuyy_hunter.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

all_trades = cur.execute("""
    SELECT id, symbol, source_signal_timestamp, entry_price, confidence, status
    FROM internal_paper_trades
    WHERE source_signal_timestamp IS NOT NULL
    ORDER BY symbol, source_signal_timestamp
""").fetchall()
all_trades = [dict(r) for r in all_trades]

clusters = find_clusters(all_trades)
total_trades = len(all_trades)
total_clustered = sum(len(c) for _, c in clusters)
excess = sum(len(c) - 1 for _, c in clusters)

print(f"Total internal_paper_trades: {total_trades}")
print(f"Trades in clusters: {total_clustered} ({round(total_clustered/total_trades*100,2)}%)")
print(f"Excess (redundant) entries: {excess}\n")

# Build set of (symbol, timestamp) for ALL clustered trades and EXCESS-only trades
# Match tolerance: +/- 1 second, since dataset timestamp may be rounded/derived
clustered_keys = []
excess_keys = []
for symbol, cluster in clusters:
    for c in cluster:
        clustered_keys.append((symbol, parse_ts(c["source_signal_timestamp"])))
    for c in cluster[1:]:
        excess_keys.append((symbol, parse_ts(c["source_signal_timestamp"])))

print(f"Loading _production_universe_dataset()...")
ds = _production_universe_dataset()
print(f"Dataset rows: {len(ds)}")
print(f"Source artifact distribution: {ds['source_artifact'].value_counts().to_dict()}\n")

# Only internal_paper_trades-sourced rows are even candidates for this overlap check
ipt_ds = ds[ds["source_artifact"] == "internal_paper_trades"].copy()
print(f"Dataset rows from internal_paper_trades: {len(ipt_ds)}")

import pandas as pd
ipt_ds["timestamp"] = pd.to_datetime(ipt_ds["timestamp"], utc=True)

def is_match(symbol, ts, key_list, tolerance_seconds=2):
    for k_symbol, k_ts in key_list:
        if k_symbol != symbol:
            continue
        if abs((ts - k_ts).total_seconds()) <= tolerance_seconds:
            return True
    return False

# Build lookup dict for speed: symbol -> list of timestamps
from collections import defaultdict as dd
clustered_lookup = dd(list)
for s, t in clustered_keys:
    clustered_lookup[s].append(t)
excess_lookup = dd(list)
for s, t in excess_keys:
    excess_lookup[s].append(t)

def fast_match(symbol, ts, lookup, tol=2):
    for cand in lookup.get(symbol, []):
        if abs((ts - cand).total_seconds()) <= tol:
            return True
    return False

ipt_ds["in_cluster"] = ipt_ds.apply(lambda row: fast_match(row["symbol"], row["timestamp"], clustered_lookup), axis=1)
ipt_ds["is_excess"] = ipt_ds.apply(lambda row: fast_match(row["symbol"], row["timestamp"], excess_lookup), axis=1)

ds_rows = len(ds)
ds_clustered = int(ipt_ds["in_cluster"].sum())
ds_excess = int(ipt_ds["is_excess"].sum())

print(f"\n=== RESULT ===")
print(f"Total dataset rows: {ds_rows}")
print(f"Dataset rows (IPT-sourced) in a duplicate cluster: {ds_clustered} ({round(ds_clustered/ds_rows*100,2)}% of total dataset)")
print(f"Dataset rows (IPT-sourced) that are 'excess' cluster members: {ds_excess} ({round(ds_excess/ds_rows*100,2)}% of total dataset)")
print(f"\n(For reference: dataset has {len(ipt_ds)} IPT-sourced rows out of {ds_rows} total)")

# Update the CP-045 report file with corrected section C
report_path = "reports/cp045_entry_dedup_dataset_impact.json"
with open(report_path) as f:
    report = json.load(f)

report["production_universe_dataset_impact"] = {
    "dataset_available": True,
    "dataset_rows": ds_rows,
    "dataset_rows_ipt_sourced": len(ipt_ds),
    "dataset_rows_in_clusters": ds_clustered,
    "dataset_rows_excess": ds_excess,
    "pct_dataset_in_clusters": round(ds_clustered/ds_rows*100, 2),
    "pct_dataset_excess": round(ds_excess/ds_rows*100, 2),
    "match_method": "symbol + timestamp (tolerance 2s)",
    "note": "Only internal_paper_trades-sourced dataset rows are candidates for clustering overlap; historical_outcomes rows excluded from this check by definition."
}

with open(report_path, "w") as f:
    json.dump(report, f, indent=2, default=str)

print(f"\nReport updated: {report_path}")
conn.close()
