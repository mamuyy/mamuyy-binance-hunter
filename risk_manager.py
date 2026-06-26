import csv
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from database import init_db
from health_guardian import resolve_runtime_heartbeat
from shadow_lifecycle import active_shadow_positions


@dataclass(frozen=True)
class RiskConfig:
    ml_accuracy_halt: float = 45.0
    drawdown_halt: float = -20.0
    drawdown_watch: float = -10.0
    stale_minutes: int = 10
    max_open_trades: int = 10
    loss_cooldown: int = 3
    base_position_multiplier: float = 1.0
    high_vol_confidence_min: float = 55.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_percent(value: Any) -> float:
    number = _safe_float(value)
    return number * 100 if 0 < number <= 1 else number


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _latest_row(connection: sqlite3.Connection, table: str) -> sqlite3.Row | None:
    try:
        return connection.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None


def _latest_ml(connection: sqlite3.Connection, model_output_path: str) -> Dict[str, float]:
    row = _latest_row(connection, "ml_results")
    if row:
        return {
            "accuracy": _normalize_percent(row["accuracy"]),
            "confidence": _normalize_percent(row["ai_confidence_score"]),
        }

    if os.path.exists(model_output_path):
        try:
            with open(model_output_path, encoding="utf-8") as model_file:
                payload = json.load(model_file)
            return {
                "accuracy": _normalize_percent(payload.get("accuracy")),
                "confidence": _normalize_percent(payload.get("ai_confidence_score")),
            }
        except (OSError, json.JSONDecodeError):
            pass

    return {"accuracy": 0.0, "confidence": 0.0}


def _latest_regime(connection: sqlite3.Connection) -> str:
    row = _latest_row(connection, "regime_logs")
    if row and row["regime_name"]:
        return str(row["regime_name"])
    row = _latest_row(connection, "signals")
    if row and "regime_name" in row.keys() and row["regime_name"]:
        return str(row["regime_name"])
    return "UNKNOWN"


def _heartbeat_age_minutes(log_path: str) -> float:
    if not os.path.exists(log_path):
        return 9999.0
    latest = None
    try:
        with open(log_path, newline="", encoding="utf-8") as log_file:
            for row in csv.DictReader(log_file):
                if row.get("engine") == "heartbeat":
                    latest = row.get("timestamp")
    except OSError:
        return 9999.0
    timestamp = _parse_timestamp(latest)
    if not timestamp:
        return 9999.0
    return (datetime.now(timezone.utc) - timestamp).total_seconds() / 60


def _runtime_heartbeat(database_path: str, log_path: str, stale_minutes: int) -> Dict[str, Any]:
    try:
        heartbeat = resolve_runtime_heartbeat(database_path, log_path, stale_minutes)
        return {
            "timestamp": heartbeat.get("timestamp") or "",
            "source": heartbeat.get("source") or "-",
            "age_minutes": _safe_float(heartbeat.get("age_minutes"), 9999.0),
        }
    except Exception:
        return {
            "timestamp": "",
            "source": "orchestrator_log",
            "age_minutes": _heartbeat_age_minutes(log_path),
        }


