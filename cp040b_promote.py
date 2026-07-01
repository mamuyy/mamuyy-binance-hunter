"""
CP-040B: Controlled promotion of existing CP-040 candidate to paper production.
No retrain. Copies audited artifact only after all pre-checks pass.
"""
import json, os, shutil, hashlib
from datetime import datetime, timezone

CANDIDATE_PATH = "model_weights_candidate.pkl"
PRODUCTION_PATH = "model_weights.pkl"
PREVIOUS_PATH   = "model_weights_previous.pkl"
REGISTRY_PATH   = "model_registry.json"
REPORT_PATH     = "reports/cp040b_candidate_paper_promotion.json"

EXPECTED_CANDIDATE_SHA = "529ee3cd"
EXPECTED_PRODUCTION_SHA = "504788a3"

def sha256_prefix(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:8]

def load_registry():
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH) as f:
        return json.load(f)

def save_registry(reg):
    with open(REGISTRY_PATH, "w") as f:
        json.dump(reg, f, indent=2)

# ── PRE-PROMOTION CHECKS ──────────────────────────────────────────────────────
print("=== PRE-PROMOTION CHECKS ===")
errors = []

# 1. Candidate exists
if not os.path.exists(CANDIDATE_PATH):
    errors.append("FAIL: model_weights_candidate.pkl not found")
else:
    print(f"[PASS] candidate exists")

# 2. Candidate sha matches
cand_sha = sha256_prefix(CANDIDATE_PATH)
if cand_sha != EXPECTED_CANDIDATE_SHA:
    errors.append(f"FAIL: candidate sha {cand_sha} != expected {EXPECTED_CANDIDATE_SHA}")
else:
    print(f"[PASS] candidate sha: {cand_sha}")

# 3. Candidate status in registry
registry = load_registry()
reg_candidate = registry.get("candidate", {})
cand_status = reg_candidate.get("status", "unknown")
if cand_status != "candidate_pending_review":
    errors.append(f"FAIL: candidate status '{cand_status}' != candidate_pending_review")
else:
    print(f"[PASS] candidate status: {cand_status}")

# 4. CP-040 metrics gates
acc   = float(reg_candidate.get("accuracy", 0))
prec  = float(reg_candidate.get("precision", 0))
wf    = float(reg_candidate.get("walkforward_score", 0))
wfh   = reg_candidate.get("walkforward_health", "")
wfpf  = float(reg_candidate.get("walkforward_profit_factor", 0))
if acc < 0.60:
    errors.append(f"FAIL: accuracy {acc:.4f} < 0.60")
else:
    print(f"[PASS] accuracy: {acc:.4f}")
if prec < 0.70:
    errors.append(f"FAIL: precision {prec:.4f} < 0.70")
else:
    print(f"[PASS] precision: {prec:.4f}")
if wf < 45:
    errors.append(f"FAIL: WF stability {wf:.2f} < 45")
else:
    print(f"[PASS] WF stability: {wf:.2f}")
if wfh != "ROBUST":
    errors.append(f"FAIL: WF health '{wfh}' != ROBUST")
else:
    print(f"[PASS] WF health: {wfh}")
if wfpf < 1.5:
    errors.append(f"FAIL: WF avg PF {wfpf:.4f} < 1.5")
else:
    print(f"[PASS] WF avg PF: {wfpf:.4f}")

# 5. Production sha matches expected (unchanged since Jun 23)
prod_sha_before = sha256_prefix(PRODUCTION_PATH)
if prod_sha_before != EXPECTED_PRODUCTION_SHA:
    errors.append(f"FAIL: production sha {prod_sha_before} != expected {EXPECTED_PRODUCTION_SHA}")
else:
    print(f"[PASS] production sha before: {prod_sha_before}")

if errors:
    print("\n=== PRE-CHECK FAILED — PROMOTION ABORTED ===")
    for e in errors:
        print(f"  {e}")
    exit(1)

print("\n[ALL PRE-CHECKS PASSED] Proceeding with promotion.")

# ── PROMOTION ─────────────────────────────────────────────────────────────────
print("\n=== PROMOTION ===")

