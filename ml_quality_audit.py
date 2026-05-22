import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.model_selection import train_test_split

from ml_engine import (
    PROFITABLE_LABELS,
    TARGET_LABELS,
    _encode,
    build_ml_dataset,
)
from walkforward import run_walkforward_validation


def _binary_target(labels: pd.Series) -> pd.Series:
    return labels.isin(PROFITABLE_LABELS).astype(int)


def _label_quality(dataset: pd.DataFrame) -> Dict[str, Any]:
    labels = dataset["target"].astype(str)
    pnl = pd.to_numeric(dataset.get("pnl_percent", 0), errors="coerce").fillna(0.0)
    profitable = labels.isin(PROFITABLE_LABELS)
    mismatches = ((profitable) & (pnl <= 0)) | ((~profitable) & (pnl > 0))
    return {
        "rows": int(len(dataset)),
        "unknown_labels": int((~labels.isin(TARGET_LABELS)).sum()),
        "label_pnl_mismatch_rows": int(mismatches.sum()),
        "label_pnl_mismatch_rate": float(mismatches.mean() if len(dataset) else 0.0),
    }


def _class_imbalance(dataset: pd.DataFrame) -> Dict[str, Any]:
    counts = dataset["target"].value_counts().to_dict()
    dominant = max(counts.values()) if counts else 0
    minority = min(counts.values()) if counts else 0
    ratio = float(dominant / minority) if minority else float("inf")
    return {
        "label_counts": {str(k): int(v) for k, v in counts.items()},
        "imbalance_ratio": ratio,
    }


def _train_global_binary_model(dataset: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    X, feature_names = _encode(dataset)
    y = _binary_target(dataset["target"])
    stratify = y if y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
        X,
        y,
        dataset.index,
        test_size=0.3 if len(dataset) >= 20 else 0.4,
        random_state=42,
        stratify=stratify,
    )
    model = RandomForestClassifier(n_estimators=250, max_depth=6, class_weight="balanced", random_state=42)
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    prob = model.predict_proba(X_test)[:, 1]

    result = dataset.loc[idx_test, ["target", "regime_name", "pnl_percent"]].copy()
    result["actual_profit"] = y_test.values
    result["pred_profit"] = pred
    result["pred_confidence"] = prob
    result["correct"] = (result["actual_profit"] == result["pred_profit"]).astype(int)
    result["false_positive"] = ((result["pred_profit"] == 1) & (result["actual_profit"] == 0)).astype(int)
    result["false_negative"] = ((result["pred_profit"] == 0) & (result["actual_profit"] == 1)).astype(int)

    importance = sorted(
        zip(feature_names, model.feature_importances_), key=lambda it: it[1], reverse=True
    )
    meta = {
        "global_accuracy": float(accuracy_score(y_test, pred)),
        "brier_score": float(brier_score_loss(y_test, prob)) if len(np.unique(y_test)) > 1 else 0.0,
        "feature_importance": [{"feature": f, "importance": float(v)} for f, v in importance],
    }
    return result, meta


