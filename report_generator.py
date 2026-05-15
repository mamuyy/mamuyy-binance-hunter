from html import escape
from typing import Any, Dict

from analytics import calculate_performance_metrics, generate_charts


def _format_number(value: Any, suffix: str = "", decimals: int = 2) -> str:
    if value == float("inf"):
        return "∞"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return f"0.{'0' * decimals}{suffix}"


def _summary_card(title: str, value: str) -> str:
    return f"""
    <div class="card">
      <span>{escape(title)}</span>
      <strong>{escape(value)}</strong>
    </div>
    """


def _dict_table(title: str, data: Dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in data.items()
    )
    return f"""
    <section>
      <h2>{escape(title)}</h2>
      <table><tbody>{rows}</tbody></table>
    </section>
    """


def _regime_table(metrics: Dict[str, Any]) -> str:
    rows = []
    for regime, data in metrics["regime_performance"].items():
        rows.append(
            "<tr>"
            f"<td>{escape(regime)}</td>"
            f"<td>{data['trades']}</td>"
            f"<td>{_format_number(data['winrate'], '%')}</td>"
            f"<td>{_format_number(data['avg_pnl'], '%')}</td>"
            f"<td>{_format_number(data['total_pnl'], '%')}</td>"
            "</tr>"
        )
    return f"""
    <section>
      <h2>Regime Analysis</h2>
      <table>
        <thead><tr><th>Regime</th><th>Trades</th><th>Winrate</th><th>Avg PnL</th><th>Total PnL</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _coin_table(title: str, metrics: Dict[str, Any], ascending: bool) -> str:
    coins = metrics["coin_performance"]
    if coins.empty:
        body = "<tr><td colspan='4'>No trades yet</td></tr>"
    else:
        ordered = coins.sort_values("total_pnl", ascending=ascending).head(10)
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.symbol))}</td>"
            f"<td>{int(row.trades)}</td>"
            f"<td>{_format_number(row.avg_pnl, '%')}</td>"
            f"<td>{_format_number(row.total_pnl, '%')}</td>"
            "</tr>"
            for row in ordered.itertuples()
        )

    return f"""
    <section>
      <h2>{escape(title)}</h2>
      <table>
        <thead><tr><th>Coin</th><th>Trades</th><th>Avg PnL</th><th>Total PnL</th></tr></thead>
        <tbody>{body}</tbody>
      </table>
    </section>
    """


def _latest_signals_table(metrics: Dict[str, Any]) -> str:
    latest = metrics["latest_signals"]
    if latest.empty:
        body = "<tr><td colspan='7'>No paper trades yet</td></tr>"
    else:
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.timestamp))}</td>"
            f"<td>{escape(str(row.symbol))}</td>"
            f"<td>{escape(str(row.status))}</td>"
            f"<td>{_format_number(row.pnl_percent, '%')}</td>"
            f"<td>{escape(str(row.score))}</td>"
            f"<td>{escape(str(row.regime_name))}</td>"
            f"<td>{escape(str(row.current_price))}</td>"
            "</tr>"
            for row in latest.itertuples()
        )

    return f"""
    <section>
      <h2>Latest Signals</h2>
      <table>
        <thead><tr><th>Time</th><th>Symbol</th><th>Status</th><th>PnL</th><th>Score</th><th>Regime</th><th>Price</th></tr></thead>
        <tbody>{body}</tbody>
      </table>
    </section>
    """


def generate_performance_report(
    paper_trades_path: str = "paper_trades.csv",
    equity_curve_path: str = "equity_curve.csv",
    output_path: str = "performance_report.html",
    chart_dir: str = "charts",
) -> Dict[str, Any]:
    metrics = calculate_performance_metrics(
        paper_trades_path=paper_trades_path,
        equity_curve_path=equity_curve_path,
    )
    charts = generate_charts(metrics, output_dir=chart_dir)

    warning = ""
    if metrics["unhealthy"]:
        warning = (
            "<div class='warning'><strong>⚠️ STRATEGY UNHEALTHY</strong><br>"
            + escape(", ".join(metrics["unhealthy_reasons"]))
            + "</div>"
        )

    summary_cards = "\n".join(
        [
            _summary_card("Total Trades", str(metrics["total_trades"])),
            _summary_card("Winrate", _format_number(metrics["winrate"], "%")),
            _summary_card("Loss Rate", _format_number(metrics["loss_rate"], "%")),
            _summary_card("Average PnL", _format_number(metrics["average_pnl"], "%")),
            _summary_card("Profit Factor", _format_number(metrics["profit_factor"])),
            _summary_card("Max Drawdown", _format_number(metrics["max_drawdown"], "%")),
            _summary_card("Risk Reward", _format_number(metrics["risk_reward_ratio"])),
            _summary_card("Sharpe", _format_number(metrics["sharpe_ratio"])),
        ]
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MAMUYY Performance Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #18212f; background: #f5f7fb; }}
    header {{ background: #101827; color: white; padding: 28px 32px; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    section {{ margin: 24px 0; background: white; border: 1px solid #dce3ee; border-radius: 8px; padding: 18px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 18px; }}
    .card {{ background: white; border: 1px solid #dce3ee; border-radius: 8px; padding: 14px; }}
    .card span {{ display: block; color: #5a6678; font-size: 13px; margin-bottom: 8px; }}
    .card strong {{ font-size: 22px; }}
    .charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
    .charts img {{ width: 100%; border: 1px solid #dce3ee; border-radius: 8px; background: white; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e6ebf2; text-align: left; font-size: 14px; }}
    th {{ color: #445066; background: #f7f9fc; }}
    .warning {{ margin-top: 18px; padding: 14px; border-radius: 8px; background: #fff4d8; border: 1px solid #f0c15a; }}
  </style>
</head>
<body>
  <header>
    <h1>MAMUYY Binance Hunter Performance Report</h1>
    <p>Paper trading analytics generated from paper_trades.csv</p>
  </header>
  <main>
    {warning}
    <div class="cards">{summary_cards}</div>
    <section>
      <h2>Charts</h2>
      <div class="charts">
        <img src="{escape(charts['equity_curve'])}" alt="Equity Curve">
        <img src="{escape(charts['win_loss_distribution'])}" alt="Win Loss Distribution">
      </div>
    </section>
    {_dict_table("Monthly Return", metrics["monthly_return"])}
    {_regime_table(metrics)}
    {_coin_table("Top Performing Coins", metrics, ascending=False)}
    {_coin_table("Worst Performing Coins", metrics, ascending=True)}
    {_latest_signals_table(metrics)}
  </main>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as report_file:
        report_file.write(html)

    metrics["charts"] = charts
    metrics["report_path"] = output_path
    return metrics
