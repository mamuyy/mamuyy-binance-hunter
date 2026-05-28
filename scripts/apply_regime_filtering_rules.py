#!/usr/bin/env python3
"""Apply read-only regime-aware filtering simulation (paper-only, recommendation-only)."""

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
    score_raw: float
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
                if str(row[label_col]).strip().upper() == "FLAT" if label_col else False:
                    excluded_flat_count += 1
                rows.append(
                    TradeRow(
                        timestamp=parse_dt(row[ts_col]),
                        matched_regime=regime,
                        score_raw=score_raw,
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


def simulate(rows: list[TradeRow]) -> dict:
    blocked_by_rule = {
        "risk_off": 0,
        "score_below_0_2": 0,
        "holding_candles_lte_3": 0,
    }

    kept: list[TradeRow] = []
    blocked: list[TradeRow] = []

    for r in rows:
        hit = False
        if r.matched_regime.upper() == "RISK OFF":
            blocked_by_rule["risk_off"] += 1
            hit = True
        if r.score_norm < 0.2:
            blocked_by_rule["score_below_0_2"] += 1
            hit = True
        if r.holding_candles <= 3:
            blocked_by_rule["holding_candles_lte_3"] += 1
            hit = True
        if hit:
            blocked.append(r)
        else:
            kept.append(r)

    def pnl_stats(group: list[TradeRow]) -> tuple[float, float, int]:
        if not group:
            return 0.0, 0.0, 0
        total = sum(x.pnl for x in group)
        n = len(group)
        return total, total / n, n

    def winrate_stats(group: list[TradeRow]) -> tuple[Optional[float], int]:
        binary = [x.is_win for x in group if x.is_win in (0, 1)]
        if not binary:
            return None, 0
        return sum(binary) / len(binary), len(binary)

    before_pnl_total, before_pnl_avg, before_samples = pnl_stats(rows)
    after_pnl_total, after_pnl_avg, after_samples = pnl_stats(kept)
    before_winrate, before_win_samples = winrate_stats(rows)
    after_winrate, after_win_samples = winrate_stats(kept)

    return {
        "rows_total": len(rows),
        "rows_kept": len(kept),
        "rows_blocked": len(blocked),
        "blocked_count_by_rule": blocked_by_rule,
        "pnl": {
            "before_total": before_pnl_total,
            "after_total": after_pnl_total,
            "before_avg": before_pnl_avg,
            "after_avg": after_pnl_avg,
            "before_samples": before_samples,
            "after_samples": after_samples,
        },
        "winrate": {
            "before": before_winrate,
            "after": after_winrate,
            "before_samples": before_win_samples,
            "after_samples": after_win_samples,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--output", default="reports/backtest_filtered_results.json")
    args = ap.parse_args()

    rows, total_rows, excluded_flat_count, score_mode = load_rows(Path(args.input))
    sim = simulate(rows)

    payload = {
        "governance": {
            "paper_only": True,
            "read_only": True,
            "strategy_mutation": False,
            "broker_order_execution_changes": False,
            "auto_promotion": False,
        },
        "input_file": args.input,
        "total_rows": total_rows,
        "rows_kept": sim["rows_kept"],
        "rows_blocked": sim["rows_blocked"],
        "blocked_count_by_rule": sim["blocked_count_by_rule"],
        "excluded_flat_count": excluded_flat_count,
        "pnl": sim["pnl"],
        "winrate": sim["winrate"],
        "sample_count": {
            "before_filter": sim["rows_total"],
            "after_filter": sim["rows_kept"],
        },
        "score_normalization": score_mode,
        "recommendation_only_note": "This simulation is read-only/paper-only and must not change live execution.",
        "brier_note": "No Brier improvement is claimed when score is non-probabilistic.",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
