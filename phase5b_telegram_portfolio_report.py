#!/usr/bin/env python3
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "data/ml_portfolio_allocation_v2_20260610.csv"
OUT = ROOT / "logs/phase5b_telegram_portfolio_report_20260610.txt"

df = pd.read_csv(CSV)
top = df.sort_values("capital_pct_v2", ascending=False).head(10)

positive = df[df["capital_pct_v2"] > 0]
blocked = df[df["capital_pct_v2"] == 0]

lines = []
lines.append("📊 MAMUYY Hunter Portfolio Allocation V2")
lines.append("")
lines.append("🟢 Top Allocation:")
for i, row in enumerate(top.itertuples(), 1):
    lines.append(
        f"{i}. {row.symbol} — {row.capital_pct_v2:.2f}% "
        f"| EV {row.ev_pct:.4f} | WR {row.winrate:.2%}"
    )

lines.append("")
lines.append(f"✅ Positive Allocation Symbols: {len(positive)}")
lines.append(f"⛔ Blocked / 0% Allocation: {len(blocked)}")
lines.append("")
lines.append("🔴 Blocked Symbols:")
for s in blocked["symbol"].head(15).tolist():
    lines.append(f"- {s}")

lines.append("")
lines.append("Mode: PAPER_ONLY")
lines.append("Runtime Changed: NO")
lines.append("Verdict: PORTFOLIO_REPORT_READY")

text = "\n".join(lines)
OUT.write_text(text, encoding="utf-8")
print(text)
