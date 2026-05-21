import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from database import init_db
from cross_market_intelligence import latest_cross_market_state
from macro_observer import latest_macro_state
from portfolio_observer import observe_portfolio


def _read_table(db_path: str, table: str, limit: int = 5000) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                connection,
                params=(limit,),
            )
    except Exception:
        return pd.DataFrame()


def _read_outcomes(db_path: str, limit: int = 10000) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.symbol,
            o.pnl_pct,
            o.win_loss,
            COALESCE(s.regime_name, 'UNKNOWN') AS regime_name
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        ORDER BY o.id DESC
        LIMIT ?
    """
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as connection:
            return pd.read_sql_query(query, connection, params=(limit,))
    except Exception:
        return pd.DataFrame()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _normalize_percent(value: Any) -> float:
    number = _number(value)
    return number * 100 if 0 < number <= 1 else number


def _age_minutes(timestamp: Any) -> float:
    if timestamp in (None, "") or pd.isna(timestamp):
        return 9999.0
    parsed = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(parsed):
        return 9999.0
    return max(0.0, (datetime.now(timezone.utc) - parsed.to_pydatetime()).total_seconds() / 60)


def _profit_factor(pnls: pd.Series) -> float:
    pnl = pd.to_numeric(pnls, errors="coerce").fillna(0.0)
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return 3.0 if gross_profit > 0 else 0.0
    return min(gross_profit / gross_loss, 3.0)


def _historical_profitability(outcomes: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    if outcomes.empty or not {"symbol", "pnl_pct"}.issubset(outcomes.columns):
        return {}
    df = outcomes.copy()
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    rows: Dict[str, Dict[str, float]] = {}
    for symbol, group in df.groupby("symbol"):
        pnl = group["pnl_pct"]
        rows[str(symbol)] = {
            "avg_pnl": float(pnl.mean()),
            "winrate": float((pnl > 0).mean() * 100),
            "profit_factor": _profit_factor(pnl),
            "trades": float(len(group)),
        }
    return rows


def _regime_quality(outcomes: pd.DataFrame) -> Dict[str, float]:
    if outcomes.empty or not {"regime_name", "pnl_pct"}.issubset(outcomes.columns):
        return {}
    df = outcomes.copy()
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    quality = {}
    for regime_name, group in df.groupby("regime_name", dropna=False):
        avg_pnl = float(group["pnl_pct"].mean())
        winrate = float((group["pnl_pct"] > 0).mean() * 100)
        quality[str(regime_name or "UNKNOWN")] = max(0.0, min(100.0, 50 + avg_pnl * 8 + (winrate - 50) * 0.4))
    return quality


def _correlation_penalties(outcomes: pd.DataFrame) -> Dict[str, float]:
    if outcomes.empty or not {"timestamp", "symbol", "pnl_pct"}.issubset(outcomes.columns):
        return {}
    df = outcomes.copy()
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    pivot = df.pivot_table(index="timestamp", columns="symbol", values="pnl_pct", aggfunc="sum").fillna(0.0)
    if pivot.shape[0] < 5 or pivot.shape[1] < 2:
        return {}
    corr = pivot.corr().abs()
    penalties = {}
    for symbol in corr.columns:
        peers = corr[symbol].drop(labels=[symbol], errors="ignore").dropna()
        penalties[str(symbol)] = max(0.0, min(25.0, float(peers.nlargest(3).mean()) * 25)) if not peers.empty else 0.0
    return penalties




def _build_shadow_correlation_guard(outcomes: pd.DataFrame, threshold: float = 0.85) -> Dict[str, Dict[str, float]]:
    if outcomes.empty or not {"timestamp", "symbol", "pnl_pct"}.issubset(outcomes.columns):
        return {}
    df = outcomes.copy()
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    pivot = df.pivot_table(index="timestamp", columns="symbol", values="pnl_pct", aggfunc="sum")
    if pivot.shape[0] < 5 or pivot.shape[1] < 2:
        return {}
    corr = pivot.corr().replace([math.inf, -math.inf], pd.NA)
    matrix: Dict[str, Dict[str, float]] = {}
    for left in corr.columns:
        left_key = str(left)
        peers: Dict[str, float] = {}
        for right in corr.columns:
            if left == right:
                continue
            value = corr.loc[left, right]
            if pd.isna(value):
                continue
            value_f = float(value)
            if abs(value_f) >= threshold:
                peers[str(right)] = round(value_f, 4)
        if peers:
            matrix[left_key] = peers
    return matrix


def _active_shadow_positions(shadow_trades: pd.DataFrame) -> pd.DataFrame:
    if shadow_trades.empty or "symbol" not in shadow_trades.columns:
        return pd.DataFrame(columns=["symbol", "pnl_percent", "lifecycle_status"])
    latest = shadow_trades.sort_values("id").drop_duplicates("symbol", keep="last").copy()
    if "lifecycle_status" in latest.columns:
        latest = latest[
            ~latest["lifecycle_status"].fillna("").astype(str).str.upper().isin({"TRADE CLOSED", "CLOSED", "WIN", "LOSS"})
        ]
    return latest


def _apply_correlation_shadow_guard(
    output: pd.DataFrame,
    active_shadow: pd.DataFrame,
    correlation_map: Dict[str, Dict[str, float]],
    max_active_shadow_positions: int,
) -> pd.DataFrame:
    guarded = output.copy()
    guarded["rejection_reason"] = ""
    guarded["guard_decision"] = "ALLOW"
    guarded["guard_blocker_symbol"] = ""
    guarded["guard_correlation"] = pd.NA

    active_count = int(len(active_shadow))
    if active_count >= max_active_shadow_positions:
        reason = f"MAX_SHADOW_EXPOSURE_CAP:{active_count}/{max_active_shadow_positions}"
        guarded["allocation_tier"] = "AVOID"
        guarded["suggested_max_weight_pct"] = 0.0
        guarded["guard_decision"] = "BLOCK"
        guarded["rejection_reason"] = reason
        guarded["reason"] = guarded["reason"].astype(str) + f" | {reason}"
        return guarded

    active_negative = set()
    if not active_shadow.empty:
        pnl_available = "pnl_percent" in active_shadow.columns and active_shadow["pnl_percent"].notna().any()
        for _, row in active_shadow.iterrows():
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            if pnl_available:
                if _number(row.get("pnl_percent"), 0.0) < 0:
                    active_negative.add(symbol)
            else:
                active_negative.add(symbol)

    fallback_mode = bool(active_negative) and ("pnl_percent" not in active_shadow.columns or not active_shadow["pnl_percent"].notna().any())
    for idx, row in guarded.iterrows():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        blockers = correlation_map.get(symbol, {})
        blocker_symbol = ""
        blocker_corr = None
        for active_symbol in active_negative:
            corr = blockers.get(active_symbol)
            if corr is None:
                continue
            if blocker_corr is None or abs(corr) > abs(blocker_corr):
                blocker_symbol = active_symbol
                blocker_corr = corr
        if blocker_symbol:
            reason = "CORRELATED_SHADOW_EXPOSURE"
            if fallback_mode:
                reason = "CORRELATED_SHADOW_EXPOSURE_FAILSAFE_NO_PNL"
            guarded.at[idx, "allocation_tier"] = "AVOID"
            guarded.at[idx, "suggested_max_weight_pct"] = 0.0
            guarded.at[idx, "guard_decision"] = "BLOCK"
            guarded.at[idx, "rejection_reason"] = reason
            guarded.at[idx, "guard_blocker_symbol"] = blocker_symbol
            guarded.at[idx, "guard_correlation"] = blocker_corr
            guarded.at[idx, "reason"] = f"{row.get('reason')} | {reason}:{blocker_symbol}:{blocker_corr}"

    return guarded

def _macro_adaptive_bonus(logs_dir: str) -> float:
    path = os.path.join(logs_dir, "adaptive_threshold_comparison.csv")
    if not os.path.exists(path):
        return 0.0
    try:
        df = pd.read_csv(path)
    except Exception:
        return 0.0
    if df.empty or "strategy" not in df.columns:
        return 0.0
    macro = df[df["strategy"] == "macro_adaptive"]
    original = df[df["strategy"] == "original"]
    if macro.empty:
        return 0.0
    macro_pf = _number(macro.iloc[0].get("profit_factor"))
    original_pf = _number(original.iloc[0].get("profit_factor"), 1.0) if not original.empty else 1.0
    return max(-8.0, min(8.0, (macro_pf - original_pf) * 6.0))


def _latest_ml_confidence(ml_results: pd.DataFrame) -> float:
    if ml_results.empty:
        return 0.0
    latest = ml_results.sort_values("id").iloc[-1] if "id" in ml_results.columns else ml_results.iloc[0]
    return _normalize_percent(latest.get("ai_confidence_score") or latest.get("accuracy"))


def _latest_signal_rows(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty or "symbol" not in signals.columns:
        return pd.DataFrame()
    sort_column = "id" if "id" in signals.columns else "timestamp"
    return signals.sort_values(sort_column).drop_duplicates("symbol", keep="last").copy()


def _tier(score: float, risk_score: float) -> str:
    if score < 35 or risk_score >= 75:
        return "AVOID"
    if score < 55 or risk_score >= 55:
        return "WATCH"
    if score < 75 or risk_score >= 35:
        return "SMALL"
    return "PRIORITY"


def _suggested_weight(tier: str, score: float, risk_score: float) -> float:
    caps = {"AVOID": 0.0, "WATCH": 2.0, "SMALL": 5.0, "PRIORITY": 10.0}
    base = caps.get(tier, 0.0)
    if base <= 0:
        return 0.0
    risk_multiplier = max(0.25, 1 - risk_score / 120)
    confidence_multiplier = max(0.5, min(1.2, score / 75))
    return round(min(base, base * risk_multiplier * confidence_multiplier), 2)


def allocate_opportunities(
    db_path: str = "mamuyy_hunter.db",
    output_path: str = "logs/opportunity_allocation.csv",
    logs_dir: str = "logs",
    max_active_shadow_positions: int = 10,
    correlation_threshold: float = 0.85,
) -> Dict[str, Any]:
    signals = _read_table(db_path, "signals", limit=2000)
    shadow_trades = _read_table(db_path, "shadow_trades", limit=2000)
    flow_logs = _read_table(db_path, "flow_logs", limit=2000)
    regime_logs = _read_table(db_path, "regime_logs", limit=500)
    ml_results = _read_table(db_path, "ml_results", limit=100)
    risk_events = _read_table(db_path, "risk_events", limit=200)
    outcomes = _read_outcomes(db_path, limit=10000)

    latest_signals = _latest_signal_rows(signals)
    profitability = _historical_profitability(outcomes)
    regime_scores = _regime_quality(outcomes)
    correlation_penalties = _correlation_penalties(outcomes)
    portfolio = observe_portfolio(db_path)
    macro_bonus = _macro_adaptive_bonus(logs_dir)
    real_macro = latest_macro_state(os.path.join(logs_dir, "macro_observer.csv"))
    real_macro_state = str(real_macro.get("macro_state") or "UNKNOWN")
    real_macro_risk = _number(real_macro.get("macro_risk_score"))
    cross_market = latest_cross_market_state(os.path.join(logs_dir, "cross_market_intelligence.csv"))
    cross_market_state = str(cross_market.get("cross_market_state") or "UNKNOWN")
    cross_stress = _number(cross_market.get("cross_market_stress_score"))
    dxy_pressure = _number(cross_market.get("dxy_pressure"))
    safe_haven_rotation = bool(cross_market.get("safe_haven_rotation"))
    global_model_confidence = _latest_ml_confidence(ml_results)

    portfolio_heat = str(portfolio.get("portfolio_heat") or "LOW")
    heat_penalty = {"LOW": 0.0, "MEDIUM": 8.0, "HIGH": 18.0}.get(portfolio_heat, 8.0)
    macro_state_penalty = {"HIGH_STRESS": 12.0, "PANIC": 24.0, "CAUTION": 6.0}.get(real_macro_state, 0.0)
    cross_market_penalty = cross_stress * 0.18 + dxy_pressure * 0.35 + (12.0 if safe_haven_rotation else 0.0)
    concentration_rows = {row.get("symbol"): _number(row.get("exposure_pct")) for row in portfolio.get("symbol_exposure", [])}

    latest_flow = _latest_signal_rows(flow_logs.rename(columns={"final_score": "score"})) if not flow_logs.empty else pd.DataFrame()
    flow_by_symbol = latest_flow.set_index("symbol").to_dict("index") if not latest_flow.empty and "symbol" in latest_flow.columns else {}
    latest_regime = "UNKNOWN"
    if not regime_logs.empty and "regime_name" in regime_logs.columns:
        latest_regime = str(regime_logs.sort_values("id").iloc[-1].get("regime_name") or "UNKNOWN")

    rows = []
    symbols = sorted(set(latest_signals.get("symbol", pd.Series(dtype=str)).astype(str)) | set(profitability.keys()))
    for symbol in symbols:
        signal = latest_signals[latest_signals["symbol"].astype(str) == symbol]
        signal_row = signal.iloc[0] if not signal.empty else pd.Series(dtype=object)
        hist = profitability.get(symbol, {})
        regime_name = str(signal_row.get("regime_name") or latest_regime or "UNKNOWN")
        shadow_score = _number(signal_row.get("shadow_score"), _number(signal_row.get("score"), 0.0))
        signal_score = _number(signal_row.get("score"), shadow_score)
        model_confidence = _normalize_percent(
            signal_row.get("model_confidence")
            or signal_row.get("adaptive_confidence_score")
            or global_model_confidence
        )
        freshness_minutes = _age_minutes(signal_row.get("timestamp"))
        freshness_score = max(0.0, 100.0 - min(freshness_minutes / 15.0, 100.0))
        hist_score = max(
            0.0,
            min(
                100.0,
                45
                + _number(hist.get("avg_pnl")) * 7
                + (_number(hist.get("winrate"), 50.0) - 50.0) * 0.35
                + (_number(hist.get("profit_factor"), 1.0) - 1.0) * 18,
            ),
        )
        regime_quality = regime_scores.get(regime_name, 50.0)
        flow = flow_by_symbol.get(symbol, {})
        pressure_quality = max(0.0, min(100.0, 50 + _number(flow.get("pressure_score")) * 0.25 + _number(flow.get("taker_delta")) * 20))
        concentration_penalty = min(25.0, max(0.0, concentration_rows.get(symbol, 0.0) - 20.0) * 0.5)
        correlation_penalty = correlation_penalties.get(symbol, 0.0)

        opportunity_score = (
            hist_score * 0.28
            + shadow_score * 0.24
            + regime_quality * 0.16
            + model_confidence * 0.12
            + freshness_score * 0.10
            + pressure_quality * 0.10
            + macro_bonus
            - heat_penalty * 0.35
            - macro_state_penalty * 0.55
            - cross_market_penalty * 0.45
            - concentration_penalty * 0.45
            - correlation_penalty * 0.35
        )
        risk_score = min(
            100.0,
            heat_penalty * 2.0
            + macro_state_penalty * 2.0
            + real_macro_risk * 0.20
            + cross_market_penalty * 1.2
            + concentration_penalty * 1.7
            + correlation_penalty * 1.4
            + max(0.0, 30.0 - freshness_score) * 0.5
            + max(0.0, 50.0 - regime_quality) * 0.4,
        )
        opportunity_score = round(max(0.0, min(100.0, opportunity_score)), 2)
        risk_score = round(max(0.0, risk_score), 2)
        allocation_tier = _tier(opportunity_score, risk_score)
        reasons = [
            f"hist_pf={_number(hist.get('profit_factor')):.2f}",
            f"shadow={shadow_score:.1f}",
            f"regime={regime_name}",
            f"heat={portfolio_heat}",
            f"macro={real_macro_state}",
            f"cross={cross_market_state}",
        ]
        if cross_market_penalty:
            reasons.append(f"cross_penalty={cross_market_penalty:.1f}")
        if concentration_penalty:
            reasons.append(f"concentration_penalty={concentration_penalty:.1f}")
        if correlation_penalty:
            reasons.append(f"correlation_penalty={correlation_penalty:.1f}")
        if freshness_minutes < 9999:
            reasons.append(f"fresh={freshness_minutes:.0f}m")
        else:
            reasons.append("no_recent_signal")

        rows.append(
            {
                "symbol": symbol,
                "opportunity_score": opportunity_score,
                "risk_score": risk_score,
                "allocation_tier": allocation_tier,
                "reason": " | ".join(reasons),
                "suggested_max_weight_pct": _suggested_weight(allocation_tier, opportunity_score, risk_score),
            }
        )

    output = pd.DataFrame(rows)
    active_shadow = _active_shadow_positions(shadow_trades)
    correlation_map = _build_shadow_correlation_guard(outcomes, threshold=correlation_threshold)
    if not output.empty:
        output = _apply_correlation_shadow_guard(output, active_shadow, correlation_map, max_active_shadow_positions)
        tier_order = {"PRIORITY": 0, "SMALL": 1, "WATCH": 2, "AVOID": 3}
        output["_tier_order"] = output["allocation_tier"].map(tier_order).fillna(9)
        output = output.sort_values(["_tier_order", "opportunity_score", "risk_score"], ascending=[True, False, True])
        output = output.drop(columns=["_tier_order"])
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    output.to_csv(output_path, index=False)
    return {
        "ok": True,
        "rows": int(len(output)),
        "output_path": output_path,
        "portfolio_heat": portfolio_heat,
        "macro_bonus": round(macro_bonus, 4),
        "allocations": output.to_dict("records"),
        "shadow_guard": {
            "active_shadow_exposure_count": int(len(active_shadow)),
            "max_active_shadow_positions": int(max_active_shadow_positions),
            "correlation_threshold": float(correlation_threshold),
            "blocked_candidates": int((output.get("guard_decision") == "BLOCK").sum()) if not output.empty and "guard_decision" in output.columns else 0,
        },
    }


def format_allocation_summary(result: Dict[str, Any]) -> str:
    allocations = pd.DataFrame(result.get("allocations", []))
    priority = allocations[allocations["allocation_tier"] == "PRIORITY"].head(10) if not allocations.empty else pd.DataFrame()
    avoid = allocations[allocations["allocation_tier"] == "AVOID"].head(10) if not allocations.empty else pd.DataFrame()
    return "\n".join(
        [
            "OPPORTUNITY ALLOCATION ENGINE V1",
            f"OK: {result.get('ok')}",
            f"Rows: {result.get('rows', 0)}",
            f"Portfolio Heat: {result.get('portfolio_heat')}",
            f"Macro Adaptive Bonus: {result.get('macro_bonus')}",
            f"CSV: {result.get('output_path')}",
            "",
            "Top Priority Symbols:",
            priority.to_string(index=False) if not priority.empty else "No PRIORITY symbols.",
            "",
            "Avoid List:",
            avoid.to_string(index=False) if not avoid.empty else "No AVOID symbols.",
        ]
    )


def shadow_guard_audit(db_path: str = "mamuyy_hunter.db", candidate_symbol: str = "", correlation_threshold: float = 0.85) -> Dict[str, Any]:
    shadow_trades = _read_table(db_path, "shadow_trades", limit=2000)
    outcomes = _read_outcomes(db_path, limit=10000)
    active_shadow = _active_shadow_positions(shadow_trades)
    correlation_map = _build_shadow_correlation_guard(outcomes, threshold=correlation_threshold)
    active_negative = set()
    pnl_available = "pnl_percent" in active_shadow.columns and active_shadow["pnl_percent"].notna().any() if not active_shadow.empty else False
    for _, row in active_shadow.iterrows():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        if pnl_available:
            if _number(row.get("pnl_percent"), 0.0) < 0:
                active_negative.add(symbol)
        else:
            active_negative.add(symbol)
    candidate = str(candidate_symbol or "").upper()
    blockers = correlation_map.get(candidate, {}) if candidate else {}
    blocker = ""
    corr = None
    for a in active_negative:
        if a in blockers and (corr is None or abs(blockers[a]) > abs(corr)):
            blocker = a
            corr = blockers[a]
    decision = "BLOCK" if blocker else "ALLOW"
    reason = "CORRELATED_SHADOW_EXPOSURE" if blocker else "NONE"
    if blocker and not pnl_available:
        reason = "CORRELATED_SHADOW_EXPOSURE_FAILSAFE_NO_PNL"
    return {
        "active_shadow_exposure_count": int(len(active_shadow)),
        "candidate_symbol": candidate or "N/A",
        "correlated_blocker_symbol": blocker or "N/A",
        "correlation_value": corr if corr is not None else "N/A",
        "decision": decision,
        "reason": reason,
    }
