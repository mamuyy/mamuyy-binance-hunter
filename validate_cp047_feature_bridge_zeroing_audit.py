"""
CP-047: Production Dataset Feature Bridge Zeroing Audit
READ-ONLY - no runtime/execution/db changes, no model promotion

Goal:
Confirm whether _production_universe_dataset() loses non-zero scanner/flow
features because the production universe SQL does not select/join them.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from ml_engine import _production_universe_dataset

REPORT_JSON = "reports/cp047_feature_bridge_zeroing_audit.json"

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

def raw_signal_stats():
    out = {}
    conn = sqlite3.connect("file:mamuyy_hunter.db?mode=ro", uri=True)
    cur = conn.cursor()
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
            "pct_nonzero": round((int(nonzero or 0) / int(total or 1)) * 100, 4),
            "min": minv,
            "max": maxv,
        }
    conn.close()
    return out

def dataset_stats():
    ds = _production_universe_dataset()
    out = {
        "rows": int(len(ds)),
        "source_artifact_distribution": ds["source_artifact"].value_counts().to_dict() if "source_artifact" in ds.columns else {},
        "features": {},
    }
    for col in FEATURES:
        if col not in ds.columns:
            out["features"][col] = {"present": False}
            continue
        series = ds[col].fillna(0)
        out["features"][col] = {
            "present": True,
            "nonzero": int((series != 0).sum()),
            "nunique": int(series.nunique()),
            "min": float(series.min()) if len(series) else None,
            "max": float(series.max()) if len(series) else None,
        }
    return out

def main():
    print("=== CP-047: Feature Bridge Zeroing Audit ===")

    raw = raw_signal_stats()
    ds = dataset_stats()

    failures = []
    for col in FEATURES:
        raw_nonzero = raw[col]["nonzero"]
        ds_nonzero = ds["features"].get(col, {}).get("nonzero", 0)
        ds_nunique = ds["features"].get(col, {}).get("nunique", 0)
        if raw_nonzero > 0 and (ds_nonzero == 0 or ds_nunique <= 1):
            failures.append(col)

    if failures:
        verdict = "FAIL"
        reason = (
            "Raw signals contain non-zero scanner/flow features, but production_universe_dataset "
            "outputs those same features as all-zero/constant. This confirms feature bridge zeroing."
        )
    else:
        verdict = "PASS"
        reason = "Production dataset preserves non-zero scanner/flow features."

    report = {
        "audit_time_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        "failed_features": failures,
        "raw_signals": raw,
        "production_universe_dataset": ds,
        "governance": {
            "read_only": True,
            "runtime_changed": False,
            "execution_changed": False,
            "model_promoted": False,
            "live_unlock": False,
        },
        "recommended_next_step": (
            "Patch _production_universe_dataset() to join/select feature columns from signals/flow source "
            "before any retraining or promotion."
        ),
    }

    import os
    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("Verdict:", verdict)
    print("Reason:", reason)
    print("Failed features:", failures)
    print("Report:", REPORT_JSON)

if __name__ == "__main__":
    main()
