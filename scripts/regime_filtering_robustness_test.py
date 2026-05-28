#!/usr/bin/env python3
"""Week 2B robustness and stability analysis for regime filtering (paper-only)."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class TradeRow:
    timestamp: datetime
    matched_regime: str
    score_norm: float
    pnl: float
    holding_candles: int
    is_win: Optional[int]


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def safe_float(raw: str) -> float:
    return float(str(raw).strip())


def safe_int(raw: str) -> int:
    return int(float(str(raw).strip()))


def pick_col(cols: set[str], options: list[str], required: bool = True) -> Optional[str]:
    col = next((c for c in options if c in cols), None)
    if required and col is None:
        raise ValueError(f"Missing required column. Need one of {options}. Found: {sorted(cols)}")
    return col


def parse_win_label(raw: str) -> Optional[int]:
    v = str(raw).strip().upper()
    if v in {"WIN", "TRUE", "1"}:
        return 1
    if v in {"LOSS", "FALSE", "0"}:
        return 0
    if v == "FLAT":
        return None
    try:
        x = int(float(v))
        if x in (0, 1):
            return x
    except Exception:
        return None
    return None


def normalize_score(score: float) -> tuple[float, str]:
    if 0.0 <= score <= 1.0:
        return score, "already_probability_0_1"
    return score / 100.0, "scaled_from_0_100"


def load_rows(csv_path: Path) -> tuple[list[TradeRow], int, int, str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")

    rows: list[TradeRow] = []
    total_rows = 0
    excluded_flat_count = 0
    score_mode = "already_probability_0_1"

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        ts_col = pick_col(cols, ["signal_timestamp", "timestamp", "ts", "datetime"])
        regime_col = pick_col(cols, ["matched_regime", "regime", "market_regime"])
        score_col = pick_col(cols, ["score", "raw_prob", "prob", "probability", "score_prob"])
        pnl_col = pick_col(cols, ["pnl_pct", "pnl", "realized_pnl", "ret"])
        hold_col = pick_col(cols, ["holding_candles", "holding_period", "hold_candles"])
        label_col = pick_col(cols, ["y", "label", "target", "win", "win_loss"], required=False)

        for row in reader:
            total_rows += 1
            try:
                score_raw = safe_float(row[score_col])
                score_norm, score_mode = normalize_score(score_raw)
                regime = str(row[regime_col]).strip() if row[regime_col] is not None else "UNKNOWN"
                regime = regime or "UNKNOWN"
                is_win = parse_win_label(row[label_col]) if label_col else None
                if label_col and str(row[label_col]).strip().upper() == "FLAT":
                    excluded_flat_count += 1
                rows.append(
                    TradeRow(
                        timestamp=parse_dt(row[ts_col]),
                        matched_regime=regime,
                        score_norm=score_norm,
                        pnl=safe_float(row[pnl_col]),
                        holding_candles=safe_int(row[hold_col]),
                        is_win=is_win,
                    )
                )
            except Exception:
                continue

    rows.sort(key=lambda x: x.timestamp)
    if not rows:
        raise ValueError("No valid rows parsed from CSV.")
    return rows, total_rows, excluded_flat_count, score_mode


def evaluate(rows: list[TradeRow], hold_lte: int = 3) -> dict:
    kept: list[TradeRow] = []
    blocked: list[TradeRow] = []

    for r in rows:
        hit = (
            r.matched_regime.upper() == "RISK OFF"
            or r.score_norm < 0.2
            or r.holding_candles <= hold_lte
        )
        if hit:
            blocked.append(r)
        else:
            kept.append(r)

    def winrate(group: list[TradeRow]) -> tuple[Optional[float], int]:
        valid = [x.is_win for x in group if x.is_win in (0, 1)]
        if not valid:
            return None, 0
        return sum(valid) / len(valid), len(valid)

    def pnl(group: list[TradeRow]) -> tuple[float, float]:
        if not group:
            return 0.0, 0.0
        total = sum(x.pnl for x in group)
        return total, total / len(group)

    win_before, win_before_n = winrate(rows)
    win_after, win_after_n = winrate(kept)
    total_before, avg_before = pnl(rows)
    total_after, avg_after = pnl(kept)

    return {
        "rows_before": len(rows),
        "rows_after": len(kept),
        "rows_blocked": len(blocked),
        "retention_rate": (len(kept) / len(rows)) if rows else 0.0,
        "winrate_before": win_before,
        "winrate_after": win_after,
        "winrate_samples_before": win_before_n,
        "winrate_samples_after": win_after_n,
        "avg_pnl_before": avg_before,
        "avg_pnl_after": avg_after,
        "total_pnl_before": total_before,
        "total_pnl_after": total_after,
        "winrate_improvement": (win_after - win_before) if (win_before is not None and win_after is not None) else None,
        "avg_pnl_improvement": avg_after - avg_before,
        "total_pnl_improvement": total_after - total_before,
    }


def split_three(rows: list[TradeRow]) -> dict[str, list[TradeRow]]:
    n = len(rows)
    i1 = n // 3
    i2 = (2 * n) // 3
    return {
        "early": rows[:i1],
        "middle": rows[i1:i2],
        "late": rows[i2:],
    }


def build_time_split(rows: list[TradeRow]) -> tuple[dict, list[dict[str, object]]]:
    splits = split_three(rows)
    out: dict[str, dict] = {}
    csv_rows: list[dict[str, object]] = []

    consistent_win = True
    consistent_avg = True
    consistent_total = True

    for name, data in splits.items():
        stats = evaluate(data, hold_lte=3)
        out[name] = stats
        csv_rows.append({"split": name, **stats})

        if stats["winrate_improvement"] is not None and stats["winrate_improvement"] < 0:
            consistent_win = False
        if stats["avg_pnl_improvement"] < 0:
            consistent_avg = False
        if stats["total_pnl_improvement"] < 0:
            consistent_total = False

    consistency = {
        "winrate_non_negative_across_splits": consistent_win,
        "avg_pnl_non_negative_across_splits": consistent_avg,
        "total_pnl_non_negative_across_splits": consistent_total,
    }

    return {"splits": out, "consistency": consistency}, csv_rows


def build_sensitivity(rows: list[TradeRow], thresholds: list[int]) -> tuple[list[dict], list[dict[str, object]]]:
    results: list[dict] = []
    csv_rows: list[dict[str, object]] = []
    for t in thresholds:
        stats = evaluate(rows, hold_lte=t)
        row = {
            "holding_candles_threshold_lte": t,
            "rows_kept": stats["rows_after"],
            "rows_blocked": stats["rows_blocked"],
            "retention_rate": stats["retention_rate"],
            "winrate_after": stats["winrate_after"],
            "avg_pnl_after": stats["avg_pnl_after"],
            "total_pnl_after": stats["total_pnl_after"],
        }
        results.append(row)
        csv_rows.append(row)
    return results, csv_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--output", default="reports/robustness_test_results.json")
    ap.add_argument("--time-split-csv", default="reports/robustness_time_split.csv")
    ap.add_argument("--sensitivity-csv", default="reports/robustness_sensitivity.csv")
    args = ap.parse_args()

    rows, total_rows, excluded_flat_count, score_mode = load_rows(Path(args.input))

    overall_policy = evaluate(rows, hold_lte=3)
    time_split, time_split_csv = build_time_split(rows)
    thresholds = [1, 2, 3, 4, 5, 7]
    sensitivity, sensitivity_csv = build_sensitivity(rows, thresholds)

    payload = {
        "governance": {
            "paper_only": True,
            "read_only": True,
            "strategy_mutation": False,
            "broker_order_execution_changes": False,
            "auto_promotion": False,
            "recommendation_only": True,
            "no_live_execution": True,
            "no_strategy_deployment": True,
            "no_modification_to_main_engine": True,
        },
        "input_file": args.input,
        "total_rows": total_rows,
        "excluded_flat_count": excluded_flat_count,
        "score_normalization": score_mode,
        "policy": {
            "block_risk_off": True,
            "block_score_norm_below": 0.2,
            "block_holding_candles_lte": 3,
        },
        "overall_baseline_vs_filtered": overall_policy,
        "time_split_validation": time_split,
        "sensitivity_analysis": {
            "holding_candles_thresholds_lte": thresholds,
            "results": sensitivity,
        },
        "recommendation_note": "Robustness output is governance evidence only; deployment promotion is not allowed from this result alone.",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    write_csv(Path(args.time_split_csv), time_split_csv)
    write_csv(Path(args.sensitivity_csv), sensitivity_csv)


if __name__ == "__main__":
    main()
