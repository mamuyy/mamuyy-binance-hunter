#!/usr/bin/env python3
"""Week 2C adaptive regime-aware threshold simulation (paper-only)."""

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


def canonical_regime(raw: str) -> str:
    v = str(raw or "").strip().upper()
    if v in {"SIDEWAYS", "CHOPPY", "TRENDING BULL", "RISK OFF"}:
        return v
    return v or "UNKNOWN"


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
                regime = canonical_regime(row[regime_col])
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


def evaluate_policy(rows: list[TradeRow], name: str, hold_map: dict[str, int], default_hold_lte: int) -> dict:
    kept: list[TradeRow] = []
    blocked_count_by_rule = {
        "risk_off": 0,
        "score_norm_below_0_2": 0,
        "holding_candles_lte": 0,
    }

    for r in rows:
        hit_risk_off = r.matched_regime == "RISK OFF"
        hit_score = r.score_norm < 0.2
        threshold = hold_map.get(r.matched_regime, default_hold_lte)
        hit_hold = r.holding_candles <= threshold

        if hit_risk_off or hit_score or hit_hold:
            if hit_risk_off:
                blocked_count_by_rule["risk_off"] += 1
            if hit_score:
                blocked_count_by_rule["score_norm_below_0_2"] += 1
            if hit_hold:
                blocked_count_by_rule["holding_candles_lte"] += 1
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
        "policy_name": name,
        "rows_before": len(rows),
        "rows_after": len(kept),
        "rows_blocked": len(rows) - len(kept),
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
        "blocked_count_by_rule": blocked_count_by_rule,
    }


def split_three(rows: list[TradeRow]) -> dict[str, list[TradeRow]]:
    n = len(rows)
    i1 = n // 3
    i2 = (2 * n) // 3
    return {"early": rows[:i1], "middle": rows[i1:i2], "late": rows[i2:]}


def candidate_policies() -> dict[str, dict[str, int]]:
    return {
        "baseline_static": {"SIDEWAYS": 3, "CHOPPY": 3, "TRENDING BULL": 3, "RISK OFF": 3},
        "conservative": {"SIDEWAYS": 2, "CHOPPY": 2, "TRENDING BULL": 3, "RISK OFF": 999999},
        "balanced": {"SIDEWAYS": 3, "CHOPPY": 3, "TRENDING BULL": 2, "RISK OFF": 999999},
        "trend_favoring": {"SIDEWAYS": 4, "CHOPPY": 4, "TRENDING BULL": 2, "RISK OFF": 999999},
    }


def compare_all(rows: list[TradeRow]) -> tuple[dict, list[dict[str, object]]]:
    policies = candidate_policies()
    splits = split_three(rows)

    all_results: dict[str, dict] = {}
    csv_rows: list[dict[str, object]] = []

    for policy_name, hold_map in policies.items():
        overall = evaluate_policy(rows, policy_name, hold_map, default_hold_lte=3)
        split_results: dict[str, dict] = {}
        for split_name, split_rows in splits.items():
            stats = evaluate_policy(split_rows, policy_name, hold_map, default_hold_lte=3)
            split_results[split_name] = stats
            csv_rows.append({"policy": policy_name, "split": split_name, **stats})

        all_results[policy_name] = {"overall": overall, "splits": split_results}

    baseline = all_results["baseline_static"]
    comparisons: dict[str, dict] = {}

    for policy_name in ("conservative", "balanced", "trend_favoring"):
        candidate = all_results[policy_name]
        late_delta = candidate["splits"]["late"]["winrate_improvement"]
        base_late_delta = baseline["splits"]["late"]["winrate_improvement"]
        late_improvement_vs_baseline = None
        if late_delta is not None and base_late_delta is not None:
            late_improvement_vs_baseline = late_delta - base_late_delta

        consistency = {
            "winrate_non_negative_all_splits": all(
                (candidate["splits"][s]["winrate_improvement"] is None) or (candidate["splits"][s]["winrate_improvement"] >= 0)
                for s in ("early", "middle", "late")
            ),
            "avg_pnl_non_negative_all_splits": all(
                candidate["splits"][s]["avg_pnl_improvement"] >= 0 for s in ("early", "middle", "late")
            ),
            "total_pnl_non_negative_all_splits": all(
                candidate["splits"][s]["total_pnl_improvement"] >= 0 for s in ("early", "middle", "late")
            ),
        }

        comparisons[policy_name] = {
            "late_split_improvement_vs_baseline_winrate_delta": late_improvement_vs_baseline,
            "late_split_winrate_improvement": late_delta,
            "baseline_late_split_winrate_improvement": base_late_delta,
            "consistency": consistency,
        }

    return {"policies": all_results, "candidate_comparison_vs_baseline": comparisons}, csv_rows


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
    ap.add_argument("--output", default="reports/adaptive_filtering_results.json")
    ap.add_argument("--time-split-csv", default="reports/adaptive_filtering_time_split.csv")
    args = ap.parse_args()

    rows, total_rows, excluded_flat_count, score_mode = load_rows(Path(args.input))
    results, csv_rows = compare_all(rows)

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
            "no_engine_changes": True,
        },
        "input_file": args.input,
        "total_rows": total_rows,
        "excluded_flat_count": excluded_flat_count,
        "score_normalization": score_mode,
        "baseline_policy": {
            "block_risk_off": True,
            "block_score_norm_below": 0.2,
            "block_holding_candles_lte": 3,
        },
        "adaptive_policy_candidates": {
            "conservative": {"SIDEWAYS": 2, "CHOPPY": 2, "TRENDING BULL": 3, "RISK OFF": "block_all"},
            "balanced": {"SIDEWAYS": 3, "CHOPPY": 3, "TRENDING BULL": 2, "RISK OFF": "block_all"},
            "trend_favoring": {"SIDEWAYS": 4, "CHOPPY": 4, "TRENDING BULL": 2, "RISK OFF": "block_all"},
        },
        **results,
        "recommendation_note": "Week 2C output is governance evidence only; no live deployment/promotion is allowed.",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(Path(args.time_split_csv), csv_rows)


if __name__ == "__main__":
    main()
