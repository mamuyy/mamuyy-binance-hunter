#!/usr/bin/env python3
"""CP-042 score>=95 forward validation / paper-only gate audit.

Read-only evidence script. It consumes the production universe dataset built by
``ml_engine._production_universe_dataset`` and writes CP-042 reports only when
non-empty evidence exists. It does not modify runtime, execution, registry,
weights, or promotion state.
"""

from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from ml_engine import _production_universe_dataset

CP_ID = "CP-042"
SCORE_GATE = 95
THRESHOLDS = [90, 92, 95, 97, 99]
SOURCES = ["all", "internal_paper_trades", "historical_outcomes"]
REPORT_JSON = "reports/cp042_score95_forward_validation.json"
REPORT_SUMMARY_CSV = "reports/cp042_score95_forward_validation_summary.csv"
REPORT_WINDOWS_CSV = "reports/cp042_score95_forward_validation_windows.csv"
REPORT_MD = "reports/cp042_score95_forward_validation.md"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat() if not pd.isna(value) else None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _pct(numerator: float, denominator: float) -> Optional[float]:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _rate_percent(rate: Optional[float]) -> Optional[float]:
    return None if rate is None else round(rate * 100.0, 2)


def _distribution(series: pd.Series) -> Dict[str, int]:
    if series is None or series.empty:
        return {}
    return {str(k): int(v) for k, v in series.fillna("<NA>").astype(str).value_counts(dropna=False).sort_index().items()}


def _binary_target(label: Any) -> str:
    label = str(label or "").strip().upper()
    if label in {"WIN", "TP1 HIT"}:
        return "WIN"
    if label == "LOSS":
        return "LOSS"
    return label or "UNKNOWN"


def _profit(row: pd.Series) -> float:
    pnl = row.get("pnl_percent")
    if pd.notna(pnl):
        try:
            return float(pnl)
        except (TypeError, ValueError):
            pass
    label = str(row.get("target", "")).upper()
    if label == "WIN":
        return 1.0
    if label == "TP1 HIT":
        return 0.5
    if label == "LOSS":
        return -1.0
    return 0.0


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    else:
        df["timestamp"] = pd.NaT
    df["score"] = pd.to_numeric(df.get("score", pd.Series(index=df.index, dtype=float)), errors="coerce")
    if "pnl_percent" in df.columns:
        df["pnl_percent"] = pd.to_numeric(df["pnl_percent"], errors="coerce")
    else:
        df["pnl_percent"] = pd.NA
    df["target_binary"] = df.get("target", pd.Series(index=df.index, dtype=str)).apply(_binary_target)
    df["is_win"] = df["target_binary"].eq("WIN")
    df["is_loss"] = df["target_binary"].eq("LOSS")
    df["profit_value"] = df.apply(_profit, axis=1)
    df["score_gate_95"] = df["score"].ge(SCORE_GATE)
    df["source_is_live_like"] = df.get("source_artifact", pd.Series(index=df.index, dtype=str)).eq("internal_paper_trades")
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df.sort_values("timestamp", na_position="last").reset_index(drop=True)


def _group_metrics(subset: pd.DataFrame, prefix: str) -> Dict[str, Any]:
    rows = int(len(subset))
    wins = int(subset["is_win"].sum()) if rows else 0
    losses = int(subset["is_loss"].sum()) if rows else 0
    return {
        f"{prefix}_rows": rows,
        f"{prefix}_win_rate": _pct(wins, rows),
        f"{prefix}_loss_rate": _pct(losses, rows),
        f"{prefix}_avg_profit": None if rows == 0 else float(subset["profit_value"].mean()),
        f"{prefix}_median_profit": None if rows == 0 else float(subset["profit_value"].median()),
        f"{prefix}_total_profit": None if rows == 0 else float(subset["profit_value"].sum()),
    }


