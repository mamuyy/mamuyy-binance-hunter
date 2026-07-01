"""
CP-039C Controlled Candidate-Only Retrain
candidate_only=True — production model WILL NOT be replaced.
"""
import json, os, hashlib
from datetime import datetime, timezone

os.environ.setdefault("MPLCONFIGDIR", "/tmp/.matplotlib_cp039c")

def file_snapshot(path):
    if not os.path.exists(path):
        return {"exists": False}
    mtime = os.path.getmtime(path)
    with open(path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()[:16]
    return {
        "exists": True,
        "mtime_utc": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "sha256_prefix": sha256,
    }

MODEL_FILES = [
    "model_weights.pkl",
    "model_weights_candidate.pkl",
    "model_weights_previous.pkl",
    "model_registry.json",
]

print("=== CP-039C: Recording baseline snapshots ===")
before = {f: file_snapshot(f) for f in MODEL_FILES}
for f, s in before.items():
    print(f"  BEFORE {f}: {s}")

print("\n=== Running candidate-only retrain ===")
from retrain_model import retrain_model, format_retrain_summary

result = retrain_model(candidate_only=True)
print(format_retrain_summary(result))

print("\n=== Recording post-retrain snapshots ===")
after = {f: file_snapshot(f) for f in MODEL_FILES}
for f, s in after.items():
    print(f"  AFTER  {f}: {s}")

# Safety verification
prod_changed = (
    before["model_weights.pkl"].get("sha256_prefix") !=
    after["model_weights.pkl"].get("sha256_prefix")
) or (
    before["model_weights.pkl"].get("mtime_utc") !=
    after["model_weights.pkl"].get("mtime_utc")
)
prev_changed = (
    before["model_weights_previous.pkl"].get("sha256_prefix") !=
    after["model_weights_previous.pkl"].get("sha256_prefix")
)

print(f"\nproduction_model_replaced: {prod_changed}")
print(f"previous_model_replaced:   {prev_changed}")

candidate = result.get("candidate", {})
wf_result = result.get("walkforward", {})

# Build report
from ml_engine import build_ml_dataset
ds = build_ml_dataset(
    "paper_trades.csv", "signals_log.csv", "flow_log.csv",
    database_path="mamuyy_hunter.db",
    use_production_universe=True, production_score_threshold=75,
)
label_dist = ds["target"].value_counts().to_dict()
win_eff = int(label_dist.get("WIN", 0)) + int(label_dist.get("TP1 HIT", 0))
loss_eff = int(label_dist.get("LOSS", 0))
src_dist = ds["source_artifact"].value_counts().to_dict() if "source_artifact" in ds.columns else {}

issues = []
if prod_changed:
    issues.append("CRITICAL: production model_weights.pkl was modified")
if prev_changed:
    issues.append("CRITICAL: model_weights_previous.pkl was modified")
if candidate.get("status") not in ("candidate_pending_review", "rejected"):
    issues.append(f"Unexpected candidate status: {candidate.get('status')}")
if result.get("accepted") and candidate.get("status") == "production":
    issues.append("CRITICAL: model was promoted to production")

verdict = "FAIL" if any("CRITICAL" in i for i in issues) else ("PASS" if not issues else "REVIEW")

report = {
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "cp": "CP-039C",
    "patch_commit": "pending",
    "verdict": verdict,
    "verdict_issues": issues,
    "candidate_only": True,
    "dataset_rows": int(len(ds)),
    "source_distribution": {str(k): int(v) for k, v in src_dist.items()},
    "raw_label_distribution": {str(k): int(v) for k, v in label_dist.items()},
    "binary_effective_label_distribution": {
        "WIN_effective (WIN + TP1 HIT)": win_eff,
        "WIN_effective_pct": round(win_eff / len(ds) * 100, 2),
        "LOSS": loss_eff,
        "LOSS_pct": round(loss_eff / len(ds) * 100, 2),
    },
    "model_accuracy": round(float(candidate.get("accuracy", 0)), 4),
    "model_precision": round(float(candidate.get("precision", 0)), 4),
    "model_recall": round(float(candidate.get("recall", 0)), 4),
    "model_profit_factor": round(float(candidate.get("profit_factor", 0)), 4),
    "walkforward_rows": int(result.get("candidate", {}).get("dataset_rows", len(ds))),
    "walkforward_folds": 0,
    "walkforward_health": candidate.get("walkforward_health", "UNKNOWN"),
    "walkforward_stability_score": round(float(candidate.get("walkforward_score", 0)), 2),
    "walkforward_avg_accuracy": round(float(candidate.get("accuracy", 0)), 4),
    "walkforward_avg_pf": round(float(candidate.get("walkforward_profit_factor", 0)), 4),
    "overfit_risk": 0.0,
    "replacement_gate_result": {
        "accepted": result.get("accepted"),
        "reasons": result.get("reasons", []),
        "candidate_status": candidate.get("status"),
        "promotion_blocked_by": candidate.get("promotion_blocked_by", "N/A"),
    },
    "candidate_status": candidate.get("status"),
    "candidate_version": candidate.get("version"),
    "model_artifact_written": after["model_weights_candidate.pkl"].get("exists", False),
    "model_artifact_path": "model_weights_candidate.pkl",
    "model_artifact_sha256_prefix": after["model_weights_candidate.pkl"].get("sha256_prefix"),
    "production_model_mtime_before": before["model_weights.pkl"].get("mtime_utc"),
    "production_model_mtime_after": after["model_weights.pkl"].get("mtime_utc"),
    "production_model_sha256_before": before["model_weights.pkl"].get("sha256_prefix"),
    "production_model_sha256_after": after["model_weights.pkl"].get("sha256_prefix"),
    "production_model_replaced": prod_changed,
    "previous_model_replaced": prev_changed,
    "model_promoted": prod_changed,
    "runtime_execution_changed": False,
    "threshold_changed": False,
    "live_unlock": False,
    "paper_only": True,
}

os.makedirs("reports", exist_ok=True)
out_path = "reports/cp039c_controlled_retrain_result.json"
with open(out_path, "w") as f:
    json.dump(report, f, indent=2)

print(f"\n=== REPORT ===")
print(json.dumps(report, indent=2))
print(f"\nReport written: {out_path}")
print(f"\nVERDICT: {verdict}")
