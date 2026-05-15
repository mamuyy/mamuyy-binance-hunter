import os
from dataclasses import dataclass
from typing import Any, Dict, List

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".matplotlib"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from ml_engine import PROFITABLE_LABELS, build_ml_dataset


@dataclass
class BaseRegimeModel:
    name: str
    expected_behavior: str
    weights: Dict[str, float]

    def score_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        score = float(signal.get("score", 0) or 0)
        adjustment = 0.0
        for feature, weight in self.weights.items():
            adjustment += self._feature_value(signal, feature) * weight
        confidence = max(0, min(100, score + adjustment))
        ranked = dict(signal)
        ranked["regime_model"] = self.name
        ranked["regime_model_adjustment"] = round(adjustment, 2)
        ranked["adaptive_confidence_score"] = round(confidence, 2)
        ranked["model_confidence"] = round(confidence, 2)
        ranked["expected_behavior"] = self.expected_behavior
        ranked["score"] = int(max(0, min(100, confidence)))
        return ranked

    def predict_probability(self, signal: Dict[str, Any]) -> float:
        return self.score_signal(signal)["adaptive_confidence_score"] / 100

    @staticmethod
    def _feature_value(signal: Dict[str, Any], feature: str) -> float:
        value = signal.get(feature, 0)
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, str):
            text = value.upper()
            if text in {"TRUE", "YES"}:
                return 1.0
            if text in {"FALSE", "NO", ""}:
                return 0.0
            if feature == "whale_activity" and "ACCUMULATION" in text:
                return 1.0
            if feature == "whale_activity" and "DISTRIBUTION" in text:
                return -1.0
            if feature == "funding_warning" and "CROWDED" in text:
                return -1.0
            return 0.0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if feature in {"oi_expansion_rate", "squeeze_probability", "pressure_score", "regime_score"}:
            return numeric / 100
        if feature in {"funding_zscore", "taker_delta"}:
            return max(-1.0, min(1.0, numeric))
        return numeric


class MomentumModel(BaseRegimeModel):
    def __init__(self) -> None:
        super().__init__(
            name="MomentumModel",
            expected_behavior="Trend continuation, breakout follow-through, OI expansion.",
            weights={
                "breakout": 12,
                "oi_expansion_rate": 10,
                "whale_activity": 10,
                "pressure_score": 8,
                "volume_spike": 3,
                "funding_warning": -8,
                "squeeze_probability": -6,
            },
        )


class MeanReversionModel(BaseRegimeModel):
    def __init__(self) -> None:
        super().__init__(
            name="MeanReversionModel",
            expected_behavior="Reclaim after sweep, exhaustion fade, failed breakout filter.",
            weights={
                "liquidity_sweep": 14,
                "funding_zscore": -6,
                "squeeze_probability": 6,
                "breakout": -8,
                "pressure_score": -3,
                "whale_activity": 5,
            },
        )


class PanicRecoveryModel(BaseRegimeModel):
    def __init__(self) -> None:
        super().__init__(
            name="PanicRecoveryModel",
            expected_behavior="Capitulation recovery, squeeze reversal, aggressive reclaim only.",
            weights={
                "liquidity_sweep": 16,
                "squeeze_probability": 12,
                "taker_delta": 10,
                "pressure_score": 6,
                "whale_activity": 8,
                "breakout": -10,
                "funding_warning": 4,
            },
        )


def model_selector(regime_name: str | None) -> BaseRegimeModel:
    regime = (regime_name or "").upper()
    if "TRENDING BULL" in regime:
        return MomentumModel()
    if "SIDEWAYS" in regime or "CHOPPY" in regime:
        return MeanReversionModel()
    if "PANIC" in regime or "RISK OFF" in regime:
        return PanicRecoveryModel()
    return MomentumModel()


def apply_regime_model_to_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    model = model_selector(signal.get("regime_name"))
    return model.score_signal(signal)


