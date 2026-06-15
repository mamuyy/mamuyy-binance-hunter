#!/usr/bin/env python3
"""Read-only alpha validation report for MAMUYY Hunter paper trades.

Safety: opens SQLite with mode=ro and executes SELECT/PRAGMA metadata only.
"""
from __future__ import annotations

import argparse, json, math, os, random, sqlite3, statistics
from collections import Counter, defaultdict
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

DB_DEFAULT = "mamuyy_hunter.db"
PRIMARY_TABLE = "internal_paper_trades"
SECONDARY_TABLE = "shadow_trades"
SEED = 20260615
BOOTSTRAPS = 5000
LOW_SAMPLE_N = 20


def pct(x):
    return None if x is None else round(100*x, 4)

def fnum(x):
    try:
        if x is None or x == "": return None
        return float(x)
    except (TypeError, ValueError):
        return None

def is_closed(v):
    return str(v or "").strip().lower() in {"closed","close","completed","complete","done","exited","exit","settled"}

def parse_dt(v):
    if v is None or v == "": return None
    if isinstance(v, (int,float)):
        try:
            return datetime.fromtimestamp(v/1000 if v > 10_000_000_000 else v)
        except Exception: return None
    s=str(v).strip().replace('Z','+00:00')
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except Exception: pass
    return None

def norm(s): return ''.join(ch for ch in s.lower() if ch.isalnum())

def pick(cols, names):
    nmap={norm(c):c for c in cols}
    for name in names:
        if norm(name) in nmap: return nmap[norm(name)]
    for c in cols:
        nc=norm(c)
        if any(norm(name) in nc for name in names): return c
    return None

def table_exists(con, table):
    return con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

def get_cols(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]

def fetch_rows(con, table):
    cur=con.execute(f'SELECT * FROM "{table}"')
    cols=[d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()], cols

def max_drawdown(pnls):
    equity=peak=0.0; mdd=0.0; curve=[]
    for p in pnls:
        equity += p; peak=max(peak,equity); dd=peak-equity; mdd=max(mdd,dd); curve.append(equity)
    return mdd, curve

def streaks(pnls):
    maxw=maxl=curw=curl=0
    for p in pnls:
        if p>0: curw+=1; curl=0
        elif p<0: curl+=1; curw=0
        else: curw=curl=0
        maxw=max(maxw,curw); maxl=max(maxl,curl)
    return maxw,maxl

def core_metrics(trades):
    pnls=[t['pnl'] for t in trades if t.get('pnl') is not None]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]; be=[p for p in pnls if p==0]
    gp=sum(wins); gl=sum(losses); mdd, curve=max_drawdown(pnls); sw,sl=streaks(pnls)
    return {
        'sample_count': len(pnls), 'wins': len(wins), 'losses': len(losses), 'breakeven': len(be),
        'win_rate': (len(wins)/len(pnls) if pnls else None),
        'average_win': (statistics.mean(wins) if wins else None),
        'average_loss': (statistics.mean(losses) if losses else None),
        'payoff_ratio': ((statistics.mean(wins)/abs(statistics.mean(losses))) if wins and losses else None),
        'expectancy_per_trade': (statistics.mean(pnls) if pnls else None),
        'gross_profit': gp, 'gross_loss': gl,
        'profit_factor': (gp/abs(gl) if gl<0 else (math.inf if gp>0 else None)),
        'cumulative_pnl': sum(pnls), 'maximum_drawdown': mdd,
        'longest_winning_streak': sw, 'longest_losing_streak': sl,
    }

def summarize_groups(trades, col):
    groups=defaultdict(list)
    for t in trades: groups[str(t['row'].get(col, 'UNKNOWN') or 'UNKNOWN')].append(t)
    return {k: {**core_metrics(v), 'sample_flag': 'LOW_SAMPLE' if len(v)<LOW_SAMPLE_N else 'OK'} for k,v in sorted(groups.items())}

def bucket_hold(v):
    x=fnum(v)
    if x is None: return 'UNKNOWN'
    if x<=5: return '<=5'
    if x<=15: return '6-15'
    if x<=50: return '16-50'
    return '>50'

def rolling(trades, n=50):
    out=[]
    for i in range(0, max(0, len(trades)-n+1)):
        m=core_metrics(trades[i:i+n])
        out.append({'start_index':i+1,'end_index':i+n,'expectancy':m['expectancy_per_trade'],'profit_factor':m['profit_factor'],'win_rate':m['win_rate']})
    return out

