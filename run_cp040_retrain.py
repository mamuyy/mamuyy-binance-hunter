import os, json, hashlib
from datetime import datetime, timezone

os.environ.setdefault("MPLCONFIGDIR", "/tmp/.matplotlib_cp040")

def snap(path):
    if not os.path.exists(path):
        return {"exists": False, "sha": None, "mtime": None}
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:16]
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    return {"exists": True, "mtime": mtime, "sha": sha}

files = ["model_weights.pkl", "model_weights_candidate.pkl", "model_weights_previous.pkl"]
before = {f: snap(f) for f in files}
print("BEFORE:")
for f, s in before.items():
    print(f"  {f}: sha={s['sha']} mtime={s['mtime']}")

from retrain_model import retrain_model, format_retrain_summary
result = retrain_model(candidate_only=True)
print(format_retrain_summary(result))

after = {f: snap(f) for f in files}
print("AFTER:")
for f, s in after.items():
    print(f"  {f}: sha={s['sha']} mtime={s['mtime']}")

prod_changed = before["model_weights.pkl"]["sha"] != after["model_weights.pkl"]["sha"]
print(f"production_model_replaced: {prod_changed}")
c = result.get("candidate", {})
print(f"candidate status: {c.get('status')}")

report = {
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "cp": "CP-040",
    "verdict": "PASS" if not prod_changed else "FAIL",
    "candidate_only": True,
    "dataset_rows": int(result.get("dataset_rows", 0)),
    "model_accuracy": round(float(c.get("accuracy", 0)), 4),
    "model_precision": round(float(c.get("precision", 0)), 4),
    "model_recall": round(float(c.get("recall", 0)), 4),
    "walkforward_stability_score": round(float(c.get("walkforward_score", 0)), 2),
    "walkforward_health": c.get("walkforward_health", "UNKNOWN"),
    "walkforward_avg_pf": round(float(c.get("walkforward_profit_factor", 0)), 4),
    "candidate_status": c.get("status"),
    "candidate_version": c.get("version"),
    "production_model_sha_before": before["model_weights.pkl"]["sha"],
    "production_model_sha_after": after["model_weights.pkl"]["sha"],
    "production_model_replaced": prod_changed,
    "model_promoted": prod_changed,
    "runtime_execution_changed": False,
    "threshold_changed": False,
    "live_unlock": False,
    "paper_only": True,
}
os.makedirs("reports", exist_ok=True)
with open("reports/cp040_retrain_result.json", "w") as f:
    json.dump(report, f, indent=2)
print(json.dumps(report, indent=2))
