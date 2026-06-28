"""
CP-040B0: Source Boundary & Drift Audit
READ-ONLY - no database, runtime, execution engine, portfolio, or model promotion changes.
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

REPORT_JSON = "reports/cp040b0_source_boundary_drift.json"
REPORT_FOLDS_CSV = "reports/cp040b0_source_boundary_drift_folds.csv"
REPORT_BOUNDARY_ROWS_CSV = "reports/cp040b0_source_boundary_rows.csv"

TRAIN_WINDOW = 500
TEST_WINDOW = 100
LOSS_RATE_FAIL_DELTA = 0.20
CROSS_SOURCE_FAIL_DELTA = -0.05
BOUNDARY_FAILURE_DELTA = -0.05


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
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def _dist(series: pd.Series) -> Dict[str, int]:
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}


def _ts_min_max(frame: pd.DataFrame) -> Dict[str, Optional[str]]:
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True) if not frame.empty else pd.Series(dtype="datetime64[ns, UTC]")
    valid = timestamps.dropna()
    return {
        "timestamp_min": valid.min().isoformat() if not valid.empty else None,
        "timestamp_max": valid.max().isoformat() if not valid.empty else None,
    }


def _majority_baseline_accuracy(y: pd.Series) -> Optional[float]:
    if y.empty:
        return None
    return float(y.value_counts(normalize=True).max())


def _fit_predict_accuracy(train: pd.DataFrame, test: pd.DataFrame) -> Optional[float]:
    if train.empty or test.empty or train["target_binary"].nunique() < 2:
        return None
    try:
        pre = fit_train_only_preprocessor(train)
        x_train = transform_with_train_preprocessor(train, pre)
        x_test = transform_with_train_preprocessor(test, pre)
        clf = RandomForestClassifier(
            n_estimators=150,
            max_depth=5,
            class_weight="balanced",
            random_state=42,
        )
        clf.fit(x_train, train["target_binary"])
        return float(accuracy_score(test["target_binary"], clf.predict(x_test)))
    except Exception as exc:  # reportable audit failure, not runtime mutation
        print(f"    model error: {exc}")
        return None


def _source_summary(frame: pd.DataFrame) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for source, group in frame.groupby("source_artifact", dropna=False):
        binary_dist = _dist(group["target_binary"])
        rows = int(len(group))
        loss_count = binary_dist.get("LOSS", 0)
        win_count = rows - loss_count
        result[str(source)] = {
            "rows": rows,
            "original_label_distribution": _dist(group["target"]),
            "binary_label_distribution": binary_dist,
            "loss_rate": round(loss_count / rows, 6) if rows else None,
            "win_non_loss_rate": round(win_count / rows, 6) if rows else None,
            **_ts_min_max(group),
        }
    return result


def _find_source_transitions(frame: pd.DataFrame) -> List[int]:
    sources = frame["source_artifact"].astype(str).tolist()
    return [idx for idx in range(1, len(sources)) if sources[idx] != sources[idx - 1]]


def _boundary_rows(frame: pd.DataFrame, transition_idx: Optional[int]) -> pd.DataFrame:
    columns = ["index", "timestamp", "source_artifact", "target", "target_binary", "symbol", "score", "pnl_percent"]
    if transition_idx is None:
        return pd.DataFrame(columns=columns)
    start = max(0, transition_idx - 20)
    end = min(len(frame), transition_idx + 21)
    rows = frame.iloc[start:end].copy()
    rows.insert(0, "index", rows.index)
    keep = [col for col in columns if col in rows.columns]
    return rows[keep]


def _fold_source_drift(frame: pd.DataFrame) -> Tuple[List[Dict[str, Any]], List[int]]:
    folds: List[Dict[str, Any]] = []
    failing_boundary_folds: List[int] = []
    start = 0
    fold_id = 1
    while start + TRAIN_WINDOW + TEST_WINDOW <= len(frame):
        train_start = start
        train_end = start + TRAIN_WINDOW - 1
        test_start = start + TRAIN_WINDOW
        test_end = start + TRAIN_WINDOW + TEST_WINDOW - 1
        train = frame.iloc[train_start : train_end + 1].copy()
        test = frame.iloc[test_start : test_end + 1].copy()

        baseline = _majority_baseline_accuracy(test["target_binary"])
        model_acc = _fit_predict_accuracy(train, test)
        delta = model_acc - baseline if model_acc is not None and baseline is not None else None
        combined_sources = pd.concat([train["source_artifact"], test["source_artifact"]]).astype(str).tolist()
        boundary_crossed = any(combined_sources[idx] != combined_sources[idx - 1] for idx in range(1, len(combined_sources)))
        if boundary_crossed and delta is not None and delta <= BOUNDARY_FAILURE_DELTA:
            failing_boundary_folds.append(fold_id)

        folds.append(
            {
                "fold_id": fold_id,
                "train_index_start": train_start,
                "train_index_end": train_end,
                "test_index_start": test_start,
                "test_index_end": test_end,
                "train_source_distribution": _dist(train["source_artifact"]),
                "test_source_distribution": _dist(test["source_artifact"]),
                "train_original_target_distribution": _dist(train["target"]),
                "test_original_target_distribution": _dist(test["target"]),
                "train_binary_target_distribution": _dist(train["target_binary"]),
                "test_binary_target_distribution": _dist(test["target_binary"]),
                "majority_baseline_accuracy_binary_test": round(baseline, 6) if baseline is not None else None,
                "recomputed_binary_model_accuracy": round(model_acc, 6) if model_acc is not None else None,
                "model_vs_baseline_delta": round(delta, 6) if delta is not None else None,
                "train_timestamp_min": _ts_min_max(train)["timestamp_min"],
                "train_timestamp_max": _ts_min_max(train)["timestamp_max"],
                "test_timestamp_min": _ts_min_max(test)["timestamp_min"],
                "test_timestamp_max": _ts_min_max(test)["timestamp_max"],
                "boundary_crossed": bool(boundary_crossed),
            }
        )
        fold_id += 1
        start += TEST_WINDOW
    return folds, failing_boundary_folds


def _walkforward_for_source(frame: pd.DataFrame, source: str) -> Dict[str, Any]:
    subset = frame[frame["source_artifact"] == source].copy()
    result: Dict[str, Any] = {"source": source, "rows": int(len(subset)), "status": "SKIPPED"}
    if len(subset) < TRAIN_WINDOW + TEST_WINDOW or subset["target_binary"].nunique() < 2:
        result["reason"] = "insufficient_rows_or_classes"
        return result
    accs = []
    baselines = []
    start = 0
    while start + TRAIN_WINDOW + TEST_WINDOW <= len(subset):
        train = subset.iloc[start : start + TRAIN_WINDOW].copy()
        test = subset.iloc[start + TRAIN_WINDOW : start + TRAIN_WINDOW + TEST_WINDOW].copy()
        acc = _fit_predict_accuracy(train, test)
        base = _majority_baseline_accuracy(test["target_binary"])
        if acc is not None:
            accs.append(acc)
        if base is not None:
            baselines.append(base)
        start += TEST_WINDOW
    result.update({
        "status": "OK" if accs else "SKIPPED",
        "folds": len(accs),
        "average_model_accuracy": round(float(np.mean(accs)), 6) if accs else None,
        "average_majority_baseline": round(float(np.mean(baselines)), 6) if baselines else None,
    })
    return result


def _cross_source(frame: pd.DataFrame, train_source: str, test_source: str) -> Dict[str, Any]:
    train = frame[frame["source_artifact"] == train_source].copy()
    test = frame[frame["source_artifact"] == test_source].copy()
    result: Dict[str, Any] = {"train_source": train_source, "test_source": test_source, "train_rows": int(len(train)), "test_rows": int(len(test)), "status": "SKIPPED"}
    if train.empty or test.empty or train["target_binary"].nunique() < 2 or test["target_binary"].nunique() < 2:
        result["reason"] = "insufficient_rows_or_classes"
        return result
    baseline = _majority_baseline_accuracy(test["target_binary"])
    acc = _fit_predict_accuracy(train, test)
    delta = acc - baseline if acc is not None and baseline is not None else None
    result.update({
        "status": "OK" if acc is not None else "SKIPPED",
        "majority_baseline_accuracy": round(baseline, 6) if baseline is not None else None,
        "model_accuracy": round(acc, 6) if acc is not None else None,
        "model_vs_baseline_delta": round(delta, 6) if delta is not None else None,
    })
    return result


def run() -> Dict[str, Any]:
    print("=== CP-040B0 Source Boundary & Drift Audit ===")
    ds = _production_universe_dataset()
    for column in ["timestamp", "source_artifact", "target", "symbol", "score", "pnl_percent"]:
        if column not in ds.columns:
            ds[column] = pd.Series(dtype="object")
    ds = ds.sort_values("timestamp").reset_index(drop=True)
    ds["target_binary"] = ds["target"].replace({"TP1 HIT": "WIN"})

    overview = {
        "total_rows": int(len(ds)),
        "source_artifact_distribution": _dist(ds["source_artifact"]),
        "original_target_distribution": _dist(ds["target"]),
        "binary_target_distribution": _dist(ds["target_binary"]),
        **_ts_min_max(ds),
    }
    source_level = _source_summary(ds)
    transitions = _find_source_transitions(ds)
    first_transition = transitions[0] if transitions else None
    boundary = {
        "source_transition_count": int(len(transitions)),
        "first_transition_index": first_transition,
        "source_before": str(ds.loc[first_transition - 1, "source_artifact"]) if first_transition is not None else None,
        "source_after": str(ds.loc[first_transition, "source_artifact"]) if first_transition is not None else None,
        "timestamp_before": ds.loc[first_transition - 1, "timestamp"].isoformat() if first_transition is not None else None,
        "timestamp_after": ds.loc[first_transition, "timestamp"].isoformat() if first_transition is not None else None,
    }
    boundary_rows = _boundary_rows(ds, first_transition)
    folds, failing_boundary_folds = _fold_source_drift(ds)

    historical = "historical_outcomes"
    ipt = "internal_paper_trades"
    sanity = {
        "historical_train_ipt_test": _cross_source(ds, historical, ipt),
        "ipt_train_historical_test": _cross_source(ds, ipt, historical),
        "historical_internal_walkforward": _walkforward_for_source(ds, historical),
        "ipt_internal_walkforward": _walkforward_for_source(ds, ipt),
    }

    loss_rates = {source: details["loss_rate"] for source, details in source_level.items()}
    hist_loss = loss_rates.get(historical)
    ipt_loss = loss_rates.get(ipt)
    loss_rate_delta = abs(hist_loss - ipt_loss) if hist_loss is not None and ipt_loss is not None else None
    cross_source_failures = [
        item for item in [sanity["historical_train_ipt_test"], sanity["ipt_train_historical_test"]]
        if item.get("model_vs_baseline_delta") is not None and item["model_vs_baseline_delta"] < CROSS_SOURCE_FAIL_DELTA
    ]
    major_drift = loss_rate_delta is not None and loss_rate_delta > LOSS_RATE_FAIL_DELTA
    boundary_aligned_failures = bool(failing_boundary_folds)
    if len(ds) == 0:
        verdict = "FAIL"
    elif major_drift or cross_source_failures or boundary_aligned_failures:
        verdict = "FAIL"
    elif loss_rate_delta is not None and loss_rate_delta > 0 or any(f.get("boundary_crossed") for f in folds):
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    report = {
        "cp_id": "CP-040B0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sandbox": True,
        "read_only": True,
        "dataset_overview": overview,
        "source_level_label_distribution": source_level,
        "source_boundary_audit": boundary,
        "fold_source_drift": folds,
        "per_source_binary_model_sanity": sanity,
        "verdict": verdict,
        "verdict_logic_evidence": {
            "dataset_empty": bool(len(ds) == 0),
            "historical_vs_ipt_loss_rate_abs_delta": round(loss_rate_delta, 6) if loss_rate_delta is not None else None,
            "loss_rate_fail_threshold_abs_delta": LOSS_RATE_FAIL_DELTA,
            "cross_source_failures": cross_source_failures,
            "failing_boundary_folds": failing_boundary_folds,
        },
        "notes": [
            "Uses _production_universe_dataset() from ml_engine.py.",
            "TP1 HIT is mapped to WIN only in target_binary for audit views.",
            "No database writes, runtime changes, execution engine changes, portfolio changes, or model promotion are performed.",
        ],
    }

    os.makedirs("reports", exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(_jsonify(report), handle, indent=2)
    pd.DataFrame(folds).to_csv(REPORT_FOLDS_CSV, index=False)
    boundary_rows.to_csv(REPORT_BOUNDARY_ROWS_CSV, index=False)

    print(f"Dataset rows: {overview['total_rows']}")
    print(f"Source distributions: {overview['source_artifact_distribution']}")
    print("Source LOSS/WIN rates:")
    for source, details in source_level.items():
        print(f"  {source}: LOSS={details['loss_rate']:.3f} WIN/non-loss={details['win_non_loss_rate']:.3f} rows={details['rows']}")
    print(f"First source boundary index: {first_transition}")
    if first_transition is not None:
        print(f"  {boundary['source_before']} @ {boundary['timestamp_before']} -> {boundary['source_after']} @ {boundary['timestamp_after']}")
    print("Per-fold source mix and baseline/model delta:")
    for fold in folds:
        print(
            f"  Fold {fold['fold_id']}: train_src={fold['train_source_distribution']} "
            f"test_src={fold['test_source_distribution']} baseline={fold['majority_baseline_accuracy_binary_test']} "
            f"model={fold['recomputed_binary_model_accuracy']} delta={fold['model_vs_baseline_delta']} "
            f"boundary_crossed={fold['boundary_crossed']}"
        )
    print("Per-source binary model sanity:")
    for name, item in sanity.items():
        print(f"  {name}: {item}")
    print(f"VERDICT: {verdict}")
    print(f"Report: {REPORT_JSON}")
    print(f"Fold CSV: {REPORT_FOLDS_CSV}")
    print(f"Boundary rows CSV: {REPORT_BOUNDARY_ROWS_CSV}")
    return report


if __name__ == "__main__":
    run()