def bootstrap(pnls):
    rnd=random.Random(SEED)
    if not pnls: return {'seed':SEED,'samples':0,'expectancy_ci_95':None,'win_rate_ci_95':None,'expectancy_gt_zero_pct':None}
    exps=[]; wr=[]
    n=len(pnls)
    for _ in range(BOOTSTRAPS):
        s=[pnls[rnd.randrange(n)] for _ in range(n)]
        exps.append(statistics.mean(s)); wr.append(sum(1 for x in s if x>0)/n)
    exps.sort(); wr.sort(); lo=int(.025*BOOTSTRAPS); hi=int(.975*BOOTSTRAPS)-1
    return {'seed':SEED,'samples':BOOTSTRAPS,'expectancy_ci_95':[exps[lo],exps[hi]],'win_rate_ci_95':[wr[lo],wr[hi]],'expectancy_gt_zero_pct':100*sum(1 for x in exps if x>0)/BOOTSTRAPS}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db', default=DB_DEFAULT); args=ap.parse_args()
    Path('logs').mkdir(exist_ok=True)
    report={'mode':'PAPER_ONLY READ_ONLY NO_BROKER_API NO_REAL_CAPITAL NO_RUNTIME_MODIFICATION','database':args.db,'primary_table':PRIMARY_TABLE,'generated_at_utc':datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00','Z')}
    if not Path(args.db).exists():
        report['critical_data_quality_failure']='database_not_found'; trades=[]; cols=[]; rows=[]
    else:
        con=sqlite3.connect(f'file:{Path(args.db).resolve()}?mode=ro', uri=True)
        if not table_exists(con, PRIMARY_TABLE):
            report['critical_data_quality_failure']='primary_table_not_found'; trades=[]; cols=[]; rows=[]
        else:
            rows, cols=fetch_rows(con, PRIMARY_TABLE)
            status_col=pick(cols, ['status','trade_status','state']); pnl_col=pick(cols, ['pnl','realized_pnl','net_pnl','profit_loss','profit','pnl_usd']); symbol_col=pick(cols,['symbol','ticker','pair'])
            time_col=pick(cols,['closed_at','exit_time','exit_timestamp','timestamp','created_at','opened_at','entry_time','time','date'])
            id_col=pick(cols,['id','trade_id','uuid'])
            closed=[r for r in rows if (is_closed(r.get(status_col)) if status_col else True)]
            invalid_status=0 if not status_col else sum(1 for r in rows if r not in closed and str(r.get(status_col) or '').strip())
            trades=[]
            for idx,r in enumerate(closed):
                dt=parse_dt(r.get(time_col)) if time_col else None
                trades.append({'row':r,'pnl':fnum(r.get(pnl_col)) if pnl_col else None,'dt':dt,'seq':idx})
            trades.sort(key=lambda t: (t['dt'] or datetime.min, t['seq']))
            missing=sum(1 for t in trades if t['pnl'] is None)
            usable=[t for t in trades if t['pnl'] is not None]
            dup_keys=[tuple(r.get(c) for c in ([id_col] if id_col else [symbol_col,time_col,pnl_col] if symbol_col and time_col and pnl_col else cols)) for r in closed]
            duplicates=sum(v-1 for v in Counter(dup_keys).values() if v>1)
            report['data_quality']={'closed_trade_count':len(closed),'date_range':[min([t['dt'] for t in trades if t['dt']], default=None).isoformat() if any(t['dt'] for t in trades) else None, max([t['dt'] for t in trades if t['dt']], default=None).isoformat() if any(t['dt'] for t in trades) else None], 'symbols': sorted({str(t['row'].get(symbol_col)) for t in trades if symbol_col and t['row'].get(symbol_col)}), 'missing_pnl_count':missing,'duplicate_count':duplicates,'invalid_status_count':invalid_status,'rows_usable_for_calculation':len(usable),'detected_columns':{'status':status_col,'pnl':pnl_col,'symbol':symbol_col,'timestamp':time_col,'id':id_col},'detected_units':{'pnl':'database numeric units; treated as absolute paper PnL'}}
            report['core_performance']=core_metrics(usable)
            dims={'market_regime':['market_regime','regime'],'symbol':['symbol','ticker','pair'],'trade_quality_rank':['trade_quality_rank','quality_rank','rank'],'position_sizing_tier_or_multiplier':['position_sizing_tier','position_size_tier','size_multiplier','position_multiplier','multiplier'],'portfolio_allocation_bucket':['allocation_bucket','portfolio_bucket','allocation_tier'],'setup_strategy':['setup','strategy','strategy_name'],'lifecycle_holding_bucket':['holding_candles','holding_period','lifecycle_bucket'],'portfolio_eligible':['portfolio_eligible','eligible'],'suggested_risk_tier':['suggested_risk_tier','risk_tier'],'ml_confidence':['ml_confidence','confidence','model_confidence']}
            report['edge_segmentation']={}
            for name,cands in dims.items():
                col=pick(cols,cands)
                if not col: report['edge_segmentation'][name]={'available':False}; continue
                if name=='lifecycle_holding_bucket' and col not in ('lifecycle_bucket',):
                    for t in usable: t['row']['__holding_bucket']=bucket_hold(t['row'].get(col))
                    col='__holding_bucket'
                report['edge_segmentation'][name]={'available':True,'column':col,'groups':summarize_groups(usable,col)}
            rank_col=pick(cols,['trade_quality_rank','quality_rank','rank'])
            report['rank_validation']=summarize_groups(usable, rank_col) if rank_col else {'available':False}
            half=len(usable)//2; report['stability']={'first_half':core_metrics(usable[:half]),'second_half':core_metrics(usable[half:]),'earliest_100':core_metrics(usable[:100]),'latest_100':core_metrics(usable[-100:]),'rolling_50':rolling(usable,50)}
            r=report['stability']['rolling_50']; first=report['stability']['first_half']['expectancy_per_trade']; second=report['stability']['second_half']['expectancy_per_trade']
            if len(usable)<100 or first is None or second is None: label='INCONCLUSIVE'
            elif second > first*1.1: label='IMPROVING'
            elif second < first*0.9: label='DEGRADING'
            else: label='STABLE'
            report['stability']['assessment']=label
            report['uncertainty']=bootstrap([t['pnl'] for t in usable])
            report['edge_attribution']={'method':'comparison groups only; ASSOCIATION / ATTRIBUTION INDICATION, not proven causation','largest_edge_contributor':'INCONCLUSIVE'}
            m=report['core_performance']; ci=report['uncertainty'].get('expectancy_ci_95')
            critical=bool(report.get('critical_data_quality_failure'))
            verdict='INCONCLUSIVE'
            if len(usable)>=300 and (m['expectancy_per_trade'] or 0)>0 and (m['profit_factor'] or 0)>1 and m['maximum_drawdown']<=15 and not critical: verdict='ALPHA_POSITIVE'
            if (m['expectancy_per_trade'] is not None and m['expectancy_per_trade']<=0) or (m['profit_factor'] is not None and m['profit_factor']<=1): verdict='NEGATIVE_EDGE'
            if ci and ci[0] <= 0 <= ci[1] and verdict=='ALPHA_POSITIVE': verdict='INCONCLUSIVE'
            report['verdict']={'research_audit_verdict':verdict,'phase_3':'NOT UNLOCKED','real_trading':'LOCKED','note':'Not Phase 3 approval and not real-execution readiness.'}
            report['readiness_references']={'closed_trades_500':len(usable)>=500,'rolling_win_rate_ge_45': bool(r and r[-1]['win_rate'] is not None and r[-1]['win_rate']>=.45),'rolling_profit_factor_ge_1_3': bool(r and r[-1]['profit_factor'] is not None and r[-1]['profit_factor']>=1.3),'maximum_drawdown_le_15': m['maximum_drawdown']<=15}
            if table_exists(con, SECONDARY_TABLE): report['secondary_reference']={'shadow_trades_count':con.execute(f'SELECT COUNT(*) FROM "{SECONDARY_TABLE}"').fetchone()[0], 'note':'Not mixed into primary forward-paper result.'}
    Path('logs/alpha_validation_report.json').write_text(json.dumps(report, indent=2, default=str))
    md=['# MAMUYY HUNTER — Alpha Validation Report','', 'Safety: PAPER_ONLY / READ_ONLY / NO BROKER API / NO REAL CAPITAL.','', '```json', json.dumps(report, indent=2, default=str), '```','']
    Path('logs/alpha_validation_report.md').write_text('\n'.join(md))
    dq=report.get('data_quality',{}); cp=report.get('core_performance',{}); seg=report.get('edge_segmentation',{})
    def best(dim, reverse=True):
        g=seg.get(dim,{}).get('groups',{}) if isinstance(seg.get(dim),dict) else {}
        vals=[(k,v.get('expectancy_per_trade')) for k,v in g.items() if v.get('expectancy_per_trade') is not None]
        return sorted(vals, key=lambda x:x[1], reverse=reverse)[0][0] if vals else 'UNAVAILABLE'
    print('MAMUYY HUNTER — ALPHA VALIDATION')
    print(f"Closed Trades: {dq.get('closed_trade_count',0)}")
    print(f"Usable Trades: {dq.get('rows_usable_for_calculation',0)}")
    print(f"Win Rate: {pct(cp.get('win_rate'))}")
    print(f"Expectancy: {cp.get('expectancy_per_trade')}")
    print(f"Profit Factor: {cp.get('profit_factor')}")
    print(f"Max Drawdown: {cp.get('maximum_drawdown')}")
    print(f"Best Regime: {best('market_regime')}")
    print(f"Worst Regime: {best('market_regime', False)}")
    print(f"Best Rank: {best('trade_quality_rank')}")
    print(f"Largest Edge Contributor: {report.get('edge_attribution',{}).get('largest_edge_contributor','INCONCLUSIVE')}")
    print(f"Stability: {report.get('stability',{}).get('assessment','INCONCLUSIVE')}")
    print(f"Verdict: {report.get('verdict',{}).get('research_audit_verdict','INCONCLUSIVE')}")
    print('Phase 3: NOT UNLOCKED')
    print('Real Trading: LOCKED')
    print('Created: logs/alpha_validation_report.json')
    print('Created: logs/alpha_validation_report.md')
if __name__ == '__main__': main()