def _pnl_rows(connection: sqlite3.Connection, lookback_hours: int = 24) -> List[float]:
    """Return official PnL rows for risk/drawdown checks.

    CP-036 governance:
    - shadow_trades is intentionally excluded.
    - CP-035 classified shadow_trades as expected-fill observability simulation,
      not actual TP/SL outcome and not official winrate/PnL source.
    - Authoritative paper source is internal_paper_trades.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    queries = [
        (
            """
            SELECT pnl AS pnl
            FROM internal_paper_trades
            WHERE UPPER(COALESCE(status, '')) = 'CLOSED'
              AND pnl IS NOT NULL
              AND COALESCE(updated_at, timestamp) >= ?
            ORDER BY id ASC
            """,
            (cutoff,),
        ),
        (
            """
            SELECT pnl AS pnl
            FROM internal_paper_trades
            WHERE UPPER(COALESCE(status, '')) = 'CLOSED'
              AND pnl IS NOT NULL
            ORDER BY id ASC
            """,
            (),
        ),
        ("SELECT pnl_percent AS pnl FROM paper_trades WHERE timestamp >= ? ORDER BY id ASC", (cutoff,)),
        ("SELECT pnl_pct AS pnl FROM historical_outcomes WHERE signal_timestamp >= ? ORDER BY id ASC", (cutoff,)),
        ("SELECT pnl_percent AS pnl FROM paper_trades ORDER BY id ASC", ()),
        ("SELECT pnl_pct AS pnl FROM historical_outcomes ORDER BY id ASC", ()),
    ]
    for query, params in queries:
        try:
            rows = [_safe_float(row["pnl"]) for row in connection.execute(query, params).fetchall()]
            if rows:
                return rows
        except sqlite3.Error:
            continue
    return []


def _max_drawdown(pnls: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def _consecutive_losses(pnls: List[float]) -> int:
    count = 0
    for pnl in reversed(pnls):
        if pnl < 0:
            count += 1
        elif pnl > 0:
            break
    return count


def _open_trades(connection: sqlite3.Connection) -> int:
    total = 0
    try:
        total += int(
            connection.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status IN ('OPEN', 'TP1 HIT')"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        pass
    try:
        # Count only lifecycle-governed ACTIVE shadows to avoid permanent congestion from stale rows.
        total += len(active_shadow_positions())
    except Exception:
        pass
    return total


def _latest_flow_volatility(connection: sqlite3.Connection) -> Dict[str, float]:
    try:
        rows = connection.execute(
            """
            SELECT squeeze_probability, funding_zscore, pressure_score
            FROM flow_logs
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return {"squeeze_probability": 0.0, "funding_zscore_abs": 0.0, "pressure_score": 50.0}
    squeeze = [_safe_float(row["squeeze_probability"]) for row in rows]
    funding = [abs(_safe_float(row["funding_zscore"])) for row in rows]
    pressure = [_safe_float(row["pressure_score"], 50.0) for row in rows]
    return {
        "squeeze_probability": sum(squeeze) / len(squeeze),
        "funding_zscore_abs": sum(funding) / len(funding),
        "pressure_score": sum(pressure) / len(pressure),
    }


def _insert_risk_event(connection: sqlite3.Connection, result: Dict[str, Any]) -> None:
    fields = [
        "timestamp",
        "status",
        "safe",
        "risk_score",
        "position_multiplier",
        "reasons_json",
        "ml_accuracy",
        "model_confidence",
        "drawdown",
        "regime_name",
        "heartbeat_age_minutes",
        "open_trades",
        "consecutive_losses",
    ]
    row = {
        "timestamp": _now_iso(),
        "status": result["status"],
        "safe": int(bool(result["safe"])),
        "risk_score": result["risk_score"],
        "position_multiplier": result["position_multiplier"],
        "reasons_json": json.dumps(result["reasons"]),
        "ml_accuracy": result["metrics"]["ml_accuracy"],
        "model_confidence": result["metrics"]["model_confidence"],
        "drawdown": result["metrics"]["drawdown"],
        "regime_name": result["metrics"]["regime_name"],
        "heartbeat_age_minutes": result["metrics"]["heartbeat_age_minutes"],
        "open_trades": result["metrics"]["open_trades"],
        "consecutive_losses": result["metrics"]["consecutive_losses"],
    }
    placeholders = ", ".join(["?"] * len(fields))
    connection.execute(
        f"INSERT INTO risk_events ({', '.join(fields)}) VALUES ({placeholders})",
        [row[field] for field in fields],
    )


