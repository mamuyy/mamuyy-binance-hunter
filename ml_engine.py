import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import OneHotEncoder

from ml_temporal_guard import asof_feature_join, validate_temporal_feature_rows


NUMERIC_FEATURES = [
    "score",
    "volume_spike",
    "breakout",
    "liquidity_sweep",
    "funding_zscore",
    "oi_expansion_rate",
    "taker_delta",
    "pressure_score",
    "squeeze_probability",
    "regime_score",
]
CATEGORICAL_FEATURES = ["regime_name", "whale_activity", "funding_warning"]
TARGET_LABELS = ["WIN", "LOSS", "TP1 HIT", "TP2 HIT"]
PROFITABLE_LABELS = {"WIN", "TP1 HIT", "TP2 HIT"}
PRODUCTION_TARGET_LABELS = ["WIN", "LOSS", "TP1 HIT"]
PRODUCTION_DATASET_CONTRACT_VERSION = "CP-039.production_universe.v1"
PRODUCTION_LABEL_MAPPING_VERSION = "CP-039.label_mapping.v1"
PRODUCTION_LABEL_MAPPING = {
    "TAKE_PROFIT_2": "WIN",
    "TAKE_PROFIT_1": "TP1 HIT",
    "STOP_LOSS": "LOSS",
    "EXPIRED_ORPHANED": "EXCLUDE",
}
PRODUCTION_EXCLUDED_OUTCOMES = {"OPEN", "UNKNOWN", "EXECUTION_SIMULATED", "EXPIRED_ORPHANED"}
PRODUCTION_REPORT_PATH = os.path.join("logs", "production_universe_dataset_build_report.json")


def _read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _paper_trades_available(path: str) -> bool:
    trades = _read_csv(path)
    return not trades.empty



def _empty_ml_dataset() -> pd.DataFrame:
    return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _column_expr(columns: set[str], table_alias: str, column: str, default: str = "NULL") -> str:
    return f"{table_alias}.{column}" if column in columns else default


def _map_production_label(value: Any) -> tuple[str, str]:
    label = str(value or "").strip().upper()
    mapped = PRODUCTION_LABEL_MAPPING.get(label)
    if mapped == "EXCLUDE" or label in PRODUCTION_EXCLUDED_OUTCOMES:
        return "", label or "BLANK_OUTCOME"
    target = mapped or (label if label in PRODUCTION_TARGET_LABELS else "")
    if target not in PRODUCTION_TARGET_LABELS:
        return "", f"UNMAPPED:{label or 'BLANK_OUTCOME'}"
    return target, ""


