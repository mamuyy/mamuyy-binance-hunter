import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pandas as pd

from config import config
from health_guardian import resolve_runtime_heartbeat
from portfolio_analytics import load_portfolio_trades
from telegram_notifier import send_or_preview


ANOMALY_CSV_PATH = "logs/anomaly_report.csv"
INCIDENT_MD_PATH = "logs/incident_report.md"
INCIDENT_JSON_PATH = "logs/incident_report.json"
CRITICAL_COOLDOWN_MINUTES = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_minutes(value: Any) -> float:
    timestamp = _parse_timestamp(value)
    if not timestamp:
        return 9999.0
    return round((datetime.now(timezone.utc) - timestamp).total_seconds() / 60, 2)


def _severity(score: float) -> str:
    if score >= 80:
        return "CRITICAL"
    if score >= 45:
        return "WARNING"
    return "INFO"


def _anomaly(anomaly_type: str, score: float, reason: str, recommended_action: str) -> Dict[str, Any]:
    score = round(max(0.0, min(100.0, float(score))), 2)
    return {
        "timestamp": _now(),
        "anomaly_type": anomaly_type,
        "severity": _severity(score),
        "score": score,
        "reason": reason,
        "recommended_action": recommended_action,
    }


def _read_only_connection(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _read_table(db_path: str, table: str, limit: int = 500) -> pd.DataFrame:
    try:
        with _read_only_connection(db_path) as connection:
            if not _table_exists(connection, table):
                return pd.DataFrame()
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _profit_factor(pnl: pd.Series) -> float:
    values = pd.to_numeric(pnl, errors="coerce").fillna(0.0)
    gross_profit = float(values[values > 0].sum())
    gross_loss = abs(float(values[values < 0].sum()))
    if gross_loss == 0:
        return 999.0 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _drawdown(pnl: pd.Series) -> pd.Series:
    values = pd.to_numeric(pnl, errors="coerce").fillna(0.0)
    equity = values.cumsum()
    return equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)


def _load_outcome_trades(db_path: str) -> pd.DataFrame:
    outcomes = _read_table(db_path, "historical_outcomes", limit=20000)
    if outcomes.empty or "pnl_pct" not in outcomes.columns:
        return pd.DataFrame()
    timestamp_column = "signal_timestamp" if "signal_timestamp" in outcomes.columns else "timestamp"
    df = pd.DataFrame()
    df["timestamp"] = pd.to_datetime(outcomes.get(timestamp_column), errors="coerce", utc=True)
    df["symbol"] = outcomes.get("symbol", pd.Series(["UNKNOWN"] * len(outcomes))).fillna("UNKNOWN").astype(str)
    df["pnl"] = pd.to_numeric(outcomes["pnl_pct"], errors="coerce").fillna(0.0)
    df["source"] = "historical_outcomes"
    return df.dropna(subset=["pnl"])


def _load_trade_dataset(db_path: str) -> pd.DataFrame:
    trades, _warnings = load_portfolio_trades(db_path)
    if not trades.empty:
        keep = [column for column in ["timestamp", "symbol", "pnl", "source"] if column in trades.columns]
        return trades[keep].copy()
    return _load_outcome_trades(db_path)


def _latest_csv_row(path: str) -> Dict[str, Any]:
    df = _read_csv(path)
    if df.empty:
        return {}
    return df.iloc[-1].to_dict()


