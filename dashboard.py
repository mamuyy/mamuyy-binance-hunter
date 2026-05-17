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
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    equity = _empty_df()
    comparison = _empty_df()
    tuning = _empty_df()
    walkforward = _empty_df()
    adaptive_comparison = _empty_df()
    adaptive_walkforward = _empty_df()
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
    return equity, comparison, tuning, walkforward, adaptive_comparison, adaptive_walkforward


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
    equity, comparison, tuning, walkforward, adaptive_comparison, adaptive_walkforward = read_shadow_simulation()
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
        return
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
    safe_plot_line(curve, "trade", "equity", "Paper Trading PnL Curve")
    st.dataframe(open_trades.head(50), use_container_width=True)

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


if __name__ == "__main__":
    main()
