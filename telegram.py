import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, Any

import requests

from config import config


def format_signal_message(signal: Dict[str, Any]) -> str:
    funding_percent = signal.get("funding", 0.0) * 100
    message = (
        "🚨 MAMUYY BINANCE HUNTER V1\n\n"
        "🌎 MARKET REGIME\n"
        f"Current Mode: {signal.get('regime_name', 'UNKNOWN')}\n"
        f"Confidence: {signal.get('regime_score', 0)}%\n\n"
        f"🔥 {signal.get('symbol')}\n"
        f"Score: {signal.get('score')}/100\n"
        f"Price: {signal.get('price')}\n"
        f"Volume Spike: {signal.get('volume_spike'):.2f}\n"
        f"Breakout: {signal.get('breakout')}\n"
        f"Liquidity Sweep: {signal.get('liquidity_sweep')}\n"
        f"Taker Buy Ratio: {signal.get('taker_buy_ratio'):.2f}\n"
        f"Funding: {funding_percent:.4f}%\n"
        f"Open Interest: {signal.get('open_interest')}"
    )
    if signal.get("flow_state"):
        message += "\n\n" + format_flow_alert_message(signal)
    return message


def format_flow_alert_message(signal: Dict[str, Any]) -> str:
    funding_zscore = signal.get("funding_zscore") or 0.0
    oi_expansion_rate = signal.get("oi_expansion_rate") or 0.0
    pressure_score = signal.get("pressure_score") or 0.0
    squeeze_probability = signal.get("squeeze_probability") or 0.0
    funding_warning = signal.get("funding_warning") or "-"

    return (
        "🚨 FLOW ALERT\n\n"
        f"Coin: {signal.get('symbol')}\n"
        f"Pressure: {pressure_score:.2f}/100\n"
        f"Funding: z={funding_zscore:.2f} ({funding_warning})\n"
        f"OI Expansion: {oi_expansion_rate:.2f}%\n"
        f"Whale Activity: {signal.get('whale_activity', '-')}\n"
        f"Squeeze Risk: {signal.get('squeeze_risk', '-')} "
        f"({squeeze_probability:.2f}%)\n"
        f"Final Score: {signal.get('score')}/100"
    )


def format_market_regime_message(regime: Dict[str, Any]) -> str:
    return (
        "🌎 MARKET REGIME\n"
        f"Current Mode: {regime.get('regime_name', 'UNKNOWN')}\n"
        f"Confidence: {regime.get('regime_score', 0)}%"
    )


def format_paper_summary_message(summary: Dict[str, Any]) -> str:
    return (
        "📊 PAPER TRADING SUMMARY\n\n"
        f"Total Trade: {summary.get('total_trade', 0)}\n"
        f"Win: {summary.get('win', 0)}\n"
        f"Loss: {summary.get('loss', 0)}\n"
        f"Winrate: {summary.get('winrate', 0.0):.2f}%\n"
        f"Average PnL: {summary.get('average_pnl', 0.0):.2f}%\n"
        f"Best Coin: {summary.get('best_coin', '-')}\n"
        f"Worst Coin: {summary.get('worst_coin', '-')}"
    )



def _format_paper_portfolio_price(value: Any) -> str:
    try:
        if value in (None, ""):
            return "n/a"
        return f"{float(value):.8g}"
    except (TypeError, ValueError):
        return "n/a"


