import sqlite3, json
from collections import defaultdict
from datetime import datetime, timezone

con = sqlite3.connect('mamuyy_hunter.db')
cur = con.cursor()

rows = cur.execute("""
    SELECT timestamp, symbol, pnl, confidence, regime, exit_reason
    FROM internal_paper_trades
    WHERE status = 'CLOSED'
    ORDER BY timestamp ASC
""").fetchall()
con.close()

trades = []
for r in rows:
    pnl = float(r[2]) if r[2] is not None else 0.0
    trades.append({
        "ts": r[0], "sym": r[1], "pnl": pnl,
        "conf": r[3], "regime": r[4] or "UNKNOWN",
        "exit_reason": r[5] or ""
    })

n = len(trades)

def stats(chunk):
    if not chunk:
        return {"n": 0, "wr": 0, "exp": 0, "pf": 0, "wins": 0, "losses": 0,
                "gross_profit": 0, "gross_loss": 0, "cum_pnl": 0, "max_loss": 0, "max_win": 0}
    wins = [t for t in chunk if t['pnl'] > 0]
    losses = [t for t in chunk if t['pnl'] < 0]
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gp / gl if gl > 0 else float('inf')
    return {
        "n": len(chunk), "wr": len(wins)/len(chunk),
        "exp": sum(t['pnl'] for t in chunk)/len(chunk),
        "pf": pf, "wins": len(wins), "losses": len(losses),
        "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "cum_pnl": round(sum(t['pnl'] for t in chunk), 2),
        "max_loss": round(min(t['pnl'] for t in chunk), 2),
        "max_win": round(max(t['pnl'] for t in chunk), 2)
    }

# Per-symbol breakdown
sym_trades = defaultdict(list)
for t in trades:
    sym_trades[t['sym']].append(t)

total_pnl = sum(t['pnl'] for t in trades)

print("=== SYMBOL CONCENTRATION (n>=5, sorted by cum_pnl) ===")
sym_stats = []
for sym, chunk in sym_trades.items():
    s = stats(chunk)
    s['symbol'] = sym
    s['pnl_share_pct'] = round(s['cum_pnl'] / total_pnl * 100, 1) if total_pnl != 0 else 0
    sym_stats.append(s)

sym_stats.sort(key=lambda x: x['cum_pnl'], reverse=True)

print(f"{'Symbol':20s} {'n':>4} {'WR':>6} {'Exp':>7} {'PF':>5} {'CumPnL':>9} {'Share%':>7} {'MaxLoss':>9} {'MaxWin':>9}")
print("-"*85)
for s in sym_stats:
    if s['n'] < 5:
        continue
    pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
    print(f"{s['symbol']:20s} {s['n']:>4} {s['wr']:>5.1%} {s['exp']:>7.2f} {pf_str:>5} {s['cum_pnl']:>9.2f} {s['pnl_share_pct']:>6.1f}% {s['max_loss']:>9.2f} {s['max_win']:>9.2f}")

# Flag outlier symbols (negative cum_pnl or max_loss > -30)
print("\n=== OUTLIER SYMBOLS (cum_pnl < 0 OR max_loss < -30) ===")
outliers = [s for s in sym_stats if s['cum_pnl'] < 0 or s['max_loss'] < -30]
for s in outliers:
    pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
    print(f"  {s['symbol']}: n={s['n']} CumPnL={s['cum_pnl']} MaxLoss={s['max_loss']} MaxWin={s['max_win']} WR={s['wr']:.1%}")

# Impact if outliers removed
outlier_syms = {s['symbol'] for s in outliers}
clean_trades = [t for t in trades if t['sym'] not in outlier_syms]
all_stats = stats(trades)
clean_stats = stats(clean_trades)

print(f"\n=== IMPACT ANALYSIS ===")
print(f"ALL trades:        n={all_stats['n']} WR={all_stats['wr']:.1%} Exp={all_stats['exp']:.2f} PF={all_stats['pf']:.2f} CumPnL={all_stats['cum_pnl']:.2f}")
print(f"Excl outliers:     n={clean_stats['n']} WR={clean_stats['wr']:.1%} Exp={clean_stats['exp']:.2f} PF={clean_stats['pf']:.2f} CumPnL={clean_stats['cum_pnl']:.2f}")
print(f"Outlier symbols:   {sorted(outlier_syms)}")

# Rolling-50 without outliers
print(f"\n=== ROLLING-50 (excl outlier symbols) ===")
nc = len(clean_trades)
for start in range(0, nc, 50):
    chunk = clean_trades[start:start+50]
    if len(chunk) < 10:
        break
    s = stats(chunk)
    pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "inf"
    d0 = chunk[0]['ts'][:10]
    d1 = chunk[-1]['ts'][:10]
    print(f"  [{start+1}-{start+len(chunk)}] {d0} to {d1}: WR={s['wr']:.1%} Exp={s['exp']:.2f} PF={pf_str}")

print(f"\n=== CONCENTRATION RISK SUMMARY ===")
top5_pnl = sum(s['cum_pnl'] for s in sym_stats[:5])
print(f"Top 5 symbols by PnL contribute: {top5_pnl:.2f} / {total_pnl:.2f} ({top5_pnl/total_pnl*100:.1f}% of total)")
outlier_drag = sum(s['cum_pnl'] for s in outliers)
print(f"Outlier symbols total PnL drag:  {outlier_drag:.2f} ({outlier_drag/total_pnl*100:.1f}% of total)")
