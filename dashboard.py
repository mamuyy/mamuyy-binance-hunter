import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from config import config
from database import (
    db_health_check,
    init_db,
)
from governance_audit import run_governance_audit
from portfolio_analytics import calculate_portfolio_analytics
from portfolio_observer import observe_portfolio
from portfolio_risk_budget import calculate_portfolio_risk_budget
from phase3_readiness import calculate_phase3_readiness
from promotion_scorecard import generate_promotion_scorecard
from risk_manager import RiskConfig, check_execution_safety


DB_PATH = config.database_url or config.database_path
REFRESH_SECONDS = 60
TRANSITION_WARNING_TAIL_ROWS = 500
EMERGENCY_BRAKE_EVENTS_TAIL_ROWS = 200
DRIFT_ROLLING_METRICS_DEFAULT_ROWS = 0
DATABASE_ANALYTICS_TTL_SECONDS = 300
DATABASE_ANALYTICS_DISPLAY_ROWS = 50
DATABASE_ANALYTICS_SESSION_KEY = "database_analytics_cached_result"


st.set_page_config(
    page_title="MAMUYY Binance Hunter Dashboard",
    page_icon="📡",
    layout="wide",
)


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame()


def _connect() -> sqlite3.Connection:
    init_db(DB_PATH)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@st.cache_data(ttl=REFRESH_SECONDS)
def read_table(table: str, limit: int = 500) -> pd.DataFrame:
    try:
        with _connect() as connection:
            df = pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        return df
    except Exception:
        return _empty_df()


