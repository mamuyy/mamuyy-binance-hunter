import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd


REGISTRY_PATH = "logs/strategy_genome_registry.json"
RESULTS_PATH = "logs/strategy_genome_results.csv"
ARCHIVE_PATH = "logs/strategy_genome_archive.csv"


BASE_STRATEGIES = [
    {
        "strategy_id": "GENOME_BASE_BALANCED",
        "strategy_name": "Balanced Survival",
        "parameters": {
            "confidence_threshold": 60,
            "macro_risk_threshold": 65,
            "regime_penalty_multiplier": 1.0,
            "correlation_penalty_multiplier": 1.0,
            "allocation_score_threshold": 35,
        },
        "regime_filter": ["TRENDING BULL", "HIGH VOLATILITY", "SIDEWAYS / CHOPPY", "UNKNOWN"],
        "macro_filter": ["LOW_RISK", "RISK_ON", "CAUTION", "CHOPPY", "UNKNOWN"],
        "cross_market_filter": ["RISK_ON", "ALTSEASON_RISK_ON", "CAUTION", "MIXED", "UNKNOWN"],
        "status": "ACTIVE",
    },
    {
        "strategy_id": "GENOME_DEFENSIVE_MACRO",
        "strategy_name": "Defensive Macro Filter",
        "parameters": {
            "confidence_threshold": 68,
            "macro_risk_threshold": 45,
            "regime_penalty_multiplier": 1.25,
            "correlation_penalty_multiplier": 1.2,
            "allocation_score_threshold": 55,
        },
        "regime_filter": ["TRENDING BULL", "HIGH VOLATILITY", "BREAKOUT EXPANSION", "UNKNOWN"],
        "macro_filter": ["LOW_RISK", "RISK_ON", "UNKNOWN"],
        "cross_market_filter": ["RISK_ON", "ALTSEASON_RISK_ON", "UNKNOWN"],
        "status": "WATCH",
    },
    {
        "strategy_id": "GENOME_ALTSEASON_MOMENTUM",
        "strategy_name": "Altseason Momentum",
        "parameters": {
            "confidence_threshold": 58,
            "macro_risk_threshold": 70,
            "regime_penalty_multiplier": 0.85,
            "correlation_penalty_multiplier": 0.9,
            "allocation_score_threshold": 35,
        },
        "regime_filter": ["TRENDING BULL", "BREAKOUT EXPANSION", "HIGH VOLATILITY", "UNKNOWN"],
        "macro_filter": ["LOW_RISK", "RISK_ON", "CAUTION", "CHOPPY", "UNKNOWN"],
        "cross_market_filter": ["ALTSEASON_RISK_ON", "RISK_ON", "UNKNOWN"],
        "status": "WATCH",
    },
]


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


def _read_table(db_path: str, table: str, query: str) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            if not exists:
                return pd.DataFrame()
            return pd.read_sql_query(query, connection)
    except Exception:
        return pd.DataFrame()


def _read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _load_registry(path: str = REGISTRY_PATH) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        registry = {
            "created_at": _now(),
            "updated_at": _now(),
            "strategies": [
                {**strategy, "created_at": _now()}
                for strategy in BASE_STRATEGIES
            ],
        }
        _save_registry(registry, path)
        return registry
    try:
        with open(path, encoding="utf-8") as registry_file:
            registry = json.load(registry_file)
    except (OSError, json.JSONDecodeError):
        registry = {"created_at": _now(), "updated_at": _now(), "strategies": []}
    if not registry.get("strategies"):
        registry["strategies"] = [{**strategy, "created_at": _now()} for strategy in BASE_STRATEGIES]
    canonical = {strategy["strategy_id"]: strategy for strategy in BASE_STRATEGIES}
    merged = []
    seen = set()
    for strategy in registry.get("strategies", []):
        strategy_id = strategy.get("strategy_id")
        if strategy_id in canonical:
            merged.append({**canonical[strategy_id], "created_at": strategy.get("created_at") or _now()})
        else:
            merged.append(strategy)
        seen.add(strategy_id)
    for strategy_id, strategy in canonical.items():
        if strategy_id not in seen:
            merged.append({**strategy, "created_at": _now()})
    registry["strategies"] = merged
    return registry