def _apply_production_label_mapping(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        frame["target"] = []
        frame["exclusion_reason"] = []
        return frame
    mapped = frame["raw_outcome"].apply(_map_production_label)
    frame = frame.copy()
    frame["target"] = mapped.apply(lambda item: item[0])
    frame["exclusion_reason"] = mapped.apply(lambda item: item[1])
    frame["status"] = frame["target"]
    return frame


def _dataset_build_hash(dataset: pd.DataFrame, threshold: float) -> str:
    if dataset.empty:
        payload = f"empty|{threshold}|{PRODUCTION_DATASET_CONTRACT_VERSION}|{PRODUCTION_LABEL_MAPPING_VERSION}"
    else:
        stable_columns = [column for column in ["source_artifact", "symbol", "timestamp", "target_timestamp", "target", "score", "pnl_percent"] if column in dataset.columns]
        payload = dataset[stable_columns].astype(str).to_csv(index=False)
        payload += f"|{threshold}|{PRODUCTION_DATASET_CONTRACT_VERSION}|{PRODUCTION_LABEL_MAPPING_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_production_report(report: Dict[str, Any], report_path: str = PRODUCTION_REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)


def _production_universe_dataset(database_path: str = "mamuyy_hunter.db", production_score_threshold: int = 75) -> pd.DataFrame:
    if not os.path.exists(database_path):
        report = {
            "build_time_utc": datetime.now(timezone.utc).isoformat(),
            "dataset_contract_version": PRODUCTION_DATASET_CONTRACT_VERSION,
            "dataset_build_hash": _dataset_build_hash(_empty_ml_dataset(), production_score_threshold),
            "production_score_threshold": production_score_threshold,
            "label_mapping_version": PRODUCTION_LABEL_MAPPING_VERSION,
            "source_priority": ["internal_paper_trades", "historical_outcomes"],
            "source_priority_used": None,
            "source_row_counts": {},
            "excluded_row_counts_by_reason": {},
            "final_label_distribution": {},
            "timestamp_range": {"min": None, "max": None},
            "selected_source": None,
            "rows": 0,
            "verdict": "DATABASE_NOT_FOUND",
        }
        _write_production_report(report)
        return _empty_ml_dataset()

    raw = pd.DataFrame()
    source_counts: Dict[str, Dict[str, int]] = {}
    excluded_counts: Dict[str, int] = {}
    selected_source = None
    try:
        with sqlite3.connect(database_path) as connection:
            paper_columns = _table_columns(connection, "internal_paper_trades")
            if paper_columns:
                target_timestamp_expr = _column_expr(paper_columns, "p", "target_timestamp", _column_expr(paper_columns, "p", "updated_at"))
                timestamp_expr = _column_expr(paper_columns, "p", "source_signal_timestamp", _column_expr(paper_columns, "p", "timestamp"))
                paper_select = [
                    f"{timestamp_expr} AS timestamp",
                    f"{target_timestamp_expr} AS target_timestamp",
                    f"{_column_expr(paper_columns, 'p', 'symbol')} AS symbol",
                    f"{_column_expr(paper_columns, 'p', 'exit_reason')} AS raw_outcome",
                    f"{_column_expr(paper_columns, 'p', 'pnl', '0')} AS pnl_percent",
                    f"{_column_expr(paper_columns, 'p', 'confidence', '0')} AS score",
                    f"{_column_expr(paper_columns, 'p', 'regime', "'UNKNOWN'")} AS regime_name",
                    "'internal_paper_trades' AS source_artifact",
                ]
                paper_total = int(connection.execute("SELECT COUNT(*) FROM internal_paper_trades").fetchone()[0])
                paper = pd.read_sql_query(
                    f"SELECT {', '.join(paper_select)} FROM internal_paper_trades p WHERE UPPER(COALESCE({_column_expr(paper_columns, 'p', 'status')}, '')) = 'CLOSED' ORDER BY timestamp ASC",
                    connection,
                )
                paper = _apply_production_label_mapping(paper)
                for reason, count in paper.loc[paper["target"] == "", "exclusion_reason"].value_counts().items():
                    excluded_counts[str(reason)] = excluded_counts.get(str(reason), 0) + int(count)
                paper = paper[paper["target"] != ""]
                source_counts["internal_paper_trades"] = {"total_rows": paper_total, "eligible_rows": int(len(paper) + sum(excluded_counts.values())), "selected_rows": int(len(paper))}
                if not paper.empty:
                    raw = paper
                    selected_source = "internal_paper_trades"
            historical_columns = _table_columns(connection, "historical_outcomes")
            if raw.empty and historical_columns:
                score_candidates = [_column_expr(historical_columns, "h", "score"), _column_expr(historical_columns, "h", "confidence"), "0"]
                score_expr = f"COALESCE({', '.join(score_candidates)})"
                outcome_exprs = [
                    _column_expr(historical_columns, "h", "exit_reason"),
                    _column_expr(historical_columns, "h", "status"),
                    _column_expr(historical_columns, "h", "win_loss"),
                ]
                hist_select = [
                    f"{_column_expr(historical_columns, 'h', 'signal_timestamp')} AS timestamp",
                    f"{_column_expr(historical_columns, 'h', 'close_timestamp')} AS target_timestamp",
                    f"{_column_expr(historical_columns, 'h', 'symbol')} AS symbol",
                    f"COALESCE({', '.join(outcome_exprs)}) AS raw_outcome",
                    f"{_column_expr(historical_columns, 'h', 'pnl_pct', '0')} AS pnl_percent",
                    f"{score_expr} AS score",
                    "'HISTORICAL_DERIVED' AS regime_name",
                    "'historical_outcomes' AS source_artifact",
                ]
                hist_total = int(connection.execute("SELECT COUNT(*) FROM historical_outcomes").fetchone()[0])
                hist = pd.read_sql_query(
                    f"SELECT {', '.join(hist_select)} FROM historical_outcomes h WHERE {score_expr} >= ? ORDER BY timestamp ASC",
                    connection,
                    params=(production_score_threshold,),
                )
                hist = _apply_production_label_mapping(hist)
                hist_excluded = hist.loc[hist["target"] == "", "exclusion_reason"].value_counts().to_dict()
                for reason, count in hist_excluded.items():
                    excluded_counts[str(reason)] = excluded_counts.get(str(reason), 0) + int(count)
                hist = hist[hist["target"] != ""]
                source_counts["historical_outcomes"] = {"total_rows": hist_total, "eligible_rows": int(len(hist) + sum(hist_excluded.values())), "selected_rows": int(len(hist))}
                if not hist.empty:
                    raw = hist
                    selected_source = "historical_outcomes"
    except (sqlite3.Error, pd.errors.DatabaseError):
        raw = pd.DataFrame()

    dataset = _prepare_dataset(raw) if not raw.empty else _empty_ml_dataset()
    build_hash = _dataset_build_hash(dataset, production_score_threshold)
    for column, value in {
        "dataset_contract_version": PRODUCTION_DATASET_CONTRACT_VERSION,
        "dataset_build_hash": build_hash,
        "production_score_threshold": production_score_threshold,
        "label_mapping_version": PRODUCTION_LABEL_MAPPING_VERSION,
    }.items():
        dataset[column] = value
    timestamps = pd.to_datetime(dataset["timestamp"], errors="coerce", utc=True) if "timestamp" in dataset.columns and not dataset.empty else pd.Series(dtype="datetime64[ns, UTC]")
    timestamp_range = {
        "min": timestamps.min().isoformat() if not timestamps.dropna().empty else None,
        "max": timestamps.max().isoformat() if not timestamps.dropna().empty else None,
    }
    label_counts = dataset["target"].value_counts().to_dict() if not dataset.empty else {}
    report = {
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_contract_version": PRODUCTION_DATASET_CONTRACT_VERSION,
        "dataset_build_hash": build_hash,
        "production_score_threshold": production_score_threshold,
        "label_mapping_version": PRODUCTION_LABEL_MAPPING_VERSION,
        "label_mapping": PRODUCTION_LABEL_MAPPING,
        "excluded_outcomes": sorted(PRODUCTION_EXCLUDED_OUTCOMES),
        "source_priority": ["internal_paper_trades", "historical_outcomes"],
        "source_priority_used": selected_source,
        "selected_source": selected_source,
        "source_row_counts": source_counts,
        "excluded_row_counts_by_reason": excluded_counts,
        "rows": int(len(dataset)),
        "final_label_distribution": label_counts,
        "label_counts": label_counts,
        "timestamp_range": timestamp_range,
        "verdict": "PRODUCTION_UNIVERSE_DATASET_BUILT",
    }
    _write_production_report(report)
    return dataset


def _historical_dataset(database_path: str = "mamuyy_hunter.db") -> pd.DataFrame:
    if not os.path.exists(database_path):
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
            o.close_timestamp AS target_timestamp,
            o.symbol,
            o.status,
            o.win_loss,
            o.pnl_pct AS pnl_percent,
            o.score,
            s.volume_spike,
            s.breakout,
            s.liquidity_sweep,
            s.regime_score,
            COALESCE(NULLIF(NULLIF(s.regime_name, ''), 'UNKNOWN'), 'HISTORICAL_DERIVED') AS regime_name,
            f.funding_zscore,
            f.oi_expansion_rate,
            f.taker_delta,
            f.pressure_score,
            f.squeeze_probability,
            f.whale_activity,
            f.funding_warning
        FROM historical_outcomes o
        LEFT JOIN signals s
          ON s.symbol = o.symbol
         AND s.timestamp = o.signal_timestamp
        LEFT JOIN flow_logs f
          ON f.symbol = o.symbol
         AND f.timestamp = o.signal_timestamp
        ORDER BY o.signal_timestamp ASC
    """
    try:
        with sqlite3.connect(database_path) as connection:
            dataset = pd.read_sql_query(query, connection)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    if dataset.empty:
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    dataset["status"] = dataset["status"].where(dataset["status"].isin(TARGET_LABELS), dataset["win_loss"])
    return _prepare_dataset(dataset)


def _status(value: Any) -> str:
    value = str(value or "").strip().upper()
    return value if value in TARGET_LABELS else ""


def _latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    if "timestamp" in df.columns:
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        return df.sort_values("timestamp").drop_duplicates("symbol", keep="last")
    return df.drop_duplicates("symbol", keep="last")


def _prepare_dataset(dataset: pd.DataFrame) -> pd.DataFrame:
    for column in NUMERIC_FEATURES:
        if column not in dataset.columns:
            dataset[column] = 0
        dataset[column] = pd.to_numeric(dataset[column], errors="coerce").fillna(0.0)
    for column in ["breakout", "liquidity_sweep"]:
        dataset[column] = dataset[column].astype(str).str.lower().isin(["true", "1", "yes"]).astype(int)
    for column in CATEGORICAL_FEATURES:
        if column not in dataset.columns:
            dataset[column] = "UNKNOWN"
        fallback = "HISTORICAL_DERIVED" if column == "regime_name" else "UNKNOWN"
        dataset[column] = dataset[column].fillna(fallback).replace({"": fallback, "UNKNOWN": fallback}).astype(str)
    if "timestamp" in dataset.columns:
        dataset["timestamp"] = pd.to_datetime(dataset["timestamp"], errors="coerce", utc=True)
    dataset["target"] = dataset.get("status", "").apply(_status)
    keep_columns = [
        column
        for column in ["timestamp", "prediction_timestamp", "feature_timestamp_max", "target_timestamp", "label_timestamp", "outcome_timestamp", "source_artifact", "symbol", "pnl_percent", *NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"]
        if column in dataset.columns
    ]
    return dataset[dataset["target"].isin(TARGET_LABELS)][keep_columns].copy()


def build_ml_dataset(
    paper_trades_path: str,
    signals_log_path: str,
    flow_log_path: str,
    database_path: str = "mamuyy_hunter.db",
    use_production_universe: bool = False,
    production_score_threshold: int = 75,
) -> pd.DataFrame:
    if use_production_universe:
        return _production_universe_dataset(database_path, production_score_threshold)

    trades = _read_csv(paper_trades_path)
    if trades.empty:
        return _historical_dataset(database_path)

    dataset = trades.copy()
    if "prediction_timestamp" not in dataset.columns:
        dataset["prediction_timestamp"] = dataset.get("timestamp", dataset.get("signal_timestamp"))
    dataset["source_artifact"] = paper_trades_path
    for source_path in (signals_log_path, flow_log_path):
        source = _read_csv(source_path)
        if source.empty or "timestamp" not in source.columns or "symbol" not in source.columns:
            continue
        columns = [c for c in ["symbol", "timestamp", *NUMERIC_FEATURES, *CATEGORICAL_FEATURES] if c in source.columns]
        joined = asof_feature_join(dataset[["symbol", "prediction_timestamp"]], source[columns])
        for column in [*NUMERIC_FEATURES, *CATEGORICAL_FEATURES]:
            if column not in joined.columns:
                continue
            if column not in dataset.columns:
                dataset[column] = joined[column]
            else:
                dataset[column] = dataset[column].where(dataset[column].notna(), joined[column])
        current_max = pd.to_datetime(dataset.get("feature_timestamp_max"), errors="coerce", utc=True) if "feature_timestamp_max" in dataset.columns else pd.Series(pd.NaT, index=dataset.index)
        joined_ts = pd.to_datetime(joined.get("feature_timestamp_max"), errors="coerce", utc=True)
        dataset["feature_timestamp_max"] = pd.concat([current_max, joined_ts], axis=1).max(axis=1)
    if "feature_timestamp_max" not in dataset.columns:
        dataset["feature_timestamp_max"] = pd.to_datetime(dataset["prediction_timestamp"], errors="coerce", utc=True)
    guard = validate_temporal_feature_rows(dataset, feature_columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES], source_artifact=paper_trades_path)
    if guard["status"] == "BLOCKED":
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])

    return _prepare_dataset(dataset)


def _new_one_hot_encoder() -> OneHotEncoder:
    try:
        return OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:
        return OneHotEncoder(handle_unknown="ignore", sparse=False)


def fit_train_only_preprocessor(train_dataset: pd.DataFrame) -> Dict[str, Any]:
    """Fit preprocessing state on training rows only.

    This intentionally only fits the categorical encoder on the supplied training
    slice. Numeric preparation remains deterministic row-local coercion in
    `_prepare_dataset`; it does not learn distributional state.
    """
    encoder = _new_one_hot_encoder()
    encoder.fit(train_dataset[CATEGORICAL_FEATURES].astype(str))
    feature_names = [*NUMERIC_FEATURES, *encoder.get_feature_names_out(CATEGORICAL_FEATURES).tolist()]
    return {
        "encoder": encoder,
        "feature_names": feature_names,
        "fit_scope": "TRAIN_ONLY",
        "fit_row_count": int(len(train_dataset)),
    }


def transform_with_train_preprocessor(dataset: pd.DataFrame, preprocessor: Dict[str, Any]) -> pd.DataFrame:
    """Transform any split using a preprocessor already fit on train rows."""
    if preprocessor.get("fit_scope") != "TRAIN_ONLY":
        raise ValueError("preprocessor must be fit with fit_scope=TRAIN_ONLY before transform")
    encoder = preprocessor["encoder"]
    encoded = encoder.transform(dataset[CATEGORICAL_FEATURES].astype(str))
    encoded_columns = encoder.get_feature_names_out(CATEGORICAL_FEATURES).tolist()
    encoded_df = pd.DataFrame(encoded, columns=encoded_columns, index=dataset.index)
    features = pd.concat([dataset[NUMERIC_FEATURES], encoded_df], axis=1)
    return features.reindex(columns=preprocessor["feature_names"], fill_value=0)


def _encode(dataset: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Compatibility wrapper for single-split callers; fits only on supplied rows."""
    preprocessor = fit_train_only_preprocessor(dataset)
    features = transform_with_train_preprocessor(dataset, preprocessor)
    return features, preprocessor["feature_names"]


def _quality(score: int) -> str:
    if score >= 70:
        return "HIGH QUALITY"
    if score >= 45:
        return "MEDIUM QUALITY"
    return "LOW QUALITY"


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _plot_importance(importances: pd.DataFrame, path: str) -> None:
    if importances.empty:
        _placeholder(path, "Feature Importance")
        return
    top = importances.head(15).sort_values("importance")
    plt.figure(figsize=(10, 6))
    plt.barh(top["feature"], top["importance"])
    plt.title("Feature Importance")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _plot_correlation(dataset: pd.DataFrame, path: str) -> Dict[str, Dict[str, float]]:
    columns = [column for column in NUMERIC_FEATURES if column in dataset.columns]
    if dataset.empty or len(columns) < 2:
        _placeholder(path, "Correlation Heatmap")
        return {}
    corr = dataset[columns].corr(numeric_only=True).fillna(0.0)
    plt.figure(figsize=(10, 8))
    plt.imshow(corr.values, cmap="coolwarm", vmin=-1, vmax=1)
    plt.colorbar(label="Correlation")
    plt.xticks(range(len(columns)), columns, rotation=45, ha="right")
    plt.yticks(range(len(columns)), columns)
    plt.title("Correlation Matrix")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()
    return {row: {col: float(corr.loc[row, col]) for col in corr.columns} for row in corr.index}


def _plot_distribution(probabilities: list[float], path: str) -> None:
    if not probabilities:
        _placeholder(path, "Prediction Distribution")
        return
    plt.figure(figsize=(8, 4))
    plt.hist(probabilities, bins=10, range=(0, 1), edgecolor="black")
    plt.title("Profitable Setup Probability Distribution")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _regime_profitability(dataset: pd.DataFrame) -> tuple[str, str]:
    if dataset.empty:
        return "-", "-"
    df = dataset.copy()
    df["profitable"] = df["target"].isin(PROFITABLE_LABELS).astype(int)
    grouped = df.groupby("regime_name")["profitable"].mean().sort_values(ascending=False)
    if grouped.empty:
        return "-", "-"
    return str(grouped.index[0]), str(grouped.index[-1])


def _base_result(rows: int, charts: Dict[str, str]) -> Dict[str, Any]:
    return {
        "rows": rows,
        "model_ready": False,
        "accuracy": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "confusion_matrix": [],
        "labels": TARGET_LABELS,
        "feature_importance": [],
        "correlation_matrix": {},
        "ai_confidence_score": 0,
        "setup_ranking": "LOW QUALITY",
        "most_profitable_regime": "-",
        "worst_regime": "-",
        "charts": charts,
        "notes": [],
    }


def run_ml_research(
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    flow_log_path: str = "flow_log.csv",
    output_path: str = "model_output.json",
    chart_dir: str = "charts",
    database_path: str = "mamuyy_hunter.db",
) -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    charts = {
        "feature_importance": os.path.join(chart_dir, "feature_importance.png"),
        "correlation_heatmap": os.path.join(chart_dir, "correlation_heatmap.png"),
        "prediction_distribution": os.path.join(chart_dir, "prediction_distribution.png"),
    }
    dataset = build_ml_dataset(paper_trades_path, signals_log_path, flow_log_path, database_path=database_path)
    result = _base_result(len(dataset), charts)
    result["data_source"] = "paper_trades" if _paper_trades_available(paper_trades_path) else "historical_outcomes"
    result["correlation_matrix"] = _plot_correlation(dataset, charts["correlation_heatmap"])
    result["most_profitable_regime"], result["worst_regime"] = _regime_profitability(dataset)

    if len(dataset) < 8 or dataset["target"].nunique() < 2:
        result["notes"].append("Not enough labeled paper trades to train a stable model.")
        _plot_importance(pd.DataFrame(), charts["feature_importance"])
        _plot_distribution([], charts["prediction_distribution"])
        with open(output_path, "w", encoding="utf-8") as output_file:
            json.dump(result, output_file, indent=2)
        return result

    y = dataset["target"]
    stratify = y if y.value_counts().min() >= 2 else None
    train_dataset, test_dataset, y_train, y_test = train_test_split(
        dataset,
        y,
        test_size=0.3 if len(dataset) >= 20 else 0.4,
        random_state=42,
        stratify=stratify,
    )
    preprocessor = fit_train_only_preprocessor(train_dataset)
    X_train = transform_with_train_preprocessor(train_dataset, preprocessor)
    X_test = transform_with_train_preprocessor(test_dataset, preprocessor)
    X_all = transform_with_train_preprocessor(dataset, preprocessor)
    feature_names = preprocessor["feature_names"]

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=5,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    probabilities = model.predict_proba(X_all)
    class_to_index = {label: index for index, label in enumerate(model.classes_)}
    profitable_probability = np.zeros(len(dataset))
    for label in PROFITABLE_LABELS:
        if label in class_to_index:
            profitable_probability += probabilities[:, class_to_index[label]]

    importances = (
        pd.DataFrame({"feature": feature_names, "importance": model.feature_importances_})
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )
    result.update(
        {
            "model_ready": True,
            "accuracy": float(accuracy_score(y_test, predictions)),
            "precision": float(precision_score(y_test, predictions, average="weighted", zero_division=0)),
            "recall": float(recall_score(y_test, predictions, average="weighted", zero_division=0)),
            "confusion_matrix": confusion_matrix(y_test, predictions, labels=TARGET_LABELS).tolist(),
            "feature_importance": importances.head(20).to_dict(orient="records"),
            "ai_confidence_score": int(round(float(profitable_probability[-1]) * 100)),
        }
    )
    result["setup_ranking"] = _quality(result["ai_confidence_score"])
    _plot_importance(importances, charts["feature_importance"])
    _plot_distribution(profitable_probability.tolist(), charts["prediction_distribution"])

    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(result, output_file, indent=2)
    return result