@st.cache_data(ttl=REFRESH_SECONDS)
def table_counts() -> dict[str, int]:
    tables = [
        "signals",
        "paper_trades",
        "flow_logs",
        "regime_logs",
        "ml_results",
        "walkforward_results",
        "historical_outcomes",
        "internal_paper_trades",
        "broadcast_events",
        "telegram_events",
    ]
    counts = {}
    try:
        with _connect() as connection:
            for table in tables:
                counts[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
    except Exception:
        return {table: 0 for table in tables}
    return counts


def risk_config_from_env() -> RiskConfig:
    return RiskConfig(
        ml_accuracy_halt=config.risk_ml_accuracy_halt,
        drawdown_halt=config.risk_drawdown_halt,
        drawdown_watch=config.risk_drawdown_watch,
        stale_minutes=config.risk_stale_minutes,
        max_open_trades=config.risk_max_open_trades,
        loss_cooldown=config.risk_loss_cooldown,
        base_position_multiplier=config.risk_base_position_multiplier,
        high_vol_confidence_min=config.risk_high_vol_confidence_min,
    )


@st.cache_data(ttl=REFRESH_SECONDS)
def read_risk_status() -> dict[str, Any]:
    try:
        return check_execution_safety(
            db_path=config.database_path,
            orchestrator_log_path="orchestrator_log.csv",
            model_output_path="model_output.json",
            config=risk_config_from_env(),
            log_event=False,
        )
    except Exception as exc:
        return {
            "safe": False,
            "status": "WATCH",
            "reasons": [f"Risk engine unavailable: {exc}"],
            "position_multiplier": 0.0,
            "risk_score": 0,
            "metrics": {},
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_portfolio_observability() -> dict[str, Any]:
    try:
        return observe_portfolio(DB_PATH)
    except Exception as exc:
        return {
            "ok": False,
            "source": "unavailable",
            "portfolio_heat": "HIGH",
            "portfolio_heat_score": 100,
            "concentration_risk": 0,
            "symbol_exposure": [],
            "regime_exposure": [],
            "market_type_exposure": [],
            "top_correlated_symbols": [],
            "warnings": [f"Portfolio observer unavailable: {exc}"],
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_portfolio_risk_budget() -> dict[str, Any]:
    try:
        return calculate_portfolio_risk_budget(
            config.database_path,
            output_path="reports/portfolio_risk_budget.json",
            write_report=True,
        )
    except Exception as exc:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_only": True,
            "governance_layer": "PAPER_ONLY_READ_ONLY_PORTFOLIO_RISK_BUDGET",
            "execution_enabled": False,
            "live_trading_enabled": False,
            "regime": "UNKNOWN",
            "total_exposure": 0.0,
            "max_allowed_exposure": 0.0,
            "utilization_ratio": 0.0,
            "risk_budget_utilization": 0.0,
            "concentration_score": 0.0,
            "concentration_label": "UNKNOWN",
            "diversification_score": 0.0,
            "recommendation": "NORMAL",
            "symbol_breakdown": [],
            "exposure_by_regime": [],
            "warnings": [f"Portfolio risk budget unavailable: {exc}"],
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_portfolio_analytics() -> dict[str, Any]:
    try:
        return calculate_portfolio_analytics(config.database_path)
    except Exception as exc:
        return {
            "ok": False,
            "warnings": [f"Portfolio analytics unavailable: {exc}"],
            "metrics": {
                "trade_count": 0,
                "total_pnl": 0.0,
                "winrate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "average_trade_pnl": 0.0,
            },
            "trades": _empty_df(),
            "equity_curve": _empty_df(),
            "macro_performance": _empty_df(),
            "competition_performance": _empty_df(),
            "macro_survival": _empty_df(),
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_opportunity_allocation(path: str = "logs/opportunity_allocation.csv") -> pd.DataFrame:
    exists = os.path.exists(path)
    try:
        if exists:
            df = pd.read_csv(path)
            df.attrs["file_exists"] = True
            return df
    except Exception:
        df = _empty_df()
        df.attrs["file_exists"] = exists
        return df
    df = _empty_df()
    df.attrs["file_exists"] = False
    return df


@st.cache_data(ttl=REFRESH_SECONDS)
def read_model_registry(path: str = "model_registry.json") -> dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as registry_file:
                return json.load(registry_file)
    except Exception:
        return {}
    return {}


@st.cache_data(ttl=REFRESH_SECONDS)
def read_webhook_payload(path: str = "logs/webhook_test_payload.json") -> dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as payload_file:
                return json.load(payload_file)
    except Exception:
        return {}
    return {}


@st.cache_data(ttl=REFRESH_SECONDS)
def read_macro_observer(path: str = "logs/macro_observer.csv") -> tuple[pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(path):
        return _empty_df(), _empty_df()
    try:
        df = pd.read_csv(path)
    except Exception:
        return _empty_df(), _empty_df()
    if df.empty:
        return df, _empty_df()
    components = _empty_df()
    try:
        components = pd.DataFrame(json.loads(str(df.iloc[-1].get("components_json") or "[]")))
    except Exception:
        components = _empty_df()
    return df, components


@st.cache_data(ttl=REFRESH_SECONDS)
def read_cross_market(path: str = "logs/cross_market_intelligence.csv") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(path):
        return _empty_df(), _empty_df(), _empty_df()
    try:
        df = pd.read_csv(path)
    except Exception:
        return _empty_df(), _empty_df(), _empty_df()
    if df.empty:
        return df, _empty_df(), _empty_df()
    try:
        components = pd.DataFrame(json.loads(str(df.iloc[-1].get("components_json") or "[]")))
    except Exception:
        components = _empty_df()
    try:
        correlation = pd.DataFrame(json.loads(str(df.iloc[-1].get("correlation_matrix_json") or "[]")))
    except Exception:
        correlation = _empty_df()
    return df, components, correlation


@st.cache_data(ttl=REFRESH_SECONDS)
def read_strategy_genome(
    results_path: str = "logs/strategy_genome_results.csv",
    archive_path: str = "logs/strategy_genome_archive.csv",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    results = _empty_df()
    archive = _empty_df()
    try:
        if os.path.exists(results_path):
            results = pd.read_csv(results_path)
    except Exception:
        results = _empty_df()
    try:
        if os.path.exists(archive_path):
            archive = pd.read_csv(archive_path)
    except Exception:
        archive = _empty_df()
    return results, archive


@st.cache_data(ttl=REFRESH_SECONDS)
def read_daily_ops_report(path: str = "logs/daily_ops_report.json") -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as report_file:
            payload = json.load(report_file)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@st.cache_data(ttl=REFRESH_SECONDS)
def read_incident_anomaly_report(
    anomaly_path: str = "logs/anomaly_report.csv",
    incident_path: str = "logs/incident_report.json",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    anomalies = _empty_df()
    incident: dict[str, Any] = {}
    try:
        if os.path.exists(anomaly_path):
            anomalies = pd.read_csv(anomaly_path)
    except Exception:
        anomalies = _empty_df()
    try:
        if os.path.exists(incident_path):
            with open(incident_path, encoding="utf-8") as incident_file:
                payload = json.load(incident_file)
            incident = payload if isinstance(payload, dict) else {}
    except Exception:
        incident = {}
    return anomalies, incident


@st.cache_data(ttl=REFRESH_SECONDS)
def read_orchestrator_diagnostics(path: str = "logs/orchestrator_diagnostics.json") -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as diagnostics_file:
            payload = json.load(diagnostics_file)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@st.cache_data(ttl=REFRESH_SECONDS)
def read_json_report(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as report_file:
            payload = json.load(report_file)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def report_freshness(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    timestamp_raw = payload.get("generated_at") or payload.get("generated_at_utc")
    timestamp = None
    if timestamp_raw:
        try:
            timestamp = datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        except ValueError:
            timestamp = None
    if timestamp is None and os.path.exists(path):
        timestamp = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    age_hours = None
    if timestamp is not None:
        age_hours = round(max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds()) / 3600.0, 2)
    return {
        "path": path,
        "present": bool(payload),
        "generated_at": timestamp.isoformat() if timestamp else "-",
        "age_hours": age_hours,
        "source": payload.get("source", "UNKNOWN") if payload else "MISSING",
    }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_optional_csv(path: str, tail_rows: int | None = None) -> pd.DataFrame:
    if not os.path.exists(path):
        return _empty_df()
    try:
        if tail_rows is None:
            return pd.read_csv(path)
        return pd.read_csv(path).tail(tail_rows).reset_index(drop=True)
    except Exception:
        return _empty_df()


def _nested_get(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def derive_market_action(
    *,
    paper_only_status: str,
    early_warning_score: float,
    early_warning_label: str,
    brake_trigger_count: int,
    holding_candles_mean_after: float | None,
    collapse_timestamp: str | None,
) -> dict[str, Any]:
    reasons: list[str] = [
        f"Early Warning: score={early_warning_score:.2f}, label={early_warning_label}",
        f"Brake trigger count: {brake_trigger_count}",
    ]

    if holding_candles_mean_after is not None:
        reasons.append(f"Holding compression mean(after): {holding_candles_mean_after:.2f}")
    if collapse_timestamp and collapse_timestamp != "-":
        reasons.append(f"Drift collapse timestamp: {collapse_timestamp}")
    reasons.append(f"PAPER_ONLY enforced: {paper_only_status == 'PAPER_ONLY'}")

    label_upper = early_warning_label.upper()

    if paper_only_status != "PAPER_ONLY":
        return {"action": "CRITICAL GOVERNANCE ERROR", "severity": "CRITICAL", "reasons": reasons}
    if label_upper == "BRAKE_CANDIDATE":
        return {"action": "NO TRADE / BRAKE REVIEW", "severity": "CRITICAL", "reasons": reasons}
    if label_upper == "RISK_ELEVATED":
        return {"action": "DEFENSIVE / HOLD", "severity": "WARNING", "reasons": reasons}
    if brake_trigger_count >= 50:
        return {"action": "DEFENSIVE / HOLD", "severity": "WARNING", "reasons": reasons}
    if holding_candles_mean_after is not None and holding_candles_mean_after < 10:
        return {"action": "WATCH / HOLD", "severity": "WARNING", "reasons": reasons}
    if early_warning_score <= 30 and brake_trigger_count == 0:
        return {"action": "NORMAL / PAPER ONLY", "severity": "OK", "reasons": reasons}
    return {"action": "OBSERVE / PAPER ONLY", "severity": "INFO", "reasons": reasons}


def render_governance_risk_intelligence() -> None:
    st.header("9. Governance / Risk Intelligence")
    st.caption("Read-only governance intelligence. Execution mode remains PAPER_ONLY.")

    research_summary_exists = os.path.exists("docs/RESEARCH_SUMMARY_FINAL.md")
    drift = read_json_report("reports/drift_detection_report.json")
    brake = read_json_report("reports/emergency_brake_simulation.json")
    transition = read_json_report("reports/transition_prediction_report.json")
    robustness = read_json_report("reports/robustness_test_results.json")
    backtest = read_json_report("reports/backtest_filtered_results.json")

    early_warning_score = float(
        _nested_get(transition, "latest_early_warning", "score", default=None)
        or transition.get("early_warning_score")
        or transition.get("warning_score")
        or transition.get("score")
        or 0.0
    )
    early_warning_label = str(
        _nested_get(transition, "latest_early_warning", "label", default=None)
        or transition.get("early_warning_label")
        or transition.get("label")
        or transition.get("warning_label")
        or "UNKNOWN"
    ).upper()
    paper_only_status = str(backtest.get("mode") or "PAPER_ONLY").upper()

    brake_active = bool(brake.get("brake_active") or brake.get("active") or False)
    trigger_count = int(
        _nested_get(brake, "summary", "brake_trigger_count", default=None)
        or brake.get("high_trigger_count")
        or brake.get("trigger_count")
        or 0
    )
    collapse_ts = (
        _nested_get(drift, "collapse", "selected_collapse_timestamp", default=None)
        or drift.get("collapse_timestamp")
        or drift.get("drift_collapse_timestamp")
        or "-"
    )
    holding_candles_mean_after_raw = _nested_get(drift, "before_vs_after", "after", "holding_candles_mean", default=None)
    holding_candles_mean_after = (
        float(holding_candles_mean_after_raw) if holding_candles_mean_after_raw is not None else None
    )
    regime_summary = (
        transition.get("regime_market_risk_summary")
        or transition.get("market_risk_summary")
        or robustness.get("regime_risk_summary")
        or "No regime / market risk summary available."
    )

    freshness_rows = [
        report_freshness("reports/drift_detection_report.json", drift),
        report_freshness("reports/emergency_brake_simulation.json", brake),
        report_freshness("reports/transition_prediction_report.json", transition),
    ]
    st.subheader("Governance Report Freshness")
    st.dataframe(pd.DataFrame(freshness_rows), use_container_width=True, hide_index=True)
    stale_rows = [row for row in freshness_rows if row.get("age_hours") is None or float(row.get("age_hours") or 0.0) > 24.0]
    if stale_rows:
        st.warning("One or more governance reports are missing or older than 24h. Run: python main.py --refresh-governance-reports")

    market_action = derive_market_action(
        paper_only_status=paper_only_status,
        early_warning_score=early_warning_score,
        early_warning_label=early_warning_label,
        brake_trigger_count=trigger_count,
        holding_candles_mean_after=holding_candles_mean_after,
        collapse_timestamp=str(collapse_ts),
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("PAPER_ONLY Status", paper_only_status)
    col2.metric("Early Warning Score", f"{early_warning_score:.2f}")
    col3.metric("Early Warning Label", early_warning_label)

    brake_review_active = brake_active or trigger_count >= 50
    brake_source = str(
        brake.get("brake_source")
        or brake.get("source")
        or _nested_get(brake, "summary", "brake_source", default=None)
        or _nested_get(brake, "summary", "source", default=None)
        or ("SIMULATION_RESEARCH" if brake else "NONE")
    ).upper()
    col1, col2, col3 = st.columns(3)
    col1.metric("Emergency Brake", "ACTIVE / REVIEW" if brake_review_active else "NO")
    col2.metric("Brake Trigger Count", trigger_count)
    col3.metric("Drift Collapse Timestamp", str(collapse_ts))
    if trigger_count >= 50 and brake_source == "SIMULATION_RESEARCH":
        st.warning("Brake source: simulation research / review required")

    st.subheader("Emergency Brake Summary")
    st.info(str(brake.get("summary") or brake.get("status") or "No emergency brake summary available."))

    st.subheader("Regime / Market Risk Summary")
    st.warning(str(regime_summary))

    st.subheader("Suggested Market Action")
    action_col, severity_col = st.columns([3, 2])
    action_col.metric("Suggested Market Action", str(market_action["action"]))
    severity_col.metric("Severity", str(market_action["severity"]))

    severity = str(market_action["severity"]).upper()
    if severity == "CRITICAL":
        st.error(f"Severity: {severity}")
    elif severity == "WARNING":
        st.warning(f"Severity: {severity}")
    elif severity == "OK":
        st.success(f"Severity: {severity}")
    else:
        st.info(f"Severity: {severity}")

    st.markdown("**Reasoning**")
    for reason in market_action.get("reasons", []):
        st.markdown(f"- {reason}")

    st.caption("This is a read-only governance recommendation, not a live trading command.")

    decision_inputs = pd.DataFrame(
        [
            {
                "early_warning_score": early_warning_score,
                "early_warning_label": early_warning_label,
                "brake_trigger_count": trigger_count,
                "holding_candles_mean_after": holding_candles_mean_after,
                "collapse_timestamp": collapse_ts,
            }
        ]
    )
    st.markdown("**Decision Inputs**")
    st.dataframe(decision_inputs, use_container_width=True)

    st.subheader("Optional Governance Artifacts")
    if research_summary_exists:
        st.success("Research summary detected: docs/RESEARCH_SUMMARY_FINAL.md")
    else:
        st.info("Research summary markdown not found.")

    with st.expander("Regime Matrix / Robustness Artifacts", expanded=False):
        regime_matrix = read_optional_csv("reports/regime_transition_matrix.csv")
        robustness_split = read_optional_csv("reports/robustness_time_split.csv")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Regime Transition Matrix**")
            show_dataframe_or_info(regime_matrix, "regime_transition_matrix.csv not found.")
            if not regime_matrix.empty and len(regime_matrix.columns) > 1:
                matrix = regime_matrix.set_index(regime_matrix.columns[0]).apply(pd.to_numeric, errors="coerce")
                st.plotly_chart(px.imshow(matrix.fillna(0), text_auto=True, aspect="auto"), use_container_width=True)
        with col2:
            st.markdown("**Robustness Time Split**")
            show_dataframe_or_info(robustness_split, "robustness_time_split.csv not found.")

    with st.expander("Emergency Brake / Transition Warning (tail)", expanded=False):
        load_governance_timeseries = st.checkbox(
            "Load emergency/transition timeseries",
            value=False,
            key="load_governance_timeseries",
            help="Loads only tailed rows to reduce dashboard CPU usage.",
        )
        if not load_governance_timeseries:
            st.info("Timeseries tables are skipped by default. Enable the checkbox to load.")
        else:
            brake_events = read_optional_csv(
                "reports/emergency_brake_events.csv",
                tail_rows=EMERGENCY_BRAKE_EVENTS_TAIL_ROWS,
            )
            warning_timeseries = read_optional_csv(
                "reports/transition_warning_timeseries.csv",
                tail_rows=TRANSITION_WARNING_TAIL_ROWS,
            )
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Emergency Brake Events (last {EMERGENCY_BRAKE_EVENTS_TAIL_ROWS})**")
                show_dataframe_or_info(brake_events, "emergency_brake_events.csv not found.")
            with col2:
                st.markdown(f"**Transition Warning Timeseries (last {TRANSITION_WARNING_TAIL_ROWS})**")
                show_dataframe_or_info(warning_timeseries, "transition_warning_timeseries.csv not found.")
                if not warning_timeseries.empty and len(warning_timeseries.columns) >= 2:
                    x_col = "timestamp" if "timestamp" in warning_timeseries.columns else warning_timeseries.columns[0]
                    y_col = (
                        "early_warning_score"
                        if "early_warning_score" in warning_timeseries.columns
                        else warning_timeseries.columns[1]
                    )
                    safe_plot_line(warning_timeseries, x_col, y_col, "Transition Early Warning Timeseries")

    with st.expander("Drift Rolling Metrics (optional heavy)", expanded=False):
        load_drift_metrics = st.checkbox(
            "Load drift_rolling_metrics.csv",
            value=False,
            key="load_drift_rolling_metrics",
            help="Large file is not loaded by default to keep dashboard responsive.",
        )
        if not load_drift_metrics:
            st.info(
                "drift_rolling_metrics.csv is intentionally skipped by default. "
                "Enable the checkbox to load and render."
            )
        else:
            drift_rolling_metrics = read_optional_csv("reports/drift_rolling_metrics.csv")
            show_dataframe_or_info(drift_rolling_metrics, "drift_rolling_metrics.csv not found.")


def status_badge(label: str, status: str, detail: str = "") -> None:
    colors = {
        "GREEN": "#15803d",
        "YELLOW": "#a16207",
        "RED": "#b91c1c",
    }
    st.markdown(
        f"""
        <div style="padding:10px 12px;border-radius:8px;border:1px solid #d8dee8;margin-bottom:8px">
            <strong>{label}</strong><br>
            <span style="color:{colors.get(status, '#475569')};font-weight:700">{status}</span>
            <span style="color:#64748b">{detail}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def latest_timestamp(df: pd.DataFrame) -> str:
    if df.empty or "timestamp" not in df.columns:
        return "-"
    value = df["timestamp"].dropna().max()
    return "-" if pd.isna(value) else str(value)


def minutes_since(timestamp: Any) -> float | None:
    if timestamp is None or pd.isna(timestamp):
        return None
    if not isinstance(timestamp, pd.Timestamp):
        timestamp = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None
    return (datetime.now(timezone.utc) - timestamp.to_pydatetime()).total_seconds() / 60


def metric_value(df: pd.DataFrame, column: str, default: Any = "-") -> Any:
    if df.empty or column not in df.columns:
        return default
    value = df.iloc[0].get(column, default)
    return default if pd.isna(value) else value


def render_risk_engine_status(risk_status: dict[str, Any]) -> None:
    status = str(risk_status.get("status", "WATCH")).upper()
    metrics = risk_status.get("metrics", {}) or {}
    badge_color = "GREEN" if status == "SAFE" else "YELLOW" if status == "WATCH" else "RED"

    st.header("Risk Engine Status")
    status_badge("Circuit Breaker", badge_color, f" {status}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Risk Score", risk_status.get("risk_score", 0))
    col2.metric("Position Multiplier", f"{float(risk_status.get('position_multiplier', 0.0)):.2f}x")
    col3.metric("Drawdown", f"{float(metrics.get('drawdown', 0.0)):.2f}%")
    col4.metric("Heartbeat Age", f"{float(metrics.get('heartbeat_age_minutes', 0.0)):.1f}m")
    col4.caption(f"Source: {metrics.get('heartbeat_source', '-')}")

    reasons = risk_status.get("reasons") or ["No active risk warnings."]
    if status == "HALT":
        st.error("RISK HALT")
    elif status == "WATCH":
        st.warning("SAFE MODE / WATCH")
    else:
        st.success("SAFE MODE")

    ml_accuracy = float(metrics.get("ml_accuracy", 0.0))
    regime_name = str(metrics.get("regime_name", "UNKNOWN")).upper()
    heartbeat_age = float(metrics.get("heartbeat_age_minutes", 0.0))
    drawdown = float(metrics.get("drawdown", 0.0))
    if ml_accuracy < config.risk_ml_accuracy_halt:
        st.warning("MODEL UNSTABLE")
    if regime_name in {"SIDEWAYS / CHOPPY", "TRENDING BEAR", "HIGH VOLATILITY"}:
        st.warning("HOSTILE REGIME")
    if heartbeat_age > config.risk_stale_minutes:
        st.warning("Stale runtime heartbeat detected.")
    if drawdown <= config.risk_drawdown_watch:
        st.warning("Drawdown warning active.")

    st.dataframe(
        pd.DataFrame({"reason": reasons}),
        use_container_width=True,
        hide_index=True,
    )


def render_portfolio_observability(result: dict[str, Any]) -> None:
    st.header("Portfolio Observability")
    heat = str(result.get("portfolio_heat") or "UNKNOWN")
    heat_color = "GREEN" if heat == "LOW" else "YELLOW" if heat == "MEDIUM" else "RED"
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        status_badge("Portfolio Heat", heat_color, heat)
    col2.metric("Heat Score", f"{float(result.get('portfolio_heat_score', 0)):.2f}/100")
    col3.metric("Concentration Risk", f"{float(result.get('concentration_risk', 0)):.2f}")
    col4.metric("Exposure Source", result.get("source", "-"))

    warnings = result.get("warnings", [])
    for warning in warnings[:3]:
        if heat == "HIGH":
            st.warning(warning)
        else:
            st.info(warning)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Top Exposure Symbols")
        show_dataframe_or_info(pd.DataFrame(result.get("symbol_exposure", [])), "No symbol exposure data yet.")

        st.subheader("Market Type Exposure")
        show_dataframe_or_info(pd.DataFrame(result.get("market_type_exposure", [])), "No market type exposure data yet.")
    with col2:
        st.subheader("Regime Exposure")
        show_dataframe_or_info(pd.DataFrame(result.get("regime_exposure", [])), "No regime exposure data yet.")

        st.subheader("Top Correlated Symbols")
        show_dataframe_or_info(
            pd.DataFrame(result.get("top_correlated_symbols", [])),
            "Not enough historical outcome data for correlation.",
        )


def _metric_text(value: Any, suffix: str = "") -> str:
    if value == float("inf"):
        return "∞"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return f"0.00{suffix}"



def render_portfolio_risk_budget(result: dict[str, Any]) -> None:
    st.header("10. Portfolio Risk Budget")
    st.caption("PAPER_ONLY governance layer: read-only analytics, no broker routing, no order placement, no live trading.")
    recommendation = str(result.get("recommendation", "NORMAL")).upper()
    badge_color = "GREEN" if recommendation == "NORMAL" else "YELLOW" if recommendation == "DEFENSIVE" else "RED"

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Exposure", f"{float(result.get('total_exposure', 0.0)):.2f}%")
    col2.metric("Regime-Aware Cap", f"{float(result.get('max_allowed_exposure', 0.0)):.2f}%")
    col3.metric("Utilization", f"{float(result.get('risk_budget_utilization', 0.0)):.2f}%")
    with col4:
        status_badge("Recommendation", badge_color, f" {recommendation}")

    col1, col2, col3 = st.columns(3)
    col1.progress(min(1.0, max(0.0, float(result.get("utilization_ratio", 0.0)))), text="Exposure budget utilization")
    col2.metric("Concentration", f"{result.get('concentration_label', 'UNKNOWN')} ({float(result.get('concentration_score', 0.0)):.2f}/100)")
    col3.metric("Diversification", f"{float(result.get('diversification_score', 0.0)):.2f}/100")

    brake_context = result.get("brake_context") if isinstance(result.get("brake_context"), dict) else {}
    if brake_context:
        col1, col2, col3 = st.columns(3)
        col1.metric("Brake Trigger Count", int(brake_context.get("trigger_count") or 0))
        col2.metric("Brake Risk Level", brake_context.get("brake_risk_level", "NONE"))
        col3.metric("Brake Source", brake_context.get("source", "NONE"))

    for warning in (result.get("warnings") or [])[:3]:
        if str(result.get("concentration_label", "")).upper() == "HIGH" or recommendation in {"REDUCE EXPOSURE", "FREEZE NEW ALLOCATION"}:
            st.warning(warning)
        else:
            st.info(warning)

    st.info(str(result.get("defensive_scaling_recommendation", "Read-only risk budget analytics only.")))
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Exposure by Symbol")
        show_dataframe_or_info(pd.DataFrame(result.get("symbol_breakdown", [])), "No symbol risk budget exposure yet.")
    with col2:
        st.subheader("Exposure by Regime")
        show_dataframe_or_info(pd.DataFrame(result.get("exposure_by_regime", [])), "No regime risk budget exposure yet.")

    with st.expander("Governance safety flags", expanded=False):
        st.json({
            "paper_only": result.get("paper_only", True),
            "execution_enabled": result.get("execution_enabled", False),
            "live_trading_enabled": result.get("live_trading_enabled", False),
            "safety": result.get("safety", []),
        })


def render_portfolio_equity_analytics(result: dict[str, Any]) -> None:
    st.header("PORTFOLIO & EQUITY ANALYTICS")
    warnings = result.get("warnings") or []
    for warning in warnings[:3]:
        st.warning(str(warning))

    metrics = result.get("metrics", {}) or {}
    equity = result.get("equity_curve")
    macro_performance = result.get("macro_performance")
    competition_performance = result.get("competition_performance")
    macro_survival = result.get("macro_survival")
    trades = result.get("trades")
    equity = equity if isinstance(equity, pd.DataFrame) else _empty_df()
    macro_performance = macro_performance if isinstance(macro_performance, pd.DataFrame) else _empty_df()
    competition_performance = competition_performance if isinstance(competition_performance, pd.DataFrame) else _empty_df()
    macro_survival = macro_survival if isinstance(macro_survival, pd.DataFrame) else _empty_df()
    trades = trades if isinstance(trades, pd.DataFrame) else _empty_df()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total PnL", _metric_text(metrics.get("total_pnl"), "%"))
    col2.metric("Winrate", _metric_text(metrics.get("winrate"), "%"))
    col3.metric("Profit Factor", "∞" if metrics.get("profit_factor") == float("inf") else _metric_text(metrics.get("profit_factor")))
    col4.metric("Max Drawdown", _metric_text(metrics.get("max_drawdown"), "%"))
    col5.metric("Trade Count", int(metrics.get("trade_count", 0) or 0))

    if equity.empty:
        st.info("No paper trades found yet. Run python main.py --paper-engine or collect paper_trades data first.")
        return

    plot_x = "timestamp" if "timestamp" in equity.columns and equity["timestamp"].notna().any() else "trade_index"
    if {"equity", plot_x}.issubset(equity.columns):
        st.plotly_chart(
            px.line(equity, x=plot_x, y="equity", color="source" if "source" in equity.columns else None, title="Equity Curve"),
            use_container_width=True,
        )
    else:
        st.info("Equity curve columns are incomplete.")

    if {"drawdown", plot_x}.issubset(equity.columns):
        st.plotly_chart(
            px.area(equity, x=plot_x, y="drawdown", title="Rolling Drawdown"),
            use_container_width=True,
        )
    else:
        st.info("Drawdown columns are incomplete.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Performance by Macro State")
        show_dataframe_or_info(macro_performance, "No macro_state performance data yet.")
        if not macro_performance.empty and {"macro_state", "total_pnl"}.issubset(macro_performance.columns):
            safe_plot_bar(macro_performance, "macro_state", "total_pnl", "Macro State Total PnL")

        st.subheader("Macro Survival")
        show_dataframe_or_info(
            macro_survival,
            "No HIGH_STRESS / PANIC / RISK_ON rows yet. Missing macro_state values are shown as UNKNOWN.",
        )

    with col2:
        st.subheader("Performance by Competition Profile")
        show_dataframe_or_info(
            competition_performance,
            "No competition_profile data yet. Missing values use DEFAULT.",
        )
        if not competition_performance.empty and {"competition_profile", "total_pnl"}.issubset(competition_performance.columns):
            safe_plot_bar(competition_performance, "competition_profile", "total_pnl", "Competition Profile Total PnL")

        st.subheader("Rolling Analytics")
        rolling_columns = [
            column
            for column in ["trade_index", "timestamp", "rolling_pnl_10", "rolling_winrate_10"]
            if column in equity.columns
        ]
        if {"rolling_pnl_10", "rolling_winrate_10"}.issubset(equity.columns):
            rolling = equity[rolling_columns].copy()
            rolling_x = "timestamp" if "timestamp" in rolling.columns and rolling["timestamp"].notna().any() else "trade_index"
            long = rolling.melt(id_vars=[rolling_x], value_vars=["rolling_pnl_10", "rolling_winrate_10"], var_name="metric", value_name="value")
            st.plotly_chart(px.line(long, x=rolling_x, y="value", color="metric", title="Rolling PnL / Winrate"), use_container_width=True)
        else:
            st.info("Not enough rows for rolling analytics.")

    st.subheader("Latest Portfolio Trades")
    latest_columns = [
        "timestamp",
        "symbol",
        "side",
        "source",
        "pnl",
        "equity",
        "drawdown",
        "macro_state",
        "competition_profile",
        "status",
    ]
    show_dataframe_or_info(
        trades[[column for column in latest_columns if column in trades.columns]].tail(50).sort_index(ascending=False),
        "No normalized portfolio trades available.",
    )


def render_opportunity_allocation(allocation: pd.DataFrame) -> None:
    st.header("Opportunity Allocation Engine")
    if not allocation.attrs.get("file_exists", False):
        st.info("Opportunity allocation file not found. Run python main.py --allocate first.")
        return

    df = allocation.copy()
    for column in ["opportunity_score", "risk_score", "suggested_max_weight_pct"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    if df.empty:
        st.info("Opportunity allocation file is empty. Run python main.py --allocate again after fresh data.")
        return

    priority_columns = [
        "symbol",
        "opportunity_score",
        "risk_score",
        "reason",
        "suggested_max_weight_pct",
    ]
    avoid_columns = [
        "symbol",
        "opportunity_score",
        "risk_score",
        "reason",
    ]
    tier_counts = df.get("allocation_tier", pd.Series(dtype=str)).value_counts().reset_index()
    if not tier_counts.empty:
        tier_counts.columns = ["allocation_tier", "count"]

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Top Priority Symbols")
        priority = df[df.get("allocation_tier", pd.Series(dtype=str)) == "PRIORITY"].head(15)
        if priority.empty:
            st.info("No PRIORITY symbols right now. Market may be under macro stress / risk-off conditions.")
        else:
            st.dataframe(priority[[column for column in priority_columns if column in priority.columns]], use_container_width=True, hide_index=True)

        st.subheader("Allocation Tier Summary")
        show_dataframe_or_info(tier_counts, "No allocation tier summary.")
    with col2:
        st.subheader("Avoid List")
        avoid = df[df.get("allocation_tier", pd.Series(dtype=str)) == "AVOID"].head(15)
        if avoid.empty:
            st.info("No AVOID symbols right now.")
        else:
            st.dataframe(avoid[[column for column in avoid_columns if column in avoid.columns]], use_container_width=True, hide_index=True)


def render_webhook_paper_engine(trades: pd.DataFrame, payload: dict[str, Any]) -> None:
    st.header("Webhook & Paper Engine")
    if trades.empty:
        st.info("No internal paper trades yet. Run python main.py --paper-engine.")
    else:
        df = trades.copy()
        df["pnl"] = pd.to_numeric(df.get("pnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        equity = df.sort_values("id")["pnl"].cumsum().reset_index(drop=True)
        drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Paper Trades", len(df))
        col2.metric("Paper Winrate", f"{(df['pnl'] > 0).mean() * 100:.2f}%")
        col3.metric("Paper Drawdown", f"{drawdown.min() if not drawdown.empty else 0:.2f}%")
        col4.metric("Paper Total PnL", f"{df['pnl'].sum():.2f}%")
        safe_plot_line(pd.DataFrame({"trade": range(1, len(equity) + 1), "equity": equity}), "trade", "equity", "Internal Paper Equity")
        columns = [
            "timestamp",
            "symbol",
            "side",
            "entry_price",
            "exit_price",
            "pnl",
            "confidence",
            "regime",
            "macro_state",
            "allocation_tier",
            "status",
        ]
        st.subheader("Latest Simulated Trades")
        st.dataframe(df[[column for column in columns if column in df.columns]].head(25), use_container_width=True)

    st.subheader("Webhook Payload Preview")
    if payload:
        st.json(payload)
    elif not trades.empty and "payload_json" in trades.columns:
        try:
            st.json(json.loads(str(trades.iloc[0].get("payload_json") or "{}")))
        except json.JSONDecodeError:
            st.info("Latest paper trade payload is not valid JSON.")
    else:
        st.info("No webhook payload preview yet. Run python main.py --webhook-test.")


def render_broadcast_control_center(broadcasts: pd.DataFrame, paper_trades: pd.DataFrame) -> None:
    st.header("Broadcast Control Center")
    if broadcasts.empty:
        st.info("No broadcast events yet. Run python main.py --broadcast-test.")
        return

    df = broadcasts.copy()
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp", ascending=False)
    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")

    latest_columns = [
        "timestamp",
        "symbol",
        "side",
        "confidence",
        "macro_state",
        "allocation_tier",
        "target_name",
        "target_type",
        "target_profile",
        "route_status",
        "route_reason",
    ]
    st.subheader("Latest Broadcasts")
    st.dataframe(
        df[[column for column in latest_columns if column in df.columns]].head(50),
        use_container_width=True,
        hide_index=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Targets")
        target_columns = ["target_name", "target_type", "target_profile", "route_status"]
        if set(target_columns).issubset(df.columns):
            targets = (
                df.groupby(target_columns, dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(targets, use_container_width=True, hide_index=True)
        else:
            st.info("Broadcast target metadata is incomplete.")

        st.subheader("Route Success / Failure")
        if "route_status" in df.columns:
            route_summary = df["route_status"].fillna("UNKNOWN").value_counts().reset_index()
            route_summary.columns = ["route_status", "count"]
            st.dataframe(route_summary, use_container_width=True, hide_index=True)
        else:
            st.info("No route status data available.")

    with col2:
        st.subheader("Signal Distribution")
        distribution_columns = ["symbol", "route_status"]
        if set(distribution_columns).issubset(df.columns):
            distribution = (
                df.groupby(distribution_columns, dropna=False)
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                .head(30)
            )
            st.dataframe(distribution, use_container_width=True, hide_index=True)
        else:
            st.info("No signal distribution data available.")

        st.subheader("Per-Target Paper Performance")
        if paper_trades.empty:
            st.info("No internal paper performance yet. Run python main.py --paper-engine.")
        else:
            trades = paper_trades.copy()
            trades["pnl"] = pd.to_numeric(trades.get("pnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
            equity = trades.sort_values("id")["pnl"].cumsum().reset_index(drop=True)
            drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
            performance = pd.DataFrame(
                [
                    {
                        "target_name": "internal_paper",
                        "trade_count": len(trades),
                        "winrate": float((trades["pnl"] > 0).mean() * 100) if len(trades) else 0.0,
                        "total_pnl": float(trades["pnl"].sum()),
                        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
                    }
                ]
            )
            st.dataframe(performance, use_container_width=True, hide_index=True)



@st.cache_data(ttl=REFRESH_SECONDS)
def read_promotion_scorecard() -> dict[str, Any]:
    report = read_json_report("reports/promotion_scorecard.json")
    if report:
        return report
    try:
        return generate_promotion_scorecard(
            db_path=config.database_path,
            output_path=None,
            write_report=False,
            top_n=10,
        )
    except Exception as exc:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_only": True,
            "candidates": [],
            "summary": {"candidate_count": 0, "top_recommendation": "HOLD"},
            "governance_constraints": {
                "paper_only": "PAPER_ONLY",
                "read_only_analytics": True,
                "no_real_execution": True,
                "no_auto_deployment": True,
                "no_phase_3_promotion": True,
            },
            "warnings": [f"Promotion scorecard unavailable: {exc}"],
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_governance_audit() -> dict[str, Any]:
    report = read_json_report("reports/governance_audit.json")
    if report:
        return report
    try:
        return run_governance_audit(
            output_path=None,
            write_report=False,
        )
    except Exception as exc:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_only": True,
            "consistency_score": 0,
            "governance_health": "UNKNOWN",
            "conflicts": [],
            "stale_reports": [],
            "missing_reports": [],
            "policy_violations": [{"report": "governance_audit", "policy": "audit_available", "detail": str(exc)}],
            "audit_severity": "CRITICAL",
            "recommendations": ["Governance audit unavailable; keep PAPER_ONLY constraints active."],
            "governance_constraints": {
                "paper_only": "PAPER_ONLY",
                "read_only_analytics": True,
                "no_execution": True,
                "no_deployment": True,
                "no_live_trading": True,
            },
        }


@st.cache_data(ttl=REFRESH_SECONDS)
def read_phase3_readiness() -> dict[str, Any]:
    report = read_json_report("reports/phase3_readiness.json")
    if report:
        return report
    try:
        return calculate_phase3_readiness(
            db_path=config.database_path,
            paper_trades_path=config.paper_trades_path,
            backup_dir=config.database_backup_dir,
            output_path="reports/phase3_readiness.json",
            write_report=False,
            health_stale_minutes=config.health_guardian_stale_minutes,
        )
    except Exception as exc:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_only": True,
            "readiness_percent": 0.0,
            "status": "LOCKED",
            "passed_criteria": [],
            "failed_criteria": ["phase3_readiness_unavailable"],
            "blockers": [f"Phase 3 readiness unavailable: {exc}"],
            "next_actions": ["Keep PAPER_ONLY active and regenerate readiness when artifacts are available."],
            "governance_constraints": {
                "paper_only": "PAPER_ONLY",
                "read_only_analytics": True,
                "no_execution_changes": True,
                "no_broker_routing": True,
                "no_strategy_promotion": True,
                "no_phase_3_unlock_automation": True,
            },
        }



def _as_list(value: Any) -> list[Any]:
    if value in (None, "", "none", "NONE"):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _upper_value(value: Any, default: str = "UNKNOWN") -> str:
    if value is None or value == "":
        return default
    return str(value).upper()


def _percent_value(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return default


def _first_existing(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return default


def _metric_display(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.2f}{suffix}" if isinstance(value, float) else f"{value}{suffix}"
    return str(value)


def _status_message(label: str, status: str, critical: set[str], warning: set[str]) -> None:
    status_upper = _upper_value(status)
    if status_upper in critical:
        st.error(f"{label}: {status_upper}")
    elif status_upper in warning:
        st.warning(f"{label}: {status_upper}")
    else:
        st.success(f"{label}: {status_upper}")


def load_dashboard_reports() -> dict[str, dict[str, Any]]:
    """Read-only report bundle for the Streamlit dashboard.

    This loader intentionally reads existing JSON artifacts from reports/ only. It does
    not invoke report generators, shell commands, database scans, broker code, or trading
    execution paths.
    """

    report_paths = {
        "phase3_readiness": "reports/phase3_readiness.json",
        "governance_audit": "reports/governance_audit.json",
        "portfolio_risk_budget": "reports/portfolio_risk_budget.json",
        "promotion_scorecard": "reports/promotion_scorecard.json",
        "drift_detection": "reports/drift_detection_report.json",
        "emergency_brake": "reports/emergency_brake_simulation.json",
        "transition_prediction": "reports/transition_prediction_report.json",
        "label_quality_audit": "reports/label_quality_audit.json",
        "stress_test": "reports/stress_test_report.json",
        "backup_verification": "reports/backup_verification.json",
    }
    return {name: read_json_report(path) for name, path in report_paths.items()}


def render_read_only_banner() -> None:
    st.info(
        "PAPER_ONLY / READ-ONLY dashboard: hanya membaca JSON di folder reports/. "
        "Tidak ada broker routing, order placement, live trading, execution mutation, atau auto-unlock Phase 3."
    )


def render_action_required_area(reports: dict[str, dict[str, Any]]) -> None:
    st.subheader("Action Required Area")
    audit = reports.get("governance_audit", {})
    risk_budget = reports.get("portfolio_risk_budget", {})

    stale_reports = _as_list(audit.get("stale_reports"))
    violations = _as_list(audit.get("policy_violations") or audit.get("violations"))
    governance_health = _upper_value(audit.get("governance_health"))
    risk_recommendation = _upper_value(risk_budget.get("recommendation"))

    reasons: list[str] = []
    severity = "ok"
    if stale_reports:
        severity = "warning"
        reasons.append(f"Stale reports terdeteksi: {len(stale_reports)} artifact.")
    if violations:
        severity = "error"
        reasons.append(f"Governance violations terdeteksi: {len(violations)} item.")
    if governance_health == "CRITICAL":
        severity = "error"
        reasons.append("Governance health CRITICAL.")
    if risk_recommendation in {"FREEZE", "FREEZE NEW ALLOCATION"}:
        severity = "error"
        reasons.append(f"Risk budget recommendation {risk_recommendation}.")

    if reasons:
        message = "\n".join(f"- {reason}" for reason in reasons)
        if severity == "error":
            st.error(message)
        else:
            st.warning(message)
        st.caption("Manual remediation commands — copy/paste di terminal, jangan dijalankan dari dashboard.")
        st.code(
            "python main.py --refresh-governance-reports\n"
            "python main.py --phase3-remediation\n"
            "python main.py --phase3-readiness",
            language="bash",
        )
    else:
        st.success("No immediate action required from the latest reports.")


def render_executive_summary_tab(reports: dict[str, dict[str, Any]]) -> None:
    phase3 = reports.get("phase3_readiness", {})
    audit = reports.get("governance_audit", {})
    risk_budget = reports.get("portfolio_risk_budget", {})
    promotion = reports.get("promotion_scorecard", {})

    readiness_percent = _percent_value(phase3.get("readiness_percent"))
    closed_paper_trades = _first_existing(
        phase3.get("closed_paper_trades"),
        phase3.get("paper_closed_trades"),
        _nested_get(phase3, "metrics", "closed_paper_trades", default=None),
    )
    if closed_paper_trades is None:
        for detail in phase3.get("criteria_details", []) if isinstance(phase3.get("criteria_details"), list) else []:
            if "closed trades" in str(detail.get("name", "")).lower():
                closed_paper_trades = detail.get("detail", "-")
                break

    pnl = _first_existing(
        _nested_get(phase3, "metrics", "shadow_paper_pnl", default=None),
        _nested_get(promotion, "summary", "shadow_paper_pnl", default=None),
    )
    winrate = _first_existing(
        _nested_get(phase3, "metrics", "winrate", default=None),
        _nested_get(promotion, "summary", "winrate", default=None),
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Phase 3 Readiness", f"{readiness_percent:.1f}%")
    col2.metric("Phase 3 Status", phase3.get("status", "LOCKED"))
    col3.metric("Governance Health", audit.get("governance_health", "UNKNOWN"))

    col4, col5, col6 = st.columns(3)
    col4.metric("Shadow/Paper PnL", _metric_display(pnl))
    col5.metric("Winrate", _metric_display(winrate, "%" if isinstance(winrate, (int, float)) else ""))
    col6.metric("Closed Paper Trades", _metric_display(closed_paper_trades))

    st.caption("Phase 3 readiness progress")
    st.progress(readiness_percent / 100.0)

    closed_numeric = None
    try:
        closed_numeric = float(closed_paper_trades)
    except (TypeError, ValueError):
        closed_numeric = None
    if closed_numeric is not None:
        st.caption("Closed paper trades progress toward 100-trade evidence gate")
        st.progress(max(0.0, min(100.0, closed_numeric)) / 100.0)

    st.subheader("Roadmap Status")
    roadmap = pd.DataFrame(
        [
            {"Phase": "Phase 1", "Status": "DONE"},
            {"Phase": "Phase 2", "Status": "DONE"},
            {"Phase": "Phase 2.5", "Status": "DONE"},
            {"Phase": "Phase 3", "Status": "LOCKED"},
        ]
    )
    st.dataframe(roadmap, use_container_width=True, hide_index=True)

    blockers = _as_list(phase3.get("blockers"))
    next_actions = _as_list(phase3.get("next_actions"))
    if blockers:
        st.warning("Phase 3 blockers: " + "; ".join(map(str, blockers[:5])))
    if next_actions:
        st.info("Next actions: " + "; ".join(map(str, next_actions[:5])))


def render_governance_risk_tab(reports: dict[str, dict[str, Any]]) -> None:
    audit = reports.get("governance_audit", {})
    risk_budget = reports.get("portfolio_risk_budget", {})
    promotion = reports.get("promotion_scorecard", {})

    recommendation = _upper_value(risk_budget.get("recommendation"))
    utilization = _first_existing(risk_budget.get("risk_budget_utilization"), risk_budget.get("utilization_ratio"), default=0.0)
    health = _upper_value(audit.get("governance_health"))
    brake_context = risk_budget.get("brake_context") or audit.get("brake_context") or {}
    brake_level = _upper_value(brake_context.get("brake_risk_level", "NONE"), "NONE")
    conflicts = _as_list(audit.get("conflicts"))
    violations = _as_list(audit.get("policy_violations") or audit.get("violations"))
    stale_reports = _as_list(audit.get("stale_reports"))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Risk Budget Recommendation", recommendation)
    col2.metric("Risk Budget Utilization", _metric_display(utilization))
    col3.metric("Governance Audit Health", health)
    col4.metric("Promotion Status", _nested_get(promotion, "summary", "top_recommendation", default="UNKNOWN"))

    _status_message("Risk Budget", recommendation, {"FREEZE", "FREEZE NEW ALLOCATION"}, {"WATCH", "DEFENSIVE", "REDUCE EXPOSURE"})
    _status_message("Governance", health, {"CRITICAL"}, {"WATCH", "STABLE_WITH_WARNINGS", "WARNING"})
    _status_message("Brake Context", brake_level, {"CRITICAL"}, {"HIGH", "WATCH", "ELEVATED"})

    if conflicts:
        st.error(f"Conflicts detected: {len(conflicts)}")
        st.dataframe(pd.DataFrame(conflicts), use_container_width=True)
    else:
        st.success("No conflicts detected.")

    if violations:
        st.error(f"Violations detected: {len(violations)}")
        st.dataframe(pd.DataFrame(violations), use_container_width=True)
    else:
        st.success("No violations detected.")

    if stale_reports:
        st.warning(f"Stale reports detected: {len(stale_reports)}")
        st.dataframe(pd.DataFrame(stale_reports), use_container_width=True)
    else:
        st.success("No stale reports reported by governance audit.")

    with st.expander("Brake Context Details", expanded=False):
        st.json(brake_context)
    with st.expander("Promotion Scorecard Details", expanded=False):
        st.json(promotion)
    with st.expander("Risk Budget Breakdown", expanded=False):
        for key in ("symbol_breakdown", "exposure_by_symbol", "exposure_by_regime", "warnings", "safety"):
            value = risk_budget.get(key)
            if value:
                st.write(f"**{key}**")
                if isinstance(value, list):
                    st.dataframe(pd.DataFrame(value), use_container_width=True)
                else:
                    st.json(value)


def render_ml_diagnostics_tab(reports: dict[str, dict[str, Any]]) -> None:
    drift = reports.get("drift_detection", {})
    transition = reports.get("transition_prediction", {})
    brake = reports.get("emergency_brake", {})
    label_quality = reports.get("label_quality_audit", {})
    stress = reports.get("stress_test", {})

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Model Accuracy", _metric_display(_nested_get(label_quality, "summary", "model_accuracy", default=None)))
    col2.metric("Walkforward Quality", _metric_display(_nested_get(reports.get("promotion_scorecard", {}), "summary", "walkforward_quality", default=None)))
    col3.metric("Drift Label", drift.get("drift_label", _nested_get(drift, "summary", "drift_label", default="UNKNOWN")))
    col4.metric("Brake Risk", brake.get("brake_risk_level", _nested_get(brake, "summary", "brake_risk_level", default="UNKNOWN")))

    st.subheader("Walkforward / Promotion Summary")
    st.json(reports.get("promotion_scorecard", {}).get("summary", {}))

    st.subheader("Drift & Transition Diagnostics")
    col_a, col_b = st.columns(2)
    with col_a:
        st.json(drift.get("summary", {"drift_label": drift.get("drift_label", "UNKNOWN"), "drift_score": drift.get("drift_score", 0.0)}))
    with col_b:
        st.json(transition.get("latest_early_warning", {}))

    with st.expander("Anomaly / incident / stress diagnostics", expanded=False):
        st.json({"stress_test": stress, "label_quality_audit": label_quality})

    with st.expander("Raw JSON reports", expanded=False):
        selected = st.selectbox("Report", options=list(reports.keys()))
        st.json(reports.get(selected, {}))


def render_governance_audit(audit: dict[str, Any]) -> None:
    st.header("12. Governance Audit")
    st.caption("Self-auditing PAPER_ONLY governance layer. Cached, summary-first, read-only analytics only.")

    conflicts = audit.get("conflicts", []) if isinstance(audit, dict) else []
    stale_reports = audit.get("stale_reports", []) if isinstance(audit, dict) else []
    violations = audit.get("policy_violations", []) if isinstance(audit, dict) else []
    severity = str(audit.get("audit_severity", "UNKNOWN")).upper()
    governance_state = str(audit.get("governance_state", "UNKNOWN")).upper()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Consistency Score", f"{int(audit.get('consistency_score', 0))}%")
    col2.metric("Governance Health", audit.get("governance_health", "UNKNOWN"))
    col3.metric("Conflicts", len(conflicts))
    col4.metric("Stale Reports", len(stale_reports))
    col5.metric("Policy Violations", len(violations))
    st.metric("Governance State", governance_state)

    if severity == "CRITICAL":
        st.error(f"Audit Severity: {severity}")
    elif severity in {"HIGH", "MEDIUM"}:
        st.warning(f"Audit Severity: {severity}")
    elif severity == "LOW":
        st.info(f"Audit Severity: {severity}")
    else:
        st.success(f"Audit Severity: {severity}")

    if stale_reports:
        st.warning(f"Stale report warnings: {len(stale_reports)} artifact(s) exceeded the audit freshness threshold. Run: python main.py --refresh-governance-reports")

    artifact_health = audit.get("artifact_health", {}) if isinstance(audit.get("artifact_health"), dict) else {}
    if artifact_health:
        st.subheader("Governance Artifact Freshness")
        rows = []
        for report_name, details in artifact_health.items():
            path = details.get("path", "") if isinstance(details, dict) else ""
            rows.append({"report": report_name, **report_freshness(path, read_json_report(path))})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    brake_context = audit.get("brake_context") if isinstance(audit.get("brake_context"), dict) else {}
    if brake_context:
        st.subheader("Emergency Brake Context")
        col1, col2, col3 = st.columns(3)
        col1.metric("Brake Trigger Count", int(brake_context.get("trigger_count") or 0))
        col2.metric("Brake Risk Level", brake_context.get("brake_risk_level", "NONE"))
        col3.metric("Brake Source", brake_context.get("brake_source", "NONE"))

    if conflicts:
        with st.expander("Governance conflicts", expanded=False):
            st.dataframe(pd.DataFrame(conflicts), use_container_width=True, hide_index=True)
    if violations:
        with st.expander("Policy violations", expanded=False):
            st.dataframe(pd.DataFrame(violations), use_container_width=True, hide_index=True)

    st.caption("Constraints: PAPER_ONLY, read-only analytics, no execution, no deployment, no live trading, no model retraining, no Phase 3 promotion.")


def render_promotion_scorecard(scorecard: dict[str, Any]) -> None:
    st.header("11. Promotion Scorecard")
    st.caption("PAPER_ONLY governance intelligence: read-only analytics, no broker routing, no order placement, no live trading, no auto deployment, no Phase 3 promotion.")

    candidates = scorecard.get("candidates", []) if isinstance(scorecard, dict) else []
    summary = scorecard.get("summary", {}) if isinstance(scorecard, dict) else {}
    constraints = scorecard.get("governance_constraints", {}) if isinstance(scorecard, dict) else {}
    top = candidates[0] if candidates else {}

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Top Candidate", top.get("strategy_setup_name", summary.get("top_candidate", "-")))
    col2.metric("Readiness", top.get("recommendation", summary.get("top_recommendation", "HOLD")))
    col3.metric("Governance", top.get("governance_compatibility", "-"))
    col4.metric("Risk Budget Override", top.get("risk_budget_override", summary.get("risk_budget_override", "INACTIVE")))
    col5.metric("Drift", top.get("drift_risk", summary.get("drift_label", "UNKNOWN")))

    st.subheader("Top Candidate Table")
    if candidates:
        candidate_df = pd.DataFrame(candidates)
        display_columns = [
            "strategy_setup_name",
            "health_score",
            "recommendation",
            "promotion_readiness",
            "governance_compatibility",
            "risk_budget_compatibility",
            "risk_budget_override",
            "drift_risk",
            "regime_stability",
            "walkforward_quality",
        ]
        st.dataframe(candidate_df[[column for column in display_columns if column in candidate_df.columns]].head(10), use_container_width=True, hide_index=True)

        freeze_reject = candidate_df[candidate_df.get("recommendation", pd.Series(dtype=str)).isin(["FREEZE", "REJECT"])]
        if not freeze_reject.empty:
            st.warning("Freeze/reject warnings are active for one or more setups. Promotion remains blocked pending manual PAPER_ONLY review.")
            warning_cols = ["strategy_setup_name", "recommendation", "recommendation_reason"]
            st.dataframe(freeze_reject[[column for column in warning_cols if column in freeze_reject.columns]].head(10), use_container_width=True, hide_index=True)
        else:
            st.success("No FREEZE/REJECT recommendation in cached top-N candidates.")
    else:
        st.info("No promotion scorecard candidates available yet. Run python main.py --promotion-scorecard.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Recommendation Badges")
        recommendation_distribution = summary.get("recommendation_distribution", {}) or {}
        show_dataframe_or_info(
            pd.DataFrame([{"recommendation": key, "count": value} for key, value in recommendation_distribution.items()]),
            "No recommendation distribution available.",
        )
    with col2:
        st.subheader("Readiness Distribution")
        readiness_distribution = summary.get("readiness_distribution", {}) or {}
        show_dataframe_or_info(
            pd.DataFrame([{"readiness": key, "count": value} for key, value in readiness_distribution.items()]),
            "No readiness distribution available.",
        )

    st.subheader("Governance Constraints")
    st.json({
        "PAPER_ONLY": constraints.get("paper_only", "PAPER_ONLY"),
        "read_only_analytics": constraints.get("read_only_analytics", True),
        "no_real_execution": constraints.get("no_real_execution", True),
        "no_auto_deployment": constraints.get("no_auto_deployment", True),
        "no_phase_3_promotion": constraints.get("no_phase_3_promotion", True),
    })


def render_phase3_readiness(readiness: dict[str, Any]) -> None:
    st.header("13. Phase 3 Readiness")
    st.caption("PAPER_ONLY readiness tracker: read-only analytics, no execution changes, no broker routing, no strategy promotion, no Phase 3 unlock automation.")

    status = str(readiness.get("status", "LOCKED")).upper()
    blockers = readiness.get("blockers", []) if isinstance(readiness.get("blockers"), list) else []
    next_actions = readiness.get("next_actions", []) if isinstance(readiness.get("next_actions"), list) else []
    passed = readiness.get("passed_criteria", []) if isinstance(readiness.get("passed_criteria"), list) else []
    failed = readiness.get("failed_criteria", []) if isinstance(readiness.get("failed_criteria"), list) else []

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Readiness", f"{float(readiness.get('readiness_percent', 0.0)):.0f}%")
    col2.metric("Status", status)
    col3.metric("Passed", len(passed))
    col4.metric("Failed", len(failed))

    if status == "READY_FOR_REVIEW":
        st.success("READY_FOR_REVIEW means manual review can begin; it does not unlock Phase 3 automatically.")
    elif status == "CANDIDATE":
        st.warning("CANDIDATE requires manual review and blocker remediation before Phase 3 readiness can advance.")
    else:
        st.error("LOCKED: Phase 3 remains blocked by readiness criteria.")

    st.markdown(f"**Top Blocker:** {blockers[0] if blockers else 'none'}")

    details = readiness.get("criteria_details", [])
    if isinstance(details, list) and details:
        st.dataframe(pd.DataFrame(details), use_container_width=True, hide_index=True)
    else:
        st.info("No readiness criteria details available yet. Run python main.py --phase3-readiness.")

    with st.expander("Blockers and next actions", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Blockers**")
            for blocker in blockers or ["none"]:
                st.markdown(f"- {blocker}")
        with col2:
            st.markdown("**Next Actions**")
            for action in next_actions or ["none"]:
                st.markdown(f"- {action}")

    st.caption("PAPER_ONLY remains active. This section is not a deployment gate, broker router, strategy promoter, or unlock automation.")


def render_telegram_notification_center(events: pd.DataFrame) -> None:
    st.header("Telegram Notification Center")
    enabled = bool(config.telegram_enabled)
    status_badge("Telegram", "GREEN" if enabled else "YELLOW", " ENABLED" if enabled else " DISABLED / PREVIEW ONLY")

    if events.empty:
        st.info("No Telegram notification events yet. Run python main.py --telegram-test or python main.py --notify-summary.")
        return

    df = events.copy()
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp", ascending=False)

    latest = df.iloc[0]
    col1, col2, col3 = st.columns(3)
    col1.metric("Last Send Status", latest.get("send_status", "-"))
    col2.metric("Last Event Type", latest.get("event_type", "-"))
    col3.metric("Total Logged Events", len(df))

    st.subheader("Event Counts")
    if "event_type" in df.columns:
        counts = df["event_type"].fillna("UNKNOWN").value_counts().reset_index()
        counts.columns = ["event_type", "count"]
        st.dataframe(counts, use_container_width=True, hide_index=True)
    else:
        st.info("No event type counts available.")

    st.subheader("Latest Telegram Events")
    columns = ["timestamp", "event_type", "send_status", "error_message", "message"]
    st.dataframe(df[[column for column in columns if column in df.columns]].head(30), use_container_width=True, hide_index=True)


def render_macro_observer(macro: pd.DataFrame, components: pd.DataFrame) -> None:
    st.header("Real Macro Observer")
    if macro.empty:
        st.info("No macro observer data yet. Run python main.py --macro-observer.")
        return
    latest = macro.iloc[-1]
    col1, col2, col3 = st.columns(3)
    col1.metric("Macro State", latest.get("macro_state", "-"))
    col2.metric("Macro Risk Score", f"{pd.to_numeric(pd.Series([latest.get('macro_risk_score')]), errors='coerce').fillna(0).iloc[0]:.2f}/100")
    col3.metric("Sources", latest.get("source_labels", "-"))
    st.subheader("Stress Contributors")
    st.info(str(latest.get("stress_contributors", "-")))
    st.subheader("Macro Components")
    if components.empty:
        st.info("No macro component detail available.")
    else:
        st.dataframe(components, use_container_width=True, hide_index=True)


def render_cross_market_intelligence(cross_market: pd.DataFrame, components: pd.DataFrame, correlation: pd.DataFrame) -> None:
    st.header("CROSS MARKET INTELLIGENCE")
    if cross_market.empty:
        st.info("No cross-market intelligence yet. Run python main.py --cross-market.")
        return
    latest = cross_market.iloc[-1]
    state = str(latest.get("cross_market_state", "UNKNOWN"))
    stress = pd.to_numeric(pd.Series([latest.get("cross_market_stress_score")]), errors="coerce").fillna(0).iloc[0]
    badge = "RED" if stress >= 70 else "YELLOW" if stress >= 45 else "GREEN"
    status_badge("Cross Market State", badge, f" {state}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Risk Alignment", latest.get("risk_alignment", "-"))
    col2.metric("Altseason Probability", f"{pd.to_numeric(pd.Series([latest.get('altseason_probability')]), errors='coerce').fillna(0).iloc[0]:.2f}%")
    col3.metric("DXY Pressure", f"{pd.to_numeric(pd.Series([latest.get('dxy_pressure')]), errors='coerce').fillna(0).iloc[0]:.2f}")
    col4.metric("Stress Score", f"{stress:.2f}/100")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Safe Haven Rotation")
        st.info(str(latest.get("safe_haven_rotation", "-")))
        st.subheader("Stress Contributors")
        st.warning(str(latest.get("stress_contributors", "none"))) if stress >= 45 else st.info(str(latest.get("stress_contributors", "none")))
        st.subheader("Source Labels")
        st.info(str(latest.get("source_labels", "-")))
    with col2:
        st.subheader("Cross-Market Components")
        if components.empty:
            st.info("No component detail available.")
        else:
            st.dataframe(components, use_container_width=True, hide_index=True)

    st.subheader("Correlation Matrix")
    if correlation.empty:
        st.info("No correlation matrix available.")
    else:
        st.dataframe(correlation, use_container_width=True, hide_index=True)
        numeric = correlation.set_index("asset") if "asset" in correlation.columns else correlation
        numeric = numeric.apply(pd.to_numeric, errors="coerce")
        st.plotly_chart(px.imshow(numeric, text_auto=True, aspect="auto", title="Cross-Market Correlation Matrix"), use_container_width=True)


def render_strategy_genome_lab(results: pd.DataFrame, archive: pd.DataFrame) -> None:
    st.header("Strategy Genome Lab")
    if results.empty:
        st.info("No strategy genome results yet. Run python main.py --strategy-genome.")
        return
    df = results.copy()
    for column in [
        "profit_factor",
        "max_drawdown",
        "stability_score",
        "trade_count",
        "regime_survival_score",
        "macro_survival_score",
        "cross_market_survival_score",
        "overfit_risk",
        "total_pnl",
        "winrate",
    ]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column].replace("inf", float("inf")), errors="coerce")

    top = df.sort_values(["stability_score", "profit_factor", "trade_count"], ascending=[False, False, False]).head(20)
    promoted = df[df.get("status", pd.Series(dtype=str)) == "PROMOTED"].head(20)
    rejected = df[df.get("status", pd.Series(dtype=str)) == "REJECTED"].sort_values("overfit_risk", ascending=False).head(20)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Strategies", len(df))
    col2.metric("Promoted", len(promoted))
    col3.metric("Rejected", int((df.get("status", pd.Series(dtype=str)) == "REJECTED").sum()))
    col4.metric("Best Stability", f"{pd.to_numeric(top.get('stability_score', pd.Series(dtype=float)), errors='coerce').max() or 0:.2f}")

    st.subheader("Top Ranked Strategies")
    top_columns = [
        "strategy_id",
        "strategy_name",
        "status",
        "profit_factor",
        "max_drawdown",
        "winrate",
        "trade_count",
        "stability_score",
        "overfit_risk",
    ]
    st.dataframe(top[[column for column in top_columns if column in top.columns]], use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Promoted Candidates")
        show_dataframe_or_info(promoted[[column for column in top_columns if column in promoted.columns]], "No promoted candidates. Manual review required before any future production use.")
        st.subheader("Regime Survival")
        survival_cols = ["strategy_id", "regime_survival_score", "macro_survival_score", "cross_market_survival_score"]
        show_dataframe_or_info(top[[column for column in survival_cols if column in top.columns]], "No survival score data.")
    with col2:
        st.subheader("Rejected Strategies")
        show_dataframe_or_info(rejected[[column for column in top_columns if column in rejected.columns]], "No rejected strategies.")
        st.subheader("Macro Survival")
        macro_cols = ["strategy_id", "macro_filter", "cross_market_filter", "macro_survival_score", "cross_market_survival_score"]
        show_dataframe_or_info(top[[column for column in macro_cols if column in top.columns]], "No macro survival data.")

    if {"profit_factor", "max_drawdown", "status", "strategy_name"}.issubset(df.columns):
        plot_df = df.copy()
        plot_df["profit_factor"] = plot_df["profit_factor"].replace(float("inf"), 5.0).fillna(0.0).clip(upper=5.0)
        st.plotly_chart(
            px.scatter(
                plot_df,
                x="max_drawdown",
                y="profit_factor",
                color="status",
                size="trade_count" if "trade_count" in plot_df.columns else None,
                hover_name="strategy_name",
                title="PF vs DD Strategy Comparison",
            ),
            use_container_width=True,
        )

    st.subheader("Mutation History")
    if archive.empty:
        st.info("No archive history yet.")
    else:
        history = archive.tail(100).sort_index(ascending=False)
        st.dataframe(history[[column for column in top_columns if column in history.columns]], use_container_width=True, hide_index=True)


def render_daily_ops_report(report: dict[str, Any]) -> None:
    st.header("Daily Ops Report")
    if not report:
        st.info("No daily ops report yet. Run python main.py --daily-ops-report.")
        return
    runtime = str(report.get("runtime_status", "WATCH")).upper()
    warnings = report.get("top_warning_reasons") or ["none"]
    status_badge("Daily Ops Health", "GREEN" if runtime == "OK" else "YELLOW", f" {runtime}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Heartbeat Age", f"{float(report.get('heartbeat_age_minutes', 0) or 0):.2f}m")
    col2.metric("Macro", report.get("macro_state", "-"))
    col3.metric("Cross Market", report.get("cross_market_state", "-"))
    col4.metric("Telegram", report.get("latest_telegram_send_status", "-"))

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Health Summary")
        st.dataframe(
            pd.DataFrame(
                [
                    {"metric": "heartbeat_source", "value": report.get("heartbeat_source", "-")},
                    {"metric": "database_ok", "value": report.get("database_ok", "-")},
                    {"metric": "guardian_status", "value": report.get("latest_guardian_status", "-")},
                    {"metric": "broadcast_routed", "value": report.get("broadcast_accepted_count", 0)},
                    {"metric": "broadcast_rejected", "value": report.get("broadcast_rejected_count", 0)},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.subheader("Warning Summary")
        st.dataframe(pd.DataFrame({"warning": warnings}), use_container_width=True, hide_index=True)
    with col2:
        st.subheader("Paper Performance Snapshot")
        st.dataframe(
            pd.DataFrame(
                [
                    {"metric": "trade_count", "value": report.get("internal_paper_trade_count", 0)},
                    {"metric": "total_pnl", "value": report.get("internal_paper_total_pnl", 0)},
                    {"metric": "max_drawdown", "value": report.get("internal_paper_max_drawdown", 0)},
                    {"metric": "macro_risk_score", "value": report.get("macro_risk_score", 0)},
                    {"metric": "cross_market_stress_score", "value": report.get("cross_market_stress_score", 0)},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        st.subheader("Recommended Next Action")
        st.info(str(report.get("recommended_next_action", "-")))


def render_incident_anomaly_intelligence(anomalies: pd.DataFrame, incident: dict[str, Any]) -> None:
    st.header("Incident & Anomaly Intelligence")
    if anomalies.empty and not incident:
        st.info("No anomaly report yet. Run python main.py --anomaly-scan or python main.py --incident-report.")
        return

    active_count = int(incident.get("active_incident_count", 0) or 0)
    critical_count = int(incident.get("critical_count", 0) or 0)
    warning_count = int(incident.get("warning_count", 0) or 0)
    health_color = "RED" if critical_count else "YELLOW" if warning_count else "GREEN"
    status_badge("Incident Health", health_color, f" active={active_count}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Active Incidents", active_count)
    col2.metric("Critical", critical_count)
    col3.metric("Warnings", warning_count)
    col4.metric("Telegram", incident.get("telegram_result", {}).get("send_status", "SKIPPED"))

    if anomalies.empty:
        st.info("Incident JSON exists but anomaly CSV is empty or unavailable.")
        return

    df = anomalies.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    if "score" in df.columns:
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)

    active = df[df.get("severity", pd.Series(dtype=str)).astype(str).str.upper().isin(["WARNING", "CRITICAL"])]
    st.subheader("Active Incidents")
    if active.empty:
        st.success("No active WARNING/CRITICAL incidents.")
    else:
        cols = ["timestamp", "severity", "anomaly_type", "score", "reason", "recommended_action"]
        st.dataframe(active[[column for column in cols if column in active.columns]], use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Severity Distribution")
        if "severity" in df.columns:
            severity = df["severity"].fillna("INFO").astype(str).value_counts().reset_index()
            severity.columns = ["severity", "count"]
            safe_plot_bar(severity, "severity", "count", "Anomaly Severity Distribution")
        else:
            st.info("No severity data available.")
    with col2:
        st.subheader("Top Recurring Incidents")
        if "anomaly_type" in df.columns:
            recurring = df["anomaly_type"].fillna("UNKNOWN").astype(str).value_counts().head(10).reset_index()
            recurring.columns = ["anomaly_type", "count"]
            st.dataframe(recurring, use_container_width=True, hide_index=True)
        else:
            st.info("No anomaly type data available.")

    st.subheader("Anomaly Timeline")
    if {"timestamp", "score", "anomaly_type"}.issubset(df.columns) and df["timestamp"].notna().any():
        st.plotly_chart(
            px.scatter(
                df.sort_values("timestamp"),
                x="timestamp",
                y="score",
                color="severity" if "severity" in df.columns else None,
                hover_name="anomaly_type",
                title="Anomaly Score Timeline",
            ),
            use_container_width=True,
        )
    else:
        st.info("Timeline unavailable until timestamped anomaly rows exist.")

    st.subheader("Recommended Operator Action")
    st.warning(str(incident.get("recommended_operator_action") or "Continue PAPER_ONLY monitoring."))


def render_orchestrator_diagnostics(diagnostics: dict[str, Any]) -> None:
    st.subheader("Orchestrator Diagnostics")
    if not diagnostics:
        st.info("No orchestrator diagnostics yet. Run python main.py --orchestrator or python main.py --orchestrator-diagnostics.")
        return
    last_event = diagnostics.get("last_event") or {}
    last_error = diagnostics.get("last_error") or "-"
    crash_count = int(diagnostics.get("crash_count_last_24h", 0) or 0)
    status_badge("Orchestrator Crash Watch", "RED" if crash_count else "GREEN", f" crashes_24h={crash_count}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Last Cycle Time", diagnostics.get("last_cycle_time") or "-")
    col2.metric("Last Step", diagnostics.get("last_completed_step") or last_event.get("last_completed_step") or "-")
    col3.metric("Crash Count 24h", crash_count)
    col4.metric("Heartbeat Written", str(last_event.get("heartbeat_written")))
    col1, col2 = st.columns(2)
    col1.metric("Last Keepalive", diagnostics.get("last_keepalive_source") or "-")
    col2.metric("Long Running Module", diagnostics.get("long_running_module_status") or "IDLE")
    st.caption(f"Last error: {last_error}")


def _registry_age_days(production: dict[str, Any] | None) -> str:
    if not production or not production.get("train_timestamp"):
        return "-"
    timestamp = pd.to_datetime(production.get("train_timestamp"), errors="coerce", utc=True)
    if pd.isna(timestamp):
        return "-"
    age = (datetime.now(timezone.utc) - timestamp.to_pydatetime()).total_seconds() / 86400
    return f"{max(age, 0):.1f}d"


def render_ml_lifecycle(registry: dict[str, Any]) -> None:
    st.header("ML Lifecycle & Drift Monitor")
    if not registry:
        st.info("No model registry yet. Run python main.py --retrain-model.")
        return
    production = registry.get("production") or {}
    candidate = registry.get("candidate") or {}
    warnings = registry.get("warnings") or ["none"]
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Production Model")
        st.metric("Version", production.get("version", "-") if production else "-")
        st.metric("Age", _registry_age_days(production))
        st.metric("PF / DD", f"{production.get('profit_factor', 0):.2f} / {production.get('max_drawdown', 0):.2f}" if production else "-")
        st.metric("Walkforward PF", f"{production.get('walkforward_profit_factor', 0):.2f}" if production else "-")
    with col2:
        st.subheader("Latest Candidate")
        st.metric("Version", candidate.get("version", "-") if candidate else "-")
        st.metric("Status", candidate.get("status", "-") if candidate else "-")
        st.metric("Accuracy", f"{candidate.get('accuracy', 0):.2%}" if candidate else "-")
        st.metric("PF / DD", f"{candidate.get('profit_factor', 0):.2f} / {candidate.get('max_drawdown', 0):.2f}" if candidate else "-")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Warnings")
        if warnings == ["none"]:
            st.success("No active model lifecycle warnings.")
        else:
            for warning in warnings:
                st.warning(str(warning))
    with col2:
        st.subheader("Rollback")
        status_badge("Rollback Available", "GREEN" if registry.get("rollback_available") else "YELLOW", " YES" if registry.get("rollback_available") else " NO")
        st.metric("Latest Retrain", candidate.get("train_timestamp", "-") if candidate else "-")

    if any(str(item).startswith("DRIFT WARNING") for item in warnings):
        st.warning("DRIFT WARNING")
    if any(str(item).startswith("MODEL AGING") for item in warnings):
        st.warning("MODEL AGING")
    if any(str(item).startswith("RETRAIN RECOMMENDED") for item in warnings):
        st.warning("RETRAIN RECOMMENDED")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "production_version": production.get("version", "-") if production else "-",
                    "candidate_version": candidate.get("version", "-") if candidate else "-",
                    "candidate_status": candidate.get("status", "-") if candidate else "-",
                    "warnings": " | ".join(map(str, warnings)),
                }
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


def render_shadow_penalty_insight(signals: pd.DataFrame) -> None:
    st.subheader("Adaptive Regime Shadow Penalty")
    required = {"calculated_score", "shadow_score", "symbol"}
    if signals.empty or not required.issubset(set(signals.columns)):
        st.info("No shadow penalty analytics yet. New signals will populate calculated_score and shadow_score.")
        return

    df = signals.copy()
    df["calculated_score"] = pd.to_numeric(df["calculated_score"], errors="coerce")
    df["shadow_score"] = pd.to_numeric(df["shadow_score"], errors="coerce")
    df = df.dropna(subset=["calculated_score", "shadow_score"])
    if df.empty:
        st.info("Shadow penalty analytics are not numeric yet.")
        return

    df["penalty_impact"] = df["calculated_score"] - df["shadow_score"]
    average_calculated = df["calculated_score"].mean()
    average_shadow = df["shadow_score"].mean()
    impact_percent = ((average_calculated - average_shadow) / average_calculated * 100) if average_calculated else 0.0

    affected = int((df["penalty_impact"] > 0).sum())
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Avg Calculated Score", f"{average_calculated:.2f}")
    col2.metric("Avg Shadow Score", f"{average_shadow:.2f}")
    col3.metric("Regime Penalty Impact", f"{impact_percent:.2f}%")
    col4.metric("Affected Signals", affected)

    columns = ["symbol", "regime_name", "calculated_score", "shadow_score", "penalty_impact"]
    top_affected = df.sort_values("penalty_impact", ascending=False)
    st.dataframe(top_affected[[c for c in columns if c in top_affected.columns]].head(20), use_container_width=True)


@st.cache_data(ttl=REFRESH_SECONDS)
def read_historical_outcomes(limit: int = 500) -> pd.DataFrame:
    try:
        with _connect() as connection:
            df = pd.read_sql_query(
                """
                SELECT
                    o.signal_timestamp AS timestamp,
                    o.symbol,
                    o.entry,
                    o.exit_price AS current_price,
                    o.pnl_pct AS pnl_percent,
                    o.status,
                    o.score,
                    COALESCE(NULLIF(NULLIF(s.regime_name, ''), 'UNKNOWN'), 'HISTORICAL_DERIVED') AS regime_name
                FROM historical_outcomes o
                LEFT JOIN signals s
                  ON s.symbol = o.symbol
                 AND s.timestamp = o.signal_timestamp
                ORDER BY o.id DESC
                LIMIT ?
                """,
                connection,
                params=(limit,),
            )
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        return df
    except Exception:
        return _empty_df()


def pnl_curve(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "pnl_percent" not in trades.columns:
        return pd.DataFrame(columns=["trade", "equity"])
    df = trades.sort_values("timestamp").copy()
    df["pnl_percent"] = pd.to_numeric(df["pnl_percent"], errors="coerce").fillna(0.0)
    df["equity"] = df["pnl_percent"].cumsum()
    df["trade"] = range(1, len(df) + 1)
    return df[["trade", "equity"]]


def current_drawdown(curve: pd.DataFrame) -> float:
    if curve.empty:
        return 0.0
    running_max = curve["equity"].cummax()
    return float((curve["equity"] - running_max).min())


def safe_plot_line(df: pd.DataFrame, x: str, y: str, title: str):
    if df.empty or x not in df.columns or y not in df.columns:
        st.info("No data yet.")
        return
    st.plotly_chart(px.line(df.sort_values(x), x=x, y=y, title=title), use_container_width=True)


def safe_plot_bar(df: pd.DataFrame, x: str, y: str, title: str):
    if df.empty or x not in df.columns or y not in df.columns:
        st.info("No data yet.")
        return
    st.plotly_chart(px.bar(df, x=x, y=y, title=title), use_container_width=True)


def load_feature_importance(ml_results: pd.DataFrame) -> pd.DataFrame:
    if ml_results.empty or "payload_json" not in ml_results.columns:
        return pd.DataFrame(columns=["feature", "importance"])
    payload = ml_results.iloc[0].get("payload_json")
    try:
        data = json.loads(payload or "{}")
        return pd.DataFrame(data.get("feature_importance", []))
    except json.JSONDecodeError:
        return pd.DataFrame(columns=["feature", "importance"])


def query_helper_df(fn) -> pd.DataFrame:
    try:
        result = fn(DB_PATH)
        if isinstance(result, list):
            return pd.DataFrame(result)
        if isinstance(result, dict) and result:
            return pd.DataFrame([result])
    except Exception:
        pass
    return pd.DataFrame()


def _safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _with_winrate(df: pd.DataFrame, pnl_column: str = "pnl_pct") -> pd.DataFrame:
    if df.empty or pnl_column not in df.columns:
        return df
    temp = df.copy()
    temp["win"] = (_safe_numeric(temp[pnl_column]) > 0).astype(int)
    return temp


@st.cache_data(ttl=DATABASE_ANALYTICS_TTL_SECONDS)
def read_symbol_performance(limit: int = DATABASE_ANALYTICS_DISPLAY_ROWS) -> pd.DataFrame:
    try:
        with _connect() as connection:
            return pd.read_sql_query(
                """
                SELECT
                    symbol,
                    COUNT(*) AS trades,
                    AVG(pnl_pct) AS avg_pnl,
                    SUM(pnl_pct) AS total_pnl,
                    AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS winrate
                FROM historical_outcomes
                GROUP BY symbol
                ORDER BY total_pnl DESC
                LIMIT ?
                """,
                connection,
                params=(limit,),
            )
    except Exception:
        return _empty_df()


@st.cache_data(ttl=DATABASE_ANALYTICS_TTL_SECONDS)
def read_worst_symbol_performance(limit: int = DATABASE_ANALYTICS_DISPLAY_ROWS) -> pd.DataFrame:
    try:
        with _connect() as connection:
            return pd.read_sql_query(
                """
                SELECT
                    symbol,
                    COUNT(*) AS trades,
                    AVG(pnl_pct) AS avg_pnl,
                    SUM(pnl_pct) AS total_pnl,
                    AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS winrate
                FROM historical_outcomes
                GROUP BY symbol
                ORDER BY total_pnl ASC
                LIMIT ?
                """,
                connection,
                params=(limit,),
            )
    except Exception:
        return _empty_df()


@st.cache_data(ttl=DATABASE_ANALYTICS_TTL_SECONDS)
def read_regime_performance(limit: int = DATABASE_ANALYTICS_DISPLAY_ROWS) -> pd.DataFrame:
    try:
        with _connect() as connection:
            return pd.read_sql_query(
                """
                SELECT
                    COALESCE(NULLIF(NULLIF(s.regime_name, ''), 'UNKNOWN'), 'HISTORICAL_DERIVED') AS regime_name,
                    COUNT(*) AS trades,
                    AVG(o.pnl_pct) AS avg_pnl,
                    SUM(o.pnl_pct) AS total_pnl,
                    AVG(CASE WHEN o.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS winrate
                FROM historical_outcomes o
                LEFT JOIN signals s
                  ON s.symbol = o.symbol
                 AND s.timestamp = o.signal_timestamp
                GROUP BY COALESCE(NULLIF(NULLIF(s.regime_name, ''), 'UNKNOWN'), 'HISTORICAL_DERIVED')
                ORDER BY avg_pnl DESC
                LIMIT ?
                """,
                connection,
                params=(limit,),
            )
    except Exception:
        return _empty_df()


@st.cache_data(ttl=DATABASE_ANALYTICS_TTL_SECONDS)
def read_feature_profitability_from_history(limit: int = DATABASE_ANALYTICS_DISPLAY_ROWS) -> pd.DataFrame:
    try:
        with _connect() as connection:
            df = pd.read_sql_query(
                """
                SELECT
                    o.pnl_pct,
                    s.score,
                    s.volume_spike,
                    s.breakout,
                    s.liquidity_sweep,
                    f.funding_zscore,
                    f.oi_expansion_rate,
                    f.taker_delta,
                    f.pressure_score,
                    f.squeeze_risk,
                    f.whale_activity
                FROM historical_outcomes o
                LEFT JOIN signals s
                  ON s.symbol = o.symbol
                 AND s.timestamp = o.signal_timestamp
                LEFT JOIN flow_logs f
                  ON f.symbol = o.symbol
                 AND f.timestamp = o.signal_timestamp
                """,
                connection,
            )
    except Exception:
        return _empty_df()
    if df.empty or "pnl_pct" not in df.columns:
        return _empty_df()

    for column in ["pnl_pct", "score", "volume_spike", "funding_zscore", "oi_expansion_rate", "taker_delta", "pressure_score"]:
        if column in df.columns:
            df[column] = _safe_numeric(df[column])
    for column in ["breakout", "liquidity_sweep"]:
        if column in df.columns:
            df[column] = df[column].astype(str).str.lower().isin(["true", "1", "yes"])

    checks = [
        ("score >= 75", df["score"] >= 75),
        ("score >= 85", df["score"] >= 85),
        ("volume_spike >= 2", df["volume_spike"] >= 2),
        ("volume_spike >= 3", df["volume_spike"] >= 3),
        ("breakout = true", df["breakout"]),
        ("liquidity_sweep = true", df["liquidity_sweep"]),
        ("funding_zscore abs < 1", df["funding_zscore"].abs() < 1),
        ("oi_expansion_rate > 0", df["oi_expansion_rate"] > 0),
        ("taker_delta > 0.10", df["taker_delta"] > 0.10),
        ("pressure_score >= 60", df["pressure_score"] >= 60),
        ("squeeze_risk = LOW", df["squeeze_risk"].fillna("").astype(str).str.upper() == "LOW"),
        ("whale accumulation", df["whale_activity"].fillna("").astype(str).str.upper().str.contains("ACCUMULATION")),
    ]

    rows = []
    for feature, mask in checks:
        subset = df[mask.fillna(False) if hasattr(mask, "fillna") else mask]
        if subset.empty:
            continue
        pnl = _safe_numeric(subset["pnl_pct"])
        rows.append(
            {
                "feature": feature,
                "trades": int(len(subset)),
                "winrate": float((pnl > 0).mean() * 100),
                "avg_pnl": float(pnl.mean()),
                "total_pnl": float(pnl.sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["avg_pnl", "trades"], ascending=[False, False]).head(limit)


@st.cache_data(ttl=DATABASE_ANALYTICS_TTL_SECONDS)
def read_optimizer_setups(path: str = "optimizer_results.csv", limit: int = DATABASE_ANALYTICS_DISPLAY_ROWS) -> pd.DataFrame:
    if not os.path.exists(path):
        return _empty_df()
    try:
        df = pd.read_csv(path)
    except Exception:
        return _empty_df()
    if df.empty:
        return _empty_df()
    if "profit_factor" in df.columns:
        df["profit_factor_numeric"] = pd.to_numeric(
            df["profit_factor"].replace("inf", float("inf")),
            errors="coerce",
        ).fillna(0.0)
        df = df.sort_values(["profit_factor_numeric", "expectancy", "trade_count"], ascending=[False, False, False])
        df = df.drop(columns=["profit_factor_numeric"])
    columns = [
        column
        for column in ["setup", "profit_factor", "winrate", "trade_count", "avg_pnl", "max_drawdown", "expectancy", "regime"]
        if column in df.columns
    ]
    return df[columns].head(limit)


@st.cache_data(ttl=REFRESH_SECONDS)
def read_shadow_simulation(
    equity_path: str = "shadow_equity_curve.csv",
    comparison_path: str = "shadow_comparison.csv",
    tuning_path: str = "logs/shadow_threshold_tuning.csv",
    walkforward_path: str = "logs/shadow_threshold_walkforward.csv",
    adaptive_comparison_path: str = "logs/adaptive_threshold_comparison.csv",
    adaptive_walkforward_path: str = "logs/adaptive_walkforward.csv",
    macro_stress_path: str = "logs/macro_stress_summary.csv",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    equity = _empty_df()
    comparison = _empty_df()
    tuning = _empty_df()
    walkforward = _empty_df()
    adaptive_comparison = _empty_df()
    adaptive_walkforward = _empty_df()
    macro_stress = _empty_df()
    try:
        if os.path.exists(equity_path):
            equity = pd.read_csv(equity_path)
    except Exception:
        equity = _empty_df()
    try:
        if os.path.exists(comparison_path):
            comparison = pd.read_csv(comparison_path)
    except Exception:
        comparison = _empty_df()
    try:
        if os.path.exists(tuning_path):
            tuning = pd.read_csv(tuning_path)
    except Exception:
        tuning = _empty_df()
    try:
        if os.path.exists(walkforward_path):
            walkforward = pd.read_csv(walkforward_path)
    except Exception:
        walkforward = _empty_df()
    try:
        if os.path.exists(adaptive_comparison_path):
            adaptive_comparison = pd.read_csv(adaptive_comparison_path)
    except Exception:
        adaptive_comparison = _empty_df()
    try:
        if os.path.exists(adaptive_walkforward_path):
            adaptive_walkforward = pd.read_csv(adaptive_walkforward_path)
    except Exception:
        adaptive_walkforward = _empty_df()
    try:
        if os.path.exists(macro_stress_path):
            macro_stress = pd.read_csv(macro_stress_path)
    except Exception:
        macro_stress = _empty_df()
    return equity, comparison, tuning, walkforward, adaptive_comparison, adaptive_walkforward, macro_stress


def _comparison_value(comparison: pd.DataFrame, metric: str, column: str = "value", default: float = 0.0) -> float:
    if comparison.empty or "metric" not in comparison.columns or column not in comparison.columns:
        return default
    rows = comparison[comparison["metric"] == metric]
    if rows.empty:
        return default
    value = pd.to_numeric(rows.iloc[0].get(column), errors="coerce")
    return default if pd.isna(value) else float(value)


def render_shadow_simulation() -> None:
    st.header("Shadow Penalty Simulation")
    equity, comparison, tuning, walkforward, adaptive_comparison, adaptive_walkforward, macro_stress = read_shadow_simulation()
    if equity.empty or comparison.empty:
        st.info("No shadow simulation data yet. Run python main.py --shadow-analysis.")
    else:
        dd_reduction = _comparison_value(comparison, "drawdown_reduction_pct")
        trade_reduction = _comparison_value(comparison, "trade_reduction_pct")
        avoided_losses = _comparison_value(comparison, "avoided_losses")
        skipped_winners = _comparison_value(comparison, "skipped_winners")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("DD Reduction", f"{dd_reduction:.2f}%")
        col2.metric("Trade Reduction", f"{trade_reduction:.2f}%")
        col3.metric("Avoided Losses", f"{avoided_losses:.0f}")
        col4.metric("Skipped Winners", f"{skipped_winners:.0f}")

        curve_columns = [column for column in ["trade_index", "equity_original", "equity_shadow"] if column in equity.columns]
        if len(curve_columns) == 3:
            curve = equity[curve_columns].copy()
            curve = curve.melt(id_vars="trade_index", var_name="curve", value_name="equity")
            fig = px.line(curve, x="trade_index", y="equity", color="curve", title="Original vs Shadow Equity Curve")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Shadow equity curve columns are incomplete.")

        section = comparison.get("section", pd.Series(dtype=str))
        regime = comparison[section == "regime"].copy()
        if not regime.empty:
            st.subheader("Regime Impact Summary")
            st.dataframe(regime.head(50), use_container_width=True)

    st.subheader("Threshold Tuning")
    if tuning.empty:
        st.info("No threshold tuning data yet. Run python main.py --shadow-analysis.")
        return
    for column in ["profit_factor", "max_drawdown", "trade_count", "dd_reduction_pct"]:
        if column in tuning.columns:
            tuning[column] = pd.to_numeric(tuning[column], errors="coerce")
    thresholds = tuning["threshold"].dropna().tolist() if "threshold" in tuning.columns else []
    if thresholds:
        selected = st.selectbox("Threshold", thresholds, index=len(thresholds) - 1)
        selected_row = tuning[tuning["threshold"] == selected]
        if not selected_row.empty:
            st.dataframe(selected_row, use_container_width=True, hide_index=True)
    if {"profit_factor", "dd_reduction_pct", "trade_count"}.issubset(tuning.columns):
        min_trades = max(int(pd.to_numeric(tuning["trade_count"], errors="coerce").max() * 0.05), 1)
        useful = tuning[
            (tuning["profit_factor"] > 1.05)
            & (tuning["dd_reduction_pct"] > 10)
            & (tuning["trade_count"] >= min_trades)
        ]
        if not useful.empty:
            st.success("Useful threshold candidates found.")
            st.dataframe(useful, use_container_width=True, hide_index=True)
    st.dataframe(tuning, use_container_width=True, hide_index=True)

    st.subheader("Threshold Walkforward Validation")
    if walkforward.empty:
        st.info("No shadow threshold walkforward data yet. Run python main.py --shadow-analysis.")
    else:
        for column in [
            "calibration_profit_factor",
            "calibration_max_drawdown",
            "calibration_trade_count",
            "forward_profit_factor",
            "forward_max_drawdown",
            "forward_trade_count",
            "stability_score",
        ]:
            if column in walkforward.columns:
                walkforward[column] = pd.to_numeric(walkforward[column], errors="coerce")
        if {"forward_profit_factor", "forward_max_drawdown", "forward_trade_count", "stability_score"}.issubset(walkforward.columns):
            max_forward_dd = abs(pd.to_numeric(walkforward["forward_max_drawdown"], errors="coerce")).replace(0, pd.NA).max()
            min_forward_trades = max(int(pd.to_numeric(walkforward["forward_trade_count"], errors="coerce").max() * 0.05), 1)
            dd_limit = max_forward_dd * 0.75 if pd.notna(max_forward_dd) else 0
            recommended = walkforward[
                (walkforward["forward_profit_factor"] > 1.05)
                & (abs(walkforward["forward_max_drawdown"]) <= dd_limit)
                & (walkforward["forward_trade_count"] >= min_forward_trades)
                & (walkforward["stability_score"] >= 55)
            ].sort_values(["stability_score", "forward_profit_factor", "forward_trade_count"], ascending=[False, False, False])
            if not recommended.empty:
                best = recommended.iloc[0]
                st.success(f"Recommended threshold: {best.get('threshold')}")
                st.dataframe(recommended.head(5), use_container_width=True, hide_index=True)
            elif "recommended_candidate" in walkforward.columns:
                flagged = walkforward[walkforward["recommended_candidate"].astype(str).str.lower() == "true"]
                if not flagged.empty:
                    st.success(f"Recommended threshold: {flagged.iloc[0].get('threshold')}")
                    st.dataframe(flagged, use_container_width=True, hide_index=True)
        st.dataframe(walkforward, use_container_width=True, hide_index=True)

    st.subheader("Adaptive Threshold Strategy")
    if adaptive_comparison.empty:
        st.info("No adaptive threshold comparison yet. Run python main.py --shadow-analysis.")
    else:
        for column in ["profit_factor", "max_drawdown", "trade_count"]:
            if column in adaptive_comparison.columns:
                adaptive_comparison[column] = pd.to_numeric(adaptive_comparison[column], errors="coerce")
        col1, col2, col3 = st.columns(3)
        if "profit_factor" in adaptive_comparison.columns and not adaptive_comparison.empty:
            best_pf = adaptive_comparison.sort_values("profit_factor", ascending=False).iloc[0]
            col1.metric("Best PF", f"{best_pf.get('strategy')}: {best_pf.get('profit_factor'):.2f}")
        if "max_drawdown" in adaptive_comparison.columns and not adaptive_comparison.empty:
            dd_frame = adaptive_comparison.copy()
            dd_frame["dd_abs"] = abs(dd_frame["max_drawdown"])
            best_dd = dd_frame.sort_values("dd_abs", ascending=True).iloc[0]
            col2.metric("Best DD Protection", f"{best_dd.get('strategy')}: {best_dd.get('max_drawdown'):.2f}")
        if not adaptive_walkforward.empty and "stability_score" in adaptive_walkforward.columns:
            adaptive_walkforward["stability_score"] = pd.to_numeric(adaptive_walkforward["stability_score"], errors="coerce")
            best_stability = adaptive_walkforward.sort_values("stability_score", ascending=False).iloc[0]
            col3.metric("Best Forward Stability", f"{best_stability.get('strategy')}: {best_stability.get('stability_score'):.2f}")
        st.dataframe(adaptive_comparison, use_container_width=True, hide_index=True)

    if not adaptive_walkforward.empty:
        st.subheader("Adaptive Walkforward")
        st.dataframe(adaptive_walkforward, use_container_width=True, hide_index=True)

    st.subheader("Macro Stress Summary")
    if macro_stress.empty:
        st.info("No macro stress summary yet. Run python main.py --shadow-analysis.")
    else:
        for column in [
            "high_stress_rows",
            "high_stress_pct",
            "trades_filtered_by_macro_override",
            "max_dd_impact",
            "profit_factor_impact",
        ]:
            if column in macro_stress.columns:
                macro_stress[column] = pd.to_numeric(macro_stress[column], errors="coerce")
        row = macro_stress.iloc[0]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("High Stress Rows", f"{row.get('high_stress_rows', 0):.0f}")
        col2.metric("Macro Filtered Trades", f"{row.get('trades_filtered_by_macro_override', 0):.0f}")
        col3.metric("DD Impact", f"{row.get('max_dd_impact', 0):.2f}")
        col4.metric("PF Impact", f"{row.get('profit_factor_impact', 0):.2f}")
        st.dataframe(macro_stress, use_container_width=True, hide_index=True)


def show_dataframe_or_info(df: pd.DataFrame, message: str) -> None:
    if df.empty:
        st.info(message)
    else:
        st.dataframe(df, use_container_width=True)


def render_alerts(signals: pd.DataFrame, trades: pd.DataFrame, ml_results: pd.DataFrame) -> None:
    alerts = []
    try:
        db_health = db_health_check(DB_PATH, migrate_csv=False, backup=False)
        if not db_health.get("ok"):
            alerts.append(("RED", "DB gagal write / health check gagal."))
    except Exception as exc:
        alerts.append(("RED", f"DB health error: {exc}"))

    if not ml_results.empty and "accuracy" in ml_results.columns:
        accuracy = pd.to_numeric(ml_results.iloc[0].get("accuracy"), errors="coerce")
        if pd.notna(accuracy) and accuracy < 0.4:
            alerts.append(("YELLOW", "ML accuracy drop di bawah 40%."))

    curve = pnl_curve(trades)
    dd = abs(current_drawdown(curve))
    if dd > 20:
        alerts.append(("RED", "Drawdown terlalu tinggi, di atas 20%."))

    if not signals.empty and "timestamp" in signals.columns:
        last_signal = signals["timestamp"].dropna().max()
        age = minutes_since(last_signal)
        if age is not None and age > 180:
            alerts.append(("YELLOW", "Tidak ada signal baru lebih dari 3 jam."))
    else:
        alerts.append(("YELLOW", "Belum ada signal di database."))

    for color, text in alerts:
        if color == "RED":
            st.error(text)
        else:
            st.warning(text)



def _limit_database_analytics_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.head(DATABASE_ANALYTICS_DISPLAY_ROWS)


def _database_analytics_cached_summary(analytics: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "generated_at_utc": analytics.get("generated_at_utc", "-"),
                "top_symbols_rows": len(analytics.get("symbol_perf", _empty_df())),
                "worst_symbols_rows": len(analytics.get("worst_symbol_perf", _empty_df())),
                "regime_rows": len(analytics.get("regime_perf", _empty_df())),
                "feature_rows": len(analytics.get("feature_profit", _empty_df())),
                "optimizer_rows": len(analytics.get("optimizer_setups", _empty_df())),
            }
        ]
    )


def collect_database_analytics() -> dict[str, Any]:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol_perf": read_symbol_performance(),
        "worst_symbol_perf": read_worst_symbol_performance(),
        "regime_perf": read_regime_performance(),
        "feature_profit": read_feature_profitability_from_history(),
        "optimizer_setups": read_optimizer_setups(),
    }


def render_database_analytics_result(analytics: dict[str, Any], ml_results: pd.DataFrame) -> None:
    symbol_perf = _limit_database_analytics_rows(analytics.get("symbol_perf", _empty_df()))
    worst_symbol_perf = _limit_database_analytics_rows(analytics.get("worst_symbol_perf", _empty_df()))
    regime_perf = _limit_database_analytics_rows(analytics.get("regime_perf", _empty_df()))
    feature_profit = _limit_database_analytics_rows(analytics.get("feature_profit", _empty_df()))
    optimizer_setups = _limit_database_analytics_rows(analytics.get("optimizer_setups", _empty_df()))

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Top Symbols")
        show_dataframe_or_info(
            symbol_perf,
            "No symbol performance yet. Run historical backfill and outcome labeling first.",
        )

        st.subheader("Top Profitable Setup")
        if optimizer_setups.empty and not regime_perf.empty:
            fallback_setup = regime_perf.head(10).copy()
            fallback_setup["setup"] = "regime=" + fallback_setup["regime_name"].astype(str)
            optimizer_fallback_cols = ["setup", "winrate", "trades", "avg_pnl", "total_pnl"]
            show_dataframe_or_info(fallback_setup[optimizer_fallback_cols], "No optimizer setup data yet.")
        else:
            show_dataframe_or_info(
                optimizer_setups,
                "No optimizer setup data yet. Run python main.py --optimize-filters.",
            )

        st.subheader("Best Regime")
        show_dataframe_or_info(
            regime_perf,
            "No regime performance yet. Run python main.py --fix-regime-labels after historical labeling.",
        )

    with col2:
        st.subheader("Best / Worst Symbol")
        if symbol_perf.empty and worst_symbol_perf.empty:
            st.info("No symbol PnL ranking yet.")
        else:
            best_worst = pd.concat(
                [
                    symbol_perf.head(5).assign(side="BEST"),
                    worst_symbol_perf.head(5).assign(side="WORST"),
                ],
                ignore_index=True,
            )
            st.dataframe(best_worst, use_container_width=True)

        st.subheader("Feature Profitability")
        if feature_profit.empty:
            importance_fallback = load_feature_importance(ml_results).head(DATABASE_ANALYTICS_DISPLAY_ROWS)
            show_dataframe_or_info(
                importance_fallback,
                "No historical feature profitability yet. Run outcome labeling and ML analysis.",
            )
        else:
            st.dataframe(feature_profit, use_container_width=True)

        st.subheader("Regime Profitability")
        if not regime_perf.empty:
            safe_plot_bar(regime_perf, "regime_name", "avg_pnl", "Regime Profitability")
        else:
            st.info("No regime profitability data yet.")


def render_database_analytics(counts: dict[str, int], ml_results: pd.DataFrame) -> None:
    st.header("8. DATABASE ANALYTICS")
    st.warning("Database analytics is optional and may be CPU intensive.")
    st.caption(
        "Default view is lightweight and read-only. Database analytics is disabled in the governance dashboard layout."
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Historical Outcomes Rows", counts.get("historical_outcomes", 0))
    col2.metric("Signal Rows", counts.get("signals", 0))
    col3.metric("Flow Log Rows", counts.get("flow_logs", 0))

    with st.expander("Optional Database Analytics", expanded=False):
        st.caption(
            "Disabled in this read-only governance dashboard. Use approved terminal commands outside Streamlit "
            "if database analytics must be regenerated."
        )
        st.code("python main.py --refresh-governance-reports", language="bash")

    cached_analytics = st.session_state.get(DATABASE_ANALYTICS_SESSION_KEY)
    if not cached_analytics:
        st.info("Database analytics has not been run in this dashboard session. Section 9 remains available below.")
        return

    st.success("Showing previous cached database analytics summary.")
    st.dataframe(_database_analytics_cached_summary(cached_analytics), use_container_width=True, hide_index=True)
    with st.expander("Show cached database analytics details", expanded=False):
        render_database_analytics_result(cached_analytics, ml_results)


def main() -> None:
    st.title("MAMUYY Binance Hunter Governance Dashboard")
    st.caption("Auto refresh setiap 60 detik. Dashboard read-only dari JSON reports/.")
    st.markdown(
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
        unsafe_allow_html=True,
    )

    reports = load_dashboard_reports()
    render_read_only_banner()
    render_action_required_area(reports)

    tabs = st.tabs(["📊 Executive Summary", "🛡️ Governance & Risk", "⚙️ ML Diagnostics"])
    with tabs[0]:
        render_executive_summary_tab(reports)
    with tabs[1]:
        render_governance_risk_tab(reports)
    with tabs[2]:
        render_ml_diagnostics_tab(reports)


if __name__ == "__main__":
    main()
