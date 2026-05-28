#!/usr/bin/env python3
"""Phase 2C calibration diagnosis (governance-safe, paper-only, read-only).

Outputs:
- reports/phase2c_calibration_diagnosis.json
- reports/phase2c_calibration_diagnosis_per_regime.csv (if regime exists)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

TARGET_BRIER = 0.24
EPS = 1e-6


@dataclass
class Row:
    ts: datetime
    prob: float
    y: int
    regime: Optional[str]


def clamp(x: float, lo: float = EPS, hi: float = 1 - EPS) -> float:
    return max(lo, min(hi, x))


def to_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_float(raw: str) -> float:
    return float(raw)


def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def brier(rows: Sequence[Row], preds: Sequence[float]) -> float:
    return sum((p - r.y) ** 2 for r, p in zip(rows, preds)) / len(rows)


def fit_platt(train_rows: Sequence[Row]) -> Tuple[float, float]:
    a, b = 1.0, 0.0
    lr = 0.05
    for _ in range(2000):
        ga = gb = 0.0
        for r in train_rows:
            z = a * r.prob + b
            p = sigmoid(z)
            e = p - r.y
            ga += e * r.prob
            gb += e
        n = len(train_rows)
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def apply_platt(rows: Sequence[Row], a: float, b: float) -> List[float]:
    return [clamp(sigmoid(a * r.prob + b)) for r in rows]


def fit_temperature(train_rows: Sequence[Row]) -> float:
    # p' = sigmoid(logit(p) / T)
    t = 1.0
    lr = 0.02
    for _ in range(1600):
        grad = 0.0
        for r in train_rows:
            p = clamp(r.prob)
            logit = math.log(p / (1 - p))
            z = logit / t
            q = sigmoid(z)
            e = q - r.y
            dq_dt = q * (1 - q) * (-logit / (t * t))
            grad += 2.0 * e * dq_dt
        t -= lr * grad / len(train_rows)
        t = max(0.05, min(10.0, t))
    return t


def apply_temperature(rows: Sequence[Row], t: float) -> List[float]:
    out = []
    for r in rows:
        p = clamp(r.prob)
        logit = math.log(p / (1 - p))
        out.append(clamp(sigmoid(logit / t)))
    return out


def fit_isotonic(train_rows: Sequence[Row]):
    pairs = sorted((r.prob, r.y) for r in train_rows)
    blocks = [{"sum": float(y), "count": 1, "avg": float(y), "minx": x, "maxx": x} for x, y in pairs]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i]["avg"] <= blocks[i + 1]["avg"]:
            i += 1
            continue
        merged = {
            "sum": blocks[i]["sum"] + blocks[i + 1]["sum"],
            "count": blocks[i]["count"] + blocks[i + 1]["count"],
            "minx": blocks[i]["minx"],
            "maxx": blocks[i + 1]["maxx"],
        }
        merged["avg"] = merged["sum"] / merged["count"]
        blocks[i:i + 2] = [merged]
        i = max(0, i - 1)
    return blocks


def apply_isotonic(rows: Sequence[Row], blocks) -> List[float]:
    out = []
    for r in rows:
        x = r.prob
        chosen = blocks[-1]
        for b in blocks:
            if x <= b["maxx"]:
                chosen = b
                break
        out.append(clamp(chosen["avg"]))
    return out


def load_rows(csv_path: Path) -> List[Row]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Validation CSV not found: {csv_path}")
    rows: List[Row] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        ts_col = next((c for c in ["signal_timestamp", "timestamp", "ts", "datetime"] if c in cols), None)
        p_col = next((c for c in ["raw_prob", "prob", "probability", "score_prob"] if c in cols), None)
        y_col = next((c for c in ["y", "label", "target", "win"] if c in cols), None)
        regime_col = next((c for c in ["matched_regime", "regime", "market_regime"] if c in cols), None)
        if not ts_col or not p_col or not y_col:
            raise ValueError(f"Missing required columns. Need timestamp/prob/label. Found: {sorted(cols)}")

        for row in reader:
            try:
                y_raw = str(row[y_col]).strip().upper()
                if y_raw in {"WIN", "TRUE"}:
                    y = 1
                elif y_raw in {"LOSS", "FALSE"}:
                    y = 0
                else:
                    y = int(float(row[y_col]))
                    if y not in (0, 1):
                        continue
                rows.append(Row(ts=to_dt(str(row[ts_col])), prob=clamp(to_float(str(row[p_col]))), y=y, regime=row.get(regime_col) if regime_col else None))
            except Exception:
                continue
    if not rows:
        raise ValueError("No valid validation rows parsed; fail-fast by governance policy.")
    rows.sort(key=lambda r: r.ts)
    return rows


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validation-csv", required=True)
    ap.add_argument("--reports-dir", default="reports")
    args = ap.parse_args()

    rows = load_rows(Path(args.validation_csv))
    if len(rows) < 40:
        raise RuntimeError(f"Validation rows too small ({len(rows)}). No synthetic fallback allowed.")

    split = max(20, len(rows) // 2)
    fit_rows = rows[:split]
    eval_rows = rows[split:]
    if len(eval_rows) < 20:
        raise RuntimeError("Temporal split leaves insufficient eval rows; abort for anti-leakage safety.")

    baseline_preds = [r.prob for r in eval_rows]
    baseline_brier = brier(eval_rows, baseline_preds)

    pa, pb = fit_platt(fit_rows)
    platt_preds = apply_platt(eval_rows, pa, pb)

    t = fit_temperature(fit_rows)
    temp_preds = apply_temperature(eval_rows, t)

    iso_model = fit_isotonic(fit_rows)
    iso_preds = apply_isotonic(eval_rows, iso_model)

    methods = {
        "baseline": baseline_brier,
        "platt_scaling": brier(eval_rows, platt_preds),
        "temperature_scaling": brier(eval_rows, temp_preds),
        "isotonic_regression": brier(eval_rows, iso_preds),
    }
    best_method = min(methods, key=methods.get)
    best_brier = methods[best_method]

    regime_rows: Dict[str, List[Tuple[Row, float]]] = {}
    for r, p in zip(eval_rows, baseline_preds):
        if r.regime:
            regime_rows.setdefault(r.regime, []).append((r, p))

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    csv_out = reports_dir / "phase2c_calibration_diagnosis_per_regime.csv"
    if regime_rows:
        with csv_out.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["regime", "samples", "baseline_brier"])
            for regime, vals in sorted(regime_rows.items()):
                rs = [x[0] for x in vals]
                ps = [x[1] for x in vals]
                w.writerow([regime, len(rs), round(brier(rs, ps), 6)])

    status = "PASS" if best_brier <= TARGET_BRIER else "FAIL"
    action_plan = [
        "Review regime-specific miscalibration and minimum-sample thresholds before any retraining.",
        "Use strict temporal CV for calibrator hyperparameters (no random split).",
        "If FAIL: improve feature quality/label stability first; avoid calibration-only patching.",
        "If PASS: keep Phase 2C status as recommendation only (no auto-promotion/deployment).",
    ]

    out = {
        "governance": {
            "paper_only_enforced": True,
            "read_only_diagnosis": True,
            "no_synthetic_fallback": True,
            "no_broker_order_execution_strategy_live_connector_changes": True,
            "no_model_deployment": True,
            "recommendation_only": True,
            "anti_leakage_warning": "Calibrator fit on earlier temporal split; eval only on later split.",
        },
        "baseline_commit": git_commit(),
        "dataset_window": {"start": rows[0].ts.isoformat(), "end": rows[-1].ts.isoformat()},
        "sample_count": {"total": len(rows), "fit": len(fit_rows), "eval": len(eval_rows)},
        "target_brier": TARGET_BRIER,
        "results": {k: round(v, 6) for k, v in methods.items()},
        "brier_gap_vs_target": round(best_brier - TARGET_BRIER, 6),
        "best_method": best_method,
        "pass_fail_status": status,
        "phase2c_promotion": "NO_AUTO_PROMOTE",
        "action_plan": action_plan,
        "per_regime_csv": str(csv_out) if regime_rows else None,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    json_out = reports_dir / "phase2c_calibration_diagnosis.json"
    json_out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote: {json_out}")
    if regime_rows:
        print(f"Wrote: {csv_out}")


if __name__ == "__main__":
    main()