def check_execution_safety(
    db_path: str = "mamuyy_hunter.db",
    orchestrator_log_path: str = "orchestrator_log.csv",
    model_output_path: str = "model_output.json",
    config: RiskConfig | None = None,
    log_event: bool = True,
) -> Dict[str, Any]:
    risk_config = config or RiskConfig()
    init_db(db_path)
    halt_reasons: List[str] = []
    watch_reasons: List[str] = []
    position_multiplier = max(risk_config.base_position_multiplier, 0.0)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        ml = _latest_ml(connection, model_output_path)
        regime_name = _latest_regime(connection)
        heartbeat = _runtime_heartbeat(db_path, orchestrator_log_path, risk_config.stale_minutes)
        heartbeat_age = heartbeat["age_minutes"]
        pnls = _pnl_rows(connection)
        drawdown = _max_drawdown(pnls)
        consecutive_losses = _consecutive_losses(pnls[-20:])
        open_trades = _open_trades(connection)
        flow_volatility = _latest_flow_volatility(connection)

        if ml["accuracy"] < risk_config.ml_accuracy_halt:
            halt_reasons.append(f"ML accuracy {ml['accuracy']:.2f}% below {risk_config.ml_accuracy_halt:.2f}%")

        if drawdown <= risk_config.drawdown_halt:
            halt_reasons.append(f"Drawdown {drawdown:.2f}% breached halt threshold {risk_config.drawdown_halt:.2f}%")
        elif drawdown <= risk_config.drawdown_watch:
            watch_reasons.append(f"Drawdown {drawdown:.2f}% breached watch threshold {risk_config.drawdown_watch:.2f}%")
            position_multiplier *= 0.5

        if heartbeat_age > risk_config.stale_minutes:
            halt_reasons.append(f"Runtime heartbeat stale for {heartbeat_age:.1f} minutes")
        elif str(heartbeat.get("source", "")).startswith("fallback_"):
            watch_reasons.append(f"Heartbeat table stale; using {heartbeat['source']}")

        regime_upper = regime_name.upper()
        if regime_upper == "SIDEWAYS / CHOPPY":
            watch_reasons.append("SIDEWAYS / CHOPPY regime: exposure reduced by 70%")
            position_multiplier *= 0.30
        elif regime_upper == "TRENDING BEAR":
            halt_reasons.append("TRENDING BEAR regime: risk halt")
            position_multiplier *= 0.10
        elif regime_upper == "HIGH VOLATILITY":
            position_multiplier *= 0.40
            if ml["confidence"] < risk_config.high_vol_confidence_min:
                halt_reasons.append(
                    f"HIGH VOLATILITY with model confidence {ml['confidence']:.2f}% below {risk_config.high_vol_confidence_min:.2f}%"
                )
            else:
                watch_reasons.append("HIGH VOLATILITY regime: exposure reduced")

        volatility_abnormal = (
            flow_volatility["squeeze_probability"] >= 70
            or flow_volatility["funding_zscore_abs"] >= 2.5
        )
        if volatility_abnormal:
            halt_reasons.append("Abnormal volatility detected from flow metrics")

        if open_trades > risk_config.max_open_trades:
            halt_reasons.append(f"Open/shadow trades {open_trades} exceed max {risk_config.max_open_trades}")
        elif open_trades >= max(1, int(risk_config.max_open_trades * 0.8)):
            watch_reasons.append(f"Open/shadow trades {open_trades} near max {risk_config.max_open_trades}")

        if consecutive_losses >= risk_config.loss_cooldown:
            halt_reasons.append(f"Cooldown active after {consecutive_losses} consecutive losses")

        risk_score = 100
        risk_score -= 30 if halt_reasons else 0
        risk_score -= min(25, max(0, risk_config.ml_accuracy_halt - ml["accuracy"]))
        risk_score -= min(25, abs(min(drawdown, 0)))
        risk_score -= min(20, max(0, heartbeat_age - risk_config.stale_minutes))
        risk_score -= min(15, consecutive_losses * 5)
        risk_score = max(0, min(100, round(risk_score, 2)))

        status = "HALT" if halt_reasons else "WATCH" if watch_reasons else "SAFE"
        safe = status != "HALT"
        if not safe:
            position_multiplier = 0.0

        result = {
            "safe": safe,
            "status": status,
            "reasons": halt_reasons + watch_reasons,
            "position_multiplier": round(max(position_multiplier, 0.0), 4),
            "risk_score": risk_score,
            "metrics": {
                "ml_accuracy": round(ml["accuracy"], 4),
                "model_confidence": round(ml["confidence"], 4),
                "drawdown": round(drawdown, 4),
                "regime_name": regime_name,
                "heartbeat_age_minutes": round(heartbeat_age, 2),
                "heartbeat_source": heartbeat.get("source", "-"),
                "heartbeat_timestamp": heartbeat.get("timestamp", ""),
                "open_trades": open_trades,
                "consecutive_losses": consecutive_losses,
                "flow_squeeze_probability": round(flow_volatility["squeeze_probability"], 4),
                "flow_funding_zscore_abs": round(flow_volatility["funding_zscore_abs"], 4),
            },
        }

        if log_event:
            _insert_risk_event(connection, result)
            connection.commit()
        return result
