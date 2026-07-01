"""
CP-039D: Candidate Model Audit
READ-ONLY. No model weights written. No promotion. No retrain.
"""
import json, os, pickle
from datetime import datetime, timezone
from collections import defaultdict

os.environ.setdefault("MPLCONFIGDIR", "/tmp/.matplotlib_cp039d")

def run():
    # Load candidate model
    with open("model_weights_candidate.pkl", "rb") as f:
        pkg = pickle.load(f)
    model = pkg["model"]
    feature_names = pkg["feature_names"]
    metadata = pkg["metadata"]

    print(f"Candidate version: {metadata.get('version')}")
    print(f"Train timestamp:   {metadata.get('train_timestamp')}")
    print(f"Dataset rows:      {metadata.get('dataset_rows')}")

    # Rebuild production universe dataset
    from ml_engine import build_ml_dataset, _encode
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (
        accuracy_score, precision_score, recall_score,
        confusion_matrix, classification_report
    )
    import numpy as np

    ds = build_ml_dataset(
        "paper_trades.csv", "signals_log.csv", "flow_log.csv",
        database_path="mamuyy_hunter.db",
        use_production_universe=True, production_score_threshold=75,
    )

    X, feat = _encode(ds)
    y = ds["target"]
    stratify = y if y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.30, random_state=42, stratify=stratify
    )

    # Retrain same model config to get consistent train/test split
    from sklearn.ensemble import RandomForestClassifier
    m = RandomForestClassifier(n_estimators=250, max_depth=6,
                               class_weight="balanced", random_state=42)
    m.fit(X_train, y_train)
    y_pred = m.predict(X_test)
    y_pred_train = m.predict(X_train)

    classes = sorted(y.unique())
    acc = accuracy_score(y_test, y_pred)
    train_acc = accuracy_score(y_train, y_pred_train)

    print(f"\n=== OVERALL METRICS ===")
    print(f"Train accuracy:  {train_acc:.4f}")
    print(f"Test accuracy:   {acc:.4f}")
    print(f"Overfit gap:     {train_acc - acc:.4f}")

    print(f"\n=== PER-CLASS METRICS ===")
    report = classification_report(y_test, y_pred, output_dict=True)
    for cls in classes:
        m2 = report.get(cls, {})
        print(f"  {cls:12s}: precision={m2.get('precision',0):.3f}  recall={m2.get('recall',0):.3f}  f1={m2.get('f1-score',0):.3f}  support={int(m2.get('support',0))}")

    print(f"\n=== CONFUSION MATRIX ===")
    cm = confusion_matrix(y_test, y_pred, labels=classes)
    header = f"{'':12s}" + "".join(f"  pred_{c[:4]:4s}" for c in classes)
    print(header)
    for i, cls in enumerate(classes):
        row = f"  act_{cls[:7]:7s}" + "".join(f"  {cm[i,j]:8d}" for j in range(len(classes)))
        print(row)

    # Feature importance
    print(f"\n=== TOP 10 FEATURE IMPORTANCE ===")
    importances = m.feature_importances_
    fi = sorted(zip(feat, importances), key=lambda x: -x[1])[:10]
    for fname, imp in fi:
        bar = "█" * int(imp * 100)
        print(f"  {fname:30s} {imp:.4f}  {bar}")

    # Regime breakdown on test set
    print(f"\n=== REGIME BREAKDOWN (test set) ===")
    import pandas as pd
    test_df = ds.iloc[X_test.index.tolist()] if hasattr(X_test, 'index') else ds.iloc[-len(y_test):]
    if "regime_name" in test_df.columns:
        test_df = test_df.copy()
        test_df["pred"] = y_pred
        test_df["correct"] = (test_df["target"] == test_df["pred"])
        for regime, grp in test_df.groupby("regime_name"):
            if len(grp) < 3:
                continue
            racc = grp["correct"].mean()
            n = len(grp)
            print(f"  {str(regime):30s} n={n:4d}  acc={racc:.3f}")

    # Source breakdown on test set
    print(f"\n=== SOURCE BREAKDOWN (test set) ===")
    if "source_artifact" in test_df.columns:
        for src, grp in test_df.groupby("source_artifact"):
            racc = grp["correct"].mean()
            n = len(grp)
            lbl = str(src).split("/")[-1][:30]
            print(f"  {lbl:30s} n={n:4d}  acc={racc:.3f}")

    # Class imbalance risk
    print(f"\n=== CLASS IMBALANCE ANALYSIS ===")
    vc = y.value_counts()
    for cls in classes:
        pct = vc.get(cls, 0) / len(y) * 100
        print(f"  {cls:12s}: {vc.get(cls,0):5d} rows ({pct:.1f}%)")
    min_class = vc.idxmin()
    print(f"  Min class: {min_class} ({vc.min()} rows) — imbalance ratio: {vc.max()/vc.min():.1f}x")

    # HISTORICAL_DERIVED regime isolation
    print(f"\n=== HISTORICAL_DERIVED REGIME IMPACT ===")
    hd = ds[ds["regime_name"] == "HISTORICAL_DERIVED"] if "regime_name" in ds.columns else pd.DataFrame()
    real = ds[ds["regime_name"] != "HISTORICAL_DERIVED"] if "regime_name" in ds.columns else ds
    print(f"  HISTORICAL_DERIVED rows: {len(hd)} ({len(hd)/len(ds)*100:.1f}%)")
    print(f"  Real regime rows:        {len(real)} ({len(real)/len(ds)*100:.1f}%)")
    if not hd.empty:
        print(f"  HISTORICAL_DERIVED label dist: {hd['target'].value_counts().to_dict()}")
    if not real.empty:
        print(f"  Real regime label dist:        {real['target'].value_counts().to_dict()}")

    # Build JSON report
    per_class = {}
    for cls in classes:
        m2 = report.get(cls, {})
        per_class[cls] = {
            "precision": round(m2.get("precision", 0), 4),
            "recall": round(m2.get("recall", 0), 4),
            "f1": round(m2.get("f1-score", 0), 4),
            "support": int(m2.get("support", 0)),
        }

    cm_dict = {}
    for i, actual in enumerate(classes):
        for j, pred in enumerate(classes):
            cm_dict[f"actual_{actual}_pred_{pred}"] = int(cm[i, j])

    fi_dict = {fname: round(float(imp), 6) for fname, imp in fi}

    issues = []
    if acc < 0.55:
        issues.append(f"accuracy {acc:.4f} below 0.55")
    if train_acc - acc > 0.15:
        issues.append(f"overfit gap {train_acc-acc:.4f} > 0.15")
    tp1_recall = report.get("TP1 HIT", {}).get("recall", 0)
    if tp1_recall < 0.30:
        issues.append(f"TP1 HIT recall {tp1_recall:.3f} < 0.30 — class underfit")

    verdict = "PASS" if not issues else ("REVIEW" if acc >= 0.50 else "FAIL")

    result = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "cp": "CP-039D",
        "candidate_version": metadata.get("version"),
        "verdict": verdict,
        "verdict_issues": issues,
        "dataset_rows": int(len(ds)),
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "train_accuracy": round(float(train_acc), 4),
        "test_accuracy": round(float(acc), 4),
        "overfit_gap": round(float(train_acc - acc), 4),
        "per_class_metrics": per_class,
        "confusion_matrix": cm_dict,
        "top10_feature_importance": fi_dict,
        "historical_derived_rows": int(len(hd)),
        "historical_derived_pct": round(len(hd)/len(ds)*100, 1),
        "real_regime_rows": int(len(real)),
        "class_imbalance_ratio": round(float(vc.max()/vc.min()), 2),
        "minority_class": str(min_class),
        "model_weights_written": False,
        "model_promoted": False,
        "runtime_execution_changed": False,
        "threshold_changed": False,
        "live_unlock": False,
        "paper_only": True,
    }

    os.makedirs("reports", exist_ok=True)
    out = "reports/cp039d_candidate_model_audit.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nReport written: {out}")
    print(f"\nVERDICT: {verdict}")
    if issues:
        for i in issues:
            print(f"  ⚠ {i}")
    return result

if __name__ == "__main__":
    run()