# 1. Backup existing production -> previous
shutil.copy2(PRODUCTION_PATH, PREVIOUS_PATH)
prev_sha = sha256_prefix(PREVIOUS_PATH)
print(f"Backed up model_weights.pkl -> model_weights_previous.pkl (sha: {prev_sha})")

# 2. Copy candidate -> production
shutil.copy2(CANDIDATE_PATH, PRODUCTION_PATH)
prod_sha_after = sha256_prefix(PRODUCTION_PATH)
print(f"Promoted model_weights_candidate.pkl -> model_weights.pkl (sha: {prod_sha_after})")

# 3. Update registry
promoted = dict(reg_candidate)
promoted["status"] = "production"
promoted["promoted_at"] = datetime.now(timezone.utc).isoformat()
promoted["promoted_by"] = "CP-040B"
promoted["promotion_scope"] = "PAPER_ONLY_MODEL"

registry["production"] = promoted
registry["candidate"] = promoted
history = registry.get("history", [])
history.append({**promoted, "accepted": True,
                "reasons": ["CP-040B: controlled promotion of audited candidate"]})
registry["history"] = history[-25:]
registry["rollback_available"] = True
save_registry(registry)
print(f"Registry updated: production = {promoted.get('version')}")

# ── POST-PROMOTION VALIDATION ─────────────────────────────────────────────────
print("\n=== POST-PROMOTION VALIDATION ===")
post_errors = []

v1 = sha256_prefix(PRODUCTION_PATH)
if v1 != EXPECTED_CANDIDATE_SHA:
    post_errors.append(f"FAIL: new production sha {v1} != {EXPECTED_CANDIDATE_SHA}")
else:
    print(f"[PASS] production sha now: {v1} (matches candidate)")

v2 = sha256_prefix(PREVIOUS_PATH)
if v2 != EXPECTED_PRODUCTION_SHA:
    post_errors.append(f"FAIL: previous sha {v2} != {EXPECTED_PRODUCTION_SHA}")
else:
    print(f"[PASS] previous sha: {v2} (old production backed up)")

reg2 = load_registry()
reg_prod_version = reg2.get("production", {}).get("version")
cand_version = reg_candidate.get("version")
if reg_prod_version != cand_version:
    post_errors.append(f"FAIL: registry production version mismatch")
else:
    print(f"[PASS] registry production version: {reg_prod_version}")

if reg2.get("production", {}).get("status") != "production":
    post_errors.append("FAIL: production status not set to 'production'")
else:
    print(f"[PASS] registry production status: production")

# ── REPORT ────────────────────────────────────────────────────────────────────
verdict = "FAIL" if post_errors else "PASS"
report = {
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "cp": "CP-040B",
    "verdict": verdict,
    "verdict_issues": post_errors,
    "promoted_candidate_id": promoted.get("version"),
    "old_production_sha256": prod_sha_before,
    "new_production_sha256": prod_sha_after,
    "previous_model_sha256": prev_sha,
    "candidate_metrics": {
        "accuracy": acc,
        "precision": prec,
        "walkforward_stability_score": wf,
        "walkforward_health": wfh,
        "walkforward_avg_pf": wfpf,
        "dataset_rows": reg_candidate.get("dataset_rows"),
    },
    "promotion_method": "copy_existing_audited_candidate_artifact",
    "production_model_replaced": True,
    "previous_model_backup_created": True,
    "model_promoted": True,
    "promotion_scope": "PAPER_ONLY_MODEL",
    "runtime_execution_changed": False,
    "threshold_changed": False,
    "scanner_changed": False,
    "telegram_execution_changed": False,
    "portfolio_engine_changed": False,
    "live_unlock": False,
    "phase3_unlock": False,
    "paper_only": True,
}

os.makedirs("reports", exist_ok=True)
with open(REPORT_PATH, "w") as f:
    json.dump(report, f, indent=2)

print(f"\n=== REPORT ===")
print(json.dumps(report, indent=2))
print(f"\nVERDICT: {verdict}")