def _detect_performance_anomalies(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    if trades.empty or "pnl" not in trades.columns:
        return [
            _anomaly(
                "paper_performance_data_missing",
                30,
                "No paper/historical outcome PnL data available for anomaly baseline.",
                "Run paper engine or historical labeling before judging performance degradation.",
            )
        ]

    trades = trades.copy()
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0.0)
    trades = trades.sort_values("timestamp", na_position="last").reset_index(drop=True)
    recent = trades.tail(min(25, max(5, len(trades) // 5)))
    previous = trades.iloc[:-len(recent)] if len(trades) > len(recent) else pd.DataFrame()

    full_pf = _profit_factor(trades["pnl"])
    recent_pf = _profit_factor(recent["pnl"])
    previous_pf = _profit_factor(previous["pnl"]) if not previous.empty else full_pf
    pf_drop = max(0.0, previous_pf - recent_pf)
    if len(recent) >= 5 and (recent_pf < 0.85 or pf_drop >= 0.35):
        anomalies.append(
            _anomaly(
                "profit_factor_drop",
                55 + min(40, pf_drop * 40) + (15 if recent_pf < 0.75 else 0),
                f"Recent PF={recent_pf:.2f}, baseline PF={previous_pf:.2f}, full PF={full_pf:.2f}.",
                "Keep PAPER_ONLY routing conservative and review recent symbol/regime contributors.",
            )
        )

    recent_sum = float(recent["pnl"].sum())
    recent_abs = float(recent["pnl"].abs().sum())
    if len(recent) >= 10 and recent_abs > 0 and abs(recent_sum) <= max(0.15, recent_abs * 0.08):
        anomalies.append(
            _anomaly(
                "equity_curve_flattening",
                48,
                f"Last {len(recent)} trades net PnL is {recent_sum:.4f} with {recent_abs:.4f} absolute movement.",
                "Monitor opportunity quality; avoid increasing exposure while edge is flat.",
            )
        )

    dd = _drawdown(trades["pnl"])
    recent_dd = float(dd.tail(len(recent)).min()) if not dd.empty else 0.0
    previous_dd = float(dd.iloc[:-len(recent)].min()) if len(dd) > len(recent) else 0.0
    dd_worsening = abs(min(0.0, recent_dd)) - abs(min(0.0, previous_dd))
    if recent_dd <= -10 or dd_worsening >= 5:
        anomalies.append(
            _anomaly(
                "drawdown_increase",
                55 + min(40, abs(recent_dd) * 1.5 + max(0.0, dd_worsening) * 3),
                f"Recent drawdown={recent_dd:.2f}, previous max drawdown={previous_dd:.2f}.",
                "Do not relax thresholds; review macro-adaptive and allocation filters before routing more signals.",
            )
        )
    return anomalies


def _detect_broadcast_anomalies(db_path: str) -> List[Dict[str, Any]]:
    broadcasts = _read_table(db_path, "broadcast_events", limit=200)
    if broadcasts.empty or "route_status" not in broadcasts.columns:
        return []
    status = broadcasts["route_status"].fillna("").astype(str).str.upper()
    rejected = status.isin(["REJECTED", "SKIPPED", "FAILED"])
    failure_rate = float(rejected.mean() * 100) if len(status) else 0.0
    if len(status) >= 10 and failure_rate >= 60:
        return [
            _anomaly(
                "excessive_broadcast_rejections",
                50 + min(45, failure_rate * 0.55),
                f"Broadcast rejection/skip/failure rate is {failure_rate:.1f}% over last {len(status)} events.",
                "Inspect Competition Control and Opportunity Allocation reasons before changing routing profiles.",
            )
        ]
    return []


def _detect_macro_anomalies() -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    cross = _latest_csv_row("logs/cross_market_intelligence.csv")
    stress = _num(cross.get("cross_market_stress_score"))
    if str(cross.get("macro_divergence", "")).lower() in {"true", "1", "yes"}:
        anomalies.append(
            _anomaly(
                "macro_divergence_spike",
                65 + min(25, stress * 0.25),
                f"Cross-market macro divergence is active; stress score={stress:.2f}.",
                "Keep macro-adaptive defense active and avoid aggressive broadcast profiles.",
            )
        )
    if stress >= 70 or str(cross.get("cross_market_state", "")).upper() in {"CROSS_MARKET_STRESS", "SAFE_HAVEN_ROTATION"}:
        anomalies.append(
            _anomaly(
                "cross_market_stress_spike",
                70 + min(25, max(0.0, stress - 70) * 0.8),
                f"Cross-market state={cross.get('cross_market_state', 'UNKNOWN')} with stress={stress:.2f}.",
                "Use defensive/observe-only routing until stress normalizes.",
            )
        )
    daily = _read_json("logs/daily_ops_report.json")
    macro_state = str(daily.get("macro_state", "")).upper()
    if macro_state in {"HIGH_STRESS", "PANIC"}:
        anomalies.append(
            _anomaly(
                "macro_state_stress",
                72 if macro_state == "HIGH_STRESS" else 88,
                f"Daily Ops latest macro state is {macro_state}.",
                "Keep PAPER_ONLY conservative and review macro observer components.",
            )
        )
    return anomalies


def _detect_strategy_anomalies() -> List[Dict[str, Any]]:
    results = _read_csv("logs/strategy_genome_results.csv")
    if results.empty or "status" not in results.columns:
        return []
    status = results["status"].fillna("").astype(str).str.upper()
    rejected = int((status == "REJECTED").sum())
    total = int(len(status))
    rejected_rate = rejected / total * 100 if total else 0.0
    low_stability = 0.0
    if "stability_score" in results.columns:
        low_stability = float((pd.to_numeric(results["stability_score"], errors="coerce").fillna(0) < 40).mean() * 100)
    if total >= 5 and (rejected_rate >= 55 or low_stability >= 50):
        return [
            _anomaly(
                "strategy_mutation_failures",
                45 + min(45, max(rejected_rate, low_stability) * 0.6),
                f"Rejected strategies={rejected_rate:.1f}%, low stability strategies={low_stability:.1f}%.",
                "Archive weak mutations and use manual review before promoting any new candidate.",
            )
        ]
    return []


def _detect_runtime_anomalies(db_path: str) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    heartbeat = resolve_runtime_heartbeat(db_path, "orchestrator_log.csv", config.health_guardian_stale_minutes)
    heartbeat_age = _num(heartbeat.get("age_minutes"), 9999.0)
    if heartbeat_age > config.health_guardian_stale_minutes:
        anomalies.append(
            _anomaly(
                "stale_runtime_heartbeat",
                85 if heartbeat_age > config.health_guardian_stale_minutes * 3 else 65,
                f"Heartbeat age={heartbeat_age:.2f}m via {heartbeat.get('source', '-')}.",
                "Run python main.py --health-guardian-once and inspect hunter tmux session.",
            )
        )

    risks = _read_table(db_path, "risk_events", limit=200)
    if not risks.empty:
        timestamps = pd.to_datetime(risks.get("timestamp"), errors="coerce", utc=True)
        recent = risks[timestamps >= (datetime.now(timezone.utc) - timedelta(hours=24))]
        actions = recent.get("action", pd.Series(dtype=str)).fillna("").astype(str).str.lower()
        recoveries = int(actions.str.contains("start_tmux_session").sum())
        halts = int(recent.get("status", pd.Series(dtype=str)).fillna("").astype(str).str.upper().eq("HALT").sum())
        if recoveries >= 2 or halts >= 3:
            anomalies.append(
                _anomaly(
                    "repeated_guardian_recoveries",
                    58 + min(35, recoveries * 12 + halts * 8),
                    f"Last 24h guardian recovery actions={recoveries}, HALT events={halts}.",
                    "Inspect VPS memory, tmux sessions, and orchestrator logs before leaving system unattended.",
                )
            )
    return anomalies


def _detect_notification_anomalies(db_path: str) -> List[Dict[str, Any]]:
    telegram = _read_table(db_path, "telegram_events", limit=50)
    if telegram.empty or "send_status" not in telegram.columns:
        return []
    status = telegram["send_status"].fillna("").astype(str).str.upper()
    failures = status.eq("FAILED")
    if len(status) >= 5 and failures.mean() >= 0.25:
        return [
            _anomaly(
                "telegram_notification_failures",
                45 + min(45, float(failures.mean() * 100)),
                f"Telegram failed {int(failures.sum())}/{len(status)} recent events.",
                "Verify TELEGRAM_ENABLED, token/chat id, and VPS outbound network without exposing secrets.",
            )
        ]
    return []


def _detect_signal_anomalies(db_path: str) -> List[Dict[str, Any]]:
    signals = _read_table(db_path, "signals", limit=500)
    if signals.empty or "timestamp" not in signals.columns:
        return [
            _anomaly(
                "signal_generation_missing",
                55,
                "No recent signal rows are available in SQLite.",
                "Check scanner/orchestrator status and Binance public API connectivity.",
            )
        ]
    latest_age = _age_minutes(signals.iloc[0].get("timestamp"))
    if latest_age > 180:
        return [
            _anomaly(
                "signal_generation_drop",
                60 + min(35, (latest_age - 180) / 20),
                f"Latest signal age is {latest_age:.1f} minutes.",
                "Inspect scanner cycle, Binance API availability, and signal filters.",
            )
        ]
    return []


def _detect_walkforward_anomalies(db_path: str) -> List[Dict[str, Any]]:
    walkforward = _read_table(db_path, "walkforward_results", limit=50)
    if walkforward.empty:
        walkforward = _read_csv("walkforward_results.csv")
    if walkforward.empty:
        return []
    anomalies: List[Dict[str, Any]] = []
    accuracy_col = "test_accuracy" if "test_accuracy" in walkforward.columns else "average_accuracy"
    if accuracy_col in walkforward.columns:
        accuracy = pd.to_numeric(walkforward[accuracy_col], errors="coerce").dropna()
        if not accuracy.empty:
            recent_acc = float(accuracy.head(min(5, len(accuracy))).mean())
            baseline_acc = float(accuracy.mean())
            if recent_acc < 0.45 or baseline_acc - recent_acc > 0.08:
                anomalies.append(
                    _anomaly(
                        "walkforward_degradation",
                        55 + min(40, max(0.45 - recent_acc, baseline_acc - recent_acc) * 200),
                        f"Recent walkforward accuracy={recent_acc:.3f}, baseline={baseline_acc:.3f}.",
                        "Run python main.py --retrain-model and review model registry warnings.",
                    )
                )
    if "profit_factor" in walkforward.columns:
        pf = pd.to_numeric(walkforward["profit_factor"], errors="coerce").dropna()
        if not pf.empty and float(pf.head(min(5, len(pf))).mean()) < 1.0:
            anomalies.append(
                _anomaly(
                    "walkforward_profit_factor_weak",
                    58,
                    f"Recent walkforward PF average={float(pf.head(min(5, len(pf))).mean()):.2f}.",
                    "Keep strategy research in PAPER_ONLY mode and avoid promoting weak candidates.",
                )
            )
    return anomalies


def _severity_summary(anomalies: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"INFO": 0, "WARNING": 0, "CRITICAL": 0}
    for item in anomalies:
        severity = str(item.get("severity", "INFO")).upper()
        summary[severity] = summary.get(severity, 0) + 1
    return summary


def _recommended_next_action(anomalies: List[Dict[str, Any]]) -> str:
    critical = [item for item in anomalies if item.get("severity") == "CRITICAL"]
    if critical:
        return str(critical[0].get("recommended_action"))
    warning = [item for item in anomalies if item.get("severity") == "WARNING"]
    if warning:
        return str(warning[0].get("recommended_action"))
    return "No urgent action. Continue PAPER_ONLY monitoring and daily data collection."


def _cooldown_active(db_path: str, event_type: str = "INCIDENT_CRITICAL") -> bool:
    telegram = _read_table(db_path, "telegram_events", limit=100)
    if telegram.empty or "event_type" not in telegram.columns or "timestamp" not in telegram.columns:
        return False
    rows = telegram[telegram["event_type"].fillna("").astype(str).eq(event_type)]
    if rows.empty:
        return False
    latest_age = _age_minutes(rows.iloc[0].get("timestamp"))
    return latest_age < CRITICAL_COOLDOWN_MINUTES


def _critical_message(anomalies: List[Dict[str, Any]]) -> str:
    critical = [item for item in anomalies if item.get("severity") == "CRITICAL"]
    lines = [
        "MAMUYY HUNTER INCIDENT",
        "PAPER_ONLY",
        "",
        f"Critical Incidents: {len(critical)}",
    ]
    for item in critical[:4]:
        lines.append(f"- {item.get('anomaly_type')}: {item.get('score')} | {item.get('reason')}")
    lines.append("")
    lines.append(f"Action: {_recommended_next_action(anomalies)}")
    return "\n".join(lines)


def _write_outputs(
    anomalies: List[Dict[str, Any]],
    anomaly_path: str,
    markdown_path: str,
    json_path: str,
    telegram_result: Dict[str, Any],
) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(anomaly_path) or ".", exist_ok=True)
    df = pd.DataFrame(anomalies)
    if df.empty:
        df = pd.DataFrame(
            [
                _anomaly(
                    "no_active_incidents",
                    10,
                    "No active anomaly detected from available telemetry.",
                    "Continue monitoring.",
                )
            ]
        )
    df.to_csv(anomaly_path, index=False)

    severity_summary = _severity_summary(df.to_dict("records"))
    incident = {
        "timestamp": _now(),
        "paper_only": True,
        "active_incident_count": int(len(df[df["severity"].isin(["WARNING", "CRITICAL"])])),
        "critical_count": severity_summary.get("CRITICAL", 0),
        "warning_count": severity_summary.get("WARNING", 0),
        "info_count": severity_summary.get("INFO", 0),
        "severity_distribution": severity_summary,
        "top_recurring_incidents": df["anomaly_type"].value_counts().head(10).to_dict(),
        "recommended_operator_action": _recommended_next_action(df.to_dict("records")),
        "telegram_result": telegram_result,
        "anomalies": df.to_dict("records"),
    }
    markdown_lines = [
        "# MAMUYY Hunter Incident Report",
        "",
        f"- Generated: `{incident['timestamp']}`",
        "- PAPER_ONLY: `True`",
        f"- Active Incidents: `{incident['active_incident_count']}`",
        f"- Critical: `{incident['critical_count']}`",
        f"- Warning: `{incident['warning_count']}`",
        "",
        "## Active Incidents",
    ]
    for item in incident["anomalies"]:
        markdown_lines.append(
            f"- `{item['severity']}` `{item['anomaly_type']}` score={item['score']}: {item['reason']}"
        )
    markdown_lines.extend(["", "## Recommended Operator Action", incident["recommended_operator_action"]])
    with open(markdown_path, "w", encoding="utf-8") as output_file:
        output_file.write("\n".join(markdown_lines) + "\n")
    with open(json_path, "w", encoding="utf-8") as output_file:
        json.dump(incident, output_file, indent=2, default=str)
    return incident


def run_anomaly_scan(
    db_path: str = "mamuyy_hunter.db",
    anomaly_path: str = ANOMALY_CSV_PATH,
    markdown_path: str = INCIDENT_MD_PATH,
    json_path: str = INCIDENT_JSON_PATH,
    notify_critical: bool = True,
) -> Dict[str, Any]:
    anomalies: List[Dict[str, Any]] = []
    trades = _load_trade_dataset(db_path)
    detectors = [
        lambda: _detect_performance_anomalies(trades),
        lambda: _detect_broadcast_anomalies(db_path),
        _detect_macro_anomalies,
        _detect_strategy_anomalies,
        lambda: _detect_runtime_anomalies(db_path),
        lambda: _detect_notification_anomalies(db_path),
        lambda: _detect_signal_anomalies(db_path),
        lambda: _detect_walkforward_anomalies(db_path),
    ]
    for detector in detectors:
        try:
            anomalies.extend(detector())
        except Exception as exc:
            anomalies.append(
                _anomaly(
                    "anomaly_detector_component_error",
                    45,
                    f"Detector failed gracefully: {exc}",
                    "Inspect anomaly_detector.py logs and keep system in PAPER_ONLY mode.",
                )
            )

    telegram_result = {"send_status": "SKIPPED", "enabled": config.telegram_enabled, "error_message": ""}
    if notify_critical and any(item.get("severity") == "CRITICAL" for item in anomalies):
        if _cooldown_active(db_path):
            telegram_result = {
                "send_status": "COOLDOWN",
                "enabled": config.telegram_enabled,
                "error_message": f"Critical incident notification cooldown {CRITICAL_COOLDOWN_MINUTES}m active",
            }
        else:
            telegram_result = send_or_preview("INCIDENT_CRITICAL", _critical_message(anomalies), db_path)

    incident = _write_outputs(anomalies, anomaly_path, markdown_path, json_path, telegram_result)
    return {
        "ok": True,
        "anomaly_report_path": anomaly_path,
        "incident_report_md_path": markdown_path,
        "incident_report_json_path": json_path,
        **incident,
    }


def format_anomaly_scan(result: Dict[str, Any]) -> str:
    distribution = result.get("severity_distribution", {})
    top = result.get("anomalies", [])[:6]
    lines = [
        "INCIDENT & ANOMALY INTELLIGENCE",
        "PAPER_ONLY: True",
        f"Active Incidents: {result.get('active_incident_count', 0)}",
        f"Severity: INFO={distribution.get('INFO', 0)} WARNING={distribution.get('WARNING', 0)} CRITICAL={distribution.get('CRITICAL', 0)}",
        f"Telegram: {result.get('telegram_result', {}).get('send_status', 'SKIPPED')}",
        "Top Anomalies:",
    ]
    for item in top:
        lines.append(
            f"- {item.get('severity')} {item.get('anomaly_type')} score={item.get('score')}: {item.get('reason')}"
        )
    lines.extend(
        [
            f"Next Action: {result.get('recommended_operator_action')}",
            f"CSV: {result.get('anomaly_report_path')}",
            f"Markdown: {result.get('incident_report_md_path')}",
            f"JSON: {result.get('incident_report_json_path')}",
        ]
    )
    return "\n".join(lines)


def load_incident_report(path: str = INCIDENT_JSON_PATH) -> Dict[str, Any]:
    return _read_json(path)