def _format_paper_portfolio_pct(value: Any) -> str:
    try:
        if value in (None, ""):
            return "n/a"
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_paper_portfolio_message(report: Dict[str, Any]) -> str:
    active_status = report.get("active_status_distribution", {})
    if not isinstance(active_status, dict):
        active_status = {}
    status_line = " | ".join(
        f"{str(status).upper()} {count}" for status, count in active_status.items()
    ) or "none"

    lines = [
        "📋 PAPER PORTFOLIO",
        f"Active Paper Trades: {report.get('total_active_trades', 0)}",
        f"Closed Paper Trades: {report.get('closed_progress', '0/100')}",
        f"Status: {status_line}",
        "Top Active:",
    ]
    top_active = report.get("top_active_trades", [])
    appended = False
    if isinstance(top_active, list) and top_active:
        for index, trade in enumerate(top_active[:5], start=1):
            if not isinstance(trade, dict):
                continue
            appended = True
            lines.append(
                f"{index}. {trade.get('symbol', '-')} | "
                f"{str(trade.get('status', '-')).upper()} | "
                f"Entry {_format_paper_portfolio_price(trade.get('entry_price'))} | "
                f"Current {_format_paper_portfolio_price(trade.get('current_price'))} | "
                f"PnL {_format_paper_portfolio_pct(trade.get('virtual_unrealized_pnl_pct'))}"
            )
    if not appended:
        lines.append("none")
    lines.append("Mode: PAPER_ONLY read-only, no execution mutation, no broker routing, no Phase 3 unlock.")
    return "\n".join(lines)


def format_paper_outcome_audit_message(report: Dict[str, Any]) -> str:
    def pct(value: Any) -> str:
        return _format_paper_portfolio_pct(value)

    def trade_line(trade: Any) -> str:
        if not isinstance(trade, dict):
            return "- n/a"
        symbol = trade.get("symbol") or "-"
        return f"{symbol} {pct(trade.get('realized_pnl_pct'))}"

    return "\n".join(
        [
            "📊 PAPER OUTCOME AUDIT",
            f"Closed Trades: {report.get('closed_progress', '0/100')}",
            f"Wins: {report.get('win_count', 0)}",
            f"Losses: {report.get('loss_count', 0)}",
            f"Winrate: {float(report.get('winrate') or 0.0):.2f}%",
            f"Net PnL: {pct(report.get('net_pnl'))}",
            f"Best: {trade_line(report.get('best_trade'))}",
            f"Worst: {trade_line(report.get('worst_trade'))}",
            "Mode: PAPER_ONLY read-only, no trade mutation, no broker routing, no Phase 3 unlock.",
        ]
    )


def format_performance_report_message(metrics: Dict[str, Any]) -> str:
    profit_factor = metrics.get("profit_factor", 0.0)
    if profit_factor == float("inf"):
        profit_factor_text = "∞"
    else:
        profit_factor_text = f"{profit_factor:.2f}"

    message = (
        "📊 PERFORMANCE REPORT\n\n"
        f"Winrate: {metrics.get('winrate', 0.0):.2f}%\n"
        f"Profit Factor: {profit_factor_text}\n"
        f"Max DD: {metrics.get('max_drawdown', 0.0):.2f}%\n"
        f"Best Regime: {metrics.get('best_regime', '-')}\n"
        f"Worst Regime: {metrics.get('worst_regime', '-')}"
    )

    if metrics.get("unhealthy"):
        message += "\n\n⚠️ STRATEGY UNHEALTHY"

    return message


def format_ml_analysis_message(result: Dict[str, Any]) -> str:
    top_features = result.get("feature_importance", [])[:3]
    feature_lines = []
    for index in range(3):
        if index < len(top_features):
            feature_lines.append(f"{index + 1}. {top_features[index].get('feature')}")
        else:
            feature_lines.append(f"{index + 1}. -")

    return (
        "🧠 ML ANALYSIS\n\n"
        "Top Features:\n"
        f"{feature_lines[0]}\n"
        f"{feature_lines[1]}\n"
        f"{feature_lines[2]}\n\n"
        f"Most Profitable Regime: {result.get('most_profitable_regime', '-')}\n"
        f"Worst Regime: {result.get('worst_regime', '-')}\n\n"
        f"Current Model Accuracy: {result.get('accuracy', 0.0):.2%}\n"
        f"AI Confidence: {result.get('ai_confidence_score', 0)}/100\n"
        f"Setup Ranking: {result.get('setup_ranking', 'LOW QUALITY')}"
    )