def _save_registry(registry: Dict[str, Any], path: str = REGISTRY_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    registry["updated_at"] = _now()
    with open(path, "w", encoding="utf-8") as registry_file:
        json.dump(registry, registry_file, indent=2, default=str)


def _mutate_strategy(strategy: Dict[str, Any]) -> List[Dict[str, Any]]:
    params = dict(strategy.get("parameters", {}))
    mutations = []
    variants = [
        ("CONF_UP", {"confidence_threshold": _num(params.get("confidence_threshold"), 60) + 5}),
        ("CONF_DOWN", {"confidence_threshold": max(45, _num(params.get("confidence_threshold"), 60) - 5)}),
        ("MACRO_STRICT", {"macro_risk_threshold": max(25, _num(params.get("macro_risk_threshold"), 60) - 10)}),
        ("REGIME_DEF", {"regime_penalty_multiplier": _num(params.get("regime_penalty_multiplier"), 1.0) + 0.15}),
        ("CORR_DEF", {"correlation_penalty_multiplier": _num(params.get("correlation_penalty_multiplier"), 1.0) + 0.20}),
        ("ALLOC_UP", {"allocation_score_threshold": _num(params.get("allocation_score_threshold"), 45) + 5}),
    ]
    for suffix, changes in variants:
        mutated_params = dict(params)
        mutated_params.update(changes)
        mutations.append(
            {
                **strategy,
                "strategy_id": f"{strategy.get('strategy_id')}_{suffix}",
                "strategy_name": f"{strategy.get('strategy_name')} {suffix}",
                "parameters": mutated_params,
                "created_at": _now(),
                "status": "WATCH",
            }
        )
    return mutations


def _strategy_universe(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = registry.get("strategies", [])
    universe = list(base)
    for strategy in base[:8]:
        universe.extend(_mutate_strategy(strategy))
    seen = set()
    unique = []
    for strategy in universe:
        strategy_id = str(strategy.get("strategy_id"))
        if strategy_id not in seen:
            unique.append(strategy)
            seen.add(strategy_id)
    return unique[:64]


def _load_dataset(
    db_path: str,
    macro_path: str,
    cross_market_path: str,
    allocation_path: str,
) -> pd.DataFrame:
    outcomes_query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.symbol,
            o.pnl_pct AS pnl,
            COALESCE(o.status, o.win_loss, 'UNKNOWN') AS status,
            COALESCE(s.score, o.score, 0) AS confidence,
            COALESCE(NULLIF(s.regime_name, ''), 'UNKNOWN') AS regime_name
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        ORDER BY o.id DESC
        LIMIT 20000
    """
    paper_query = """
        SELECT
            timestamp,
            symbol,
            pnl AS pnl,
            status,
            confidence,
            COALESCE(macro_state, 'UNKNOWN') AS macro_state,
            COALESCE(regime, 'UNKNOWN') AS regime_name,
            COALESCE(allocation_tier, 'WATCH') AS allocation_tier
        FROM internal_paper_trades
        ORDER BY id DESC
        LIMIT 5000
    """
    outcomes = _read_table(db_path, "historical_outcomes", outcomes_query)
    paper = _read_table(db_path, "internal_paper_trades", paper_query)
    frames = []
    if not outcomes.empty:
        outcomes["source"] = "historical_outcomes"
        frames.append(outcomes)
    if not paper.empty:
        paper["source"] = "internal_paper_trades"
        frames.append(paper)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce").fillna(0.0)
    df["confidence"] = pd.to_numeric(df.get("confidence"), errors="coerce").fillna(0.0)
    df["regime_name"] = df.get("regime_name", pd.Series(["UNKNOWN"] * len(df))).fillna("UNKNOWN").astype(str)

    macro = _read_csv(macro_path)
    cross = _read_csv(cross_market_path)
    allocation = _read_csv(allocation_path)
    latest_macro = macro.iloc[-1].to_dict() if not macro.empty else {}
    latest_cross = cross.iloc[-1].to_dict() if not cross.empty else {}
    df["macro_state"] = df.get("macro_state", pd.Series([latest_macro.get("macro_state", "UNKNOWN")] * len(df))).fillna(latest_macro.get("macro_state", "UNKNOWN")).astype(str)
    df["macro_risk_score"] = _num(latest_macro.get("macro_risk_score"))
    df["cross_market_state"] = str(latest_cross.get("cross_market_state", "UNKNOWN"))
    df["cross_market_stress_score"] = _num(latest_cross.get("cross_market_stress_score"))

    if not allocation.empty and "symbol" in allocation.columns:
        keep = [
            column
            for column in ["symbol", "opportunity_score", "risk_score", "allocation_tier"]
            if column in allocation.columns
        ]
        df = df.merge(allocation[keep].drop_duplicates("symbol"), on="symbol", how="left", suffixes=("", "_allocation"))
    if "opportunity_score" not in df.columns:
        df["opportunity_score"] = df["confidence"]
    df["opportunity_score"] = pd.to_numeric(df["opportunity_score"], errors="coerce").fillna(df["confidence"])
    if "allocation_tier" not in df.columns:
        df["allocation_tier"] = "WATCH"
    df["allocation_tier"] = df["allocation_tier"].fillna("WATCH").astype(str)
    df["timestamp"] = pd.to_datetime(df.get("timestamp"), errors="coerce", utc=True)
    return df.sort_values("timestamp", na_position="last").reset_index(drop=True)


def _profit_factor(pnl: pd.Series) -> float:
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_drawdown(pnl: pd.Series) -> float:
    equity = pnl.cumsum()
    if equity.empty:
        return 0.0
    return float((equity - equity.cummax()).min())


def _survival_score(df: pd.DataFrame, column: str, hostile_values: List[str]) -> float:
    if df.empty or column not in df.columns:
        return 50.0
    hostile = df[df[column].astype(str).str.upper().isin([value.upper() for value in hostile_values])]
    if hostile.empty:
        return 75.0
    pnl = hostile["pnl"]
    return round(max(0.0, min(100.0, 50.0 + float((pnl > 0).mean() * 40.0) + float(pnl.mean() * 4.0))), 4)


def _correlation_penalty(df: pd.DataFrame) -> float:
    if df.empty or not {"timestamp", "symbol", "pnl"}.issubset(df.columns):
        return 0.0
    pivot = df.pivot_table(index="timestamp", columns="symbol", values="pnl", aggfunc="sum").fillna(0.0)
    if pivot.shape[0] < 5 or pivot.shape[1] < 2:
        return 0.0
    corr = pivot.corr().abs()
    values = corr.where(~pd.DataFrame(True, index=corr.index, columns=corr.columns).where(corr.index == corr.columns, False)).stack()
    return float(values.mean() * 20.0) if not values.empty else 0.0


def _apply_strategy(df: pd.DataFrame, strategy: Dict[str, Any]) -> pd.DataFrame:
    if df.empty:
        return df
    params = strategy.get("parameters", {})
    mask = pd.Series(True, index=df.index)
    mask &= df["confidence"] >= _num(params.get("confidence_threshold"), 60)
    mask &= df["opportunity_score"] >= _num(params.get("allocation_score_threshold"), 45)
    mask &= df["macro_risk_score"] <= _num(params.get("macro_risk_threshold"), 65)
    regime_filter = {str(item).upper() for item in strategy.get("regime_filter", [])}
    macro_filter = {str(item).upper() for item in strategy.get("macro_filter", [])}
    cross_filter = {str(item).upper() for item in strategy.get("cross_market_filter", [])}
    if regime_filter:
        mask &= df["regime_name"].astype(str).str.upper().isin(regime_filter)
    if macro_filter:
        mask &= df["macro_state"].astype(str).str.upper().isin(macro_filter)
    if cross_filter:
        mask &= df["cross_market_state"].astype(str).str.upper().isin(cross_filter)
    return df[mask].copy()


def _evaluate(strategy: Dict[str, Any], dataset: pd.DataFrame) -> Dict[str, Any]:
    selected = _apply_strategy(dataset, strategy)
    pnl = pd.to_numeric(selected.get("pnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    trade_count = int(len(pnl))
    profit_factor = _profit_factor(pnl)
    max_dd = _max_drawdown(pnl)
    winrate = float((pnl > 0).mean() * 100) if trade_count else 0.0
    avg_pnl = float(pnl.mean()) if trade_count else 0.0
    regime_survival = _survival_score(selected, "regime_name", ["RISK OFF", "PANIC", "TRENDING BEAR", "SIDEWAYS / CHOPPY"])
    macro_survival = _survival_score(selected, "macro_state", ["HIGH_STRESS", "PANIC", "RISK_OFF"])
    cross_survival = _survival_score(selected, "cross_market_state", ["CROSS_MARKET_STRESS", "SAFE_HAVEN_ROTATION"])
    corr_penalty = _correlation_penalty(selected) * _num(strategy.get("parameters", {}).get("correlation_penalty_multiplier"), 1.0)
    overfit_risk = max(0.0, min(100.0, (25.0 if trade_count < 30 else 8.0) + abs(max_dd) * 0.7 + corr_penalty))
    stability_score = max(
        0.0,
        min(
            100.0,
            45.0
            + min(25.0, trade_count / 4.0)
            + min(20.0, (profit_factor if not math.isinf(profit_factor) else 3.0) * 8.0)
            + avg_pnl * 2.5
            + (regime_survival + macro_survival + cross_survival) / 12.0
            - overfit_risk * 0.35,
        ),
    )
    status = "REJECTED"
    if trade_count >= 30 and profit_factor >= 1.08 and max_dd > -25 and stability_score >= 60:
        status = "PROMOTED"
    elif trade_count >= 10 and profit_factor >= 0.95 and stability_score >= 45:
        status = "WATCH"
    return {
        "timestamp": _now(),
        "strategy_id": strategy.get("strategy_id"),
        "strategy_name": strategy.get("strategy_name"),
        "status": status,
        "parameters_json": json.dumps(strategy.get("parameters", {}), default=str),
        "regime_filter": "|".join(map(str, strategy.get("regime_filter", []))),
        "macro_filter": "|".join(map(str, strategy.get("macro_filter", []))),
        "cross_market_filter": "|".join(map(str, strategy.get("cross_market_filter", []))),
        "total_pnl": round(float(pnl.sum()), 6),
        "profit_factor": round(profit_factor, 6) if not math.isinf(profit_factor) else math.inf,
        "max_drawdown": round(float(max_dd), 6),
        "winrate": round(winrate, 4),
        "trade_count": trade_count,
        "regime_survival_score": regime_survival,
        "macro_survival_score": macro_survival,
        "cross_market_survival_score": cross_survival,
        "overfit_risk": round(overfit_risk, 4),
        "stability_score": round(stability_score, 4),
    }


def _archive_results(results: pd.DataFrame, archive_path: str) -> None:
    os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
    if results.empty:
        return
    if os.path.exists(archive_path):
        existing = _read_csv(archive_path)
        combined = pd.concat([existing, results], ignore_index=True) if not existing.empty else results
        combined.tail(2000).to_csv(archive_path, index=False)
    else:
        results.to_csv(archive_path, index=False)


def run_strategy_genome(
    db_path: str = "mamuyy_hunter.db",
    registry_path: str = REGISTRY_PATH,
    results_path: str = RESULTS_PATH,
    archive_path: str = ARCHIVE_PATH,
    macro_path: str = "logs/macro_observer.csv",
    cross_market_path: str = "logs/cross_market_intelligence.csv",
    allocation_path: str = "logs/opportunity_allocation.csv",
) -> Dict[str, Any]:
    registry = _load_registry(registry_path)
    dataset = _load_dataset(db_path, macro_path, cross_market_path, allocation_path)
    strategies = _strategy_universe(registry)
    rows = [_evaluate(strategy, dataset) for strategy in strategies]
    results = pd.DataFrame(rows)
    if not results.empty:
        results = results.sort_values(
            ["status", "stability_score", "profit_factor", "trade_count"],
            ascending=[True, False, False, False],
        )
    os.makedirs(os.path.dirname(results_path) or ".", exist_ok=True)
    results.to_csv(results_path, index=False)
    _archive_results(results, archive_path)

    promoted = results[results["status"] == "PROMOTED"].head(10).to_dict("records") if not results.empty else []
    registry["last_run_at"] = _now()
    registry["last_dataset_rows"] = int(len(dataset))
    registry["last_promoted"] = promoted
    _save_registry(registry, registry_path)
    return {
        "ok": True,
        "paper_only": True,
        "dataset_rows": int(len(dataset)),
        "strategy_count": int(len(strategies)),
        "results_path": results_path,
        "archive_path": archive_path,
        "top": results.head(10).to_dict("records") if not results.empty else [],
        "promoted": promoted,
        "rejected_count": int((results["status"] == "REJECTED").sum()) if not results.empty else 0,
    }


def strategy_ranking(results_path: str = RESULTS_PATH) -> Dict[str, Any]:
    results = _read_csv(results_path)
    if results.empty:
        return {"ok": False, "rows": 0, "top": [], "message": "No strategy genome results. Run python main.py --strategy-genome first."}
    ranked = results.sort_values(["stability_score", "profit_factor", "trade_count"], ascending=[False, False, False])
    return {
        "ok": True,
        "rows": int(len(ranked)),
        "top": ranked.head(20).to_dict("records"),
        "promoted": ranked[ranked["status"] == "PROMOTED"].head(10).to_dict("records"),
        "rejected": ranked[ranked["status"] == "REJECTED"].tail(10).to_dict("records"),
    }


def format_strategy_genome_result(result: Dict[str, Any]) -> str:
    top = pd.DataFrame(result.get("top", []))
    return "\n".join(
        [
            "STRATEGY GENOME LAB",
            f"OK: {result.get('ok')}",
            f"Paper Only: {result.get('paper_only', True)}",
            f"Dataset Rows: {result.get('dataset_rows', 0)}",
            f"Strategies Evaluated: {result.get('strategy_count', result.get('rows', 0))}",
            f"Rejected Count: {result.get('rejected_count', '-')}",
            f"Results: {result.get('results_path', RESULTS_PATH)}",
            f"Archive: {result.get('archive_path', ARCHIVE_PATH)}",
            "",
            "Top Strategies:",
            top.to_string(index=False) if not top.empty else result.get("message", "No strategy rows."),
        ]
    )