def _regime_stats(pred_df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for regime, group in pred_df.groupby("regime_name"):
        out[str(regime)] = {
            "rows": int(len(group)),
            "accuracy": float(group["correct"].mean()),
            "false_positive_rate": float(group["false_positive"].mean()),
            "false_negative_rate": float(group["false_negative"].mean()),
        }
    return out


def _profit_weighted_accuracy(pred_df: pd.DataFrame) -> float:
    pnl = pd.to_numeric(pred_df["pnl_percent"], errors="coerce").fillna(0.0).abs()
    if float(pnl.sum()) == 0:
        return float(pred_df["correct"].mean())
    return float(((pred_df["correct"] * pnl).sum()) / pnl.sum())


def _calibration_table(pred_df: pd.DataFrame, bins: int = 5) -> List[Dict[str, Any]]:
    frame = pred_df.copy()
    frame["bin"] = pd.cut(frame["pred_confidence"], bins=bins, include_lowest=True)
    table: List[Dict[str, Any]] = []
    for bucket, group in frame.groupby("bin", observed=False):
        if len(group) == 0:
            continue
        table.append(
            {
                "bin": str(bucket),
                "rows": int(len(group)),
                "avg_confidence": float(group["pred_confidence"].mean()),
                "actual_profitable_rate": float(group["actual_profit"].mean()),
            }
        )
    return table


def _feature_usefulness_by_regime(dataset: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    for regime, subset in dataset.groupby("regime_name"):
        if len(subset) < 12 or subset["target"].nunique() < 2:
            continue
        X, feature_names = _encode(subset)
        y = _binary_target(subset["target"])
        if y.nunique() < 2:
            continue
        model = RandomForestClassifier(n_estimators=150, max_depth=5, class_weight="balanced", random_state=42)
        model.fit(X, y)
        pairs = sorted(zip(feature_names, model.feature_importances_), key=lambda it: it[1], reverse=True)[:8]
        result[str(regime)] = [{"feature": f, "importance": float(v)} for f, v in pairs]
    return result


def _diagnosis(audit: Dict[str, Any]) -> List[str]:
    findings: List[str] = []
    global_acc = float(audit.get("global_accuracy", 0.0))
    walkforward = audit.get("walkforward_score") or audit.get("walkforward") or {}
    rolling_acc = float(walkforward.get("average_accuracy", audit.get("rolling_accuracy", 0.0)))
    label_quality = audit.get("label_quality") or {}
    mismatch = float(label_quality.get("label_pnl_mismatch_rate", 0.0))
    class_imbalance = audit.get("class_imbalance") or {}
    imbalance = float(class_imbalance.get("imbalance_ratio", 0.0))
    profitability = audit.get("profitability_outcome") or audit.get("profitability") or {}
    profit_weighted_acc = profitability.get("profit_weighted_accuracy")

    missing_sections: List[str] = []
    for name in ("walkforward", "global_accuracy", "profitability"):
        if name == "walkforward" and not walkforward:
            missing_sections.append(name)
        if name == "global_accuracy" and "global_accuracy" not in audit:
            missing_sections.append(name)
        if name == "profitability" and not profitability:
            missing_sections.append(name)

    if missing_sections:
        findings.append(
            f"insufficient_data: missing sections -> {', '.join(missing_sections)}."
        )

    if global_acc < 0.45 and rolling_acc >= 0.6:
        findings.append("Metric mismatch: global holdout underperforms while walk-forward remains robust.")
    if mismatch > 0.15:
        findings.append("Label mismatch risk is high: many rows disagree between label and realized PnL sign.")
    if imbalance >= 2.0:
        findings.append("Class imbalance is material and may bias signal selection.")
    if isinstance(profit_weighted_acc, (int, float)) and float(profit_weighted_acc) < 0.5:
        findings.append("Profitability signal is weak: profit-weighted accuracy is below 50%.")

    regime_stats = audit.get("regime_stats") or {}
    if regime_stats:
        worst = min(regime_stats.items(), key=lambda it: it[1]["accuracy"])
        best = max(regime_stats.items(), key=lambda it: it[1]["accuracy"])
        if worst[1]["accuracy"] + 0.15 < best[1]["accuracy"]:
            findings.append(
                f"Regime mismatch present: weakest regime is {worst[0]} ({worst[1]['accuracy']:.2%}) vs best {best[0]} ({best[1]['accuracy']:.2%})."
            )

    if not findings:
        findings.append("No single root cause dominates; monitor calibration and profitability-weighted accuracy for promotion.")
    return findings


def _safe_scoring_improvement_plan(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    class_imbalance = audit.get("class_imbalance") or {}
    imbalance = float(class_imbalance.get("imbalance_ratio", 0.0))
    calibration = audit.get("confidence_calibration") or {}
    brier = float(calibration.get("brier_score", 1.0))
    plan: List[Dict[str, Any]] = [
        {
            "priority": "high",
            "category": "class_imbalance",
            "action": "Enable inverse-frequency sample weighting in model scoring pipeline.",
            "safety": "Read-only feature/scoring change only; does not affect broker, DB schema, or execution routing.",
            "acceptance_criteria": "Minority-class recall improves by >= 3pp while profit-weighted accuracy does not decline.",
        },
        {
            "priority": "high",
            "category": "confidence_calibration",
            "action": "Apply post-hoc probability calibration (isotonic or Platt) on out-of-fold predictions.",
            "safety": "Calibration layer wraps model probabilities only and preserves PAPER_ONLY behavior.",
            "acceptance_criteria": "Brier score improves by >= 0.01 and confidence-vs-hit-rate gap narrows across bins.",
        },
        {
            "priority": "medium",
            "category": "decision_threshold",
            "action": "Retune profit decision threshold using walk-forward folds for maximum profit-weighted utility.",
            "safety": "Scoring policy update only; no order execution path changes.",
            "acceptance_criteria": "Profit-weighted accuracy improves and false-positive rate does not materially increase.",
        },
        {
            "priority": "medium",
            "category": "monitoring",
            "action": "Track per-regime calibration error and per-class precision/recall in each audit run.",
            "safety": "Telemetry-only extension, read-only.",
            "acceptance_criteria": "Alerts when any regime calibration error exceeds configured tolerance.",
        },
    ]
    if imbalance < 2.0:
        plan = [item for item in plan if item["category"] != "class_imbalance"]
    if brier <= 0.20:
        plan = [item for item in plan if item["category"] != "confidence_calibration"]
    return plan


def run_audit(output_path: str = "ml_quality_audit.json") -> Dict[str, Any]:
    dataset = build_ml_dataset("paper_trades.csv", "signals_log.csv", "flow_log.csv", database_path="mamuyy_hunter.db")
    if len(dataset) < 8 or dataset["target"].nunique() < 2:
        audit = {
            "rows": int(len(dataset)),
            "status": "insufficient_data",
            "message": "Not enough labeled rows for full ML quality audit.",
            "requirements": {"min_rows": 8, "min_classes": 2},
            "model_promotion_rule_v2": [
                "Keep PAPER_ONLY and continue data collection until minimum sample threshold is reached.",
                "Require walk-forward average_accuracy >= 0.60 for >= 5 folds before promotion.",
                "Require profitability-weighted accuracy >= 0.55 and average_winrate >= 42%.",
            ],
        }
        Path(output_path).write_text(json.dumps(audit, indent=2), encoding="utf-8")
        return audit

    pred_df, model_meta = _train_global_binary_model(dataset)
    walk = run_walkforward_validation(database_path="mamuyy_hunter.db")
    audit = {
        "rows": int(len(dataset)),
        "global_accuracy": model_meta["global_accuracy"],
        "rolling_accuracy": float(walk.get("average_accuracy", 0.0)),
        "walkforward_score": {
            "folds": int(walk.get("folds", 0)),
            "average_accuracy": float(walk.get("average_accuracy", 0.0)),
            "average_winrate": float(walk.get("average_winrate", 0.0)),
            "model_health": walk.get("model_health", "UNKNOWN"),
            "overfit_risk_score": float(walk.get("overfit_risk_score", 0.0)),
        },
        "profitability_outcome": {
            "profit_weighted_accuracy": _profit_weighted_accuracy(pred_df),
            "test_profitability_rate": float(pred_df["actual_profit"].mean()),
            "predicted_profitability_rate": float(pred_df["pred_profit"].mean()),
        },
        "label_quality": _label_quality(dataset),
        "class_imbalance": _class_imbalance(dataset),
        "regime_stats": _regime_stats(pred_df),
        "confidence_calibration": {
            "brier_score": model_meta["brier_score"],
            "calibration_table": _calibration_table(pred_df),
        },
        "feature_usefulness_by_regime": _feature_usefulness_by_regime(dataset),
        "global_feature_importance": model_meta["feature_importance"][:15],
    }
    audit["diagnosis"] = _diagnosis(audit)
    audit["safe_scoring_improvement_plan"] = _safe_scoring_improvement_plan(audit)
    audit["model_promotion_rule_v2"] = [
        "Promote only if walk-forward average_accuracy >= 0.60 for >= 5 folds.",
        "Require profitability-weighted accuracy >= 0.55 and average_winrate >= 42%.",
        "Block promotion if any active regime accuracy < 0.40 with >= 20 samples.",
        "Block promotion if Brier score > 0.24 (poor confidence calibration).",
        "Block promotion if label_pnl_mismatch_rate > 0.12 until labeling logic is repaired.",
    ]

    Path(output_path).write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def _print_report(audit: Dict[str, Any]) -> None:
    print("=== ML INTELLIGENCE QUALITY AUDIT ===")
    print(f"Rows: {audit['rows']}")
    if audit.get("status") == "insufficient_data":
        print(f"Status: {audit['status']}")
        print(audit["message"])
        print("\nModel Promotion Rule V2:")
        for item in audit["model_promotion_rule_v2"]:
            print(f"- {item}")
        return
    print(f"Global Accuracy (holdout): {audit['global_accuracy']:.2%}")
    print(f"Rolling Accuracy (walk-forward): {audit['rolling_accuracy']:.2%}")
    print(f"Model Health: {audit['walkforward_score']['model_health']}")
    print(f"Overfit Risk: {audit['walkforward_score']['overfit_risk_score']:.2f}")
    print(f"Profit-Weighted Accuracy: {audit['profitability_outcome']['profit_weighted_accuracy']:.2%}")
    print("\nTop Diagnosis:")
    for item in audit["diagnosis"]:
        print(f"- {item}")
    print("\nSafe Scoring Improvement Plan:")
    for item in audit.get("safe_scoring_improvement_plan", []):
        print(f"- [{item['priority']}] {item['category']}: {item['action']}")
    print("\nModel Promotion Rule V2:")
    for item in audit["model_promotion_rule_v2"]:
        print(f"- {item}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ML intelligence quality audit")
    parser.add_argument("--output", default="ml_quality_audit.json", help="Path to JSON audit output")
    args = parser.parse_args()
    audit = run_audit(output_path=args.output)
    _print_report(audit)


if __name__ == "__main__":
    main()