def format_walkforward_report_message(result: Dict[str, Any]) -> str:
    return (
        "🧪 WALK FORWARD REPORT\n\n"
        f"Model Health: {result.get('model_health', 'UNSTABLE')}\n"
        f"Overfit Risk: {result.get('overfit_risk_score', 0.0):.2f}/100\n"
        f"Rolling Accuracy: {result.get('average_accuracy', 0.0):.2%}\n"
        f"Rolling Winrate: {result.get('average_winrate', 0.0):.2f}%\n"
        f"Best Regime: {result.get('best_regime', '-')}\n"
        f"Worst Regime: {result.get('worst_regime', '-')}"
    )


def format_regime_model_message(result: Dict[str, Any]) -> str:
    return (
        "🧠 REGIME MODEL\n\n"
        f"Current Regime: {result.get('current_regime', 'UNKNOWN')}\n"
        f"Selected Model: {result.get('selected_model', '-')}\n"
        f"Model Confidence: {result.get('model_confidence', 0):.2f}%\n"
        f"Expected Behavior: {result.get('expected_behavior', '-')}"
    )


def format_portfolio_message(result: Dict[str, Any]) -> str:
    allocation = result.get("recommended_allocation", {})
    if allocation:
        top = sorted(allocation.items(), key=lambda item: item[1], reverse=True)[:5]
        allocation_text = ", ".join(f"{symbol}: {weight:.2f}%" for symbol, weight in top)
    else:
        allocation_text = "-"
    return (
        "📦 PORTFOLIO ENGINE\n\n"
        f"Portfolio Health: {result.get('portfolio_health', 'YELLOW')} "
        f"({result.get('portfolio_health_score', 0)}/100)\n"
        f"Risk Score: {result.get('portfolio_risk_score', 0)}/100\n"
        f"Diversification: {result.get('diversification_score', 0)}/100\n"
        f"Largest Exposure: {result.get('largest_exposure', '-')}\n"
        f"Recommended Allocation: {allocation_text}"
    )


def format_execution_message(result: Dict[str, Any]) -> str:
    return (
        "⚡ EXECUTION ENGINE\n\n"
        f"Execution Profile: {result.get('execution_profile', 'NORMAL')}\n"
        f"Expected Slippage: {result.get('expected_slippage', 0)}%\n"
        f"Fill Probability: {result.get('fill_probability', 0)}%\n"
        f"Execution Quality: {result.get('execution_quality', 0)}/100\n"
        f"Adjusted PnL Impact: {result.get('adjusted_pnl_impact', 0)}%"
    )


def format_shadow_message(result: Dict[str, Any]) -> str:
    return (
        "👻 SHADOW LIVE ENGINE\n\n"
        f"Live PnL (Rolling Active): {result.get('rolling_live_pnl_pct', result.get('live_pnl', 0))}%\n"
        f"Cumulative Shadow PnL: {result.get('cumulative_shadow_pnl_pct', 0)}%\n"
        f"Shadow Winrate (last 500): {result.get('live_winrate', 0)}%\n"
        f"Execution Drift: {result.get('execution_drift', 0)}%\n"
        f"Current Regime: {result.get('current_regime', 'UNKNOWN')}\n"
        f"Shadow Exposure (Rolling Active): {result.get('rolling_live_exposure_pct', result.get('live_exposure', 0))}%\n"
        f"Cumulative Shadow Exposure: {result.get('cumulative_shadow_exposure_pct', 0)}%\n"
        f"Health: {result.get('shadow_health', 'WARNING')}"
    )


def format_orchestrator_message(result: Dict[str, Any]) -> str:
    return (
        "🛠 ORCHESTRATOR\n\n"
        f"System Health: {result.get('system_health_score', 0)}/100\n"
        f"Running Engines: {', '.join(result.get('running_engines', [])) or '-'}\n"
        f"Failed Engines: {', '.join(result.get('failed_engines', [])) or '-'}\n"
        f"Recovery Actions: {', '.join(result.get('recovery_actions', []))}\n"
        f"Scheduler Mode: {result.get('scheduler_mode', 'NORMAL')}"
    )


