#!/usr/bin/env python3
"""Regime-Aware Filtering diagnosis (paper-only, read-only, recommendation-only).

Inputs:
- data/ml_calibration_matched_20260520.csv (default)

Outputs:
- reports/regime_aware_filtering_diagnosis.json
- reports/regime_aware_filtering_diagnosis.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class TradeRow:
    timestamp: datetime
    matched_regime: str
    score: float
    pnl: float
    is_win: int
    holding_candles: int


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_label(raw: str) -> int:
    v = str(raw).strip().upper()
    if v in {"WIN", "TRUE"}:
        return 1
    if v in {"LOSS", "FALSE"}:
        return 0
    y = int(float(raw))
    if y not in (0, 1):
        raise ValueError("label must be binary")
    return y


def safe_float(raw: str) -> float:
    return float(str(raw).strip())


def safe_int(raw: str) -> int:
    return int(float(str(raw).strip()))


def pick_col(cols: set[str], options: List[str], required: bool = True) -> Optional[str]:
    col = next((c for c in options if c in cols), None)
    if required and col is None:
        raise ValueError(f"Missing required column. Need one of {options}. Found: {sorted(cols)}")
    return col


def load_rows(csv_path: Path) -> List[TradeRow]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")

    out: List[TradeRow] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])

        ts_col = pick_col(cols, ["signal_timestamp", "timestamp", "ts", "datetime"])
        regime_col = pick_col(cols, ["matched_regime", "regime", "market_regime"])
        score_col = pick_col(cols, ["score", "raw_prob", "prob", "probability", "score_prob"])
        pnl_col = pick_col(cols, ["pnl_pct", "pnl", "realized_pnl", "ret"])
        label_col = pick_col(cols, ["y", "label", "target", "win"]) 
        hold_col = pick_col(cols, ["holding_candles", "holding_period", "hold_candles"])

        for row in reader:
            try:
                regime = str(row[regime_col]).strip() if row[regime_col] is not None else "UNKNOWN"
                if not regime:
                    regime = "UNKNOWN"
                out.append(
                    TradeRow(
                        timestamp=parse_dt(str(row[ts_col])),
                        matched_regime=regime,
                        score=safe_float(str(row[score_col])),
                        pnl=safe_float(str(row[pnl_col])),
                        is_win=parse_label(str(row[label_col])),
                        holding_candles=safe_int(str(row[hold_col])),
                    )
                )
            except Exception:
                continue

    if not out:
        raise ValueError("No valid rows parsed from CSV.")
    out.sort(key=lambda x: x.timestamp)
    return out


def quantile_bucket(score: float) -> str:
    if score < 0.2:
        return "q1_[0.0,0.2)"
    if score < 0.4:
        return "q2_[0.2,0.4)"
    if score < 0.6:
        return "q3_[0.4,0.6)"
    if score < 0.8:
        return "q4_[0.6,0.8)"
    return "q5_[0.8,1.0]"


def summarize(rows: List[TradeRow]) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
    by_regime: Dict[str, List[TradeRow]] = defaultdict(list)
    by_hold: Dict[int, List[TradeRow]] = defaultdict(list)
    by_interaction: Dict[Tuple[str, str], List[TradeRow]] = defaultdict(list)

    for r in rows:
        by_regime[r.matched_regime].append(r)
        by_hold[r.holding_candles].append(r)
        b = quantile_bucket(r.score)
        by_interaction[(r.matched_regime, b)].append(r)

    def agg(group: List[TradeRow]) -> dict:
        wins = sum(x.is_win for x in group)
        n = len(group)
        pnl_sum = sum(x.pnl for x in group)
        pnl_avg = pnl_sum / n
        return {
            "samples": n,
            "winrate": wins / n,
            "pnl_total": pnl_sum,
            "pnl_avg": pnl_avg,
            "holding_candles_median": statistics.median(x.holding_candles for x in group),
        }

    regime_summary = {k: agg(v) for k, v in sorted(by_regime.items(), key=lambda kv: len(kv[1]), reverse=True)}
    hold_summary = {str(k): agg(v) for k, v in sorted(by_hold.items(), key=lambda kv: kv[0])}
    interaction_summary = {f"{k[0]}__{k[1]}": agg(v) for k, v in sorted(by_interaction.items())}
    return regime_summary, hold_summary, interaction_summary


def recommend(regime_summary: Dict[str, dict], min_samples: int) -> Dict[str, List[dict]]:
    reduce_list: List[dict] = []
    allow_list: List[dict] = []
    monitor_list: List[dict] = []

    for regime, m in regime_summary.items():
        item = {
            "matched_regime": regime,
            "samples": m["samples"],
            "winrate": round(m["winrate"], 6),
            "pnl_avg": round(m["pnl_avg"], 6),
            "pnl_total": round(m["pnl_total"], 6),
        }
        if m["samples"] < min_samples:
            item["reason"] = f"low sample (<{min_samples})"
            monitor_list.append(item)
        elif m["winrate"] < 0.45 or m["pnl_avg"] < 0:
            item["reason"] = "underperforming winrate/pnl"
            reduce_list.append(item)
        elif m["winrate"] >= 0.55 and m["pnl_avg"] > 0:
            item["reason"] = "consistent positive winrate/pnl"
            allow_list.append(item)
        else:
            item["reason"] = "mixed signal"
            monitor_list.append(item)

    return {"reduce": reduce_list, "allow": allow_list, "monitor": monitor_list}


def write_outputs(reports_dir: Path, payload: dict, regime_summary: Dict[str, dict]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "regime_aware_filtering_diagnosis.json"
    csv_path = reports_dir / "regime_aware_filtering_diagnosis.csv"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["matched_regime", "samples", "winrate", "pnl_total", "pnl_avg", "holding_candles_median"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for regime, m in regime_summary.items():
            w.writerow({"matched_regime": regime, **m})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/ml_calibration_matched_20260520.csv")
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--min-samples", type=int, default=30)
    args = ap.parse_args()

    rows = load_rows(Path(args.input))
    regime_summary, hold_summary, interaction_summary = summarize(rows)
    recommendations = recommend(regime_summary, min_samples=args.min_samples)

    payload = {
        "governance": {
            "paper_only": True,
            "read_only": True,
            "strategy_mutation": False,
            "broker_order_execution_changes": False,
            "auto_promotion": False,
            "note": "diagnosis and recommendation only",
        },
        "input": args.input,
        "rows": len(rows),
        "analysis": {
            "by_matched_regime": regime_summary,
            "by_holding_candles": hold_summary,
            "score_bucket_x_regime": interaction_summary,
        },
        "recommendations": recommendations,
    }
    write_outputs(Path(args.reports_dir), payload, regime_summary)


if __name__ == "__main__":
    main()
