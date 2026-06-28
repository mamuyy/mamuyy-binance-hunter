#!/usr/bin/env python3
"""CP-041 Ranking / EV / Lifecycle Pivot Audit.

Read-only governance audit using the CP-039D production-universe dataset builder.
Produces reports only when the dataset is non-empty; never mutates runtime, models,
registry, execution engine, or promotion state.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ml_engine import _production_universe_dataset

REPORT_DIR = "reports"
JSON_PATH = os.path.join(REPORT_DIR, "cp041_ranking_ev_lifecycle_pivot.json")
SUMMARY_CSV_PATH = os.path.join(REPORT_DIR, "cp041_ranking_ev_lifecycle_pivot_summary.csv")
LEDGER_CSV_PATH = os.path.join(REPORT_DIR, "cp041_ranking_ev_lifecycle_pivot_ledger.csv")
MD_PATH = os.path.join(REPORT_DIR, "cp041_ranking_ev_lifecycle_pivot.md")
SCORE_BUCKETS = [(75, 79), (80, 84), (85, 89), (90, 94), (95, 100)]
CONF_BUCKETS = [(0.00, 0.25), (0.25, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 1.00)]
TOP_KS = [1, 3, 5, 10]
THRESHOLDS = [75, 80, 85, 90, 95]
SOURCE_NAMES = ["historical_outcomes", "internal_paper_trades"]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return value


def _rate(series: pd.Series) -> float | None:
    return None if len(series) == 0 else float(series.mean())


def _dist(df: pd.DataFrame, column: str) -> dict[str, int]:
    return {} if column not in df.columns or df.empty else {str(k): int(v) for k, v in df[column].value_counts(dropna=False).to_dict().items()}


def _avg(df: pd.DataFrame, column: str) -> float | None:
    return None if column not in df.columns or df.empty else float(pd.to_numeric(df[column], errors="coerce").mean())


def _corr(df: pd.DataFrame, x: str, y: str, method: str) -> float | None:
    if x not in df.columns or y not in df.columns or len(df) < 3:
        return None
    tmp = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(tmp) < 3 or tmp[x].nunique() < 2 or tmp[y].nunique() < 2:
        return None
    value = tmp[x].corr(tmp[y], method=method)
    return None if pd.isna(value) else float(value)


def _normalize_target(value: Any) -> str:
    label = str(value or "").strip().upper()
    if label in {"WIN", "TP1 HIT", "TP2 HIT", "TP1", "TP2", "TAKE_PROFIT_1", "TAKE_PROFIT_2"}:
        return "WIN"
    if label in {"LOSS", "SL", "STOP_LOSS"}:
        return "LOSS"
    return label


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["source_artifact", "symbol", "timestamp", "target"]:
        if col not in out.columns:
            out[col] = None
    out["timestamp_dt"] = pd.to_datetime(out["timestamp"], errors="coerce", utc=True)
    out = out.sort_values("timestamp_dt", na_position="last").reset_index(drop=True)
    if "confidence" not in out.columns and "score" in out.columns:
        # Preserve score as score; do not invent confidence.
        pass
    out["target_binary"] = out["target"].map(_normalize_target)
    out["is_win"] = out["target_binary"].eq("WIN")
    out["is_loss"] = out["target_binary"].eq("LOSS")
    pnl_col = "pnl_pct" if "pnl_pct" in out.columns else "pnl_percent" if "pnl_percent" in out.columns else None
    if pnl_col:
        out["profit_value"] = pd.to_numeric(out[pnl_col], errors="coerce")
    else:
        out["profit_value"] = np.nan
    fallback = out["target"].astype(str).str.upper().map({"WIN": 1.0, "TP2 HIT": 1.0, "TP1 HIT": 0.5, "LOSS": -1.0})
    out["profit_value"] = out["profit_value"].where(out["profit_value"].notna(), fallback).fillna(0.0)
    return out


def _bucket_label(value: Any, buckets: list[tuple[float, float]]) -> str | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for lo, hi in buckets:
        if v >= lo and (v <= hi if hi == buckets[-1][1] else v < hi + 1):
            return f"{lo:g}-{hi:g}"
    return None


def _perf(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)),
        "win_rate": _rate(df["is_win"]) if "is_win" in df else None,
        "loss_rate": _rate(df["is_loss"]) if "is_loss" in df else None,
        "avg_profit_value": _avg(df, "profit_value"),
        "median_profit_value": None if df.empty else float(df["profit_value"].median()),
        "total_profit_value": None if df.empty else float(df["profit_value"].sum()),
        "source_distribution": _dist(df, "source_artifact"),
        "target_distribution": _dist(df, "target"),
        "average_score": _avg(df, "score"),
        "average_confidence": _avg(df, "confidence"),
    }


def _bucket_audit(df: pd.DataFrame, column: str, buckets: list[tuple[float, float]]) -> list[dict[str, Any]]:
    if column not in df.columns:
        return []
    tmp = df.copy()
    tmp[f"{column}_bucket"] = tmp[column].map(lambda v: _bucket_label(v, buckets))
    return [{"bucket": b, **_perf(tmp[tmp[f"{column}_bucket"] == b])} for b in [f"{lo:g}-{hi:g}" for lo, hi in buckets]]


def _topk_group(group: pd.DataFrame, k: int) -> dict[str, Any]:
    ranked = group.sort_values("score", ascending=False) if "score" in group.columns else group
    top = ranked.head(k)
    rest = ranked.iloc[k:]
    return {
        "top_k_rows": int(len(top)), "top_k_win_rate": _rate(top["is_win"]), "top_k_loss_rate": _rate(top["is_loss"]), "top_k_avg_profit": _avg(top, "profit_value"),
        "rest_rows": int(len(rest)), "rest_win_rate": _rate(rest["is_win"]), "rest_loss_rate": _rate(rest["is_loss"]), "rest_avg_profit": _avg(rest, "profit_value"),
        "top_vs_rest_winrate_delta": None if rest.empty else float(top["is_win"].mean() - rest["is_win"].mean()),
        "top_vs_rest_profit_delta": None if rest.empty else float(top["profit_value"].mean() - rest["profit_value"].mean()),
    }


def _topk_audit(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "score" not in df.columns:
        return []
    modes = {"global": [df]}
    if "source_artifact" in df: modes["by_source_artifact"] = [g for _, g in df.groupby("source_artifact")]
    if df["timestamp_dt"].notna().any(): modes["by_day"] = [g for _, g in df.groupby(df["timestamp_dt"].dt.date)]
    if "symbol" in df and df["symbol"].value_counts().ge(12).any(): modes["by_symbol"] = [g for _, g in df.groupby("symbol") if len(g) >= 12]
    rows = []
    for mode, groups in modes.items():
        for k in TOP_KS:
            samples = [_topk_group(g, k) for g in groups if len(g) > k]
            if not samples: continue
            row = {"mode": mode, "top_k": k, "sample_count": len(samples)}
            for key in samples[0]:
                vals = [s[key] for s in samples if s[key] is not None]
                row[key] = float(np.mean(vals)) if vals and key.endswith(("rate", "profit", "delta")) else int(sum(vals)) if vals else None
            rows.append(row)
    return rows


def _loss_avoidance(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "score" not in df.columns: return []
    rows = []
    for t in THRESHOLDS:
        kept, filt = df[df["score"] >= t], df[df["score"] < t]
        rows.append({"threshold": f">={t}", "kept_rows": len(kept), "filtered_rows": len(filt), "kept_win_rate": _rate(kept["is_win"]), "kept_loss_rate": _rate(kept["is_loss"]), "filtered_win_rate": _rate(filt["is_win"]), "filtered_loss_rate": _rate(filt["is_loss"]), "loss_avoidance_delta": None if filt.empty or kept.empty else float(filt["is_loss"].mean() - kept["is_loss"].mean()), "profit_delta_kept_vs_filtered": None if filt.empty or kept.empty else float(kept["profit_value"].mean() - filt["profit_value"].mean()), "kept_source_distribution": _dist(kept, "source_artifact"), "filtered_source_distribution": _dist(filt, "source_artifact")})
    return rows


def _lifecycle(df: pd.DataFrame) -> dict[str, Any]:
    cols = [c for c in df.columns if any(tok in c.lower() for tok in ["lifecycle", "holding", "exit", "regime", "sl", "tp", "duration", "candles", "outcome"])]
    return {c: [{"value": str(v), **_perf(g)} for v, g in df.groupby(c, dropna=False)] for c in cols}


def _ev(df: pd.DataFrame) -> dict[str, Any]:
    out = {"profit_weighted_accuracy_proxy": float((np.sign(df["profit_value"]) == df["is_win"].map({True: 1, False: -1})).mean()) if len(df) else None}
    if "score" in df.columns and df["score"].nunique() > 1:
        tmp = df.copy(); tmp["score_quantile"] = pd.qcut(tmp["score"], q=min(5, tmp["score"].nunique()), duplicates="drop")
        out["avg_profit_by_score_quantile"] = {str(k): float(v) for k, v in tmp.groupby("score_quantile", observed=False)["profit_value"].mean().items()}
        out["cumulative_profit_by_score_descending"] = tmp.sort_values("score", ascending=False)["profit_value"].cumsum().round(8).tolist()
        th = [{"threshold": t, "rows": len(df[df.score >= t]), "avg_profit": _avg(df[df.score >= t], "profit_value"), "total_profit": float(df[df.score >= t]["profit_value"].sum())} for t in THRESHOLDS]
        out["thresholds"] = th; valid = [x for x in th if x["rows"]]
        out["best_threshold_by_average_profit"] = max(valid, key=lambda x: x["avg_profit"]) if valid else None
        out["best_threshold_by_total_profit"] = max(valid, key=lambda x: x["total_profit"]) if valid else None
        out["worst_threshold"] = min(valid, key=lambda x: x["avg_profit"]) if valid else None
        profits = list(out["avg_profit_by_score_quantile"].values())
        out["higher_score_monotonically_improves_profit"] = all(a <= b for a, b in zip(profits, profits[1:]))
    return out


def main() -> int:
    df = _prepare(_production_universe_dataset())
    if df.empty:
        print("CP-041 ABORT: dataset_rows=0; no reports written from empty Codex DB.")
        print("Final verdict: ABORT")
        print("Phase 3 remains LOCKED")
        return 0
    score_cols = [c for c in ["score"] if c in df.columns]
    conf_cols = [c for c in ["confidence"] if c in df.columns]
    pnl_cols = [c for c in ["pnl_pct", "pnl_percent"] if c in df.columns]
    overview = {"total_rows": len(df), "source_distribution": _dist(df, "source_artifact"), "target_distribution": _dist(df, "target"), "binary_target_distribution": _dist(df, "target_binary"), "timestamp_min": df["timestamp_dt"].min().isoformat() if df["timestamp_dt"].notna().any() else None, "timestamp_max": df["timestamp_dt"].max().isoformat() if df["timestamp_dt"].notna().any() else None, "available_score_confidence_pnl_columns": {"score": score_cols, "confidence": conf_cols, "pnl": pnl_cols}, "missingness_summary": {c: int(df[c].isna().sum()) for c in df.columns}}
    score_audit = _bucket_audit(df, "score", SCORE_BUCKETS); conf_audit = _bucket_audit(df, "confidence", CONF_BUCKETS)
    topk = _topk_audit(df); loss = _loss_avoidance(df); lifecycle = _lifecycle(df); ev = _ev(df)
    source_aware = {"mixed_additive": {"score_bucket_performance": score_audit, "topk_performance": topk, "loss_avoidance_thresholds": loss, "correlation_score_profit_pearson": _corr(df,"score","profit_value","pearson"), "correlation_score_profit_spearman": _corr(df,"score","profit_value","spearman"), "correlation_score_is_win_pearson": _corr(df,"score","is_win","pearson"), "correlation_score_is_win_spearman": _corr(df,"score","is_win","spearman")}}
    for src in SOURCE_NAMES:
        sdf = df[df["source_artifact"].eq(src)]
        source_aware[src] = {"rows": len(sdf), "score_bucket_performance": _bucket_audit(sdf,"score",SCORE_BUCKETS), "topk_performance": _topk_audit(sdf), "loss_avoidance_thresholds": _loss_avoidance(sdf), "correlation_score_profit_pearson": _corr(sdf,"score","profit_value","pearson"), "correlation_score_profit_spearman": _corr(sdf,"score","profit_value","spearman"), "correlation_score_is_win_pearson": _corr(sdf,"score","is_win","pearson"), "correlation_score_is_win_spearman": _corr(sdf,"score","is_win","spearman")}
    best_top = max([r for r in topk if r.get("top_vs_rest_winrate_delta") is not None and r.get("top_vs_rest_profit_delta") is not None], key=lambda r: (r["top_vs_rest_winrate_delta"], r["top_vs_rest_profit_delta"]), default={})
    best_loss = max([r for r in loss if r.get("loss_avoidance_delta") is not None], key=lambda r: r["loss_avoidance_delta"], default={})
    positive_src = any((source_aware[s].get("correlation_score_profit_spearman") or 0) > 0 or any((r.get("top_vs_rest_profit_delta") or 0) > 0 for r in source_aware[s]["topk_performance"]) for s in SOURCE_NAMES)
    pass_cond = (best_top.get("top_vs_rest_winrate_delta", 0) >= .05 and best_top.get("top_vs_rest_profit_delta", 0) >= .10 and best_loss.get("loss_avoidance_delta", 0) >= .05 and best_loss.get("kept_rows", 0) >= .2*len(df) and positive_src)
    any_signal = best_top.get("top_vs_rest_winrate_delta", 0) > 0 or best_top.get("top_vs_rest_profit_delta", 0) > 0 or best_loss.get("loss_avoidance_delta", 0) > 0
    neg_corr = (source_aware["mixed_additive"].get("correlation_score_profit_spearman") or 0) < 0 and (source_aware["mixed_additive"].get("correlation_score_is_win_spearman") or 0) < 0
    verdict = "PASS" if pass_cond else "REVIEW" if any_signal and not neg_corr else "FAIL"
    ledger = df.copy(); ledger["score_bucket"] = ledger["score"].map(lambda v: _bucket_label(v, SCORE_BUCKETS)) if "score" in ledger else None
    ledger["rank_global"] = ledger["score"].rank(method="first", ascending=False) if "score" in ledger else None
    ledger["rank_within_source"] = ledger.groupby("source_artifact")["score"].rank(method="first", ascending=False) if "score" in ledger else None
    for k in TOP_KS: ledger[f"is_top_{k}"] = ledger["rank_global"].le(k) if "rank_global" in ledger else False
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {"cp_id":"CP-041", "verdict":verdict, "phase3_status":"LOCKED", "classifier_gate":"FROZEN", "model_promotion":"HOLD", "paper_only":True, "runtime_changed":False, "execution_changed":False, "registry_changed":False, "dataset_overview":overview, "score_bucket_audit":score_audit, "confidence_bucket_audit":conf_audit, "topk_ranking_audit":topk, "loss_avoidance_audit":loss, "lifecycle_outcome_audit":lifecycle, "source_aware_ranking_audit":source_aware, "profit_weighted_ev_audit":ev, "verdict_evidence":{"best_topk_signal":best_top,"best_loss_avoidance_threshold":best_loss,"ipt_or_live_like_positive_evidence":positive_src,"negative_correlations":neg_corr}, "next_recommendation":"Keep Phase 3 locked and PAPER_ONLY enforced; review CP-041 evidence before any new gate proposal."}
    with open(JSON_PATH,"w",encoding="utf-8") as f: json.dump(_json_safe(report), f, indent=2, sort_keys=True)
    pd.DataFrame([{"section":"score_bucket","key":r.get("bucket"), **r} for r in score_audit] + [{"section":"topk","key":f"{r.get('mode')}:{r.get('top_k')}", **r} for r in topk] + [{"section":"loss_avoidance","key":r.get("threshold"), **r} for r in loss]).to_csv(SUMMARY_CSV_PATH,index=False)
    ledger[[c for c in ["timestamp","symbol","source_artifact","score","confidence","target","target_binary","profit_value","score_bucket","rank_global","rank_within_source","is_top_1","is_top_3","is_top_5","is_top_10"] if c in ledger.columns]].to_csv(LEDGER_CSV_PATH,index=False)
    with open(MD_PATH,"w",encoding="utf-8") as f: f.write(f"# CP-041 Ranking / EV / Lifecycle Pivot Audit\n\nVerdict: **{verdict}**  \nPhase 3: **LOCKED**  \nClassifier gate: **FROZEN**  \nModel promotion: **HOLD**  \nPAPER_ONLY: **true**\n\nRows: {len(df)}\n\nBest top-k signal: `{best_top}`\n\nBest loss avoidance threshold: `{best_loss}`\n")
    print(f"Dataset rows/source distribution: {len(df)} / {_dist(df, 'source_artifact')}")
    print(f"Score/confidence/pnl columns detected: {score_cols}/{conf_cols}/{pnl_cols}")
    print(f"Best score bucket: {max(score_audit, key=lambda r: r.get('avg_profit_value') if r.get('avg_profit_value') is not None else -999, default={})}")
    print(f"Best top-k signal: {best_top}")
    print(f"Best loss avoidance threshold: {best_loss}")
    print(f"IPT-only ranking signal summary: {source_aware['internal_paper_trades']}")
    print(f"Final verdict: {verdict}")
    print("Phase 3 remains LOCKED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