def _read_json_report(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nested_get(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current



def _format_percent(value: Any) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "0%"


def format_portfolio_risk_budget_summary(report: Dict[str, Any] | None = None) -> str:
    risk_budget = report if isinstance(report, dict) else _read_json_report("reports/portfolio_risk_budget.json")
    if not risk_budget:
        return (
            "📦 RISK BUDGET\n"
            "Exposure: 0%\n"
            "Concentration: UNKNOWN\n"
            "Recommendation: NORMAL"
        )
    return (
        "📦 RISK BUDGET\n"
        f"Exposure: {_format_percent(risk_budget.get('total_exposure', 0.0))}\n"
        f"Concentration: {str(risk_budget.get('concentration_label', 'UNKNOWN')).upper()}\n"
        f"Recommendation: {str(risk_budget.get('recommendation', 'NORMAL')).upper()}"
    )


def _derive_market_action(
    *,
    paper_only_status: str,
    early_warning_score: float,
    early_warning_label: str,
    brake_trigger_count: int,
    holding_candles_mean_after: float | None,
    collapse_timestamp: str | None,
) -> Dict[str, Any]:
    reasons = [
        f"Early Warning: score={early_warning_score:.2f}, label={early_warning_label}",
        f"Brake trigger count: {brake_trigger_count}",
        f"PAPER_ONLY enforced: {paper_only_status == 'PAPER_ONLY'}",
    ]
    if holding_candles_mean_after is not None:
        reasons.append(f"Holding compression mean(after): {holding_candles_mean_after:.2f}")
    if collapse_timestamp and collapse_timestamp != "-":
        reasons.append(f"Drift collapse timestamp: {collapse_timestamp}")

    label_upper = early_warning_label.upper()
    if paper_only_status != "PAPER_ONLY":
        return {"action": "DEFENSIVE / HOLD", "severity": "CRITICAL", "reasons": reasons}
    if label_upper == "BRAKE_CANDIDATE":
        return {"action": "DEFENSIVE / HOLD", "severity": "CRITICAL", "reasons": reasons}
    if label_upper == "RISK_ELEVATED":
        return {"action": "DEFENSIVE / HOLD", "severity": "WARNING", "reasons": reasons}
    if brake_trigger_count >= 50:
        return {"action": "DEFENSIVE / HOLD", "severity": "WARNING", "reasons": reasons}
    if holding_candles_mean_after is not None and holding_candles_mean_after < 10:
        return {"action": "HOLD", "severity": "WARNING", "reasons": reasons}
    if early_warning_score <= 30 and brake_trigger_count == 0:
        return {"action": "HOLD", "severity": "OK", "reasons": reasons}
    return {"action": "HOLD", "severity": "INFO", "reasons": reasons}


def _severity_emoji(severity: str) -> str:
    severity_upper = str(severity).upper()
    if severity_upper == "OK":
        return "🟢"
    if severity_upper in {"INFO", "OBSERVE"}:
        return "🔵"
    if severity_upper in {"WARNING", "RISK_ELEVATED"}:
        return "🟠"
    if severity_upper in {"CRITICAL", "BRAKE_CANDIDATE"}:
        return "🔴"
    return "🔵"


def _brake_display(brake_trigger_count: int) -> str:
    if brake_trigger_count == 0:
        return "OFF"
    if brake_trigger_count < 50:
        return "WATCH"
    return "ACTIVE / REVIEW"


def format_governance_intelligence_message() -> str:
    transition = _read_json_report("reports/transition_prediction_report.json")
    brake = _read_json_report("reports/emergency_brake_simulation.json")
    drift = _read_json_report("reports/drift_detection_report.json")
    risk_budget = _read_json_report("reports/portfolio_risk_budget.json")
    paper_only_status = "PAPER_ONLY"
    early_warning_score = float(
        _nested_get(transition, "latest_early_warning", "score", default=None)
        or transition.get("early_warning_score")
        or transition.get("warning_score")
        or 0.0
    )
    early_warning_label = str(
        _nested_get(transition, "latest_early_warning", "label", default=None)
        or transition.get("early_warning_label")
        or transition.get("warning_label")
        or "UNKNOWN"
    )
    brake_trigger_count = int(
        _nested_get(brake, "summary", "brake_trigger_count", default=None)
        or _nested_get(brake, "summary", "trigger_count", default=None)
        or brake.get("high_trigger_count")
        or brake.get("trigger_count")
        or 0
    )
    brake_source = str(
        brake.get("brake_source")
        or brake.get("source")
        or _nested_get(brake, "summary", "brake_source", default=None)
        or _nested_get(brake, "summary", "source", default=None)
        or ("SIMULATION_RESEARCH" if brake else "NONE")
    ).upper()
    brake_source_note = (
        "Brake source: simulation research / review required"
        if brake_trigger_count >= 50 and brake_source == "SIMULATION_RESEARCH"
        else f"Brake source: {brake_source.lower()}"
    )
    collapse_timestamp = str(
        _nested_get(drift, "collapse_risk", "collapse_timestamp", default=None)
        or drift.get("collapse_timestamp")
        or drift.get("drift_collapse_timestamp")
        or "-"
    )
    if not collapse_timestamp.strip():
        collapse_timestamp = "-"
    holding_candles_mean_after = _nested_get(drift, "holding_candles", "mean_after", default=None)
    if holding_candles_mean_after is not None:
        holding_candles_mean_after = float(holding_candles_mean_after)
    current_regime = str(
        _nested_get(transition, "latest_early_warning", "regime_name", default=None)
        or transition.get("current_regime")
        or transition.get("regime_name")
        or _nested_get(drift, "current_regime", default=None)
        or "UNKNOWN"
    )
    if not current_regime.strip():
        current_regime = "UNKNOWN"

    decision = _derive_market_action(
        paper_only_status=paper_only_status,
        early_warning_score=early_warning_score,
        early_warning_label=early_warning_label,
        brake_trigger_count=brake_trigger_count,
        holding_candles_mean_after=holding_candles_mean_after,
        collapse_timestamp=collapse_timestamp,
    )
    severity = str(decision.get("severity", "INFO")).upper()
    severity_emoji = _severity_emoji(severity)
    emergency_brake = _brake_display(brake_trigger_count)
    reasons = "\n".join(f"- {reason}" for reason in decision["reasons"])
    transition_status = "loaded" if transition else "missing"
    brake_status = "loaded" if brake else "missing"
    drift_status = "loaded" if drift else "missing"
    risk_budget_status = "loaded" if risk_budget else "missing"
    risk_budget_summary = format_portfolio_risk_budget_summary(risk_budget)

    return (
        "🛡 GOVERNANCE INTELLIGENCE\n\n"
        "PAPER_ONLY: ACTIVE\n"
        f"{severity_emoji} Severity: {severity}\n"
        f"ACTION: {decision['action']}\n"
        f"Current Regime: {current_regime}\n"
        f"Early Warning: {early_warning_score:.2f} ({early_warning_label})\n"
        f"Emergency Brake: {emergency_brake} (trigger_count={brake_trigger_count})\n"
        f"{brake_source_note}\n"
        f"Drift Collapse: {collapse_timestamp}\n"
        f"Report Health: transition {transition_status}, brake {brake_status}, drift {drift_status}, risk_budget {risk_budget_status}\n"
        f"{risk_budget_summary}\n"
        "Reason:\n"
        f"{reasons}\n"
        "Reminder: read-only governance signal, not live trading command.\n\n"
        "Governance: PAPER_ONLY, read-only, no live trading."
    )



def format_promotion_scorecard_message(report: Dict[str, Any] | None = None) -> str:
    scorecard = report if isinstance(report, dict) else _read_json_report("reports/promotion_scorecard.json")
    candidates = scorecard.get("candidates", []) if isinstance(scorecard, dict) else []
    summary = scorecard.get("summary", {}) if isinstance(scorecard, dict) else {}
    top = candidates[0] if candidates else {}
    return (
        "🏆 PROMOTION SCORECARD\n\n"
        f"Top Candidate: {top.get('strategy_setup_name', summary.get('top_candidate', '-'))}\n"
        f"Readiness: {top.get('recommendation', summary.get('top_recommendation', 'HOLD'))}\n"
        f"Governance: {top.get('governance_compatibility', 'PASS' if summary.get('governance_status') == 'SAFE' else 'WATCH')}\n"
        f"Risk Budget Override: {top.get('risk_budget_override', summary.get('risk_budget_override', 'INACTIVE'))}\n"
        f"Drift: {top.get('drift_risk', summary.get('drift_label', 'UNKNOWN'))}\n"
        "Mode: PAPER_ONLY read-only, no auto deployment."
    )


PAPER_CLOSED_STATUSES = {
    "CLOSED",
    "WIN",
    "LOSS",
    "STOP_LOSS",
    "TAKE_PROFIT",
    "TRADE CLOSED",
    "TP2 HIT",
    "SL HIT",
    "STOP LOSS",
    "TAKE PROFIT",
    "EXPIRED",
}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalized_status_counts(payload: Any) -> Dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    counts: Dict[str, int] = {}
    for status, count in payload.items():
        normalized = str(status or "OPEN").strip().upper() or "OPEN"
        counts[normalized] = counts.get(normalized, 0) + _safe_int(count, 0)
    return counts


def _active_status_counts_from_lifecycle(lifecycle: Dict[str, Any]) -> Dict[str, int]:
    active_counts = _normalized_status_counts(lifecycle.get("active_status_counts"))
    if active_counts:
        return active_counts

    status_counts = _normalized_status_counts(lifecycle.get("status_counts"))
    return {status: count for status, count in status_counts.items() if status not in PAPER_CLOSED_STATUSES and count > 0}


def _readiness_paper_trade_counts(readiness: Dict[str, Any]) -> Dict[str, int]:
    progress = str(readiness.get("closed_paper_trades_progress", ""))
    progress_closed = None
    progress_target = None
    if "/" in progress:
        left, right = progress.split("/", 1)
        progress_closed = _safe_int(left.strip(), 0)
        progress_target = _safe_int(right.strip(), 100)

    closed_count = _safe_int(
        readiness.get("closed_paper_trades")
        if readiness.get("closed_paper_trades") is not None
        else readiness.get("closed_paper_trade_count"),
        progress_closed if progress_closed is not None else 0,
    )
    target = _safe_int(
        readiness.get("closed_paper_trades_target")
        or readiness.get("closed_paper_trade_target"),
        progress_target if progress_target is not None else 100,
    )
    trade_count = _safe_int(readiness.get("paper_trade_count"), 0)
    return {"closed_count": closed_count, "target": max(target, 1), "trade_count": trade_count}


def _paper_trade_counts_from_database(db_path: str | None = None) -> Dict[str, Any]:
    if not db_path:
        db_path = config.database_path or os.getenv("DATABASE_PATH", "mamuyy_hunter.db")

    if not db_path or not os.path.exists(db_path):
        return {"available": False}

    try:
        with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='internal_paper_trades'"
            ).fetchone()
            if table is None:
                return {"available": False}
            status_rows = connection.execute(
                """
                SELECT UPPER(COALESCE(NULLIF(TRIM(status), ''), 'OPEN')) AS status, COUNT(*) AS count
                FROM internal_paper_trades
                GROUP BY UPPER(COALESCE(NULLIF(TRIM(status), ''), 'OPEN'))
                """
            ).fetchall()
            status_counts = {str(status): int(count or 0) for status, count in status_rows}
            closed_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM internal_paper_trades
                    WHERE UPPER(COALESCE(NULLIF(TRIM(status), ''), 'OPEN')) = 'CLOSED'
                    """
                ).fetchone()[0]
                or 0
            )
    except sqlite3.Error:
        return {"available": False}

    active_status_counts = {
        status: count
        for status, count in status_counts.items()
        if status != "CLOSED" and count > 0
    }
    return {
        "available": True,
        "closed_count": closed_count,
        "active_count": sum(active_status_counts.values()),
        "active_status_counts": active_status_counts,
        "status_counts": status_counts,
        "trade_count": sum(status_counts.values()),
    }


def _lifecycle_paper_trade_counts(lifecycle: Dict[str, Any]) -> Dict[str, Any]:
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    diagnostics = lifecycle.get("diagnostics") if isinstance(lifecycle.get("diagnostics"), dict) else {}

    active_status_counts = _active_status_counts_from_lifecycle(lifecycle)
    if not active_status_counts and diagnostics:
        active_status_counts = _active_status_counts_from_lifecycle(diagnostics)

    closed_count = _safe_int(lifecycle.get("current_closed_count"), 0)
    if closed_count == 0 and diagnostics:
        closed_count = _safe_int(diagnostics.get("current_closed_count"), 0)
    progress = str(lifecycle.get("progress") or diagnostics.get("progress") or "")
    progress_target = None
    if "/" in progress:
        left, right = progress.split("/", 1)
        closed_count = max(closed_count, _safe_int(left.strip(), 0))
        progress_target = _safe_int(right.strip(), 100)

    target = _safe_int(lifecycle.get("target_closed_count"), progress_target if progress_target is not None else 100)
    if target <= 0 and diagnostics:
        target = _safe_int(diagnostics.get("target_closed_count"), progress_target if progress_target is not None else 100)

    active_count = _safe_int(lifecycle.get("active_count"), 0)
    if active_count == 0 and diagnostics:
        active_count = _safe_int(diagnostics.get("active_count"), 0)
    if active_count == 0 and active_status_counts:
        active_count = sum(active_status_counts.values())

    inserted_open_trades = _safe_int(lifecycle.get("inserted_open_trades"), 0)
    metrics = lifecycle.get("metrics") if isinstance(lifecycle.get("metrics"), dict) else {}
    trade_count = _safe_int(lifecycle.get("trade_count") or metrics.get("trade_count"), 0)

    if active_count == 0:
        active_count = _safe_int(lifecycle.get("open_count") or metrics.get("open_count"), inserted_open_trades)

    return {
        "closed_count": closed_count,
        "active_count": active_count,
        "active_status_counts": active_status_counts,
        "target": max(target, 1),
        "trade_count": trade_count,
        "inserted_open_trades": inserted_open_trades,
    }


def get_paper_trade_progress_for_telegram(
    readiness: Dict[str, Any] | None = None,
    db_path: str | None = None,
    lifecycle: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return read-only paper trade progress for Telegram Phase 3 readiness.

    Source priority is database internal_paper_trades first, then Phase 3 readiness,
    then paper trade lifecycle only as a fallback.  Lifecycle values are never allowed
    to reset a higher database/readiness closed count to zero.
    """
    readiness = readiness if isinstance(readiness, dict) else _read_json_report("reports/phase3_readiness.json")
    readiness_counts = _readiness_paper_trade_counts(readiness if isinstance(readiness, dict) else {})
    database_counts = _paper_trade_counts_from_database(db_path)
    lifecycle_counts = _lifecycle_paper_trade_counts(
        lifecycle if isinstance(lifecycle, dict) else _read_json_report("reports/paper_trade_lifecycle.json")
    )

    target = max(
        readiness_counts.get("target", 100),
        lifecycle_counts.get("target", 100),
        1,
    )

    closed_candidates = [readiness_counts.get("closed_count", 0)]
    if database_counts.get("available"):
        closed_candidates.insert(0, database_counts.get("closed_count", 0))
    elif lifecycle_counts.get("closed_count", 0) > 0:
        closed_candidates.append(lifecycle_counts.get("closed_count", 0))
    closed_count = max(_safe_int(candidate, 0) for candidate in closed_candidates)

    trade_count = 0
    active_status_counts: Dict[str, int] = {}
    source = "readiness"
    if database_counts.get("available"):
        active_count = _safe_int(database_counts.get("active_count"), 0)
        active_status_counts = database_counts.get("active_status_counts", {}) or {}
        trade_count = _safe_int(database_counts.get("trade_count"), 0)
        source = "database"
    else:
        lifecycle_active = _safe_int(lifecycle_counts.get("active_count"), 0)
        lifecycle_trade_count = _safe_int(lifecycle_counts.get("trade_count"), 0)
        lifecycle_closed = _safe_int(lifecycle_counts.get("closed_count"), 0)
        if lifecycle_active > 0 and lifecycle_closed <= closed_count and lifecycle_trade_count >= lifecycle_active + lifecycle_closed:
            active_count = lifecycle_active
            active_status_counts = lifecycle_counts.get("active_status_counts", {}) or {}
            trade_count = lifecycle_trade_count
            source = "lifecycle"
        else:
            trade_count = max(
                readiness_counts.get("trade_count", 0),
                lifecycle_trade_count,
                closed_count + lifecycle_active,
            )
            active_count = max(trade_count - closed_count, 0)
            source = "readiness"

    if active_count == 0 and trade_count > closed_count:
        active_count = trade_count - closed_count

    return {
        "closed_count": closed_count,
        "active_count": active_count,
        "active_status_counts": active_status_counts,
        "target": target,
        "trade_count": trade_count,
        "source": source,
    }


def _resolve_paper_trade_progress(readiness: Dict[str, Any]) -> Dict[str, Any]:
    return get_paper_trade_progress_for_telegram(readiness)


def format_phase3_readiness_message(report: Dict[str, Any] | None = None) -> str:
    readiness = report if isinstance(report, dict) else _read_json_report("reports/phase3_readiness.json")
    blockers = readiness.get("blockers", []) if isinstance(readiness, dict) else []
    top_blocker = readiness.get("top_blocker") or (blockers[0] if blockers else "none")
    try:
        readiness_percent = float(readiness.get("readiness_percent", 0.0))
    except (TypeError, ValueError):
        readiness_percent = 0.0

    paper_progress = get_paper_trade_progress_for_telegram(readiness)
    closed_count = paper_progress["closed_count"]
    active_count = paper_progress["active_count"]
    target = paper_progress["target"]
    progress_percent = min((closed_count / target) * 100.0, 100.0)
    status = str(readiness.get("status", "LOCKED")).upper()

    return (
        "🚦 PHASE 3 READINESS\n"
        f"Readiness: {readiness_percent:.0f}%\n"
        f"Status: {status}\n"
        f"Paper Trades: {closed_count}/{target} closed\n"
        f"Active Paper Trades: {active_count}\n"
        f"Paper Progress: {progress_percent:.0f}%\n"
        f"Top Blocker: {top_blocker}\n"
        "PAPER_ONLY remains active."
    )


def format_governance_audit_message(report: Dict[str, Any] | None = None) -> str:
    audit = report if isinstance(report, dict) else _read_json_report("reports/governance_audit.json")
    conflicts = audit.get("conflicts", []) if isinstance(audit, dict) else []
    violations = audit.get("policy_violations", []) if isinstance(audit, dict) else []
    return (
        "🧠 GOVERNANCE AUDIT\n\n"
        f"Consistency: {int(audit.get('consistency_score', 0))}%\n"
        f"Conflicts: {len(conflicts)}\n"
        f"Governance State: {audit.get('governance_state', 'UNKNOWN')}\n"
        f"Violations: {'none' if not violations else len(violations)}\n"
        f"Status: {audit.get('governance_health', 'UNKNOWN')}"
    )

def send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str,
    timeout: int = 15,
) -> bool:
    if not bot_token or not chat_id:
        print("Telegram token/chat_id belum diisi. Alert tidak dikirim.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, data=payload, timeout=timeout)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Gagal mengirim Telegram alert: {exc}")
        return False
