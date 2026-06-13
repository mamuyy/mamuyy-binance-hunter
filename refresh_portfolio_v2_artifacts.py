"""Phase 3.01 offline refresher for Portfolio V2 research artifacts.

This script replaces the hard-coded 20260610 research chain with a dynamic,
lineage-aware refresh. It discovers the newest compatible position-sizing CSV,
refuses stale or malformed input, and writes fresh timestamped allocation,
health, rebalancing, and manifest artifacts.

Safety: file analytics only. No Telegram, broker, Binance, order, or runtime V1
integration is imported or invoked.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

SOURCE_PATTERNS: Tuple[str, ...] = (
    "data/ml_calibration_with_position_sizing_*.csv",
    "data/ml_calibration_with_position_sizing.csv",
)
REQUIRED_COLUMNS = {
    "symbol",
    "win_loss",
    "pnl_pct",
    "position_size_multiplier",
}
DEFAULT_SOURCE_MAX_AGE_MINUTES = 36 * 60
DEFAULT_MIN_ROWS_PER_SYMBOL = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def source_metadata(path: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or utc_now()
    if not path or not os.path.isfile(path):
        return {
            "path": path,
            "available": False,
            "modified_at": None,
            "age_minutes": None,
            "size_bytes": None,
        }
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return {
        "path": path,
        "available": True,
        "modified_at": utc_iso(modified),
        "age_minutes": round(max(0.0, (current - modified).total_seconds() / 60.0), 2),
        "size_bytes": os.path.getsize(path),
    }


def csv_columns(path: str) -> set[str]:
    try:
        frame = pd.read_csv(path, nrows=0)
    except Exception:
        return set()
    return {str(column) for column in frame.columns}


def discover_latest_compatible_source(patterns: Sequence[str] = SOURCE_PATTERNS) -> Optional[str]:
    candidates: List[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    compatible = [
        path
        for path in candidates
        if os.path.isfile(path) and REQUIRED_COLUMNS.issubset(csv_columns(path))
    ]
    if not compatible:
        return None
    return max(compatible, key=lambda path: (os.path.getmtime(path), path))


def build_allocation(source_path: str, min_rows_per_symbol: int = DEFAULT_MIN_ROWS_PER_SYMBOL) -> pd.DataFrame:
    frame = pd.read_csv(source_path, low_memory=False)
    missing = sorted(REQUIRED_COLUMNS.difference(frame.columns))
    if missing:
        raise ValueError("missing required columns: " + ", ".join(missing))

    frame = frame[frame["win_loss"].isin(["WIN", "LOSS"])].copy()
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["pnl_pct"] = pd.to_numeric(frame["pnl_pct"], errors="coerce").fillna(0.0)
    frame["position_size_multiplier"] = pd.to_numeric(
        frame["position_size_multiplier"], errors="coerce"
    ).fillna(0.0)

    rows_out: List[Dict[str, Any]] = []
    for symbol, group in frame.groupby("symbol"):
        rows = len(group)
        if rows < max(1, int(min_rows_per_symbol)):
            continue
        wins = group[group["pnl_pct"] > 0]["pnl_pct"]
        losses = group[group["pnl_pct"] <= 0]["pnl_pct"]
        if wins.empty or losses.empty:
            continue
        winrate = len(wins) / rows
        average_win = float(wins.mean())
        average_loss = abs(float(losses.mean()))
        expected_value = winrate * average_win - (1 - winrate) * average_loss
        position_multiplier = float(group["position_size_multiplier"].mean())
        sample_weight = min(rows / 300.0, 1.0)
        allocation_score = expected_value * position_multiplier * sample_weight
        if expected_value <= 0:
            allocation_score = 0.0
        rows_out.append(
            {
                "symbol": symbol,
                "rows": int(rows),
                "winrate": round(winrate, 4),
                "ev_pct": round(expected_value, 4),
                "position_multiplier": round(position_multiplier, 4),
                "sample_weight": round(sample_weight, 4),
                "allocation_score_v2": round(allocation_score, 6),
            }
        )

    columns = [
        "symbol",
        "rows",
        "winrate",
        "ev_pct",
        "position_multiplier",
        "sample_weight",
        "allocation_score_v2",
        "capital_pct_v2",
    ]
    if not rows_out:
        return pd.DataFrame(columns=columns)

    allocation = pd.DataFrame(rows_out).sort_values(
        ["allocation_score_v2", "symbol"], ascending=[False, True]
    )
    total_score = float(allocation["allocation_score_v2"].sum())
    if total_score > 0:
        raw = allocation["allocation_score_v2"] / total_score * 100.0
        allocation["capital_pct_v2"] = raw.round(2)
        rounding_delta = round(100.0 - float(allocation["capital_pct_v2"].sum()), 2)
        if abs(rounding_delta) <= 0.05 and not allocation.empty:
            first_index = allocation.index[0]
            allocation.loc[first_index, "capital_pct_v2"] = round(
                float(allocation.loc[first_index, "capital_pct_v2"]) + rounding_delta, 2
            )
    else:
        allocation["capital_pct_v2"] = 0.0
    return allocation[columns].reset_index(drop=True)


def build_health(allocation: pd.DataFrame) -> Dict[str, Any]:
    active = allocation[allocation["capital_pct_v2"] > 0].copy()
    symbol_count = len(active)
    if active.empty:
        largest_exposure = 0.0
        largest_symbol = None
    else:
        largest_row = active.sort_values("capital_pct_v2", ascending=False).iloc[0]
        largest_exposure = float(largest_row["capital_pct_v2"])
        largest_symbol = str(largest_row["symbol"])
    diversification_score = min(round(symbol_count / 30.0 * 100.0, 2), 100.0)
    risk_score = round(largest_exposure, 2)
    if diversification_score >= 80 and risk_score <= 15:
        health = "GREEN"
    elif diversification_score >= 60 and risk_score <= 25:
        health = "YELLOW"
    else:
        health = "RED"
    return {
        "phase": "Phase 5C Portfolio Health Dashboard",
        "active_symbols": symbol_count,
        "largest_exposure_symbol": largest_symbol,
        "largest_exposure_pct": largest_exposure,
        "diversification_score": diversification_score,
        "risk_score": risk_score,
        "portfolio_health": health,
        "safety": {
            "read_only": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
    }


def records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    return json.loads(frame.to_json(orient="records")) if not frame.empty else []


def build_rebalancing(allocation: pd.DataFrame) -> Dict[str, Any]:
    buy = allocation.sort_values("capital_pct_v2", ascending=False).head(10)[
        ["symbol", "capital_pct_v2"]
    ]
    reduce = allocation[
        (allocation["capital_pct_v2"] > 0) & (allocation["capital_pct_v2"] < 2)
    ][["symbol", "capital_pct_v2"]]
    remove = allocation[allocation["capital_pct_v2"] == 0][
        ["symbol", "capital_pct_v2"]
    ]
    return {
        "phase": "Phase 5D Portfolio Rebalancing Engine",
        "buy_more": records(buy),
        "reduce": records(reduce),
        "remove": records(remove),
        "summary": {
            "buy_count": len(buy),
            "reduce_count": len(reduce),
            "remove_count": len(remove),
        },
        "safety": {
            "paper_only": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
    }


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def output_paths(tag: str) -> Dict[str, Path]:
    return {
        "allocation_csv": Path(f"data/ml_portfolio_allocation_v2_{tag}.csv"),
        "allocation_report": Path(f"logs/phase4e_portfolio_allocation_v2_report_{tag}.json"),
        "health_report": Path(f"logs/phase5c_portfolio_health_dashboard_report_{tag}.json"),
        "rebalancing_report": Path(f"logs/phase5d_portfolio_rebalancing_engine_report_{tag}.json"),
        "manifest": Path("logs/portfolio_v2_refresh_manifest.json"),
    }


def run_refresh(
    *,
    source_path: Optional[str] = None,
    source_max_age_minutes: int = DEFAULT_SOURCE_MAX_AGE_MINUTES,
    min_rows_per_symbol: int = DEFAULT_MIN_ROWS_PER_SYMBOL,
    output_tag: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or utc_now()
    selected_source = source_path or discover_latest_compatible_source()
    source_meta = source_metadata(selected_source, current)
    tag = output_tag or current.strftime("%Y%m%dT%H%M%SZ")
    paths = output_paths(tag)

    blocked_reasons: List[str] = []
    if not source_meta["available"]:
        blocked_reasons.append("compatible position-sizing source not found")
    elif float(source_meta["age_minutes"]) > max(0, int(source_max_age_minutes)):
        blocked_reasons.append(
            f"position-sizing source stale: {source_meta['age_minutes']} minutes old; "
            f"limit {source_max_age_minutes}"
        )

    if blocked_reasons:
        return {
            "generated_at": utc_iso(current),
            "status": "BLOCKED_STALE_SOURCE" if source_meta["available"] else "BLOCKED_SOURCE_NOT_FOUND",
            "blocked_reasons": blocked_reasons,
            "source": source_meta,
            "outputs_written": False,
            "dry_run": dry_run,
            "safety": {
                "paper_only": True,
                "runtime_v1_changed": False,
                "broker_routing": False,
                "telegram_sent": False,
                "order_attempted": False,
            },
        }

    try:
        allocation = build_allocation(str(selected_source), min_rows_per_symbol)
    except Exception as exc:
        return {
            "generated_at": utc_iso(current),
            "status": "BLOCKED_SOURCE_INVALID",
            "blocked_reasons": [str(exc)],
            "source": source_meta,
            "outputs_written": False,
            "dry_run": dry_run,
            "safety": {
                "paper_only": True,
                "runtime_v1_changed": False,
                "broker_routing": False,
                "telegram_sent": False,
                "order_attempted": False,
            },
        }

    if len(allocation) < 5 or float(allocation["capital_pct_v2"].sum()) <= 0:
        return {
            "generated_at": utc_iso(current),
            "status": "BLOCKED_INSUFFICIENT_ALLOCATION",
            "blocked_reasons": [
                f"allocation produced {len(allocation)} symbols and "
                f"{float(allocation['capital_pct_v2'].sum()):.2f}% total"
            ],
            "source": source_meta,
            "outputs_written": False,
            "dry_run": dry_run,
            "safety": {
                "paper_only": True,
                "runtime_v1_changed": False,
                "broker_routing": False,
                "telegram_sent": False,
                "order_attempted": False,
            },
        }

    health = build_health(allocation)
    rebalancing = build_rebalancing(allocation)
    allocation_report = {
        "phase": "Phase 4E Portfolio Allocation V2",
        "generated_at": utc_iso(current),
        "source_csv": str(selected_source),
        "source_modified_at": source_meta["modified_at"],
        "source_age_minutes": source_meta["age_minutes"],
        "output_csv": str(paths["allocation_csv"]),
        "rows": int(len(allocation)),
        "allocation_total_pct": round(float(allocation["capital_pct_v2"].sum()), 2),
        "sample_penalty_rule": "sample_weight = min(rows / 300, 1.0)",
        "top_allocations": records(allocation.head(20)),
        "bottom_allocations": records(allocation.tail(10)),
        "safety": {
            "read_only_source": True,
            "production_runtime_changed": False,
            "execution_changed": False,
        },
        "verdict": "PORTFOLIO_ALLOCATION_V2_CREATED",
    }
    health.update(
        {
            "generated_at": utc_iso(current),
            "source_csv": str(paths["allocation_csv"]),
            "source_position_sizing_csv": str(selected_source),
        }
    )
    rebalancing.update(
        {
            "generated_at": utc_iso(current),
            "source_csv": str(paths["allocation_csv"]),
            "source_position_sizing_csv": str(selected_source),
        }
    )
    result = {
        "generated_at": utc_iso(current),
        "status": "DRY_RUN_READY" if dry_run else "REFRESHED",
        "blocked_reasons": [],
        "source": source_meta,
        "allocation_rows": int(len(allocation)),
        "allocation_total_pct": round(float(allocation["capital_pct_v2"].sum()), 2),
        "portfolio_health": health["portfolio_health"],
        "largest_exposure_symbol": health["largest_exposure_symbol"],
        "largest_exposure_pct": health["largest_exposure_pct"],
        "diversification_score": health["diversification_score"],
        "output_paths": {name: str(path) for name, path in paths.items()},
        "outputs_written": not dry_run,
        "dry_run": dry_run,
        "safety": {
            "paper_only": True,
            "runtime_v1_changed": False,
            "broker_routing": False,
            "telegram_sent": False,
            "order_attempted": False,
        },
    }
    if not dry_run:
        paths["allocation_csv"].parent.mkdir(parents=True, exist_ok=True)
        allocation.to_csv(paths["allocation_csv"], index=False)
        write_json(paths["allocation_report"], allocation_report)
        write_json(paths["health_report"], health)
        write_json(paths["rebalancing_report"], rebalancing)
        write_json(paths["manifest"], result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Portfolio V2 research artifacts")
    parser.add_argument("--source-path")
    parser.add_argument("--source-max-age-minutes", type=int, default=DEFAULT_SOURCE_MAX_AGE_MINUTES)
    parser.add_argument("--min-rows-per-symbol", type=int, default=DEFAULT_MIN_ROWS_PER_SYMBOL)
    parser.add_argument("--output-tag")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_refresh(
        source_path=args.source_path,
        source_max_age_minutes=args.source_max_age_minutes,
        min_rows_per_symbol=args.min_rows_per_symbol,
        output_tag=args.output_tag,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"REFRESHED", "DRY_RUN_READY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