def _segment_metrics(name: str, segment: pd.DataFrame, min_rows: int = 1, min_score95_rows: int = 1) -> Dict[str, Any]:
    score95 = segment[segment["score_gate_95"]]
    non95 = segment[~segment["score_gate_95"]]
    out: Dict[str, Any] = {
        "segment_name": name,
        "rows": int(len(segment)),
        "timestamp_min": segment["timestamp"].min().isoformat() if not segment.empty and pd.notna(segment["timestamp"].min()) else None,
        "timestamp_max": segment["timestamp"].max().isoformat() if not segment.empty and pd.notna(segment["timestamp"].max()) else None,
        "source_distribution": _distribution(segment.get("source_artifact", pd.Series(dtype=str))),
        "score95_source_distribution": _distribution(score95.get("source_artifact", pd.Series(dtype=str))),
    }
    out.update(_group_metrics(score95, "score95"))
    out.update(_group_metrics(non95, "non_score95"))
    out["loss_avoidance_delta"] = None
    if out["non_score95_loss_rate"] is not None and out["score95_loss_rate"] is not None:
        out["loss_avoidance_delta"] = out["non_score95_loss_rate"] - out["score95_loss_rate"]
    out["profit_delta"] = None
    if out["non_score95_avg_profit"] is not None and out["score95_avg_profit"] is not None:
        out["profit_delta"] = out["score95_avg_profit"] - out["non_score95_avg_profit"]
    out["low_sample"] = bool(out["rows"] < min_rows or out["score95_rows"] < min_score95_rows)
    passes = (
        not out["low_sample"]
        and (out["score95_win_rate"] or 0) >= 0.65
        and (out["score95_loss_rate"] if out["score95_loss_rate"] is not None else 1) <= 0.35
        and (out["score95_avg_profit"] or -1) > 0
        and (out["loss_avoidance_delta"] or -1) >= 0.10
    )
    out["status"] = "LOW_SAMPLE" if out["low_sample"] else ("PASS" if passes else "FAIL")
    return out


def _forward_splits(df: pd.DataFrame, name_prefix: str, min_rows: int = 1, min_score95_rows: int = 1) -> List[Dict[str, Any]]:
    results = []
    n = len(df)
    for pct in [50, 60, 70, 80]:
        cut = int(n * pct / 100)
        results.append(_segment_metrics(f"{name_prefix}_first_{pct}_last_{100-pct}", df.iloc[cut:], min_rows, min_score95_rows))
    if df["timestamp"].notna().any():
        for freq, label in [("ME", "monthly"), ("W", "weekly")]:
            try:
                groups = df.set_index("timestamp").groupby(pd.Grouper(freq=freq))
                for key, chunk in groups:
                    chunk = chunk.reset_index(drop=False)
                    if not chunk.empty:
                        results.append(_segment_metrics(f"{name_prefix}_{label}_{key.date()}", chunk, min_rows, min_score95_rows))
            except Exception:
                continue
    return results


def _stability_for_threshold(df: pd.DataFrame, threshold: int, source_scope: str) -> Dict[str, Any]:
    scoped = df if source_scope == "all" else df[df["source_artifact"].eq(source_scope)]
    kept = scoped[scoped["score"].ge(threshold)]
    filtered = scoped[scoped["score"].lt(threshold)]
    segments = [_segment_metrics(f"thr_{threshold}_{source_scope}_{p}", scoped.iloc[int(len(scoped)*p):], min_score95_rows=1) for p in [0.5, 0.6, 0.7, 0.8]]
    return {
        "threshold": threshold,
        "source_scope": source_scope,
        "rows_kept": int(len(kept)),
        "sample_pct": _pct(len(kept), len(scoped)),
        "win_rate": _pct(int(kept["is_win"].sum()), len(kept)),
        "loss_rate": _pct(int(kept["is_loss"].sum()), len(kept)),
        "avg_profit": None if kept.empty else float(kept["profit_value"].mean()),
        "total_profit": None if kept.empty else float(kept["profit_value"].sum()),
        "filtered_loss_rate": _pct(int(filtered["is_loss"].sum()), len(filtered)),
        "loss_avoidance_delta": None if kept.empty or filtered.empty else _pct(int(filtered["is_loss"].sum()), len(filtered)) - _pct(int(kept["is_loss"].sum()), len(kept)),
        "stability_across_forward_splits": [s["status"] for s in segments],
        "pass_count": sum(s["status"] == "PASS" for s in segments),
        "fail_count": sum(s["status"] == "FAIL" for s in segments),
        "low_sample_count": sum(s["status"] == "LOW_SAMPLE" for s in segments),
    }


