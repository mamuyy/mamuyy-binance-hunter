from typing import Dict, Any

import requests


def format_signal_message(signal: Dict[str, Any]) -> str:
    funding_percent = signal.get("funding", 0.0) * 100
    message = (
        "🚨 MAMUYY BINANCE HUNTER V1\n\n"
        "🌎 MARKET REGIME\n"
        f"Current Mode: {signal.get('regime_name', 'UNKNOWN')}\n"
        f"Confidence: {signal.get('regime_score', 0)}%\n\n"
        f"🔥 {signal.get('symbol')}\n"
        f"Score: {signal.get('score')}/100\n"
        f"Price: {signal.get('price')}\n"
        f"Volume Spike: {signal.get('volume_spike'):.2f}\n"
        f"Breakout: {signal.get('breakout')}\n"
        f"Liquidity Sweep: {signal.get('liquidity_sweep')}\n"
        f"Taker Buy Ratio: {signal.get('taker_buy_ratio'):.2f}\n"
        f"Funding: {funding_percent:.4f}%\n"
        f"Open Interest: {signal.get('open_interest')}"
    )
    if signal.get("flow_state"):
        message += "\n\n" + format_flow_alert_message(signal)
    return message


def format_flow_alert_message(signal: Dict[str, Any]) -> str:
    funding_zscore = signal.get("funding_zscore") or 0.0
    oi_expansion_rate = signal.get("oi_expansion_rate") or 0.0
    pressure_score = signal.get("pressure_score") or 0.0
    squeeze_probability = signal.get("squeeze_probability") or 0.0
    funding_warning = signal.get("funding_warning") or "-"

    return (
        "🚨 FLOW ALERT\n\n"
        f"Coin: {signal.get('symbol')}\n"
        f"Pressure: {pressure_score:.2f}/100\n"
        f"Funding: z={funding_zscore:.2f} ({funding_warning})\n"
        f"OI Expansion: {oi_expansion_rate:.2f}%\n"
        f"Whale Activity: {signal.get('whale_activity', '-')}\n"
        f"Squeeze Risk: {signal.get('squeeze_risk', '-')} "
        f"({squeeze_probability:.2f}%)\n"
        f"Final Score: {signal.get('score')}/100"
    )


def format_market_regime_message(regime: Dict[str, Any]) -> str:
    return (
        "🌎 MARKET REGIME\n"
        f"Current Mode: {regime.get('regime_name', 'UNKNOWN')}\n"
        f"Confidence: {regime.get('regime_score', 0)}%"
    )


def format_paper_summary_message(summary: Dict[str, Any]) -> str:
    return (
        "📊 PAPER TRADING SUMMARY\n\n"
        f"Total Trade: {summary.get('total_trade', 0)}\n"
        f"Win: {summary.get('win', 0)}\n"
        f"Loss: {summary.get('loss', 0)}\n"
        f"Winrate: {summary.get('winrate', 0.0):.2f}%\n"
        f"Average PnL: {summary.get('average_pnl', 0.0):.2f}%\n"
        f"Best Coin: {summary.get('best_coin', '-')}\n"
        f"Worst Coin: {summary.get('worst_coin', '-')}"
    )


def format_performance_report_message(metrics: Dict[str, Any]) -> str:
    profit_factor = metrics.get("profit_factor", 0.0)
    if profit_factor == float("inf"):
        profit_factor_text = "∞"
    else:
        profit_factor_text = f"{profit_factor:.2f}"

    message = (
        "📊 PERFORMANCE REPORT\n\n"
        f"Winrate: {metrics.get('winrate', 0.0):.2f}%\n"
        f"Profit Factor: {profit_factor_text}\n"
        f"Max DD: {metrics.get('max_drawdown', 0.0):.2f}%\n"
        f"Best Regime: {metrics.get('best_regime', '-')}\n"
        f"Worst Regime: {metrics.get('worst_regime', '-')}"
    )

    if metrics.get("unhealthy"):
        message += "\n\n⚠️ STRATEGY UNHEALTHY"

    return message


def format_ml_analysis_message(result: Dict[str, Any]) -> str:
    top_features = result.get("feature_importance", [])[:3]
    feature_lines = []
    for index in range(3):
        if index < len(top_features):
            feature_lines.append(f"{index + 1}. {top_features[index].get('feature')}")
        else:
            feature_lines.append(f"{index + 1}. -")

    return (
        "🧠 ML ANALYSIS\n\n"
        "Top Features:\n"
        f"{feature_lines[0]}\n"
        f"{feature_lines[1]}\n"
        f"{feature_lines[2]}\n\n"
        f"Most Profitable Regime: {result.get('most_profitable_regime', '-')}\n"
        f"Worst Regime: {result.get('worst_regime', '-')}\n\n"
        f"Current Model Accuracy: {result.get('accuracy', 0.0):.2%}\n"
        f"AI Confidence: {result.get('ai_confidence_score', 0)}/100\n"
        f"Setup Ranking: {result.get('setup_ranking', 'LOW QUALITY')}"
    )


