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
        "flow_state",
        "whale_activity",
        "squeeze_risk",
        "regime_name",
    ]
    st.dataframe(signals[[c for c in signal_cols if c in signals.columns]].head(50), use_container_width=True)

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