def _placeholder(path: str, title: str) -> None:
    plt.figure(figsize=(8, 4))
    plt.title(title)
    plt.text(0.5, 0.5, "Not enough data", ha="center", va="center")
    plt.xticks([])
    plt.yticks([])
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _plot_comparison(summary: pd.DataFrame, path: str) -> None:
    if summary.empty:
        _placeholder(path, "Regime Model Comparison")
        return
    plt.figure(figsize=(9, 4))
    plt.bar(summary["model"], summary["profitability"])
    plt.title("Regime Model Profitability")
    plt.ylabel("Average PnL (%)")
    plt.xticks(rotation=20, ha="right")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def _plot_heatmap(summary: pd.DataFrame, path: str) -> None:
    if summary.empty:
        _placeholder(path, "Regime Accuracy Heatmap")
        return
    pivot = summary.pivot_table(index="model", values="accuracy", aggfunc="mean")
    plt.figure(figsize=(6, 4))
    plt.imshow(pivot.values, cmap="Greens", vmin=0, vmax=1)
    plt.colorbar(label="Accuracy")
    plt.yticks(range(len(pivot.index)), pivot.index)
    plt.xticks([0], ["Accuracy"])
    plt.title("Regime Model Accuracy")
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()


def analyze_regime_models(
    paper_trades_path: str = "paper_trades.csv",
    signals_log_path: str = "signals_log.csv",
    flow_log_path: str = "flow_log.csv",
    chart_dir: str = "charts",
) -> Dict[str, Any]:
    os.makedirs(chart_dir, exist_ok=True)
    comparison_chart = os.path.join(chart_dir, "regime_model_comparison.png")
    heatmap_chart = os.path.join(chart_dir, "regime_accuracy_heatmap.png")
    dataset = build_ml_dataset(paper_trades_path, signals_log_path, flow_log_path)
    if dataset.empty:
        _plot_comparison(pd.DataFrame(), comparison_chart)
        _plot_heatmap(pd.DataFrame(), heatmap_chart)
        return {
            "current_regime": "UNKNOWN",
            "selected_model": model_selector("UNKNOWN").name,
            "model_confidence": 0,
            "expected_behavior": model_selector("UNKNOWN").expected_behavior,
            "regime_model_accuracy": {},
            "regime_model_profitability": {},
            "regime_model_winrate": {},
            "charts": {"comparison": comparison_chart, "heatmap": heatmap_chart},
            "notes": ["Not enough data for regime-specific model analysis."],
        }

    rows: List[Dict[str, Any]] = []
    for _, row in dataset.iterrows():
        data = row.to_dict()
        model = model_selector(data.get("regime_name"))
        probability = model.predict_probability(data)
        predicted_profitable = probability >= 0.55
        actual_profitable = data.get("target") in PROFITABLE_LABELS
        rows.append(
            {
                "model": model.name,
                "regime": data.get("regime_name", "UNKNOWN"),
                "accuracy": int(predicted_profitable == actual_profitable),
                "profitability": float(data.get("pnl_percent", 0) or 0),
                "win": int(actual_profitable),
            }
        )
    result_df = pd.DataFrame(rows)
    summary = (
        result_df.groupby("model")
        .agg(accuracy=("accuracy", "mean"), profitability=("profitability", "mean"), winrate=("win", "mean"))
        .reset_index()
    )
    _plot_comparison(summary, comparison_chart)
    _plot_heatmap(summary, heatmap_chart)
    latest = dataset.iloc[-1].to_dict()
    selected = model_selector(latest.get("regime_name"))
    confidence = selected.predict_probability(latest) * 100
    return {
        "current_regime": latest.get("regime_name", "UNKNOWN"),
        "selected_model": selected.name,
        "model_confidence": round(confidence, 2),
        "expected_behavior": selected.expected_behavior,
        "adaptive_confidence_score": round(confidence, 2),
        "regime_model_accuracy": dict(zip(summary["model"], summary["accuracy"])),
        "regime_model_profitability": dict(zip(summary["model"], summary["profitability"])),
        "regime_model_winrate": dict(zip(summary["model"], summary["winrate"] * 100)),
        "charts": {"comparison": comparison_chart, "heatmap": heatmap_chart},
        "notes": [],
    }