def format_walkforward_report_message(result: Dict[str, Any]) -> str:
    return (
        "🧪 WALK FORWARD REPORT\n\n"
        f"Model Health: {result.get('model_health', 'UNSTABLE')}\n"
        f"Overfit Risk: {result.get('overfit_risk_score', 0.0):.2f}/100\n"
        f"Rolling Accuracy: {result.get('average_accuracy', 0.0):.2%}\n"
        f"Rolling Winrate: {result.get('average_winrate', 0.0):.2f}%\n"
        f"Best Regime: {result.get('best_regime', '-')}\n"
        f"Worst Regime: {result.get('worst_regime', '-')}"
    )


def format_regime_model_message(result: Dict[str, Any]) -> str:
    return (
        "🧠 REGIME MODEL\n\n"
        f"Current Regime: {result.get('current_regime', 'UNKNOWN')}\n"
        f"Selected Model: {result.get('selected_model', '-')}\n"
        f"Model Confidence: {result.get('model_confidence', 0):.2f}%\n"
        f"Expected Behavior: {result.get('expected_behavior', '-')}"
    )


def format_portfolio_message(result: Dict[str, Any]) -> str:
    allocation = result.get("recommended_allocation", {})
    if allocation:
        top = sorted(allocation.items(), key=lambda item: item[1], reverse=True)[:5]
        allocation_text = ", ".join(f"{symbol}: {weight:.2f}%" for symbol, weight in top)
    else:
        allocation_text = "-"
    return (
        "📦 PORTFOLIO ENGINE\n\n"
        f"Portfolio Health: {result.get('portfolio_health', 'YELLOW')} "
        f"({result.get('portfolio_health_score', 0)}/100)\n"
        f"Risk Score: {result.get('portfolio_risk_score', 0)}/100\n"
        f"Diversification: {result.get('diversification_score', 0)}/100\n"
        f"Largest Exposure: {result.get('largest_exposure', '-')}\n"
        f"Recommended Allocation: {allocation_text}"
    )


def format_execution_message(result: Dict[str, Any]) -> str:
    return (
        "⚡ EXECUTION ENGINE\n\n"
        f"Execution Profile: {result.get('execution_profile', 'NORMAL')}\n"
        f"Expected Slippage: {result.get('expected_slippage', 0)}%\n"
        f"Fill Probability: {result.get('fill_probability', 0)}%\n"
        f"Execution Quality: {result.get('execution_quality', 0)}/100\n"
        f"Adjusted PnL Impact: {result.get('adjusted_pnl_impact', 0)}%"
    )


def format_shadow_message(result: Dict[str, Any]) -> str:
    return (
        "👻 SHADOW LIVE ENGINE\n\n"
        f"Live PnL (Rolling Active): {result.get('rolling_live_pnl_pct', result.get('live_pnl', 0))}%\n"
        f"Cumulative Shadow PnL: {result.get('cumulative_shadow_pnl_pct', 0)}%\n"
        f"Live Winrate: {result.get('live_winrate', 0)}%\n"
        f"Execution Drift: {result.get('execution_drift', 0)}%\n"
        f"Current Regime: {result.get('current_regime', 'UNKNOWN')}\n"
        f"Shadow Exposure (Rolling Active): {result.get('rolling_live_exposure_pct', result.get('live_exposure', 0))}%\n"
        f"Cumulative Shadow Exposure: {result.get('cumulative_shadow_exposure_pct', 0)}%\n"
        f"Health: {result.get('shadow_health', 'WARNING')}"
    )


def format_orchestrator_message(result: Dict[str, Any]) -> str:
    return (
        "🛠 ORCHESTRATOR\n\n"
        f"System Health: {result.get('system_health_score', 0)}/100\n"
        f"Running Engines: {', '.join(result.get('running_engines', [])) or '-'}\n"
        f"Failed Engines: {', '.join(result.get('failed_engines', [])) or '-'}\n"
        f"Recovery Actions: {', '.join(result.get('recovery_actions', []))}\n"
        f"Scheduler Mode: {result.get('scheduler_mode', 'NORMAL')}"
    )


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str,
    timeout: int = 15,
) -> bool:
    if not bot_token or not chat_id:
        print("Telegram token/chat_id belum diisi. Alert tidak dikirim.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, data=payload, timeout=timeout)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Gagal mengirim Telegram alert: {exc}")
        return False
