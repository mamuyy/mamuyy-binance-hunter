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
from portfolio_analytics import calculate_portfolio_analytics
from portfolio_observer import observe_portfolio
from risk_manager import RiskConfig, check_execution_safety


DB_PATH = config.database_url or config.database_path
REFRESH_SECONDS = 60


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


@st.cache_data(ttl=REFRESH_SECONDS)
def read_symbol_performance(limit: int = 20) -> pd.DataFrame:
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


@st.cache_data(ttl=REFRESH_SECONDS)
def read_worst_symbol_performance(limit: int = 20) -> pd.DataFrame:
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


@st.cache_data(ttl=REFRESH_SECONDS)
def read_regime_performance(limit: int = 20) -> pd.DataFrame:
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


@st.cache_data(ttl=REFRESH_SECONDS)
def read_feature_profitability_from_history() -> pd.DataFrame:
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
    return pd.DataFrame(rows).sort_values(["avg_pnl", "trades"], ascending=[False, False]).head(20)


@st.cache_data(ttl=REFRESH_SECONDS)
def read_optimizer_setups(path: str = "optimizer_results.csv", limit: int = 20) -> pd.DataFrame:
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


def main() -> None:
    signals = read_table("signals")
    trades = read_table("paper_trades")
    if trades.empty:
        trades = read_historical_outcomes()
    flows = read_table("flow_logs")
    regimes = read_table("regime_logs")
    ml_results = read_table("ml_results", limit=50)
    walkforward = read_table("walkforward_results")
    counts = table_counts()
    risk_status = read_risk_status()
    portfolio_observability = read_portfolio_observability()
    portfolio_analytics = read_portfolio_analytics()
    opportunity_allocation = read_opportunity_allocation()
    model_registry = read_model_registry()
    internal_paper_trades = read_table("internal_paper_trades", limit=200)
    broadcast_events = read_table("broadcast_events", limit=300)
    telegram_events = read_table("telegram_events", limit=200)
    webhook_payload = read_webhook_payload()
    macro_observer, macro_components = read_macro_observer()
    cross_market, cross_market_components, cross_market_correlation = read_cross_market()
    strategy_genome_results, strategy_genome_archive = read_strategy_genome()
    daily_ops_report = read_daily_ops_report()
    anomaly_report, incident_report = read_incident_anomaly_report()
    orchestrator_diagnostics = read_orchestrator_diagnostics()

    st.title("MAMUYY Binance Hunter Live Dashboard")
    st.caption("Auto refresh setiap 60 detik. Dashboard read-only dari SQLite.")
    st.markdown(
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>",
        unsafe_allow_html=True,
    )

    render_alerts(signals, trades, ml_results)

    st.header("1. SYSTEM HEALTH")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        status_badge("Scanner Status", "GREEN" if not signals.empty else "YELLOW", "")
        st.metric("Latest Runtime", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with col2:
        health = db_health_check(DB_PATH, migrate_csv=False, backup=False)
        status_badge("Database Status", "GREEN" if health.get("ok") else "RED", "")
        st.metric("Total DB Rows", sum(counts.values()))
    with col3:
        st.metric("Latest Signal", latest_timestamp(signals))
        st.metric("Latest ML Run", latest_timestamp(ml_results))
    with col4:
        st.metric("Latest Walkforward Run", latest_timestamp(walkforward))
        st.dataframe(pd.DataFrame([counts]), use_container_width=True)

    render_risk_engine_status(risk_status)
    render_orchestrator_diagnostics(orchestrator_diagnostics)
    render_daily_ops_report(daily_ops_report)
    render_incident_anomaly_intelligence(anomaly_report, incident_report)
    render_macro_observer(macro_observer, macro_components)
    render_cross_market_intelligence(cross_market, cross_market_components, cross_market_correlation)
    render_strategy_genome_lab(strategy_genome_results, strategy_genome_archive)
    render_portfolio_observability(portfolio_observability)

    st.header("2. MARKET REGIME")
    col1, col2 = st.columns([1, 2])
    with col1:
        st.metric("Current Regime", metric_value(regimes, "regime_name"))
        st.metric("Regime Confidence", metric_value(regimes, "regime_score", 0))
    with col2:
        safe_plot_line(regimes, "timestamp", "regime_score", "Regime Confidence History")

    st.header("3. LIVE SIGNALS")
    signal_cols = [
        "timestamp",
        "symbol",
        "score",
        "calculated_score",
        "shadow_score",
        "penalty_applied",
        "flow_state",
        "whale_activity",
        "squeeze_risk",
        "regime_name",
    ]
    st.dataframe(signals[[c for c in signal_cols if c in signals.columns]].head(50), use_container_width=True)
    render_shadow_penalty_insight(signals)
    render_shadow_simulation()

    st.header("4. PAPER TRADING")
    open_trades = trades[trades.get("status", pd.Series(dtype=str)).isin(["OPEN", "TP1 HIT"])] if not trades.empty else trades
    wins = int((trades.get("status", pd.Series(dtype=str)) == "WIN").sum()) if not trades.empty else 0
    total = len(trades)
    winrate = (wins / total * 100) if total else 0.0
    curve = pnl_curve(trades)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Open Trades", len(open_trades))
    col2.metric("Winrate", f"{winrate:.2f}%")
    col3.metric("Current Drawdown", f"{current_drawdown(curve):.2f}%")
    if not trades.empty and "pnl_percent" in trades.columns:
        best = trades.loc[pd.to_numeric(trades["pnl_percent"], errors="coerce").idxmax()]
        worst = trades.loc[pd.to_numeric(trades["pnl_percent"], errors="coerce").idxmin()]
        col4.metric("Best/Worst", f"{best.get('symbol', '-')}/{worst.get('symbol', '-')}")
    else:
        col4.metric("Best/Worst", "-")
    safe_plot_line(curve, "trade", "equity", "Paper Trading PnL Curve")
    st.dataframe(open_trades.head(50), use_container_width=True)

    render_portfolio_equity_analytics(portfolio_analytics)

    st.header("5. FLOW ANALYTICS")
    col1, col2 = st.columns(2)
    with col1:
        safe_plot_line(flows, "timestamp", "funding_zscore", "Funding Anomaly")
        safe_plot_line(flows, "timestamp", "pressure_score", "Pressure Score")
    with col2:
        if not flows.empty and "whale_activity" in flows.columns:
            freq = flows["whale_activity"].value_counts().reset_index()
            freq.columns = ["whale_activity", "count"]
            safe_plot_bar(freq, "whale_activity", "count", "Whale Activity Frequency")
        else:
            st.info("No whale activity data yet.")
        safe_plot_line(flows, "timestamp", "squeeze_probability", "Squeeze Probability")

    st.header("6. ML ANALYTICS")
    feature_importance = load_feature_importance(ml_results)
    col1, col2, col3 = st.columns(3)
    col1.metric("Model Accuracy", metric_value(ml_results, "accuracy", 0))
    col2.metric("AI Confidence", metric_value(ml_results, "ai_confidence_score", 0))
    col3.metric("Model Health", metric_value(ml_results, "setup_ranking", "LOW QUALITY"))
    safe_plot_bar(feature_importance.head(15), "feature", "importance", "Feature Importance")
    prediction_path = os.path.join(config.chart_output_dir, "prediction_distribution.png")
    if os.path.exists(prediction_path):
        st.image(prediction_path, caption="Prediction Distribution")
    render_ml_lifecycle(model_registry)

    st.header("7. WALKFORWARD ANALYTICS")
    col1, col2, col3 = st.columns(3)
    col1.metric("Rolling Accuracy", f"{pd.to_numeric(walkforward.get('test_accuracy', pd.Series(dtype=float)), errors='coerce').mean() or 0:.2%}")
    col2.metric("Rolling Winrate", f"{pd.to_numeric(walkforward.get('winrate', pd.Series(dtype=float)), errors='coerce').mean() or 0:.2f}%")
    train_acc = pd.to_numeric(walkforward.get("train_accuracy", pd.Series(dtype=float)), errors="coerce").mean()
    test_acc = pd.to_numeric(walkforward.get("test_accuracy", pd.Series(dtype=float)), errors="coerce").mean()
    overfit = max(0.0, float((train_acc or 0) - (test_acc or 0)) * 100)
    col3.metric("Overfit Risk", f"{overfit:.2f}/100")
    safe_plot_line(walkforward, "fold", "test_accuracy", "Rolling Accuracy")
    safe_plot_line(walkforward, "fold", "winrate", "Rolling Winrate")
    if not walkforward.empty and "best_regime" in walkforward.columns:
        st.dataframe(walkforward[["fold", "best_regime", "worst_regime"]].head(50), use_container_width=True)

    st.header("8. DATABASE ANALYTICS")
    symbol_perf = read_symbol_performance()
    worst_symbol_perf = read_worst_symbol_performance()
    regime_perf = read_regime_performance()
    feature_profit = read_feature_profitability_from_history()
    optimizer_setups = read_optimizer_setups()

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
            regime_perf.head(10),
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
            importance_fallback = load_feature_importance(ml_results).head(20)
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

    render_opportunity_allocation(opportunity_allocation)
    render_webhook_paper_engine(internal_paper_trades, webhook_payload)
    render_broadcast_control_center(broadcast_events, internal_paper_trades)
    render_telegram_notification_center(telegram_events)


if __name__ == "__main__":
    main()
