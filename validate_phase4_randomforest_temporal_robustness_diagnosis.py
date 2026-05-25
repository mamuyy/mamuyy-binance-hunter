#!/usr/bin/env python3
"""Read-only Phase 4 RandomForest temporal robustness diagnosis.

Produces a JSON report explaining temporal instability on rolling/expanding folds.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data/ml_calibration_matched_20260520.csv"
RF_REPORT = ROOT / "logs/phase4_randomforest_robustness_report.json"
FULL_REPORT = ROOT / "logs/phase4_nonlinear_model_full_report.json"
OUT_PATH = ROOT / "logs/phase4_randomforest_temporal_robustness_diagnosis_report.json"

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
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    if len(y_true) == 0:
        return None
    ece = 0.0
    n = len(y_true)
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        mask = (y_prob >= lo) & (y_prob < hi) if i < bins - 1 else (y_prob >= lo) & (y_prob <= hi)
        cnt = int(np.sum(mask))
        if cnt == 0:
            continue
        conf = float(np.mean(y_prob[mask]))
        acc = float(np.mean(y_true[mask]))
        ece += (cnt / n) * abs(acc - conf)
    return float(ece)


def safe_auc(y_true, y_prob):
    if np.unique(y_true).size < 2:
        return None
    return float(roc_auc_score(y_true, y_prob))


def build_features(df):
    df = df.copy()
    df["score_norm"] = (df.get("score", 50.0) - 50.0) / 50.0
    df["regime_score_norm"] = (df.get("matched_regime_score", 50.0) - 50.0) / 50.0
    df["delta_norm"] = df.get("regime_match_delta_seconds", 0.0).clip(upper=1800.0) / 1800.0
    df["holding_norm"] = df.get("holding_candles", 0.0) / 20.0
    return df


def psi_like(train_s, valid_s, bins=8):
    train_s = pd.Series(train_s).astype(float)
    valid_s = pd.Series(valid_s).astype(float)
    if train_s.empty or valid_s.empty:
        return None
    edges = np.quantile(train_s, np.linspace(0, 1, bins + 1))
    edges = np.unique(edges)
    if len(edges) < 3:
        return 0.0
    tr_hist, _ = np.histogram(train_s, bins=edges)
    va_hist, _ = np.histogram(valid_s, bins=edges)
    tr_p = np.clip(tr_hist / max(tr_hist.sum(), 1), 1e-6, 1.0)
    va_p = np.clip(va_hist / max(va_hist.sum(), 1), 1e-6, 1.0)
    return float(np.sum((va_p - tr_p) * np.log(va_p / tr_p)))


def fold_metrics(clf, train_df, valid_df):
    xtr, ytr = train_df[FEATURES], train_df["y"]
    xva, yva = valid_df[FEATURES], valid_df["y"]
    clf.fit(xtr, ytr)
    pva = clf.predict_proba(xva)[:, 1]

    return {
        "train_rows": int(len(train_df)),
        "validation_rows": int(len(valid_df)),
        "validation_brier": float(brier_score_loss(yva, pva)),
        "AUC": safe_auc(yva, pva),
        "ECE": ece_score(yva, pva, bins=10),
        "train_winrate": float(ytr.mean()) if len(ytr) else None,
        "validation_winrate": float(yva.mean()) if len(yva) else None,
        "winrate_delta": float(yva.mean() - ytr.mean()) if len(ytr) and len(yva) else None,
        "regime_distribution_drift": psi_like(train_df["matched_regime_score"], valid_df["matched_regime_score"], bins=8),
        "prediction_min": float(np.min(pva)) if len(pva) else None,
        "prediction_max": float(np.max(pva)) if len(pva) else None,
        "prediction_mean": float(np.mean(pva)) if len(pva) else None,
        "prediction_std": float(np.std(pva, ddof=0)) if len(pva) else None,
    }


def summarize_folds(folds):
    briers = [f["validation_brier"] for f in folds if f.get("validation_brier") is not None]
    pass_count = sum(1 for f in folds if (f["validation_brier"] <= 0.24 and (f["ECE"] is None or f["ECE"] <= 0.07) and (f["AUC"] is None or f["AUC"] >= 0.60)))
    fail_count = len(folds) - pass_count
    return {
        "num_folds": len(folds),
        "pass_folds": int(pass_count),
        "fail_folds": int(fail_count),
        "average_brier": float(np.mean(briers)) if briers else None,
        "median_brier": float(np.median(briers)) if briers else None,
        "best_fold": min(folds, key=lambda x: x["validation_brier"]) if folds else None,
        "worst_fold": max(folds, key=lambda x: x["validation_brier"]) if folds else None,
    }


def main():
    df = pd.read_csv(CSV_PATH)
    df = df[df["win_loss"].isin(["WIN", "LOSS"])].copy()
    df["y"] = (df["win_loss"] == "WIN").astype(int)
    df["signal_timestamp"] = pd.to_datetime(df["signal_timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["signal_timestamp"]).sort_values("signal_timestamp").reset_index(drop=True)
    df = build_features(df)

    clf = RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=5, random_state=42, n_jobs=-1)

    n = len(df)
    rolling_folds = []
    window = max(30, int(n * 0.18))
    train_window = max(80, int(n * 0.45))
    step = max(20, int(window * 0.6))
    fold_id = 1
    for start in range(0, max(n - train_window - window + 1, 0), step):
        tr = df.iloc[start : start + train_window].copy()
        va = df.iloc[start + train_window : start + train_window + window].copy()
        if len(tr) < 60 or len(va) < 20:
            continue
        m = fold_metrics(clf, tr, va)
        m["fold"] = fold_id
        m["split_type"] = "rolling"
        rolling_folds.append(m)
        fold_id += 1

    expanding_folds = []
    base_train = max(80, int(n * 0.35))
    valid_window = max(30, int(n * 0.15))
    step2 = max(20, int(valid_window * 0.75))
    fold_id = 1
    for tr_end in range(base_train, n - valid_window + 1, step2):
        tr = df.iloc[:tr_end].copy()
        va = df.iloc[tr_end : tr_end + valid_window].copy()
        if len(tr) < 60 or len(va) < 20:
            continue
        m = fold_metrics(clf, tr, va)
        m["fold"] = fold_id
        m["split_type"] = "expanding"
        expanding_folds.append(m)
        fold_id += 1

    all_folds = rolling_folds + expanding_folds
    bad_folds = [
        f for f in all_folds
        if (f["validation_brier"] > 0.24) or ((f["ECE"] is not None) and (f["ECE"] > 0.07)) or ((f["AUC"] is not None) and (f["AUC"] < 0.60))
    ]

    causes = []
    if any((f.get("regime_distribution_drift") or 0) > 0.2 for f in bad_folds):
        causes.append("regime_drift")
    if any(abs(f.get("winrate_delta") or 0) > 0.08 for f in bad_folds):
        causes.append("winrate_drift")
    if any((f.get("validation_rows") or 0) < 40 for f in bad_folds):
        causes.append("low_fold_support")
    if any((f.get("ECE") or 0) > 0.07 for f in bad_folds):
        causes.append("calibration_error")
    if any((f.get("prediction_std") or 0) < 0.03 for f in bad_folds):
        causes.append("feature_distribution_shift")
    if not causes:
        causes.append("mild_temporal_noise")

    combined_briers = [f["validation_brier"] for f in all_folds]
    window_specific = (max(combined_briers) - min(combined_briers) >= 0.015) if combined_briers else False
    recommendation = "A) collect more outcomes" if window_specific else "B) apply temporal/regime-aware calibration"

    report = {
        "mode": "READ_ONLY_PHASE4_RANDOMFOREST_TEMPORAL_ROBUSTNESS_DIAGNOSIS",
        "reference_fixed_brier": REFERENCE_BRIER,
        "rolling_summary": {"folds": rolling_folds, **summarize_folds(rolling_folds)},
        "expanding_summary": {"folds": expanding_folds, **summarize_folds(expanding_folds)},
        "bad_folds": bad_folds,
        "instability_causes": {
            "detected": causes,
            "assessment": "temporary_or_window_specific" if window_specific else "potentially_structural",
        },
        "recommendation": recommendation,
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
            "phase4_randomforest_robustness_report": read_optional_json(RF_REPORT),
            "phase4_nonlinear_model_full_report": read_optional_json(FULL_REPORT),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT_PATH), "bad_folds": len(bad_folds), "recommendation": recommendation}, indent=2))


if __name__ == "__main__":
    main()
