"""
CP-040B1: Source-Aware Window Sensitivity Audit
READ-ONLY - no database, runtime, execution engine, portfolio, ml engine, dataset builder,
or model promotion changes.
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score

sys.path.insert(0, ".")
from ml_engine import (  # noqa: E402
    _production_universe_dataset,
    fit_train_only_preprocessor,
    transform_with_train_preprocessor,
)

REPORT_JSON = "reports/cp040b1_source_aware_window_sensitivity.json"
REPORT_FOLDS_CSV = "reports/cp040b1_source_aware_window_sensitivity_folds.csv"
REPORT_SUMMARY_CSV = "reports/cp040b1_source_aware_window_sensitivity_summary.csv"

HISTORICAL = "historical_outcomes"
IPT = "internal_paper_trades"

SCOPE_WINDOWS = {
    "mixed_additive": [(500, 100), (400, 80), (300, 70), (250, 50), (200, 50)],
    "historical_only": [(300, 70), (250, 50), (200, 50), (150, 50)],
    "ipt_only": [(300, 70), (250, 50), (200, 50), (150, 50)],
}
EXPANDING_WINDOWS = {
    "mixed_additive": [(300, 100), (250, 75), (200, 50)],
    "historical_only": [(250, 75), (200, 50), (150, 50)],
    "ipt_only": [(250, 75), (200, 50), (150, 50)],
}


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonify(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def _dist(series: pd.Series) -> Dict[str, int]:
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}


def _ts_min_max(frame: pd.DataFrame, column: str = "timestamp") -> Dict[str, Optional[str]]:
    if frame.empty or column not in frame.columns:
        return {"timestamp_min": None, "timestamp_max": None}
    timestamps = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
    return {
        "timestamp_min": timestamps.min().isoformat() if not timestamps.empty else None,
        "timestamp_max": timestamps.max().isoformat() if not timestamps.empty else None,
    }


def _majority_label_and_accuracy(y: pd.Series) -> Tuple[Optional[str], Optional[float]]:
    if y.empty:
        return None, None
    counts = y.value_counts(dropna=False)
    label = str(counts.index[0])
    return label, float(counts.iloc[0] / len(y))


def _source_transitions(frame: pd.DataFrame) -> List[int]:
    if "source_artifact" not in frame.columns:
        return []
    sources = frame["source_artifact"].astype(str).tolist()
    return [idx for idx in range(1, len(sources)) if sources[idx] != sources[idx - 1]]


def _boundary_crossed(train_start: int, test_end_exclusive: int, transitions: List[int]) -> bool:
    return any(train_start < boundary < test_end_exclusive for boundary in transitions)


def _empty_fold(scope: str, strategy: str, fold_id: int, train_start: int, train_end: int, test_start: int, test_end: int, train: pd.DataFrame, test: pd.DataFrame, boundary_crossed: bool, status: str, reason: str) -> Dict[str, Any]:
    return {
        "scope": scope,
        "strategy": strategy,
        "fold_id": fold_id,
        "train_start_idx": train_start,
        "train_end_idx": train_end,
        "test_start_idx": test_start,
        "test_end_idx": test_end,
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_timestamp_min": _ts_min_max(train)["timestamp_min"],
        "train_timestamp_max": _ts_min_max(train)["timestamp_max"],
        "test_timestamp_min": _ts_min_max(test)["timestamp_min"],
        "test_timestamp_max": _ts_min_max(test)["timestamp_max"],
        "train_source_distribution": _dist(train.get("source_artifact", pd.Series(dtype=object))),
        "test_source_distribution": _dist(test.get("source_artifact", pd.Series(dtype=object))),
        "train_binary_distribution": _dist(train.get("target_binary", pd.Series(dtype=object))),
        "test_binary_distribution": _dist(test.get("target_binary", pd.Series(dtype=object))),
        "majority_label": None,
        "majority_baseline_accuracy": None,
        "model_accuracy": None,
        "model_vs_baseline_delta": None,
        "predicted_distribution": {},
        "boundary_crossed": bool(boundary_crossed),
        "status": status,
        "skipped_reason": reason,
    }


def _evaluate_fold(scope: str, strategy: str, fold_id: int, train_start: int, train_end_exclusive: int, test_start: int, test_end_exclusive: int, train: pd.DataFrame, test: pd.DataFrame, transitions: List[int]) -> Dict[str, Any]:
    crossed = _boundary_crossed(train_start, test_end_exclusive, transitions) if scope == "mixed_additive" else False
    train_end = train_end_exclusive - 1 if train_end_exclusive > train_start else None
    test_end = test_end_exclusive - 1 if test_end_exclusive > test_start else None
    if test.empty:
        return _empty_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, crossed, "SKIPPED", "empty_test")
    if train["target_binary"].nunique() < 2:
        return _empty_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, crossed, "SKIPPED", "train_less_than_two_target_binary_classes")
    majority_label, baseline = _majority_label_and_accuracy(test["target_binary"])
    try:
        pre = fit_train_only_preprocessor(train)
        x_train = transform_with_train_preprocessor(train, pre)
        x_test = transform_with_train_preprocessor(test, pre)
        if x_train.empty or x_test.empty or len(x_train.columns) == 0:
            return _empty_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, crossed, "SKIPPED", "empty_transformed_features")
        clf = RandomForestClassifier(n_estimators=150, max_depth=5, class_weight="balanced", random_state=42)
        clf.fit(x_train, train["target_binary"])
        pred = clf.predict(x_test)
        acc = float(accuracy_score(test["target_binary"], pred))
        delta = acc - baseline if baseline is not None else None
        predicted_distribution = _dist(pd.Series(pred))
    except Exception as exc:
        return _empty_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, crossed, "SKIPPED", f"model_error: {exc}")
    row = _empty_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, crossed, "OK", "")
    row.update({
        "majority_label": majority_label,
        "majority_baseline_accuracy": round(baseline, 6) if baseline is not None else None,
        "model_accuracy": round(acc, 6),
        "model_vs_baseline_delta": round(delta, 6) if delta is not None else None,
        "predicted_distribution": predicted_distribution,
    })
    return row


def _summarize(scope: str, strategy: str, train_window: Optional[int], test_window: Optional[int], frame: pd.DataFrame, folds: List[Dict[str, Any]], skipped_reason: str = "") -> Dict[str, Any]:
    valid = [f for f in folds if f.get("status") == "OK" and f.get("model_accuracy") is not None]
    weights = np.array([f["test_rows"] for f in valid], dtype=float)
    accs = np.array([f["model_accuracy"] for f in valid], dtype=float)
    bases = np.array([f["majority_baseline_accuracy"] for f in valid], dtype=float)
    deltas = accs - bases if len(valid) else np.array([])
    beating = [f for f in valid if f.get("model_vs_baseline_delta") is not None and f["model_vs_baseline_delta"] > 0]
    return {
        "scope": scope,
        "strategy": strategy,
        "train_window": train_window,
        "test_window": test_window,
        "fold_count": int(len(folds)),
        "rows": int(len(frame)),
        "source_distribution": _dist(frame.get("source_artifact", pd.Series(dtype=object))),
        "original_target_distribution": _dist(frame.get("target", pd.Series(dtype=object))),
        "binary_target_distribution": _dist(frame.get("target_binary", pd.Series(dtype=object))),
        "avg_model_accuracy": round(float(np.mean(accs)), 6) if len(valid) else None,
        "weighted_model_accuracy": round(float(np.average(accs, weights=weights)), 6) if len(valid) else None,
        "avg_majority_baseline_accuracy": round(float(np.mean(bases)), 6) if len(valid) else None,
        "weighted_majority_baseline_accuracy": round(float(np.average(bases, weights=weights)), 6) if len(valid) else None,
        "avg_model_vs_baseline_delta": round(float(np.mean(deltas)), 6) if len(valid) else None,
        "weighted_model_vs_baseline_delta": round(float(np.average(deltas, weights=weights)), 6) if len(valid) else None,
        "folds_beating_baseline": int(len(beating)),
        "folds_beating_baseline_rate": round(len(beating) / len(valid), 6) if valid else None,
        "best_fold_accuracy": round(float(np.max(accs)), 6) if len(valid) else None,
        "worst_fold_accuracy": round(float(np.min(accs)), 6) if len(valid) else None,
        "valid_fold_count": int(len(valid)),
        "skipped_reason": skipped_reason,
        **_ts_min_max(frame),
    }


def _walkforward(frame: pd.DataFrame, scope: str, train_window: int, test_window: int, expanding: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    transitions = _source_transitions(frame)
    strategy = f"expanding_{train_window}_{test_window}" if expanding else f"{train_window}/{test_window}"
    folds = []
    if len(frame) < train_window + test_window:
        return _summarize(scope, strategy, train_window, test_window, frame, folds, "insufficient_rows"), folds
    start = 0
    fold_id = 1
    while start + train_window + test_window <= len(frame):
        train_start = 0 if expanding else start
        train_end = start + train_window
        test_start = train_end
        test_end = test_start + test_window
        train = frame.iloc[train_start:train_end].copy()
        test = frame.iloc[test_start:test_end].copy()
        folds.append(_evaluate_fold(scope, strategy, fold_id, train_start, train_end, test_start, test_end, train, test, transitions))
        start += test_window
        fold_id += 1
    return _summarize(scope, strategy, train_window, test_window, frame, folds), folds


def _cross_source_eval(frame: pd.DataFrame, strategy: str, train: pd.DataFrame, test: pd.DataFrame) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    combined = pd.concat([train, test], ignore_index=True)
    fold = _evaluate_fold("cross_source", strategy, 1, 0, len(train), len(train), len(train) + len(test), train.copy(), test.copy(), [])
    return _summarize("cross_source", strategy, int(len(train)), int(len(test)), combined, [fold], fold.get("skipped_reason", "") if fold.get("status") != "OK" else ""), [fold]


def _rank_summaries(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        summaries,
        key=lambda r: (
            r.get("weighted_model_vs_baseline_delta") if r.get("weighted_model_vs_baseline_delta") is not None else -999,
            r.get("folds_beating_baseline_rate") if r.get("folds_beating_baseline_rate") is not None else -999,
            r.get("weighted_model_accuracy") if r.get("weighted_model_accuracy") is not None else -999,
            r.get("valid_fold_count") or 0,
        ),
        reverse=True,
    )


def _verdict(ranked: List[Dict[str, Any]], cross_summaries: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    best = next((r for r in ranked if r.get("valid_fold_count", 0) > 0 and r["scope"] != "cross_source"), None)
    cross_bad = [r for r in cross_summaries if r.get("weighted_model_vs_baseline_delta") is not None and r["weighted_model_vs_baseline_delta"] < -0.05]
    source_aware = [r for r in ranked if r["scope"] in {"historical_only", "ipt_only"} and r.get("weighted_model_vs_baseline_delta") is not None and r["weighted_model_vs_baseline_delta"] >= 0.03]
    if best is None:
        verdict = "FAIL"
    elif not source_aware or best.get("weighted_model_accuracy", 0) < 0.55 or cross_bad:
        verdict = "FAIL"
    elif any(r.get("weighted_model_accuracy", 0) >= 0.60 and r.get("weighted_model_vs_baseline_delta", 0) >= 0.03 and r.get("folds_beating_baseline_rate", 0) >= 0.60 for r in source_aware) and not cross_bad:
        verdict = "PASS"
    else:
        verdict = "REVIEW"
    return verdict, {"best_strategy": best, "source_aware_delta_ge_0_03": source_aware, "cross_source_below_baseline_more_than_0_05": cross_bad}


def run() -> Dict[str, Any]:
    print("=== CP-040B1 Source-Aware Window Sensitivity Audit ===")
    ds = _production_universe_dataset()
    for column in ["timestamp", "source_artifact", "target"]:
        if column not in ds.columns:
            ds[column] = pd.Series(dtype="object")
    ds = ds.sort_values("timestamp").reset_index(drop=True)
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    print(f"Dataset rows: {len(ds)}")
    print(f"Source distribution: {_dist(ds['source_artifact'])}")
    print(f"Original target distribution: {_dist(ds['target'])}")
    print(f"Binary target distribution: {_dist(ds['target_binary'])}")

    summaries: List[Dict[str, Any]] = []
    folds: List[Dict[str, Any]] = []
    scope_frames = {
        "mixed_additive": ds,
        "historical_only": ds[ds["source_artifact"] == HISTORICAL].reset_index(drop=True),
        "ipt_only": ds[ds["source_artifact"] == IPT].reset_index(drop=True),
    }
    for scope, frame in scope_frames.items():
        for train_window, test_window in SCOPE_WINDOWS[scope]:
            summary, fold_rows = _walkforward(frame, scope, train_window, test_window)
            summaries.append(summary); folds.extend(fold_rows)
        for train_window, test_window in EXPANDING_WINDOWS[scope]:
            summary, fold_rows = _walkforward(frame, scope, train_window, test_window, expanding=True)
            summaries.append(summary); folds.extend(fold_rows)

    hist = scope_frames["historical_only"]
    ipt = scope_frames["ipt_only"]
    cross_specs = [
        ("train_historical_test_ipt", hist, ipt),
        ("train_ipt_test_historical", ipt, hist),
        ("historical_first70_last30", hist.iloc[: int(len(hist) * 0.70)], hist.iloc[int(len(hist) * 0.70):]),
        ("ipt_first70_last30", ipt.iloc[: int(len(ipt) * 0.70)], ipt.iloc[int(len(ipt) * 0.70):]),
    ]
    cross_summaries = []
    for strategy, train, test in cross_specs:
        summary, fold_rows = _cross_source_eval(ds, strategy, train.reset_index(drop=True), test.reset_index(drop=True))
        summaries.append(summary); cross_summaries.append(summary); folds.extend(fold_rows)

    ranked = _rank_summaries(summaries)
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    verdict, verdict_evidence = _verdict(ranked, cross_summaries)

    report = {
        "cp_id": "CP-040B1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sandbox": True,
        "read_only": True,
        "paper_only_governance_enforced": True,
        "dataset_overview": {"rows": int(len(ds)), "source_distribution": _dist(ds["source_artifact"]), "original_target_distribution": _dist(ds["target"]), "binary_target_distribution": _dist(ds["target_binary"]), **_ts_min_max(ds)},
        "strategy_rankings": ranked,
        "folds": folds,
        "verdict": verdict,
        "phase3_status": "LOCKED",
        "verdict_evidence": verdict_evidence,
        "notes": [
            "Uses _production_universe_dataset() from ml_engine.py.",
            "TP1 HIT is mapped to WIN only in derived target_binary.",
            "RandomForest settings match CP-040A.",
            "No DB writes, runtime/execution/portfolio/ml_engine/dataset-builder changes, or model promotion are performed.",
        ],
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(_jsonify(report), handle, indent=2)
    pd.DataFrame(folds).to_csv(REPORT_FOLDS_CSV, index=False)
    pd.DataFrame(ranked).to_csv(REPORT_SUMMARY_CSV, index=False)

    print("Top 10 ranked strategies:")
    for row in ranked[:10]:
        print(f"  {row['rank']}. {row['scope']} {row['strategy']} acc={row['weighted_model_accuracy']} base={row['weighted_majority_baseline_accuracy']} delta={row['weighted_model_vs_baseline_delta']} valid={row['valid_fold_count']}")
    for scope in ["mixed_additive", "historical_only", "ipt_only"]:
        best = next((r for r in ranked if r["scope"] == scope), None)
        print(f"Best {scope} strategy: {best}")
    print("Cross_source results:")
    for row in cross_summaries:
        print(f"  {row['strategy']}: acc={row['weighted_model_accuracy']} base={row['weighted_majority_baseline_accuracy']} delta={row['weighted_model_vs_baseline_delta']} status_valid={row['valid_fold_count']}")
    print(f"FINAL VERDICT: {verdict}")
    print("Phase 3 remains LOCKED: YES")
    print(f"Report: {REPORT_JSON}")
    print(f"Folds CSV: {REPORT_FOLDS_CSV}")
    print(f"Summary CSV: {REPORT_SUMMARY_CSV}")
    return report


if __name__ == "__main__":
    run()
