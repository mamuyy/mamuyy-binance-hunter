#!/usr/bin/env python3
"""Read-only Alpha Validation & Edge Verification report for MAMUYY Hunter.

The script opens SQLite with ``mode=ro`` and only performs metadata PRAGMA and
SELECT reads. It never writes, migrates, sends orders, retrains models, or edits
runtime configuration.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_DEFAULT = "mamuyy_hunter.db"
PRIMARY_TABLE = "internal_paper_trades"
SECONDARY_TABLE = "shadow_trades"
LOW_SAMPLE_N = 20
BOOTSTRAPS = 5000
SEED = 20260615
UNKNOWN = "UNKNOWN"
NOT_EVALUABLE = "NOT_EVALUABLE"

CLOSED_STATUSES = {"closed", "close", "completed", "complete", "done", "exited", "exit", "settled"}
ORDINARY_NON_CLOSED_STATUSES = {
    "open", "active", "pending", "queued", "new", "created", "filled", "partial", "partially_filled",
    "watch", "watching", "paper_open", "running", "cancelled", "canceled", "rejected", "expired", "blocked",
}


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100.0, 4)


def norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def pick(cols: list[str], names: list[str]) -> str | None:
    exact = {norm(col): col for col in cols}
    for name in names:
        if norm(name) in exact:
            return exact[norm(name)]
    for col in cols:
        ncol = norm(col)
        if any(norm(name) in ncol for name in names):
            return col
    return None


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=UTC)
        except Exception:
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def classify_status(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "invalid_null"
    normalized = str(value).strip().lower()
    if normalized in CLOSED_STATUSES:
        return "closed"
    if normalized in ORDINARY_NON_CLOSED_STATUSES:
        return "non_closed"
    return "invalid_unknown"


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def fetch_rows(connection: sqlite3.Connection, table: str) -> tuple[list[dict[str, Any]], list[str]]:
    cursor = connection.execute(f'SELECT * FROM "{table}"')
    cols = [description[0] for description in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()], cols


def calculate_drawdown(pnls: list[float], starting_equity: float | None = None) -> dict[str, Any]:
    equity = starting_equity or 0.0
    peak = equity
    maximum_drawdown_absolute = 0.0
    curve = []
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        maximum_drawdown_absolute = max(maximum_drawdown_absolute, peak - equity)
        curve.append(equity)
    maximum_drawdown_pct = None
    reason = "missing_starting_equity_or_capital"
    if starting_equity is not None and starting_equity > 0:
        maximum_drawdown_pct = (maximum_drawdown_absolute / starting_equity) * 100.0
        reason = None
    return {
        "maximum_drawdown_absolute": maximum_drawdown_absolute,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "maximum_drawdown_pct_reason": reason,
        "equity_curve": curve,
    }


def streaks(pnls: list[float]) -> tuple[int, int]:
    max_wins = max_losses = current_wins = current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        max_wins = max(max_wins, current_wins)
        max_losses = max(max_losses, current_losses)
    return max_wins, max_losses


def core_metrics(trades: list[dict[str, Any]], starting_equity: float | None = None) -> dict[str, Any]:
    pnls = [trade["pnl"] for trade in trades if trade.get("pnl") is not None]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    breakeven = [pnl for pnl in pnls if pnl == 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    win_streak, loss_streak = streaks(pnls)
    drawdown = calculate_drawdown(pnls, starting_equity)
    profit_factor = None
    profit_factor_reason = None
    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)
    elif gross_profit > 0:
        profit_factor_reason = "no_losses"
    return {
        "sample_count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "average_win": statistics.mean(wins) if wins else None,
        "average_loss": statistics.mean(losses) if losses else None,
        "payoff_ratio": statistics.mean(wins) / abs(statistics.mean(losses)) if wins and losses else None,
        "expectancy_per_trade": statistics.mean(pnls) if pnls else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "profit_factor_reason": profit_factor_reason,
        "cumulative_pnl": sum(pnls),
        "maximum_drawdown_absolute": drawdown["maximum_drawdown_absolute"],
        "maximum_drawdown_pct": drawdown["maximum_drawdown_pct"],
        "maximum_drawdown_pct_reason": drawdown["maximum_drawdown_pct_reason"],
        "longest_winning_streak": win_streak,
        "longest_losing_streak": loss_streak,
    }


def sample_flag(count: int) -> str:
    return "LOW_SAMPLE" if count < LOW_SAMPLE_N else "OK"


def summarize_groups(trades: list[dict[str, Any]], col: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade["row"].get(col, UNKNOWN) or UNKNOWN)].append(trade)
    return {
        key: {**core_metrics(group), "sample_flag": sample_flag(len(group)), "headline_eligible": len(group) >= LOW_SAMPLE_N}
        for key, group in sorted(groups.items())
    }


def confidence_bucket(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return UNKNOWN
    if number <= 1.0:
        number *= 100.0
    if number < 75:
        return "<75"
    if number < 80:
        return "75-80"
    if number < 85:
        return "80-85"
    if number < 90:
        return "85-90"
    if number < 95:
        return "90-95"
    return "95-100"


def holding_bucket(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return UNKNOWN
    if number <= 5:
        return "<=5"
    if number <= 15:
        return "6-15"
    if number <= 50:
        return "16-50"
    return ">50"


def headline_group(groups: dict[str, dict[str, Any]], reverse: bool = True) -> str:
    eligible = [
        (name, metrics.get("expectancy_per_trade"))
        for name, metrics in groups.items()
        if metrics.get("headline_eligible") and metrics.get("expectancy_per_trade") is not None
    ]
    if not eligible:
        return "UNAVAILABLE"
    return sorted(eligible, key=lambda item: item[1], reverse=reverse)[0][0]


def rolling_metrics(trades: list[dict[str, Any]], window: int = 50) -> list[dict[str, Any]]:
    output = []
    for index in range(0, max(0, len(trades) - window + 1)):
        metrics = core_metrics(trades[index : index + window])
        output.append({
            "start_index": index + 1,
            "end_index": index + window,
            "sample_count": metrics["sample_count"],
            "expectancy": metrics["expectancy_per_trade"],
            "profit_factor": metrics["profit_factor"],
            "profit_factor_reason": metrics["profit_factor_reason"],
            "win_rate": metrics["win_rate"],
        })
    return output


def metric_delta(early: dict[str, Any], late: dict[str, Any]) -> dict[str, Any]:
    keys = ["expectancy_per_trade", "profit_factor", "win_rate", "cumulative_pnl"]
    return {key: (late.get(key) - early.get(key) if late.get(key) is not None and early.get(key) is not None else None) for key in keys}


def assess_stability(first: dict[str, Any], second: dict[str, Any], earliest: dict[str, Any], latest: dict[str, Any], latest_50: dict[str, Any] | None) -> str:
    if min(first.get("sample_count", 0), second.get("sample_count", 0)) < LOW_SAMPLE_N:
        return "INCONCLUSIVE"
    first_exp = first.get("expectancy_per_trade")
    second_exp = second.get("expectancy_per_trade")
    early_exp = earliest.get("expectancy_per_trade")
    late_exp = latest.get("expectancy_per_trade")
    if None in (first_exp, second_exp, early_exp, late_exp):
        return "INCONCLUSIVE"
    materially_weaker = second_exp < first_exp * 0.75 or late_exp < early_exp * 0.75
    materially_better = second_exp > first_exp * 1.25 and late_exp > early_exp * 1.10
    if latest_50 and latest_50.get("expectancy_per_trade") is not None and early_exp > 0 and latest_50["expectancy_per_trade"] < 0:
        materially_weaker = True
    if materially_weaker:
        return "DEGRADING"
    if materially_better:
        return "IMPROVING"
    return "STABLE"


def bootstrap(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"seed": SEED, "samples": 0, "expectancy_ci_95": None, "win_rate_ci_95": None, "expectancy_gt_zero_pct": None}
    rng = random.Random(SEED)
    n = len(pnls)
    expectancies = []
    win_rates = []
    for _ in range(BOOTSTRAPS):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        expectancies.append(statistics.mean(sample))
        win_rates.append(sum(1 for pnl in sample if pnl > 0) / n)
    expectancies.sort()
    win_rates.sort()
    lo = int(0.025 * BOOTSTRAPS)
    hi = int(0.975 * BOOTSTRAPS) - 1
    return {
        "seed": SEED,
        "samples": BOOTSTRAPS,
        "expectancy_ci_95": [expectancies[lo], expectancies[hi]],
        "win_rate_ci_95": [win_rates[lo], win_rates[hi]],
        "expectancy_gt_zero_pct": 100.0 * sum(1 for exp in expectancies if exp > 0) / BOOTSTRAPS,
    }


def strict_json_dump(payload: dict[str, Any], path: Path) -> None:
    text = json.dumps(payload, indent=2, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def detect_starting_equity(rows: list[dict[str, Any]], cols: list[str]) -> tuple[float | None, str | None, str | None]:
    col = pick(cols, ["starting_equity", "starting_capital", "initial_equity", "initial_capital", "capital_baseline", "equity_baseline", "account_equity", "account_balance"])
    if not col:
        return None, None, "missing_starting_equity_or_capital"
    values = [safe_float(row.get(col)) for row in rows]
    values = [value for value in values if value is not None and value > 0]
    if not values:
        return None, col, "no_positive_starting_equity_or_capital"
    return values[0], col, None



def apply_empty_failure(report: dict[str, Any], reason: str) -> dict[str, Any]:
    report["critical_data_quality_failure"] = reason
    report["data_quality"] = {
        "total_rows": 0, "closed_rows": 0, "closed_trade_count": 0, "non_closed_rows": 0,
        "invalid_null_status_count": 0, "invalid_unknown_status_count": 0, "missing_pnl_count": 0,
        "duplicate_count": 0, "rows_usable_for_calculation": 0, "date_range": [None, None],
        "symbols": [], "detected_columns": {}, "detected_units": {"drawdown_pct": "missing_starting_equity_or_capital"},
    }
    report["core_performance"] = core_metrics([])
    report["edge_segmentation"] = {"market_regime": {"available": False}, "trade_quality_rank": {"available": False}}
    report["rank_validation"] = {"available": False}
    report["stability"] = {
        "first_half": core_metrics([]), "second_half": core_metrics([]), "earliest_100": core_metrics([]),
        "latest_100": core_metrics([]), "earliest_100_to_latest_100_delta": {}, "latest_50": None,
        "rolling_50": [], "latest_rolling_50": None, "assessment": "INCONCLUSIVE", "recent_degradation_note": None,
    }
    report["uncertainty"] = bootstrap([])
    report["edge_attribution"] = {"method": "comparison groups only; ASSOCIATION / ATTRIBUTION INDICATION, not proven causation", "largest_edge_contributor": "INCONCLUSIVE"}
    report["readiness_references"] = {
        "closed_trades_500": False,
        "rolling_win_rate_ge_45": {"window": "latest_rolling_50", "value": None, "passed": False},
        "rolling_profit_factor_ge_1_3": {"window": "latest_rolling_50", "value": None, "passed": False},
        "maximum_drawdown_pct_le_15": {"value": None, "passed": UNKNOWN, "reason": "missing_starting_equity_or_capital"},
    }
    report["verdict"] = {"research_audit_verdict": "INCONCLUSIVE", "phase_3": "NOT UNLOCKED", "real_trading": "LOCKED"}
    return report

def build_report(db_path: str = DB_DEFAULT) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "PAPER_ONLY READ_ONLY NO_BROKER_API NO_REAL_CAPITAL NO_RUNTIME_MODIFICATION",
        "database": db_path,
        "primary_table": PRIMARY_TABLE,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    path = Path(db_path)
    if not path.exists():
        return apply_empty_failure(report, "database_not_found")

    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        if not table_exists(connection, PRIMARY_TABLE):
            return apply_empty_failure(report, "primary_table_not_found")
        rows, cols = fetch_rows(connection, PRIMARY_TABLE)
        status_col = pick(cols, ["status", "trade_status", "state"])
        pnl_col = pick(cols, ["pnl", "realized_pnl", "net_pnl", "profit_loss", "profit", "pnl_usd"])
        symbol_col = pick(cols, ["symbol", "ticker", "pair"])
        time_col = pick(cols, ["closed_at", "exit_time", "exit_timestamp", "timestamp", "created_at", "opened_at", "entry_time", "time", "date"])
        id_col = pick(cols, ["id", "trade_id", "uuid"])
        starting_equity, starting_equity_col, starting_equity_reason = detect_starting_equity(rows, cols)

        classifications = [classify_status(row.get(status_col)) if status_col else "closed" for row in rows]
        closed_rows = [row for row, status in zip(rows, classifications) if status == "closed"]
        invalid_null = sum(1 for status in classifications if status == "invalid_null")
        invalid_unknown = sum(1 for status in classifications if status == "invalid_unknown")
        non_closed = sum(1 for status in classifications if status == "non_closed")

        trades = []
        for index, row in enumerate(closed_rows):
            trades.append({"row": row, "pnl": safe_float(row.get(pnl_col)) if pnl_col else None, "dt": parse_dt(row.get(time_col)) if time_col else None, "seq": index})
        trades.sort(key=lambda trade: (trade["dt"] or datetime.min, trade["seq"]))
        usable = [trade for trade in trades if trade["pnl"] is not None]
        dates = [trade["dt"] for trade in trades if trade["dt"]]
        duplicate_cols = [id_col] if id_col else [col for col in [symbol_col, time_col, pnl_col] if col]
        duplicate_keys = [tuple(row.get(col) for col in duplicate_cols) for row in closed_rows] if duplicate_cols else []
        duplicates = sum(count - 1 for count in Counter(duplicate_keys).values() if count > 1)

        report["data_quality"] = {
            "total_rows": len(rows),
            "closed_rows": len(closed_rows),
            "closed_trade_count": len(closed_rows),
            "non_closed_rows": non_closed,
            "invalid_null_status_count": invalid_null,
            "invalid_unknown_status_count": invalid_unknown,
            "missing_pnl_count": sum(1 for trade in trades if trade["pnl"] is None),
            "duplicate_count": duplicates,
            "rows_usable_for_calculation": len(usable),
            "date_range": [min(dates).isoformat() if dates else None, max(dates).isoformat() if dates else None],
            "symbols": sorted({str(trade["row"].get(symbol_col)) for trade in trades if symbol_col and trade["row"].get(symbol_col)}),
            "detected_columns": {"status": status_col, "pnl": pnl_col, "symbol": symbol_col, "timestamp": time_col, "id": id_col, "starting_equity": starting_equity_col},
            "detected_units": {"pnl": "database numeric units; treated as absolute paper PnL", "drawdown_pct": starting_equity_reason or "computed_from_detected_starting_equity"},
        }

        core = core_metrics(usable, starting_equity)
        report["core_performance"] = core

        dims = {
            "market_regime": ["market_regime", "regime"],
            "symbol": ["symbol", "ticker", "pair"],
            "trade_quality_rank": ["trade_quality_rank", "quality_rank", "rank"],
            "position_sizing_tier_or_multiplier": ["position_sizing_tier", "position_size_tier", "size_multiplier", "position_multiplier", "multiplier"],
            "portfolio_allocation_bucket": ["allocation_bucket", "portfolio_bucket", "allocation_tier"],
            "setup_strategy": ["setup", "strategy", "strategy_name"],
            "lifecycle_holding_bucket": ["holding_candles", "holding_period", "lifecycle_bucket"],
            "portfolio_eligible": ["portfolio_eligible", "eligible"],
            "suggested_risk_tier": ["suggested_risk_tier", "risk_tier"],
            "ml_confidence": ["ml_confidence", "confidence", "model_confidence"],
        }
        report["edge_segmentation"] = {}
        for name, candidates in dims.items():
            col = pick(cols, candidates)
            if not col:
                report["edge_segmentation"][name] = {"available": False}
                continue
            group_col = col
            if name == "lifecycle_holding_bucket" and "bucket" not in norm(col):
                group_col = "__holding_bucket"
                for trade in usable:
                    trade["row"][group_col] = holding_bucket(trade["row"].get(col))
            if name == "ml_confidence":
                group_col = "__ml_confidence_bucket"
                for trade in usable:
                    trade["row"][group_col] = confidence_bucket(trade["row"].get(col))
            groups = summarize_groups(usable, group_col)
            report["edge_segmentation"][name] = {
                "available": True,
                "column": col,
                "bucketed_column": group_col if group_col != col else None,
                "groups": groups,
                "headline_best": headline_group(groups, True),
                "headline_worst": headline_group(groups, False),
            }

        rank_col = pick(cols, ["trade_quality_rank", "quality_rank", "rank"])
        report["rank_validation"] = summarize_groups(usable, rank_col) if rank_col else {"available": False}

        half = len(usable) // 2
        earliest_100 = core_metrics(usable[:100], starting_equity)
        latest_100 = core_metrics(usable[-100:], starting_equity)
        latest_50 = core_metrics(usable[-50:], starting_equity) if len(usable) >= 50 else None
        rolling_50 = rolling_metrics(usable, 50)
        first_half = core_metrics(usable[:half], starting_equity)
        second_half = core_metrics(usable[half:], starting_equity)
        report["stability"] = {
            "first_half": first_half,
            "second_half": second_half,
            "earliest_100": earliest_100,
            "latest_100": latest_100,
            "earliest_100_to_latest_100_delta": metric_delta(earliest_100, latest_100),
            "latest_50": latest_50,
            "rolling_50": rolling_50,
            "latest_rolling_50": rolling_50[-1] if rolling_50 else None,
            "assessment": assess_stability(first_half, second_half, earliest_100, latest_100, latest_50),
            "recent_degradation_note": "Recent latest-50 weakness is monitoring evidence, not standalone proof of negative edge." if latest_50 and latest_50.get("expectancy_per_trade") is not None and latest_50["expectancy_per_trade"] < 0 else None,
        }
        report["uncertainty"] = bootstrap([trade["pnl"] for trade in usable])
        report["edge_attribution"] = {"method": "comparison groups only; ASSOCIATION / ATTRIBUTION INDICATION, not proven causation", "largest_edge_contributor": "INCONCLUSIVE"}

        drawdown_gate = UNKNOWN if core["maximum_drawdown_pct"] is None else core["maximum_drawdown_pct"] <= 15.0
        critical = bool(report.get("critical_data_quality_failure"))
        verdict = "INCONCLUSIVE"
        profit_factor = core.get("profit_factor")
        expectancy = core.get("expectancy_per_trade")
        ci = report["uncertainty"].get("expectancy_ci_95")
        if expectancy is not None and (expectancy <= 0 or (profit_factor is not None and profit_factor <= 1.0)):
            verdict = "NEGATIVE_EDGE"
        elif len(usable) >= 300 and expectancy and expectancy > 0 and profit_factor and profit_factor > 1.0 and drawdown_gate is True and not critical:
            verdict = "ALPHA_POSITIVE"
        if verdict == "ALPHA_POSITIVE" and ci and ci[0] <= 0 <= ci[1]:
            verdict = "INCONCLUSIVE"
        report["verdict"] = {"research_audit_verdict": verdict, "phase_3": "NOT UNLOCKED", "real_trading": "LOCKED", "note": "Not Phase 3 approval and not real-execution readiness."}
        report["readiness_references"] = {
            "closed_trades_500": len(usable) >= 500,
            "rolling_win_rate_ge_45": {"window": "latest_rolling_50", "value": rolling_50[-1]["win_rate"] if rolling_50 else None, "passed": bool(rolling_50 and rolling_50[-1]["win_rate"] is not None and rolling_50[-1]["win_rate"] >= 0.45)},
            "rolling_profit_factor_ge_1_3": {"window": "latest_rolling_50", "value": rolling_50[-1]["profit_factor"] if rolling_50 else None, "passed": bool(rolling_50 and rolling_50[-1]["profit_factor"] is not None and rolling_50[-1]["profit_factor"] >= 1.3)},
            "maximum_drawdown_pct_le_15": {"value": core["maximum_drawdown_pct"], "passed": drawdown_gate, "reason": core["maximum_drawdown_pct_reason"]},
        }
        if table_exists(connection, SECONDARY_TABLE):
            report["secondary_reference"] = {"shadow_trades_count": connection.execute(f'SELECT COUNT(*) FROM "{SECONDARY_TABLE}"').fetchone()[0], "note": "Not mixed into primary forward-paper result."}
        return report
    finally:
        connection.close()


def fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    dq = report.get("data_quality", {})
    core = report.get("core_performance", {})
    uncertainty = report.get("uncertainty", {})
    stability = report.get("stability", {})
    regime = report.get("edge_segmentation", {}).get("market_regime", {})
    readiness = report.get("readiness_references", {})
    lines = [
        "# MAMUYY HUNTER — Alpha Validation Report",
        "",
        "Safety: PAPER_ONLY / READ_ONLY / NO BROKER API / NO REAL CAPITAL / NO RUNTIME MODIFICATION.",
        "Phase 3 remains **NOT UNLOCKED**. Real Trading remains **LOCKED**.",
        "",
        "## Data Quality",
        f"- Total rows: {dq.get('total_rows', 0)}",
        f"- Closed rows used for primary audit: {dq.get('closed_rows', 0)}",
        f"- Non-closed rows excluded: {dq.get('non_closed_rows', 0)}",
        f"- Invalid/null statuses: {dq.get('invalid_null_status_count', 0)} null, {dq.get('invalid_unknown_status_count', 0)} unknown",
        f"- Missing PnL rows among closed trades: {dq.get('missing_pnl_count', 0)}",
        f"- Duplicate closed rows: {dq.get('duplicate_count', 0)}",
        f"- Usable closed trades: {dq.get('rows_usable_for_calculation', 0)}",
        f"- Date range: {dq.get('date_range')}",
        "",
        "## Core Performance",
        f"- Win rate: {fmt(pct(core.get('win_rate')))}%",
        f"- Expectancy per trade: {fmt(core.get('expectancy_per_trade'))}",
        f"- Profit factor: {fmt(core.get('profit_factor'))} ({fmt(core.get('profit_factor_reason'))})",
        f"- Cumulative PnL: {fmt(core.get('cumulative_pnl'))}",
        f"- Maximum drawdown absolute: {fmt(core.get('maximum_drawdown_absolute'))}",
        f"- Maximum drawdown pct: {fmt(core.get('maximum_drawdown_pct'))} ({fmt(core.get('maximum_drawdown_pct_reason'))})",
        "",
        "## Uncertainty",
        f"- Bootstrap seed/samples: {uncertainty.get('seed')} / {uncertainty.get('samples')}",
        f"- Expectancy 95% CI: {uncertainty.get('expectancy_ci_95')}",
        f"- Win-rate 95% CI: {uncertainty.get('win_rate_ci_95')}",
        f"- Bootstrap expectancy > 0: {fmt(uncertainty.get('expectancy_gt_zero_pct'))}%",
        "",
        "## Stability",
        f"- First half expectancy: {fmt(stability.get('first_half', {}).get('expectancy_per_trade'))}",
        f"- Second half expectancy: {fmt(stability.get('second_half', {}).get('expectancy_per_trade'))}",
        f"- Earliest 100 expectancy: {fmt(stability.get('earliest_100', {}).get('expectancy_per_trade'))}",
        f"- Latest 100 expectancy: {fmt(stability.get('latest_100', {}).get('expectancy_per_trade'))}",
        f"- Earliest-100 to latest-100 delta: {stability.get('earliest_100_to_latest_100_delta')}",
        f"- Latest rolling-50: {stability.get('latest_rolling_50')}",
        f"- Latest 50: {stability.get('latest_50')}",
        f"- Assessment: {stability.get('assessment', 'INCONCLUSIVE')}",
        f"- Recent degradation note: {fmt(stability.get('recent_degradation_note'))}",
        "",
        "## Valid Regime Groups (sample_count >= 20)",
    ]
    groups = regime.get("groups", {}) if isinstance(regime, dict) else {}
    valid = [(name, metrics) for name, metrics in groups.items() if metrics.get("headline_eligible")]
    if not valid:
        lines.append("- UNAVAILABLE")
    else:
        for name, metrics in valid:
            lines.append(f"- {name}: n={metrics.get('sample_count')}, win_rate={fmt(pct(metrics.get('win_rate')))}%, expectancy={fmt(metrics.get('expectancy_per_trade'))}, PF={fmt(metrics.get('profit_factor'))}")
    lines.extend([
        "",
        "## Sample Limitations",
        "- Groups with sample_count < 20 are retained in JSON as LOW_SAMPLE exploratory groups and excluded from headline best/worst rankings.",
        "- Drawdown percentage and the 15% gate are NOT_EVALUABLE unless starting equity/capital is detected.",
        "- shadow_trades, if present, are secondary reference only and are not mixed into primary forward-paper results.",
        "",
        "## Readiness References (Report Only)",
        f"- 500 closed trades: {readiness.get('closed_trades_500', False)}",
        f"- Rolling win rate >= 45%: {readiness.get('rolling_win_rate_ge_45')}",
        f"- Rolling PF >= 1.3: {readiness.get('rolling_profit_factor_ge_1_3')}",
        f"- Max drawdown pct <= 15%: {readiness.get('maximum_drawdown_pct_le_15')}",
        "",
        "## Verdict and Locks",
        f"- Verdict: {report.get('verdict', {}).get('research_audit_verdict', 'INCONCLUSIVE')}",
        "- Phase 3: NOT UNLOCKED",
        "- Real Trading: LOCKED",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(report: dict[str, Any], output_dir: str = "logs") -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(exist_ok=True)
    json_path = out / "alpha_validation_report.json"
    md_path = out / "alpha_validation_report.md"
    strict_json_dump(report, json_path)
    write_markdown(report, md_path)
    return json_path, md_path


def console_summary(report: dict[str, Any]) -> str:
    dq = report.get("data_quality", {})
    core = report.get("core_performance", {})
    edge = report.get("edge_segmentation", {})
    regime = edge.get("market_regime", {}) if isinstance(edge, dict) else {}
    rank = edge.get("trade_quality_rank", {}) if isinstance(edge, dict) else {}
    lines = [
        "MAMUYY HUNTER — ALPHA VALIDATION",
        f"Closed Trades: {dq.get('closed_rows', 0)}",
        f"Usable Trades: {dq.get('rows_usable_for_calculation', 0)}",
        f"Win Rate: {fmt(pct(core.get('win_rate')))}",
        f"Expectancy: {fmt(core.get('expectancy_per_trade'))}",
        f"Profit Factor: {fmt(core.get('profit_factor'))}",
        f"Max Drawdown: absolute={fmt(core.get('maximum_drawdown_absolute'))}, pct={fmt(core.get('maximum_drawdown_pct'))}",
        f"Best Regime: {regime.get('headline_best', 'UNAVAILABLE') if isinstance(regime, dict) else 'UNAVAILABLE'}",
        f"Worst Regime: {regime.get('headline_worst', 'UNAVAILABLE') if isinstance(regime, dict) else 'UNAVAILABLE'}",
        f"Best Rank: {rank.get('headline_best', 'UNAVAILABLE') if isinstance(rank, dict) else 'UNAVAILABLE'}",
        f"Largest Edge Contributor: {report.get('edge_attribution', {}).get('largest_edge_contributor', 'INCONCLUSIVE')}",
        f"Stability: {report.get('stability', {}).get('assessment', 'INCONCLUSIVE')}",
        f"Verdict: {report.get('verdict', {}).get('research_audit_verdict', 'INCONCLUSIVE')}",
        "Phase 3: NOT UNLOCKED",
        "Real Trading: LOCKED",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_DEFAULT)
    parser.add_argument("--output-dir", default="logs")
    args = parser.parse_args()
    report = build_report(args.db)
    json_path, md_path = write_outputs(report, args.output_dir)
    print(console_summary(report))
    print(f"Created: {json_path}")
    print(f"Created: {md_path}")


if __name__ == "__main__":
    main()
