import json
import os
import sqlite3
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


def _historical_dataset(database_path: str = "mamuyy_hunter.db") -> pd.DataFrame:
    if not os.path.exists(database_path):
        return pd.DataFrame(columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"])
    query = """
        SELECT
            o.signal_timestamp AS timestamp,
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
        for column in ["timestamp", "symbol", "pnl_percent", *NUMERIC_FEATURES, *CATEGORICAL_FEATURES, "target"]
        if column in dataset.columns
    ]
    return dataset[dataset["target"].isin(TARGET_LABELS)][keep_columns].copy()


def build_ml_dataset(
    paper_trades_path: str,
    signals_log_path: str,
    flow_log_path: str,
    database_path: str = "mamuyy_hunter.db",
) -> pd.DataFrame:
    trades = _read_csv(paper_trades_path)
    if trades.empty:
        return _historical_dataset(database_path)

    dataset = trades.copy()
    sources = [_latest_by_symbol(_read_csv(signals_log_path)), _latest_by_symbol(_read_csv(flow_log_path))]
    for source in sources:
        if source.empty:
            continue
        columns = [c for c in ["symbol", *NUMERIC_FEATURES, *CATEGORICAL_FEATURES] if c in source.columns]
        dataset = dataset.merge(source[columns], on="symbol", how="left", suffixes=("", "_src"))
        for column in [*NUMERIC_FEATURES, *CATEGORICAL_FEATURES]:
            src_column = f"{column}_src"
            if src_column not in dataset.columns:
                continue
            if column not in dataset.columns:
                dataset[column] = dataset[src_column]
            else:
                dataset[column] = dataset[column].where(dataset[column].notna(), dataset[src_column])
            dataset = dataset.drop(columns=[src_column])

    return _prepare_dataset(dataset)


def _encode(dataset: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    encoded = encoder.fit_transform(dataset[CATEGORICAL_FEATURES].astype(str))
    encoded_columns = encoder.get_feature_names_out(CATEGORICAL_FEATURES).tolist()
    encoded_df = pd.DataFrame(encoded, columns=encoded_columns, index=dataset.index)
    features = pd.concat([dataset[NUMERIC_FEATURES], encoded_df], axis=1)
    return features, features.columns.tolist()


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

    X, feature_names = _encode(dataset)
    y = dataset["target"]
    stratify = y if y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.3 if len(dataset) >= 20 else 0.4,
        random_state=42,
        stratify=stratify,
    )
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=5,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    probabilities = model.predict_proba(X)
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
