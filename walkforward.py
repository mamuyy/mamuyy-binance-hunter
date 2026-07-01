import os
from typing import Any, Dict, List, Optional

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score

from ml_engine import PROFITABLE_LABELS, TARGET_LABELS, build_ml_dataset, fit_train_only_preprocessor, transform_with_train_preprocessor


RESULT_FIELDS = [
    "fold",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "train_accuracy",
    "test_accuracy",
    "precision",
    "recall",
    "profit_factor",
    "winrate",
    "best_regime",
    "worst_regime",
]


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _profit_factor(df: pd.DataFrame) -> float:
    if df.empty or "pnl_percent" not in df.columns:
        return 0.0
    pnl = pd.to_numeric(df["pnl_percent"], errors="coerce").fillna(0.0)
    gross_profit = pnl[pnl > 0].sum()
    gross_loss = abs(pnl[pnl < 0].sum())
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return float(gross_profit / gross_loss)


def _winrate(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return float(df["target"].isin(PROFITABLE_LABELS).mean() * 100)


def _regime_accuracy(df: pd.DataFrame, predictions: np.ndarray) -> Dict[str, float]:
    if df.empty or "regime_name" not in df.columns:
        return {}
    temp = df.copy()
    temp["prediction"] = predictions
    temp["correct"] = (temp["target"] == temp["prediction"]).astype(int)
    return {
        str(regime): float(group["correct"].mean())
        for regime, group in temp.groupby("regime_name")
    }


def _best_worst_regime(df: pd.DataFrame) -> tuple[str, str]:
    if df.empty or "regime_name" not in df.columns:
        return "-", "-"
    temp = df.copy()
    temp["profitable"] = temp["target"].isin(PROFITABLE_LABELS).astype(int)
    grouped = temp.groupby("regime_name")["profitable"].mean().sort_values(ascending=False)
    if grouped.empty:
        return "-", "-"
    return str(grouped.index[0]), str(grouped.index[-1])


def _health(stability_score: float, overfit_risk_score: float) -> str:
    if overfit_risk_score >= 65:
        return "OVERFIT RISK"
    if stability_score < 45:
        return "UNSTABLE"
    return "ROBUST"


def _plot_line(values: List[float], path: str, title: str, ylabel: str) -> None:
    if not values:
        _placeholder(path, title)
        return
    plt.figure(figsize=(9, 4))
    plt.plot(range(1, len(values) + 1), values, marker="o")
    plt.title(title)
    plt.xlabel("Fold")
    plt.ylabel(ylabel)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _plot_equity(fold_returns: List[float], path: str) -> None:
    if not fold_returns:
        _placeholder(path, "Walk-Forward Equity Curve")
        return
    equity = np.cumsum(fold_returns)
    plt.figure(figsize=(9, 4))
    plt.plot(range(1, len(equity) + 1), equity, marker="o")
    plt.title("Walk-Forward Equity Curve")
    plt.xlabel("Fold")
    plt.ylabel("Cumulative OOS PnL (%)")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _write_results(rows: List[Dict[str, Any]], path: str) -> None:
    pd.DataFrame(rows, columns=RESULT_FIELDS).to_csv(path, index=False)


def run_walkforward_validation(
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    output_path: str = "walkforward_results.csv",
    chart_dir: str = "charts",
    database_path: str = "mamuyy_hunter.db",
    train_window: int = 30,
    test_window: int = 10,
    prebuilt_dataset: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    charts = {
        "walkforward_equity_curve": os.path.join(chart_dir, "walkforward_equity_curve.png"),
        "rolling_accuracy": os.path.join(chart_dir, "rolling_accuracy.png"),
        "rolling_winrate": os.path.join(chart_dir, "rolling_winrate.png"),
    }
    if prebuilt_dataset is not None:
        dataset = prebuilt_dataset.copy()
    else:
        dataset = build_ml_dataset(
            paper_trades_path,
            signals_log_path,
            "__missing_flow_log.csv",
            database_path=database_path,
        )
    if "timestamp" in dataset.columns:
        dataset["timestamp"] = pd.to_datetime(dataset["timestamp"], errors="coerce", utc=True)
        dataset = dataset.sort_values("timestamp").reset_index(drop=True)
    else:
        dataset = dataset.reset_index(drop=True)

    min_rows = train_window + test_window
    if len(dataset) < min_rows or dataset["target"].nunique() < 2:
        _write_results([], output_path)
        _plot_equity([], charts["walkforward_equity_curve"])
        _plot_line([], charts["rolling_accuracy"], "Rolling Accuracy", "Accuracy")
        _plot_line([], charts["rolling_winrate"], "Rolling Winrate", "Winrate (%)")
        return {
            "rows": int(len(dataset)),
            "folds": 0,
            "average_accuracy": 0.0,
            "average_precision": 0.0,
            "average_recall": 0.0,
            "average_profit_factor": 0.0,
            "average_winrate": 0.0,
            "regime_specific_accuracy": {},
            "model_stability_score": 0.0,
            "overfit_risk_score": 0.0,
            "model_health": "UNSTABLE",
            "best_regime": "-",
            "worst_regime": "-",
            "charts": charts,
            "notes": ["Not enough labeled trades for walk-forward validation."],
        }

    rows = []
    accuracies = []
    train_accuracies = []
    precisions = []
    recalls = []
    profit_factors = []
    winrates = []
    fold_returns = []
    regime_accuracy_parts: Dict[str, List[float]] = {}

    fold = 1
    start = 0
    while start + train_window + test_window <= len(dataset):
        train = dataset.iloc[start : start + train_window].copy()
        test = dataset.iloc[start + train_window : start + train_window + test_window].copy()
        start += test_window

        if train["target"].nunique() < 2:
            continue

        preprocessor = fit_train_only_preprocessor(train)
        X_train = transform_with_train_preprocessor(train, preprocessor)
        X_test = transform_with_train_preprocessor(test, preprocessor)
        y_train = train["target"]
        y_test = test["target"]

        model = RandomForestClassifier(
            n_estimators=150,
            max_depth=5,
            class_weight="balanced",
            random_state=42,
        )
        model.fit(X_train, y_train)
        train_predictions = model.predict(X_train)
        test_predictions = model.predict(X_test)

        train_accuracy = float(accuracy_score(y_train, train_predictions))
        test_accuracy = float(accuracy_score(y_test, test_predictions))
        precision = float(precision_score(y_test, test_predictions, average="weighted", zero_division=0))
        recall = float(recall_score(y_test, test_predictions, average="weighted", zero_division=0))
        profit_factor = _profit_factor(test)
        winrate = _winrate(test)
        best_regime, worst_regime = _best_worst_regime(test)

        for regime, value in _regime_accuracy(test, test_predictions).items():
            regime_accuracy_parts.setdefault(regime, []).append(value)

        rows.append(
            {
                "fold": fold,
                "train_start": train.index.min(),
                "train_end": train.index.max(),
                "test_start": test.index.min(),
                "test_end": test.index.max(),
                "train_accuracy": train_accuracy,
                "test_accuracy": test_accuracy,
                "precision": precision,
                "recall": recall,
                "profit_factor": profit_factor,
                "winrate": winrate,
                "best_regime": best_regime,
                "worst_regime": worst_regime,
            }
        )
        train_accuracies.append(train_accuracy)
        accuracies.append(test_accuracy)
        precisions.append(precision)
        recalls.append(recall)
        profit_factors.append(profit_factor if np.isfinite(profit_factor) else 0.0)
        winrates.append(winrate)
        fold_returns.append(float(pd.to_numeric(test.get("pnl_percent", 0), errors="coerce").fillna(0.0).sum()))
        fold += 1

    _write_results(rows, output_path)
    _plot_equity(fold_returns, charts["walkforward_equity_curve"])
    _plot_line(accuracies, charts["rolling_accuracy"], "Rolling Accuracy", "Accuracy")
    _plot_line(winrates, charts["rolling_winrate"], "Rolling Winrate", "Winrate (%)")

    train_gap = max(0.0, float(np.mean(train_accuracies) - np.mean(accuracies))) if accuracies else 0.0
    overfit_risk_score = min(100.0, train_gap * 100)
    stability_score = max(0.0, 100.0 - (float(np.std(accuracies)) * 100 if accuracies else 100.0))
    regime_specific_accuracy = {
        regime: float(np.mean(values)) for regime, values in regime_accuracy_parts.items()
    }
    best_regime = max(regime_specific_accuracy, key=regime_specific_accuracy.get, default="-")
    worst_regime = min(regime_specific_accuracy, key=regime_specific_accuracy.get, default="-")

    return {
        "rows": int(len(dataset)),
        "folds": len(rows),
        "average_accuracy": float(np.mean(accuracies)) if accuracies else 0.0,
        "average_precision": float(np.mean(precisions)) if precisions else 0.0,
        "average_recall": float(np.mean(recalls)) if recalls else 0.0,
        "average_profit_factor": float(np.mean(profit_factors)) if profit_factors else 0.0,
        "average_winrate": float(np.mean(winrates)) if winrates else 0.0,
        "regime_specific_accuracy": regime_specific_accuracy,
        "model_stability_score": stability_score,
        "overfit_risk_score": overfit_risk_score,
        "model_health": _health(stability_score, overfit_risk_score),
        "best_regime": best_regime,
        "worst_regime": worst_regime,
        "charts": charts,
        "notes": ["Train accuracy is far above test accuracy. Overfit warning."] if overfit_risk_score >= 30 else [],
    }
