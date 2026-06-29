"""
CP-044: Score95 Shadow Monitoring Audit

READ-ONLY report generator. This script does not change runtime behavior,
execution behavior, Telegram behavior, candidate queue behavior, dashboards,
model registries, model weights, databases, or Phase 3 state.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from ml_engine import build_ml_dataset  # noqa: E402

CP_ID = "CP-044"
REPORT_JSON = "reports/cp044_score95_shadow_monitoring.json"
REPORT_MD = "reports/cp044_score95_shadow_monitoring.md"
REPORT_WINDOWS_CSV = "reports/cp044_score95_shadow_monitoring_windows.csv"
REPORT_ROWS_CSV = "reports/cp044_score95_shadow_monitoring_rows.csv"

EVIDENCE_CP041 = "reports/cp041_ranking_ev_lifecycle_pivot.json"
EVIDENCE_CP042 = "reports/cp042_score95_forward_validation.json"
EVIDENCE_CP043 = "reports/cp043_score95_paper_watchlist_policy.json"

THRESHOLDS = {
    "minimum_new_score95_rows": 30,
    "ideal_new_score95_rows": 50,
    "max_loss_rate": 0.35,
    "ideal_loss_rate": 0.30,
    "min_avg_profit": 0.0,
    "min_rolling_pass_rate": 0.70,
    "max_recent_consecutive_fails": 1,
    "max_top_symbol_concentration": 0.40,
    "max_top_regime_concentration": 0.60,
}

WIN_LABELS = {"WIN", "TP1 HIT"}
LOSS_LABELS = {"LOSS"}
IPT = "internal_paper_trades"
HISTORICAL = "historical_outcomes"


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
        return None if np.isnan(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat() if not pd.isna(value) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _dist(series: Optional[pd.Series]) -> Dict[str, int]:
    if series is None:
        return {}
    if series.empty:
        return {}
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).to_dict().items()}


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _find_nested_key(payload: Any, key: str) -> Optional[Any]:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_nested_key(value, key)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_nested_key(item, key)
            if found not in (None, ""):
                return found
    return None


def _timestamp_range(frame: pd.DataFrame, column: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not column or frame.empty or column not in frame.columns:
        return None, None
    timestamps = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
    if timestamps.empty:
        return None, None
    return timestamps.min().isoformat(), timestamps.max().isoformat()


def _pick_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _binary_target(value: Any) -> Optional[str]:
    label = str(value or "").strip().upper()
    if label in WIN_LABELS:
        return "WIN"
    if label in LOSS_LABELS:
        return "LOSS"
    return None


def _safe_rate(count: int, denominator: int) -> Optional[float]:
    return float(count / denominator) if denominator else None


def _profit_metrics(frame: pd.DataFrame, pnl_column: Optional[str]) -> Dict[str, Optional[float]]:
    keys = ["avg_profit", "median_profit", "total_profit", "min_profit", "max_profit"]
    if not pnl_column or frame.empty or pnl_column not in frame.columns:
        return {key: None for key in keys}
    profits = pd.to_numeric(frame[pnl_column], errors="coerce").dropna()
    if profits.empty:
        return {key: None for key in keys}
    return {
        "avg_profit": float(profits.mean()),
        "median_profit": float(profits.median()),
        "total_profit": float(profits.sum()),
        "min_profit": float(profits.min()),
        "max_profit": float(profits.max()),
    }


def _top_concentration(frame: pd.DataFrame, column: Optional[str]) -> Tuple[Optional[str], Optional[float]]:
    if not column or frame.empty or column not in frame.columns:
        return None, None
    counts = frame[column].astype(str).value_counts(dropna=False)
    if counts.empty:
        return None, None
    return str(counts.index[0]), float(counts.iloc[0] / len(frame))


def _cohort_metrics(frame: pd.DataFrame, pnl_column: Optional[str]) -> Dict[str, Any]:
    binary = frame.get("binary_target", pd.Series(dtype=object)).dropna()
    wins = int((binary == "WIN").sum())
    losses = int((binary == "LOSS").sum())
    denominator = wins + losses
    return {
        "rows": int(len(frame)),
        "win_rate": _safe_rate(wins, denominator),
        "loss_rate": _safe_rate(losses, denominator),
        **_profit_metrics(frame, pnl_column),
    }

def _window_status(score95: pd.DataFrame, pnl_column: Optional[str]) -> Tuple[str, Optional[float], Optional[float], Optional[float]]:
    binary = score95["binary_target"].dropna() if "binary_target" in score95.columns else pd.Series(dtype=object)
    wins = int((binary == "WIN").sum())
    losses = int((binary == "LOSS").sum())
    denominator = wins + losses
    win_rate = _safe_rate(wins, denominator)
    loss_rate = _safe_rate(losses, denominator)
    avg_profit = _profit_metrics(score95, pnl_column)["avg_profit"]
    if len(score95) < 5:
        return "LOW_SAMPLE", win_rate, loss_rate, avg_profit
    loss_ok = loss_rate is not None and loss_rate <= THRESHOLDS["max_loss_rate"]
    profit_ok = True if not pnl_column else (avg_profit is not None and avg_profit > THRESHOLDS["min_avg_profit"])
    return ("PASS" if loss_ok and profit_ok else "FAIL"), win_rate, loss_rate, avg_profit


def _rolling_windows(new_forward: pd.DataFrame, score_column: Optional[str], pnl_column: Optional[str]) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    if new_forward.empty:
        return windows
    ranges = [(0, len(new_forward))] if len(new_forward) < 20 else [(i, min(i + 20, len(new_forward))) for i in range(0, max(len(new_forward) - 19, 1), 10)]
    for idx, (start, end) in enumerate(ranges, start=1):
        window = new_forward.iloc[start:end]
        score95 = window[pd.to_numeric(window[score_column], errors="coerce") >= 95] if score_column else window.iloc[0:0]
        status, win_rate, loss_rate, avg_profit = _window_status(score95, pnl_column)
        windows.append({
            "window_id": idx,
            "start_timestamp": window["_audit_timestamp"].min().isoformat() if not window.empty else None,
            "end_timestamp": window["_audit_timestamp"].max().isoformat() if not window.empty else None,
            "rows": int(len(window)),
            "score95_rows": int(len(score95)),
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "avg_profit": avg_profit,
            "status": status,
        })
    return windows


def main() -> int:
    os.makedirs("reports", exist_ok=True)
    evidence = {"cp041": _load_json(EVIDENCE_CP041), "cp042": _load_json(EVIDENCE_CP042), "cp043": _load_json(EVIDENCE_CP043)}
    missing_flags: Dict[str, bool] = {
        "dataset_unavailable": False,
        "missing_score_column": False,
        "missing_timestamp_column": False,
        "missing_profit_column": False,
    }
    try:
        dataset = build_ml_dataset("paper_trades.csv", "signals_log.csv", "flow_log.csv", database_path="mamuyy_hunter.db", use_production_universe=True, production_score_threshold=75)
    except Exception as exc:  # read-only audit should degrade to report instead of crashing
        dataset = pd.DataFrame()
        missing_flags["dataset_unavailable"] = True
        dataset_error = str(exc)
    else:
        dataset_error = None
        missing_flags["dataset_unavailable"] = dataset.empty

    columns = list(dataset.columns)
    timestamp_column = _pick_column(columns, ["timestamp", "prediction_timestamp", "target_timestamp", "label_timestamp", "outcome_timestamp"])
    score_column = _pick_column(columns, ["score"])
    pnl_column = _pick_column(columns, ["pnl_percent", "profit_percent", "realized_pnl_percent", "pnl"])
    regime_column = _pick_column(columns, ["regime_name", "regime"])
    missing_flags["missing_score_column"] = score_column is None
    missing_flags["missing_timestamp_column"] = timestamp_column is None
    missing_flags["missing_profit_column"] = pnl_column is None

    if timestamp_column:
        dataset = dataset.copy()
        dataset["_audit_timestamp"] = pd.to_datetime(dataset[timestamp_column], errors="coerce", utc=True)
        dataset = dataset.sort_values("_audit_timestamp", ascending=True).reset_index(drop=True)

    ts_min, ts_max = _timestamp_range(dataset, timestamp_column)
    cp042_ts = _find_nested_key(evidence["cp042"], "dataset_timestamp_max") or _find_nested_key(evidence["cp042"], "timestamp_max")
    cp042_baseline = pd.to_datetime(cp042_ts, errors="coerce", utc=True) if cp042_ts else pd.NaT
    if pd.notna(cp042_baseline):
        baseline_timestamp = cp042_baseline
        baseline_source = "CP-042 dataset timestamp_max"
        baseline_estimated = False
    elif timestamp_column and not dataset.empty:
        baseline_timestamp = dataset["_audit_timestamp"].dropna().max()
        baseline_source = "current_dataset_timestamp_max"
        baseline_estimated = True
    else:
        baseline_timestamp = pd.NaT
        baseline_source = "unavailable"
        baseline_estimated = True

    if timestamp_column and pd.notna(baseline_timestamp):
        new_forward = dataset[dataset["_audit_timestamp"] > baseline_timestamp].copy()
    else:
        new_forward = pd.DataFrame(columns=dataset.columns)
    if "target" in new_forward.columns:
        new_forward["binary_target"] = new_forward["target"].apply(_binary_target)
        new_forward = new_forward[new_forward["binary_target"].notna()].copy()
    score95 = new_forward[pd.to_numeric(new_forward[score_column], errors="coerce") >= 95].copy() if score_column else new_forward.iloc[0:0].copy()

    binary = score95.get("binary_target", pd.Series(dtype=object)).dropna()
    wins = int((binary == "WIN").sum())
    losses = int((binary == "LOSS").sum())
    denominator = wins + losses
    windows = _rolling_windows(new_forward, score_column, pnl_column)
    rolling_pass = sum(1 for w in windows if w["status"] == "PASS")
    rolling_fail = sum(1 for w in windows if w["status"] == "FAIL")
    rolling_low = sum(1 for w in windows if w["status"] == "LOW_SAMPLE")
    decisive_windows = rolling_pass + rolling_fail
    rolling_pass_rate = _safe_rate(rolling_pass, decisive_windows)
    recent_consecutive_fails = 0
    for window in reversed(windows):
        if window["status"] == "FAIL":
            recent_consecutive_fails += 1
        elif window["status"] == "PASS":
            break

    top_symbol, top_symbol_conc = _top_concentration(score95, "symbol")
    top_regime, top_regime_conc = _top_concentration(score95, regime_column)
    profits = _profit_metrics(score95, pnl_column)
    loss_rate = _safe_rate(losses, denominator)
    win_rate = _safe_rate(wins, denominator)
    avg_profit = profits["avg_profit"]
    major_forward_contradiction = bool(
        len(score95) >= THRESHOLDS["minimum_new_score95_rows"]
        and (
            (loss_rate is not None and loss_rate > THRESHOLDS["max_loss_rate"])
            or (avg_profit is not None and avg_profit <= THRESHOLDS["min_avg_profit"])
            or (rolling_pass_rate is not None and rolling_pass_rate < THRESHOLDS["min_rolling_pass_rate"])
            or recent_consecutive_fails > THRESHOLDS["max_recent_consecutive_fails"]
        )
    )

    pass_fail_checks = {
        "minimum_new_score95_rows": len(score95) >= THRESHOLDS["minimum_new_score95_rows"],
        "ideal_new_score95_rows": len(score95) >= THRESHOLDS["ideal_new_score95_rows"],
        "loss_rate_within_limit": loss_rate is not None and loss_rate <= THRESHOLDS["max_loss_rate"],
        "avg_profit_positive": (avg_profit is not None and avg_profit > THRESHOLDS["min_avg_profit"]) if pnl_column else None,
        "rolling_pass_rate_within_limit": rolling_pass_rate is not None and rolling_pass_rate >= THRESHOLDS["min_rolling_pass_rate"],
        "recent_consecutive_fails_within_limit": recent_consecutive_fails <= THRESHOLDS["max_recent_consecutive_fails"],
        "top_symbol_concentration_within_limit": top_symbol_conc is not None and top_symbol_conc <= THRESHOLDS["max_top_symbol_concentration"],
        "top_regime_concentration_within_limit": None if top_regime_conc is None else top_regime_conc <= THRESHOLDS["max_top_regime_concentration"],
        "no_major_forward_contradiction": not major_forward_contradiction,
    }

    if missing_flags["dataset_unavailable"] or missing_flags["missing_score_column"] or missing_flags["missing_timestamp_column"]:
        verdict = "REVIEW_REQUIRED"
    elif len(score95) < THRESHOLDS["minimum_new_score95_rows"]:
        verdict = "INSUFFICIENT_NEW_FORWARD_DATA"
    elif major_forward_contradiction or not all(v for k, v in pass_fail_checks.items() if k in {"loss_rate_within_limit", "recent_consecutive_fails_within_limit", "top_symbol_concentration_within_limit", "no_major_forward_contradiction"}):
        verdict = "SHADOW_MONITORING_REVIEW"
    elif (
        len(score95) >= THRESHOLDS["minimum_new_score95_rows"]
        and loss_rate is not None and loss_rate <= THRESHOLDS["max_loss_rate"]
        and avg_profit is not None and avg_profit > 0
        and rolling_pass_rate is not None and rolling_pass_rate >= THRESHOLDS["min_rolling_pass_rate"]
        and recent_consecutive_fails <= THRESHOLDS["max_recent_consecutive_fails"]
        and top_symbol_conc is not None and top_symbol_conc <= THRESHOLDS["max_top_symbol_concentration"]
        and not major_forward_contradiction
    ):
        verdict = "SHADOW_MONITORING_PASS_REVIEW_REQUIRED"
    else:
        verdict = "SHADOW_MONITORING_REVIEW"

    risk_notes = []
    if baseline_estimated:
        risk_notes.append("Baseline timestamp was estimated because CP-042 timestamp_max evidence was unavailable.")
    for flag, enabled in missing_flags.items():
        if enabled:
            risk_notes.append(flag)
    if dataset_error:
        risk_notes.append(f"dataset_error: {dataset_error}")
    if len(score95) < THRESHOLDS["minimum_new_score95_rows"]:
        risk_notes.append("Insufficient fresh score>=95 rows for CP-045 readiness confidence.")
    if major_forward_contradiction:
        risk_notes.append("Major forward contradiction detected in score>=95 shadow monitoring evidence.")

    source_series = score95.get("source_artifact", pd.Series(dtype=object)).astype(str) if "source_artifact" in score95.columns else pd.Series(dtype=object)
    source_cohort_summary = {
        "all_sources": _cohort_metrics(score95, pnl_column),
        "internal_paper_trades": _cohort_metrics(score95[source_series == IPT], pnl_column),
        "historical_outcomes": _cohort_metrics(score95[source_series == HISTORICAL], pnl_column),
    }

    summary = {
        "new_forward_rows": int(len(new_forward)),
        "new_score95_rows": int(len(score95)),
        "score95_source_distribution": _dist(score95.get("source_artifact")),
        "source_cohort_summary": source_cohort_summary,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        **profits,
        "top_symbol": top_symbol,
        "top_symbol_concentration": top_symbol_conc,
        "top_regime": top_regime,
        "top_regime_concentration": top_regime_conc,
        "rolling_pass_count": rolling_pass,
        "rolling_fail_count": rolling_fail,
        "rolling_low_sample_count": rolling_low,
        "rolling_pass_rate": rolling_pass_rate,
        "recent_consecutive_fails": recent_consecutive_fails,
        "major_forward_contradiction": major_forward_contradiction,
    }
    report = {
        "cp_id": CP_ID,
        "verdict": verdict,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase3_status": "LOCKED",
        "classifier_gate": "FROZEN",
        "model_promotion": "HOLD",
        "paper_only": True,
        "runtime_changed": False,
        "execution_changed": False,
        "telegram_changed": False,
        "candidate_queue_changed": False,
        "dashboard_changed": False,
        "registry_changed": False,
        "baseline": {"baseline_timestamp": _jsonify(baseline_timestamp), "baseline_source": baseline_source, "baseline_estimated": baseline_estimated},
        "dataset_overview": {
            "total_rows": int(len(dataset)),
            "timestamp_min": ts_min,
            "timestamp_max": ts_max,
            "source_distribution": _dist(dataset.get("source_artifact")),
            "target_distribution": _dist(dataset.get("target")),
            "score_column": score_column,
            "pnl_column": pnl_column,
            "timestamp_column": timestamp_column,
        },
        "shadow_monitoring_summary": summary,
        "thresholds": THRESHOLDS,
        "pass_fail_checks": pass_fail_checks,
        "missing_flags": missing_flags,
        "risk_notes": risk_notes,
        "final_recommendation": "CP-044 does not approve unlock. Use this evidence only to decide whether CP-045 readiness review is warranted.",
        "loaded_evidence_files": {"cp041": bool(evidence["cp041"]), "cp042": bool(evidence["cp042"]), "cp043": bool(evidence["cp043"])}
    }

    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(_jsonify(report), handle, indent=2, sort_keys=True)

    with open(REPORT_WINDOWS_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["window_id", "start_timestamp", "end_timestamp", "rows", "score95_rows", "win_rate", "loss_rate", "avg_profit", "status"])
        writer.writeheader(); writer.writerows(_jsonify(windows))

    row_columns = ["timestamp", "symbol", "source_artifact", "score", "target", "binary_target"]
    if pnl_column:
        row_columns.append(pnl_column)
    if regime_column:
        row_columns.append(regime_column)
    rows_out = score95.copy()
    if not rows_out.empty:
        rows_out["timestamp"] = rows_out["_audit_timestamp"].apply(lambda ts: ts.isoformat() if pd.notna(ts) else None)
    rows_out.reindex(columns=row_columns).to_csv(REPORT_ROWS_CSV, index=False)

    md = [
        f"# CP-044 Score95 Shadow Monitoring Audit", "",
        "## Governance Header", "",
        "* Phase 3 status: LOCKED", "* Classifier gate: FROZEN", "* Model promotion: HOLD", "* PAPER_ONLY: true", "* Runtime/execution/Telegram/candidate queue/dashboard/registry changes: false", "",
        "## Baseline Source", "",
        f"* Baseline timestamp: {report['baseline']['baseline_timestamp']}", f"* Baseline source: {baseline_source}", f"* Baseline estimated: {baseline_estimated}", "",
        "## Dataset Overview", "",
        f"* Total rows: {len(dataset)}", f"* Timestamp range: {ts_min} -> {ts_max}", f"* Source distribution: {report['dataset_overview']['source_distribution']}", f"* Target distribution: {report['dataset_overview']['target_distribution']}", "",
        "## New Score95 Monitoring Summary", "",
        f"* New forward rows: {summary['new_forward_rows']}", f"* New score>=95 rows: {summary['new_score95_rows']}", f"* Win rate: {summary['win_rate']}", f"* Loss rate: {summary['loss_rate']}", f"* Average profit: {summary['avg_profit']}", f"* Top symbol concentration: {summary['top_symbol_concentration']}", f"* Rolling pass/fail/low-sample: {rolling_pass}/{rolling_fail}/{rolling_low}", "",
        "## Pass/Fail Checks", "",
        *[f"* {key}: {value}" for key, value in pass_fail_checks.items()], "",
        "## Risk Notes", "",
        *[f"* {note}" for note in (risk_notes or ["No additional risk notes."])], "",
        "## Final Recommendation", "",
        f"* Verdict: {verdict}", "* CP-044 can only recommend CP-045 readiness review; it never approves Phase 3 unlock.",
    ]
    with open(REPORT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(md) + "\n")

    print("CP-044 generated")
    print(f"dataset rows: {len(dataset)}")
    print(f"baseline timestamp/source: {_jsonify(baseline_timestamp)} / {baseline_source}")
    print(f"new forward rows: {len(new_forward)}")
    print(f"new score95 rows: {len(score95)}")
    print(f"verdict: {verdict}")
    print("Phase 3 remains LOCKED")
    print("No runtime/execution/model changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
