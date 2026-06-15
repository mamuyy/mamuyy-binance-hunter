#!/usr/bin/env python3
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent

ALLOC = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
HEALTH = ROOT / "logs/phase5c_portfolio_health_dashboard_report_20260610.json"
REBALANCE = ROOT / "logs/phase5d_portfolio_rebalancing_engine_report_20260610.json"
OUT = ROOT / "logs/phase7a_telegram_portfolio_v2_report_20260611.txt"

alloc = pd.read_csv(ALLOC)
health = json.loads(HEALTH.read_text(encoding="utf-8"))
rebalance = json.loads(REBALANCE.read_text(encoding="utf-8"))

top = alloc.sort_values("capital_pct_v2", ascending=False).head(10)

lines = []
lines.append("📦 PORTFOLIO ENGINE V2")
lines.append("")
lines.append(f"Portfolio Health: {health.get('portfolio_health', 'UNKNOWN')}")
lines.append(f"Risk Score: {health.get('risk_score', 0)}/100")
lines.append(f"Diversification: {health.get('diversification_score', 0)}/100")
lines.append(f"Largest Exposure: {health.get('largest_exposure_symbol', '-')}")
lines.append("")
lines.append("🟢 Top Allocation:")
for i, row in enumerate(top.itertuples(), 1):
    lines.append(
        f"{i}. {row.symbol} — {row.capital_pct_v2:.2f}% "
        f"| EV {row.ev_pct:.4f} | WR {row.winrate:.2%}"
    )

lines.append("")
lines.append("🔄 Rebalancing:")
lines.append("BUY MORE:")
for item in rebalance.get("buy_more", [])[:5]:
    lines.append(f"- {item['symbol']} {item['capital_pct_v2']:.2f}%")

lines.append("")
lines.append("REDUCE:")
for item in rebalance.get("reduce", [])[:5]:
    lines.append(f"- {item['symbol']} {item['capital_pct_v2']:.2f}%")

lines.append("")
lines.append("REMOVE:")
for item in rebalance.get("remove", [])[:10]:
    lines.append(f"- {item['symbol']}")

lines.append("")
lines.append("Mode: V2_ADVISORY_ONLY")
lines.append("Runtime V1 Changed: NO")
lines.append("Broker Routing: NO")
lines.append("Use Case: Manual Trading Watchlist")

text = "\n".join(lines)
OUT.write_text(text, encoding="utf-8")
print(text)
