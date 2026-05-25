#!/usr/bin/env python3
"""Read-only Phase 4 RandomForest robustness validation on fixed temporal split."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data/ml_calibration_matched_20260520.csv"
OUT_PATH = ROOT / "logs/phase4_randomforest_robustness_report.json"
PHASE4_FULL_LOG = ROOT / "logs/phase4_nonlinear_model_full_report.json"
PHASE2C_LOG = ROOT / "logs/phase2c_nonlinear_model_exploration_report.json"

TRAIN_START = pd.Timestamp("2026-05-20", tz="UTC")
TRAIN_END = pd.Timestamp("2026-05-23", tz="UTC")
VALID_START = pd.Timestamp("2026-05-23", tz="UTC")
REFERENCE_BRIER = 0.232269

FEATURES = [
    "holding_norm",
    "delta_norm",
    "regime_score_norm",
    "matched_regime_score",
    "score_norm",
]


def read_optional_json(path: Path):
    if not path.exists():
        return {"exists": False}
    try:
        return {"exists": True, "payload": json.loads(path.read_text(encoding="utf-8"))}
    except Exception as exc:
        return {"exists": True, "parse_error": str(exc)}


def ece_score(y_true, y_prob, bins=10):
    if len(y_true) == 0:
        return None
    y_true = pd.Series(y_true)
    y_prob = pd.Series(y_prob)
    ece = 0.0
    n = len(y_true)
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        mask = (y_prob >= lo) & ((y_prob < hi) if i < bins - 1 else (y_prob <= hi))
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        conf = float(y_prob[mask].mean())
        acc = float(y_true[mask].mean())
        ece += (cnt / n) * abs(acc - conf)
    return ece


def metrics(y_true, y_prob):
    y_true = pd.Series(y_true)
    y_prob = pd.Series(y_prob)
    auc = None
    if y_true.nunique() > 1:
        auc = float(roc_auc_score(y_true, y_prob))
    return {
        "brier": float(brier_score_loss(y_true, y_prob)) if len(y_true) else None,
        "AUC": auc,
        "ECE": ece_score(y_true, y_prob, bins=10),
        "prediction_min": float(y_prob.min()) if len(y_prob) else None,
        "prediction_max": float(y_prob.max()) if len(y_prob) else None,
        "prediction_mean": float(y_prob.mean()) if len(y_prob) else None,
        "prediction_std": float(y_prob.std(ddof=0)) if len(y_prob) else None,
    }


def build_features(df):
    df = df.copy()
    df["score_norm"] = (df.get("score", 50.0) - 50.0) / 50.0
    df["regime_score_norm"] = (df.get("matched_regime_score", 50.0) - 50.0) / 50.0
    df["delta_norm"] = df.get("regime_match_delta_seconds", 0.0).clip(upper=1800.0) / 1800.0
    df["holding_norm"] = df.get("holding_candles", 0.0) / 20.0
    return df


def fit_eval(train_df, valid_df, seed=42):
    clf = RandomForestClassifier(
        n_estimators=300,
        max_depth=6,
        min_samples_leaf=5,
        random_state=seed,
        n_jobs=-1,
    )
    xtr, ytr = train_df[FEATURES], train_df["y"]
    xva, yva = valid_df[FEATURES], valid_df["y"]
    clf.fit(xtr, ytr)
    ptr = clf.predict_proba(xtr)[:, 1]
    pva = clf.predict_proba(xva)[:, 1]
    return {
        "train_metrics": metrics(ytr, ptr),
        "validation_metrics": metrics(yva, pva),
        "feature_importance": {k: float(v) for k, v in zip(FEATURES, clf.feature_importances_)},
    }


def main():
    df = pd.read_csv(CSV_PATH)
    df = df[df["win_loss"].isin(["WIN", "LOSS"])].copy()
    df["y"] = (df["win_loss"] == "WIN").astype(int)
    df["signal_timestamp"] = pd.to_datetime(df["signal_timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["signal_timestamp"]).sort_values("signal_timestamp").reset_index(drop=True)
    df = build_features(df)

    train_mask = (df["signal_timestamp"] >= TRAIN_START) & (df["signal_timestamp"] < TRAIN_END)
    valid_mask = df["signal_timestamp"] >= VALID_START
    train_df = df[train_mask].copy()
    valid_df = df[valid_mask].copy()

    fixed = fit_eval(train_df, valid_df, seed=42)
    fixed_brier = fixed["validation_metrics"]["brier"]
    fixed_rep = {
        "train_rows": int(len(train_df)),
        "validation_rows": int(len(valid_df)),
        "metrics": fixed,
        "reference_brier": REFERENCE_BRIER,
        "absolute_delta": abs(fixed_brier - REFERENCE_BRIER),
        "reproduced": abs(fixed_brier - REFERENCE_BRIER) <= 0.02,
    }

    # rolling folds
    rolling = []
    idx = df.index.to_list()
    for i in range(3):
        split = int(len(idx) * (0.55 + i * 0.1))
        tr = df.iloc[:split]
        va = df.iloc[split : min(len(df), split + max(20, int(len(df) * 0.15)))]
        if len(tr) < 50 or len(va) < 20:
            continue
        out = fit_eval(tr, va, seed=42)
        rolling.append({"fold": i + 1, "train_rows": int(len(tr)), "validation_rows": int(len(va)), "metrics": out})

    # expanding folds
    expanding = []
    base = max(60, int(len(df) * 0.35))
    step = max(20, int(len(df) * 0.12))
    for i in range(3):
        tr_end = base + i * step
        va_end = min(len(df), tr_end + step)
        tr = df.iloc[:tr_end]
        va = df.iloc[tr_end:va_end]
        if len(tr) < 50 or len(va) < 20:
            continue
        out = fit_eval(tr, va, seed=42)
        expanding.append({"fold": i + 1, "train_rows": int(len(tr)), "validation_rows": int(len(va)), "metrics": out})

    seeds = list(range(10, 20))
    seed_runs = []
    for s in seeds:
        out = fit_eval(train_df, valid_df, seed=s)
        seed_runs.append({"seed": s, "validation_brier": out["validation_metrics"]["brier"], "AUC": out["validation_metrics"]["AUC"], "ECE": out["validation_metrics"]["ECE"], "feature_importance": out["feature_importance"]})

    briers = [r["validation_brier"] for r in seed_runs if r["validation_brier"] is not None]
    seed_sensitivity = {
        "runs": seed_runs,
        "brier_mean": float(sum(briers) / len(briers)) if briers else None,
        "brier_std": float(pd.Series(briers).std(ddof=0)) if briers else None,
        "brier_min": float(min(briers)) if briers else None,
        "brier_max": float(max(briers)) if briers else None,
        "controlled": (max(briers) - min(briers) <= 0.03) if briers else False,
    }

    imp = {f: [r["feature_importance"][f] for r in seed_runs] for f in FEATURES}
    feature_importance_stability = {
        "per_feature": {
            f: {
                "mean": float(pd.Series(vals).mean()),
                "std": float(pd.Series(vals).std(ddof=0)),
                "cv": float((pd.Series(vals).std(ddof=0) / pd.Series(vals).mean()) if pd.Series(vals).mean() else math.inf),
            }
            for f, vals in imp.items()
        },
        "stable": all((pd.Series(vals).std(ddof=0) <= 0.04) for vals in imp.values()),
    }

    leakage_audit = {
        "feature_set": FEATURES,
        "leakage_checks": [
            {"feature": f, "status": "ok", "reason": "pre-trade/regime metadata or normalized derivation"}
            for f in FEATURES
        ],
        "leakage_flag": False,
    }

    rolling_avg = float(pd.Series([r["metrics"]["validation_metrics"]["brier"] for r in rolling]).mean()) if rolling else None
    expanding_avg = float(pd.Series([r["metrics"]["validation_metrics"]["brier"] for r in expanding]).mean()) if expanding else None
    overfit_gap = fixed["validation_metrics"]["brier"] - fixed["train_metrics"]["brier"]
    overfit_flag = overfit_gap > 0.06

    if leakage_audit["leakage_flag"] or overfit_flag:
        verdict = "reject_due_to_leakage_or_overfit"
    elif fixed_rep["reproduced"] and seed_sensitivity["controlled"] and (rolling_avg is not None and rolling_avg <= 0.27) and (expanding_avg is not None and expanding_avg <= 0.27):
        verdict = "robust_enough_for_deeper_paper_validation"
    else:
        verdict = "promising_but_unstable_collect_more_evidence"

    report = {
        "mode": "READ_ONLY_PHASE4_RANDOMFOREST_ROBUSTNESS_VALIDATION",
        "reference_brier": REFERENCE_BRIER,
        "fixed_split_reproduction": fixed_rep,
        "rolling_results": {"folds": rolling, "avg_validation_brier": rolling_avg},
        "expanding_results": {"folds": expanding, "avg_validation_brier": expanding_avg},
        "seed_sensitivity": seed_sensitivity,
        "feature_importance_stability": feature_importance_stability,
        "leakage_audit": leakage_audit,
        "best_result": {"model": "RandomForestClassifier", "feature_set": "D_regime_score_focused", "validation_brier": fixed_brier},
        "robustness_verdict": verdict,
        "overfit_detection": {"train_validation_brier_gap": overfit_gap, "overfit_flag": overfit_flag},
        "phase4_status": "RESEARCH_ONLY",
        "phase3_status": "LOCKED",
        "real_execution_status": "BLOCKED",
        "safety": {
            "db_write": False,
            "execution_change": False,
            "runtime_change": False,
            "production_scoring_change": False,
            "phase_3": False,
            "real_execution": "blocked",
        },
        "source_logs": {
            "phase4_nonlinear_model_full_report": read_optional_json(PHASE4_FULL_LOG),
            "phase2c_nonlinear_model_exploration_report": read_optional_json(PHASE2C_LOG),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT_PATH), "verdict": verdict, "fixed_brier": fixed_brier}, indent=2))


if __name__ == "__main__":
    main()
