"""
CP-048: Feature Bridge Fix Validation
READ-ONLY validation - no runtime execution, no model promotion

Validates that _production_universe_dataset() now preserves scanner/flow
features from signals instead of letting _prepare_dataset() zero-fill them.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from ml_engine import _production_universe_dataset

REPORT_JSON = "reports/cp048_feature_bridge_fix.json"

FEATURES = [
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "regime_score",
]

def raw_nonzero_stats():
    conn = sqlite3.connect("file:mamuyy_hunter.db?mode=ro", uri=True)
    cur = conn.cursor()
    out = {}
    for col in FEATURES:
        cur.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN COALESCE({col},0) != 0 THEN 1 ELSE 0 END),
                   MIN({col}),
                   MAX({col})
            FROM signals
        """)
        total, nonzero, minv, maxv = cur.fetchone()
        out[col] = {
            "total": int(total or 0),
            "nonzero": int(nonzero or 0),
            "min": minv,
            "max": maxv,
        }
    conn.close()
    return out

def main():
    print("=== CP-048: Feature Bridge Fix Validation ===")

    raw = raw_nonzero_stats()
    ds = _production_universe_dataset()

    dataset_features = {}
    failed = []
    for col in FEATURES:
        series = ds[col].fillna(0) if col in ds.columns else []
        nonzero = int((series != 0).sum()) if col in ds.columns else 0
        nunique = int(series.nunique()) if col in ds.columns else 0
        dataset_features[col] = {
            "present": col in ds.columns,
            "nonzero": nonzero,
            "nunique": nunique,
            "min": float(series.min()) if col in ds.columns and len(series) else None,
            "max": float(series.max()) if col in ds.columns and len(series) else None,
        }
        if raw[col]["nonzero"] > 0 and nonzero == 0:
            failed.append(col)

    verdict = "PASS" if not failed else "FAIL"
    reason = (
        "production_universe_dataset now preserves non-zero scanner/flow features."
        if verdict == "PASS"
        else "Some raw non-zero scanner/flow features are still zero in production_universe_dataset."
    )

    source_dist = ds["source_artifact"].value_counts().to_dict() if "source_artifact" in ds.columns else {}
    label_dist = ds["target"].value_counts().to_dict() if "target" in ds.columns else {}

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "failed_features": failed,
        "rows": int(len(ds)),
        "source_artifact_distribution": source_dist,
        "label_distribution": label_dist,
        "raw_signals": raw,
        "production_universe_dataset_features": dataset_features,
        "governance": {
            "read_only_validation": True,
            "runtime_execution_changed": False,
            "model_promoted": False,
            "live_unlock": False,
            "threshold_changed": False,
        },
    }

    import os
    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("Verdict:", verdict)
    print("Reason:", reason)
    print("Rows:", len(ds))
    print("Source:", source_dist)
    print("Label:", label_dist)
    print("Failed:", failed)
    print("Dataset feature nonzero:")
    for col, stats in dataset_features.items():
        print(f"  {col}: nonzero={stats['nonzero']} nunique={stats['nunique']} min={stats['min']} max={stats['max']}")
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