def _rolling_windows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    windows = []
    for size in [50, 75, 100]:
        if len(df) < size:
            continue
        for start in range(0, len(df) - size + 1, size):
            windows.append(_segment_metrics(f"rolling_{size}_{start}_{start+size-1}", df.iloc[start:start+size], min_rows=size, min_score95_rows=1))
    return windows


def _flatten_rows(sections: Dict[str, Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows = []
    for section, entries in sections.items():
        for entry in entries:
            row = {"section": section, **entry}
            rows.append(row)
    return rows


def _write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    keys = sorted({k for row in rows for k in row.keys()})
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def _markdown(report: Dict[str, Any]) -> str:
    ve = report["verdict_evidence"]
    return f"""# CP-042 Score >=95 Forward Validation / Paper-only Gate Audit

- Verdict: **{report['verdict']}**
- Phase 3 status: **LOCKED**
- Classifier gate: **FROZEN**
- PAPER_ONLY: **true**
- Runtime changed: **false**
- Execution changed: **false**
- Registry changed: **false**

## Dataset Overview

- Rows: {report['dataset_overview']['total_rows']}
- Source distribution: `{report['dataset_overview']['source_distribution']}`
- Target distribution: `{report['dataset_overview']['target_distribution']}`
- Score >=95 rows: {report['dataset_overview']['score95_row_count']}

## Verdict Evidence

- IPT valid forward segments: {ve['ipt_valid_forward_segments']}
- IPT pass segments: {ve['ipt_pass_segments']}
- IPT fail segments: {ve['ipt_fail_segments']}
- IPT low-sample segments: {ve['ipt_low_sample_segments']}
- Major contradiction: {ve['major_forward_contradiction']}

## Gate Policy Recommendation Draft

{report['gate_policy_recommendation']}

## Next Recommendation

{report['next_recommendation']}
"""


def main() -> int:
    dataset = _production_universe_dataset()
    if dataset.empty or len(dataset) == 0:
        print("CP-042 graceful abort: dataset_rows=0; no CP-042 reports written or updated.")
        print("Phase 3 remains LOCKED")
        return 0

    df = _prepare(dataset)
    os.makedirs("reports", exist_ok=True)

    overview = {
        "total_rows": int(len(df)),
        "source_distribution": _distribution(df["source_artifact"]),
        "target_distribution": _distribution(df["target"]),
        "binary_target_distribution": _distribution(df["target_binary"]),
        "timestamp_min": df["timestamp"].min().isoformat() if pd.notna(df["timestamp"].min()) else None,
        "timestamp_max": df["timestamp"].max().isoformat() if pd.notna(df["timestamp"].max()) else None,
        "score_availability": {"available_rows": int(df["score"].notna().sum()), "missing_rows": int(df["score"].isna().sum())},
        "pnl_availability": {"available_rows": int(df["pnl_percent"].notna().sum()), "missing_rows": int(df["pnl_percent"].isna().sum())},
        "score95_row_count": int(df["score_gate_95"].sum()),
        "score95_source_distribution": _distribution(df[df["score_gate_95"]]["source_artifact"]),
    }

    forward = _forward_splits(df, "all")
    ipt = _forward_splits(df[df["source_artifact"].eq("internal_paper_trades")].reset_index(drop=True), "ipt_only", min_rows=20, min_score95_rows=10)
    hist = _forward_splits(df[df["source_artifact"].eq("historical_outcomes")].reset_index(drop=True), "historical_only")
    stability = [_stability_for_threshold(df, t, s) for s in SOURCES for t in THRESHOLDS]
    windows = _rolling_windows(df)

    ipt_valid = [s for s in ipt if not s["low_sample"]]
    ipt_pass = [s for s in ipt_valid if s["status"] == "PASS"]
    ipt_fail = [s for s in ipt_valid if s["status"] == "FAIL"]
    major_contradiction = any(s["rows"] >= max(20, len(df[df["source_artifact"].eq("internal_paper_trades")]) * 0.2) and s["status"] == "FAIL" for s in ipt_valid)
    aggregate_95 = _segment_metrics("aggregate_all", df)
    promising = aggregate_95["score95_win_rate"] and aggregate_95["score95_win_rate"] >= 0.65 and (aggregate_95["score95_avg_profit"] or -1) > 0
    if len(ipt_pass) >= 3 and not major_contradiction:
        verdict = "PASS"
    elif promising or ipt_pass:
        verdict = "REVIEW"
    else:
        verdict = "FAIL"

    report = {
        "cp_id": CP_ID,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "phase3_status": "LOCKED",
        "classifier_gate": "FROZEN",
        "model_promotion": "HOLD",
        "paper_only": True,
        "runtime_changed": False,
        "execution_changed": False,
        "registry_changed": False,
        "dataset_overview": overview,
        "forward_chronological_splits": forward,
        "ipt_only_forward_validation": ipt,
        "historical_only_comparison": hist,
        "gate_stability_audit": stability,
        "rolling_forward_windows": windows,
        "gate_policy_recommendation": "Draft only: retain score >=95 as a PAPER_ONLY candidate gate requiring source-aware IPT/live-like evidence, minimum rolling-window evidence, no automatic execution, no model promotion, and no Phase 3 unlock.",
        "verdict_evidence": {
            "ipt_valid_forward_segments": len(ipt_valid),
            "ipt_pass_segments": len(ipt_pass),
            "ipt_fail_segments": len(ipt_fail),
            "ipt_low_sample_segments": sum(s["low_sample"] for s in ipt),
            "major_forward_contradiction": major_contradiction,
            "aggregate_score95_status": aggregate_95["status"],
        },
        "next_recommendation": "Keep Phase 3 LOCKED and classifier promotion on HOLD. Continue PAPER_ONLY forward collection until live-like rolling windows provide sufficient source-aware evidence.",
    }

    with open(REPORT_JSON, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(report), handle, indent=2, sort_keys=True)
    _write_csv(REPORT_SUMMARY_CSV, _flatten_rows({"forward": forward, "ipt_only": ipt, "historical_only": hist, "stability": stability}))
    _write_csv(REPORT_WINDOWS_CSV, windows)
    with open(REPORT_MD, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))

    best = max(forward, key=lambda s: ((s.get("score95_avg_profit") or -10**9), (s.get("loss_avoidance_delta") or -10**9))) if forward else {}
    print(f"Dataset rows: {len(df)}; source_distribution={overview['source_distribution']}")
    print(f"score>=95 rows: {overview['score95_row_count']}; source_distribution={overview['score95_source_distribution']}")
    print(f"Best forward split: {best.get('segment_name')} status={best.get('status')} score95_avg_profit={best.get('score95_avg_profit')}")
    print(f"IPT-only score>=95 forward summary: valid={len(ipt_valid)} pass={len(ipt_pass)} fail={len(ipt_fail)} low_sample={sum(s['low_sample'] for s in ipt)}")
    print("Threshold stability summary: " + ", ".join(f"{s['source_scope']} >= {s['threshold']}: pass={s['pass_count']} fail={s['fail_count']} low={s['low_sample_count']}" for s in stability))
    print(f"Rolling window summary: windows={len(windows)} pass={sum(w['status']=='PASS' for w in windows)} fail={sum(w['status']=='FAIL' for w in windows)} low_sample={sum(w['status']=='LOW_SAMPLE' for w in windows)}")
    print(f"Final verdict: {verdict}")
    print("Phase 3 remains LOCKED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
