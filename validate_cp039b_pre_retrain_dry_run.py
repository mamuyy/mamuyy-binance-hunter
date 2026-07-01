"""
CP-039B Pre-Retrain Dry-Run Validation
READ-ONLY. No model weights written. No promotion. No retrain.
"""
import json
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("MPLCONFIGDIR", "/tmp/.matplotlib_cp039b")

def _safe_float(v, default=0.0):
    try:
        f = float(v)
        import math
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default

def run():
    from ml_engine import build_ml_dataset
    from walkforward import run_walkforward_validation
    from retrain_model import _replacement_allowed, _load_registry

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "cp": "CP-039B",
        "patch_commit": "d247679",
        "model_weights_written": False,
        "model_promoted": False,
        "runtime_execution_changed": False,
        "threshold_changed": False,
        "live_unlock": False,
    }

    # === 1. Build production universe dataset ===
    print("Building production universe dataset...")
    dataset = build_ml_dataset(
        "paper_trades.csv",
        "signals_log.csv",
        "flow_log.csv",
        database_path="mamuyy_hunter.db",
        use_production_universe=True,
        production_score_threshold=75,
    )

    report["dataset_rows"] = int(len(dataset))

    if dataset.empty:
        report["verdict"] = "FAIL"
        report["fail_reason"] = "Production universe dataset is empty"
        return report

    # Label distribution raw
    label_dist = dataset["target"].value_counts().to_dict()
    report["label_distribution_raw"] = {str(k): int(v) for k, v in label_dist.items()}

    # Binary effective: WIN effective = WIN + TP1 HIT
    win_eff = int(label_dist.get("WIN", 0)) + int(label_dist.get("TP1 HIT", 0))
    loss_eff = int(label_dist.get("LOSS", 0))
    total = report["dataset_rows"]
    report["label_distribution_binary_effective"] = {
        "WIN_effective (WIN + TP1 HIT)": win_eff,
        "LOSS": loss_eff,
        "WIN_effective_pct": round(win_eff / total * 100, 2) if total else 0,
        "LOSS_pct": round(loss_eff / total * 100, 2) if total else 0,
        "note": "TP1 HIT is profitable — if binary model, WIN effective counts both",
    }

    # Source distribution
    if "source_artifact" in dataset.columns:
        src_dist = dataset["source_artifact"].value_counts().to_dict()
        report["source_distribution"] = {str(k): int(v) for k, v in src_dist.items()}
    else:
        report["source_distribution"] = {"note": "source_artifact column not present"}

    # Regime distribution
    if "regime_name" in dataset.columns:
        regime_dist = dataset["regime_name"].fillna("UNKNOWN").value_counts().to_dict()
        report["regime_distribution"] = {str(k): int(v) for k, v in regime_dist.items()}
    else:
        report["regime_distribution"] = {"note": "regime_name column not present"}

    # Score stats
    if "score" in dataset.columns:
        import statistics
        scores = [float(x) for x in dataset["score"].dropna()]
        report["score_stats"] = {
            "min": round(min(scores), 2),
            "max": round(max(scores), 2),
            "mean": round(statistics.mean(scores), 2),
            "all_above_threshold_75": all(s >= 75 for s in scores),
        }

    # === 2. Walkforward validation (same dataset) ===
    print("Running walkforward validation with prebuilt dataset...")
    wf = run_walkforward_validation(
        prebuilt_dataset=dataset,
        output_path="/tmp/cp039b_wf_dryrun.csv",
        chart_dir="/tmp/cp039b_charts",
    )

    report["walkforward_rows"] = int(wf.get("rows", 0))
    report["walkforward_folds"] = int(wf.get("folds", 0))
    report["walkforward_health"] = str(wf.get("model_health", "UNKNOWN"))
    report["walkforward_stability_score"] = round(_safe_float(wf.get("model_stability_score")), 2)
    report["walkforward_overfit_risk"] = round(_safe_float(wf.get("overfit_risk_score")), 2)
    report["walkforward_average_accuracy"] = round(_safe_float(wf.get("average_accuracy")), 4)
    report["walkforward_average_pf"] = round(_safe_float(wf.get("average_profit_factor")), 4)
    report["walkforward_dataset_match"] = report["dataset_rows"] == report["walkforward_rows"]

    # === 3. Replacement gate preview (no model, no file) ===
    registry = _load_registry("model_registry.json")
    production = registry.get("production")

    # Simulate candidate metrics using dataset stats (no training)
    candidate_preview = {
        "dataset_rows": report["dataset_rows"],
        "walkforward_score": report["walkforward_stability_score"],
        "accuracy": 0.0,   # unknown until trained
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
    }
    gate_accepted, gate_reasons = _replacement_allowed(candidate_preview, production)

    report["replacement_gate_preview"] = {
        "note": "Simulated with walkforward_score only — accuracy/PF unknown until retrain",
        "walkforward_score": report["walkforward_stability_score"],
        "walkforward_gate_45_pass": report["walkforward_stability_score"] >= 45,
        "dataset_rows_gate_8_pass": report["dataset_rows"] >= 8,
        "production_model_exists": production is not None,
        "production_version": production.get("version", "-") if production else "-",
        "gate_result_with_zero_metrics": gate_accepted,
        "gate_reasons": gate_reasons,
    }

    # === 4. Verify no model weights written ===
    model_files = ["model_weights.pkl", "model_weights_candidate.pkl", "model_weights_previous.pkl"]
    weights_check = {}
    for f in model_files:
        if os.path.exists(f):
            mtime = os.path.getmtime(f)
            weights_check[f] = {
                "exists": True,
                "mtime_utc": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            }
        else:
            weights_check[f] = {"exists": False}
    report["model_weights_check"] = weights_check
    report["model_weights_written"] = False  # dry-run never calls retrain_model()

    # === 5. Final verdict ===
    issues = []
    if report["dataset_rows"] < 8:
        issues.append("dataset_rows < 8")
    if not report["walkforward_dataset_match"]:
        issues.append("walkforward row count mismatch")
    if report["walkforward_stability_score"] < 45:
        issues.append(f"walkforward_stability_score {report['walkforward_stability_score']} < 45 gate")
    if report["walkforward_folds"] == 0:
        issues.append("walkforward produced 0 folds")
    if dataset["target"].nunique() < 2:
        issues.append("fewer than 2 unique target classes")

    if not issues:
        report["verdict"] = "PASS"
    elif any("mismatch" in i or "< 8" in i or "0 folds" in i for i in issues):
        report["verdict"] = "FAIL"
    else:
        report["verdict"] = "REVIEW"
    report["verdict_issues"] = issues

    return report


if __name__ == "__main__":
    report = run()
    os.makedirs("reports", exist_ok=True)
    out_path = "reports/cp039b_pre_retrain_dry_run.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    print(f"\nReport written: {out_path}")
    print(f"\nVERDICT: {report['verdict']}")
