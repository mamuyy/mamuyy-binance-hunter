import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from execution_engine import simulate_execution
from shadow_lifecycle import active_shadow_positions


def ensure_shadow_table(db_path: str = "mamuyy_hunter.db") -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                lifecycle_status TEXT,
                regime_name TEXT,
                signal_score REAL,
                expected_fill REAL,
                simulated_live_fill REAL,
                execution_drift REAL,
                latency_impact REAL,
                prediction_drift REAL,
                regime_drift REAL,
                exposure REAL,
                pnl_percent REAL,
                execution_quality_score REAL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_shadow_trades_timestamp ON shadow_trades(timestamp)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_shadow_trades_symbol ON shadow_trades(symbol)")
        connection.commit()


def _read_table(db_path: str, table: str) -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(f"SELECT * FROM {table}", connection)
    except Exception:
        return pd.DataFrame()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _line(values: List[float], path: str, title: str, ylabel: str) -> None:
    if not values:
        _placeholder(path, title)
        return
    plt.figure(figsize=(9, 4))
    plt.plot(range(1, len(values) + 1), values)
    plt.title(title)
    plt.ylabel(ylabel)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _insert_rows(db_path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    fields = [
        "timestamp",
        "symbol",
        "lifecycle_status",
        "regime_name",
        "signal_score",
        "expected_fill",
        "simulated_live_fill",
        "execution_drift",
        "latency_impact",
        "prediction_drift",
        "regime_drift",
        "exposure",
        "pnl_percent",
        "execution_quality_score",
    ]
    with sqlite3.connect(db_path) as connection:
        placeholders = ", ".join(["?"] * len(fields))
        connection.executemany(
            f"INSERT INTO shadow_trades ({', '.join(fields)}) VALUES ({placeholders})",
            [[row.get(field) for field in fields] for row in rows],
        )
        connection.commit()


def _health(execution_drift: float, drawdown: float, prediction_drift: float) -> str:
    if execution_drift > 1.0 or abs(drawdown) > 15 or prediction_drift > 35:
        return "UNSTABLE"
    if execution_drift > 0.5 or abs(drawdown) > 8 or prediction_drift > 20:
        return "WARNING"
    return "HEALTHY"


def run_shadow_live(db_path: str = "mamuyy_hunter.db", chart_dir: str = "charts") -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    ensure_shadow_table(db_path)
    charts = {
        "shadow_equity_curve": os.path.join(chart_dir, "shadow_equity_curve.png"),
        "live_drawdown_curve": os.path.join(chart_dir, "live_drawdown_curve.png"),
        "execution_drift_chart": os.path.join(chart_dir, "execution_drift_chart.png"),
        "regime_drift_chart": os.path.join(chart_dir, "regime_drift_chart.png"),
    }
    signals = _read_table(db_path, "signals")
    regimes = _read_table(db_path, "regime_logs")
    shadow = _read_table(db_path, "shadow_trades")
    if signals.empty:
        for path, title in [
            (charts["shadow_equity_curve"], "Shadow Equity Curve"),
            (charts["live_drawdown_curve"], "Live Drawdown Curve"),
            (charts["execution_drift_chart"], "Execution Drift"),
            (charts["regime_drift_chart"], "Regime Drift"),
        ]:
            _placeholder(path, title)
        return {
            "live_pnl": 0.0,
            "rolling_live_pnl_pct": 0.0,
            "cumulative_shadow_pnl_pct": 0.0,
            "live_winrate": 0.0,
            "live_drawdown": 0.0,
            "live_exposure": 0.0,
            "rolling_live_exposure_pct": 0.0,
            "cumulative_shadow_exposure_pct": 0.0,
            "execution_drift": 0.0,
            "prediction_drift": 0.0,
            "regime_drift": 0.0,
            "current_regime": "UNKNOWN",
            "shadow_health": "WARNING",
            "charts": charts,
            "notes": ["No signals available for shadow live simulation."],
        }

    latest = signals.sort_values("id").drop_duplicates("symbol", keep="last").tail(20)
    current_regime = "UNKNOWN"
    if not regimes.empty and "regime_name" in regimes.columns:
        current_regime = str(regimes.sort_values("id").iloc[-1].get("regime_name") or "UNKNOWN")

    rows = []
    for _, signal in latest.iterrows():
        execution = simulate_execution(signal)
        expected_fill = _num(execution.get("expected_fill_price"))
        drift = execution["slippage_percent"] * (0.25 + execution["latency_risk"] / 200)
        score = _num(signal.get("score"))
        confidence = _num(signal.get("model_confidence") or signal.get("adaptive_confidence_score") or score)
        prediction_drift = abs(score - confidence)
        regime_drift = 0.0 if str(signal.get("regime_name") or "UNKNOWN") == current_regime else 1.0
        exposure = min(0.12, max(0.01, score / 1000))
        pnl = (score - 50) / 20 - execution["execution_cost"]
        rows.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": signal.get("symbol"),
                "lifecycle_status": "execution simulated",
                "regime_name": signal.get("regime_name") or current_regime,
                "signal_score": score,
                "expected_fill": expected_fill,
                "simulated_live_fill": expected_fill * (1 + drift / 100),
                "execution_drift": drift,
                "latency_impact": execution["latency_risk"],
                "prediction_drift": prediction_drift,
                "regime_drift": regime_drift,
                "exposure": exposure,
                "pnl_percent": pnl,
                "execution_quality_score": execution["execution_quality_score"],
            }
        )
    _insert_rows(db_path, rows)
    shadow = pd.concat([shadow, pd.DataFrame(rows)], ignore_index=True)
    shadow_window = shadow.tail(500)
    pnl = pd.to_numeric(shadow_window.get("pnl_percent", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax() if not equity.empty else pd.Series(dtype=float)
    execution_drift = pd.to_numeric(shadow.get("execution_drift", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    prediction_drift = pd.to_numeric(shadow.get("prediction_drift", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    regime_drift = pd.to_numeric(shadow.get("regime_drift", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    exposure = pd.to_numeric(shadow_window.get("exposure", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    cumulative_pnl_pct = float(equity.iloc[-1]) if not equity.empty else 0.0
    cumulative_exposure_pct = float(exposure.mean() * 100) if len(exposure) else 0.0

    # Live metrics should reflect currently active lifecycle-governed shadows only.
    active_positions = active_shadow_positions(db_path=db_path)
    rolling_live_pnl_pct = 0.0
    rolling_live_exposure_pct = 0.0
    if active_positions:
        rolling_live_pnl_pct = float(sum(_num(position.get("pnl_percent")) for position in active_positions))
        rolling_live_exposure_pct = float(sum(_num(position.get("exposure")) for position in active_positions) * 100)

    _line(equity.tolist(), charts["shadow_equity_curve"], "Shadow Equity Curve", "PnL (%)")
    _line(drawdown.tolist(), charts["live_drawdown_curve"], "Live Drawdown Curve", "Drawdown (%)")
    _line(execution_drift.tolist(), charts["execution_drift_chart"], "Execution Drift", "Drift (%)")
    _line(regime_drift.tolist(), charts["regime_drift_chart"], "Regime Drift", "Drift Flag")
    live_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    avg_execution_drift = float(execution_drift.mean()) if len(execution_drift) else 0.0
    avg_prediction_drift = float(prediction_drift.mean()) if len(prediction_drift) else 0.0
    return {
        "live_pnl": round(rolling_live_pnl_pct, 4),
        "rolling_live_pnl_pct": round(rolling_live_pnl_pct, 4),
        "cumulative_shadow_pnl_pct": round(cumulative_pnl_pct, 4),
        "live_winrate": round(float((pnl > 0).mean() * 100) if len(pnl) else 0.0, 2),
        "live_drawdown": round(live_drawdown, 4),
        "live_exposure": round(rolling_live_exposure_pct, 2),
        "rolling_live_exposure_pct": round(rolling_live_exposure_pct, 2),
        "cumulative_shadow_exposure_pct": round(cumulative_exposure_pct, 2),
        "execution_drift": round(avg_execution_drift, 4),
        "prediction_drift": round(avg_prediction_drift, 2),
        "regime_drift": round(float(regime_drift.mean()) if len(regime_drift) else 0.0, 4),
        "current_regime": current_regime,
        "shadow_health": _health(avg_execution_drift, live_drawdown, avg_prediction_drift),
        "charts": charts,
        "notes": [],
    }
