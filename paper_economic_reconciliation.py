import csv, json, math, os, sqlite3, statistics, tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPORT_PATH='reports/paper_economic_reconciliation.json'
EQUITY_PATH='reports/paper_economic_equity_curve.csv'
OVERLAP_PATH='reports/paper_overlap_audit.csv'
PHASE='Phase 9D.1B-A Paper Economic Metric Reconciliation'
LEGACY_INTERPRETATION='Arithmetic sum of individual closed-trade percentage returns; not capital-normalized account ROI.'

@dataclass(frozen=True)
class EconomicAuditConfig:
    initial_capital: float = float(os.getenv('ECON_AUDIT_INITIAL_CAPITAL','10000'))
    allocation_pct_per_trade: float = float(os.getenv('ECON_AUDIT_ALLOCATION_PCT_PER_TRADE','1.0'))
    max_gross_exposure_pct: float = float(os.getenv('ECON_AUDIT_MAX_GROSS_EXPOSURE_PCT','100.0'))
    round_trip_fee_bps: float = float(os.getenv('ECON_AUDIT_ROUND_TRIP_FEE_BPS','8'))
    slippage_bps: float = float(os.getenv('ECON_AUDIT_SLIPPAGE_BPS','15'))
    match_tolerance_pct: float = float(os.getenv('ECON_AUDIT_MATCH_TOLERANCE_PCT','0.01'))
    small_difference_tolerance_pct: float = float(os.getenv('ECON_AUDIT_SMALL_DIFF_TOLERANCE_PCT','0.10'))
    extreme_return_abs_pct: float = float(os.getenv('ECON_AUDIT_EXTREME_RETURN_ABS_PCT','50'))
    severe_return_abs_pct: float = float(os.getenv('ECON_AUDIT_SEVERE_RETURN_ABS_PCT','100'))
    min_valid_closed_trades: int = int(os.getenv('ECON_AUDIT_MIN_VALID_CLOSED_TRADES','100'))
    min_cost_adjusted_normalized_return_pct: float = float(os.getenv('ECON_AUDIT_MIN_COST_ADJUSTED_NORMALIZED_RETURN_PCT','0'))
    min_profit_factor: float = float(os.getenv('ECON_AUDIT_MIN_PROFIT_FACTOR','1.0'))
    max_realized_drawdown_pct: float = float(os.getenv('ECON_AUDIT_MAX_REALIZED_DRAWDOWN_PCT','20'))
    max_top_symbol_concentration_pct: float = float(os.getenv('ECON_AUDIT_MAX_TOP_SYMBOL_CONCENTRATION_PCT','50'))
    max_outlier_contribution_pct: float = float(os.getenv('ECON_AUDIT_MAX_OUTLIER_CONTRIBUTION_PCT','25'))
    max_overlap_dependence_pct: float = float(os.getenv('ECON_AUDIT_MAX_OVERLAP_DEPENDENCE_PCT','25'))

def _now(): return datetime.now(timezone.utc).isoformat()
def _num(v):
    try:
        if v is None or v=='': return None
        x=float(v); return x if math.isfinite(x) else None
    except Exception: return None
def _dt(v):
    if not v: return None
    s=str(v).replace('Z','+00:00')
    try: return datetime.fromisoformat(s)
    except Exception: return None
def _get(r,k,d=None): return r[k] if k in r.keys() else d
def _pct(vals,p):
    if not vals: return None
    vals=sorted(vals); k=(len(vals)-1)*p/100; f=math.floor(k); c=math.ceil(k)
    return vals[int(k)] if f==c else vals[f]*(c-k)+vals[c]*(k-f)
def _round(v,n=4): return None if v is None else round(float(v),n)

def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.tmp_', suffix='.json', dir=os.path.dirname(path) or '.')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(data,f,indent=2,sort_keys=True,default=str); f.write('\n')
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def _write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in sorted(rows,key=lambda x: tuple(str(x.get(k,'')) for k in fields)): w.writerow({k:r.get(k,'') for k in fields})

def _connect_ro(db_path):
    if not os.path.exists(db_path): return None
    c=sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro",uri=True); c.row_factory=sqlite3.Row; return c

def _recompute(row):
    entry=_num(_get(row,'entry_price')); exitp=_num(_get(row,'exit_price'))
    side=str(_get(row,'side','') or '').upper().strip()
    if entry is None or entry<=0 or exitp is None: return None
    if side in ('SHORT','SELL'): return (entry-exitp)/entry*100
    if side in ('LONG','BUY',''): return (exitp-entry)/entry*100
    return None

def _legacy_return(row):
    pnl=_num(_get(row,'pnl'))
    if pnl is not None: return pnl
    r=_recompute(row)
    if r is not None: return r
    entry=_num(_get(row,'entry_price')); cur=_num(_get(row,'current_price'))
    return (cur-entry)/entry*100 if entry and cur is not None else 0.0

def _trade(row,cfg):
    stored=_num(_get(row,'pnl')); rec=_recompute(row); diff=None if stored is None or rec is None else stored-rec
    if rec is None: status='CANNOT_RECOMPUTE'
    elif stored is None: status='MATCH'
    elif abs(diff)<=cfg.match_tolerance_pct: status='MATCH'
    elif abs(diff)<=cfg.small_difference_tolerance_pct: status='SMALL_DIFFERENCE'
    else: status='MATERIAL_DIFFERENCE'
    opened=_get(row,'timestamp') or _get(row,'opened_at'); closed=_get(row,'updated_at') or _get(row,'closed_at')
    reasons=[]
    if not _get(row,'symbol'): reasons.append('missing_symbol')
    if not _get(row,'side'): reasons.append('missing_side')
    if _num(_get(row,'entry_price')) is None: reasons.append('missing_entry_price')
    if _num(_get(row,'entry_price')) is not None and _num(_get(row,'entry_price'))<=0: reasons.append('zero_or_negative_entry_price')
    if _num(_get(row,'exit_price')) is None: reasons.append('missing_exit_price')
    if not opened: reasons.append('missing_opened_at')
    if not closed: reasons.append('missing_closed_at')
    od,cd=_dt(opened),_dt(closed)
    if od and cd and cd<od: reasons.append('invalid_timestamp_ordering')
    if rec is None: reasons.append('return_unavailable')
    return {'trade_id':_get(row,'id'),'symbol':_get(row,'symbol') or 'UNKNOWN','side':_get(row,'side') or '', 'side_normalized':str(_get(row,'side','') or '').upper(),'status':str(_get(row,'status','') or '').upper(),'entry_price':_num(_get(row,'entry_price')),'exit_price':_num(_get(row,'exit_price')),'stored_return_pct':_round(stored),'recomputed_return_pct':_round(rec),'return_pct':rec,'return_difference_pct':_round(diff),'return_source':'recomputed_side_aware_entry_exit' if rec is not None else 'unavailable','reconciliation_status':status,'opened_at':opened,'closed_at':closed,'opened_dt':od,'closed_dt':cd,'exit_reason':_get(row,'exit_reason') or 'UNKNOWN','regime':_get(row,'regime'),'score':_num(_get(row,'confidence')),'exclusion_reasons':reasons,'valid':not reasons and status in ('MATCH','SMALL_DIFFERENCE')}

def _stats(trades):
    vals=[t['return_pct'] for t in trades if t.get('return_pct') is not None]
    wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
    gp=sum(wins); gn=sum(losses)
    if not vals:
        profit_factor=None; profit_factor_state='NO_TRADES'
    elif not losses:
        profit_factor=None; profit_factor_state='NO_LOSSES'
    else:
        profit_factor=_round(gp/abs(gn)); profit_factor_state='FINITE'
    seq=sorted(trades,key=lambda t:(str(t.get('closed_at') or ''), str(t.get('trade_id'))))
    mw=ml=cw=cl=0
    for t in seq:
        v=t.get('return_pct') or 0
        if v>0: cw+=1; cl=0
        elif v<0: cl+=1; cw=0
        else: cw=cl=0
        mw=max(mw,cw); ml=max(ml,cl)
    return {'trades':len(vals),'wins':len(wins),'losses':len(losses),'breakeven':sum(1 for v in vals if v==0),'winrate':_round(len(wins)/len(vals)*100 if vals else 0),'mean_return':_round(statistics.mean(vals) if vals else 0),'median_return':_round(statistics.median(vals) if vals else 0),'standard_deviation':_round(statistics.pstdev(vals) if len(vals)>1 else 0),'downside_deviation':_round(statistics.pstdev([min(0,v) for v in vals]) if len(vals)>1 else 0),'gross_positive_return':_round(gp),'gross_negative_return':_round(gn),'profit_factor': profit_factor,'profit_factor_state': profit_factor_state,'average_winner':_round(statistics.mean(wins) if wins else 0),'average_loser':_round(statistics.mean(losses) if losses else 0),'payoff_ratio': None if not losses or statistics.mean(losses)==0 else _round(statistics.mean(wins)/abs(statistics.mean(losses)) if wins else 0),'expectancy_per_closed_trade':_round(statistics.mean(vals) if vals else 0),'maximum_consecutive_wins':mw,'maximum_consecutive_losses':ml,'return_percentiles':{str(p):_round(_pct(vals,p)) for p in (5,25,50,75,95)},'best_trade': max(trades,key=lambda t:t.get('return_pct') if t.get('return_pct') is not None else -1e9, default=None),'worst_trade': min(trades,key=lambda t:t.get('return_pct') if t.get('return_pct') is not None else 1e9, default=None)}

def _one_symbol(valid):
    active={}; acc=[]; rej=[]
    for t in sorted(valid,key=lambda x:(x['opened_dt'], x['closed_dt'], str(x['trade_id']))):
        sym=t['symbol']; last=active.get(sym)
        if last and t['opened_dt'] < last['closed_dt']: rej.append(t); continue
        acc.append(t); active[sym]=t
    return acc, rej

def _overlap(valid, accepted_ids):
    rows=[]; clusters={}; cid=0; dup_keys={}
    for t in valid:
        key=(t['symbol'],t['side_normalized'],t['opened_at'],t['closed_at'],_round(t['return_pct'])) ; dup_keys[key]=dup_keys.get(key,0)+1
    for t in valid:
        same=[o for o in valid if o is not t and o['symbol']==t['symbol'] and o['opened_dt']<t['closed_dt'] and t['opened_dt']<o['closed_dt']]
        conc=[o for o in valid if o is not t and o['opened_dt']<t['closed_dt'] and t['opened_dt']<o['closed_dt']]
        if same:
            root=min([t]+same,key=lambda x:x['opened_dt'])['trade_id']; clusters.setdefault(root,len(clusters)+1); c=clusters[root]
        else: c=''
        key=(t['symbol'],t['side_normalized'],t['opened_at'],t['closed_at'],_round(t['return_pct']))
        rows.append({'trade_id':t['trade_id'],'symbol':t['symbol'],'opened_at':t['opened_at'],'closed_at':t['closed_at'],'return_pct':_round(t['return_pct']),'same_symbol_overlap_count':len(same),'total_concurrent_count':len(conc)+1,'overlap_cluster_id':c,'duplicate_event_flag':dup_keys[key]>1,'included_in_one_symbol_policy':t['trade_id'] in accepted_ids})
    return rows

def _blocked_capital(cfg, reason):
    return {
        'scenario_status':'BLOCKED_DATA_QUALITY',
        'block_reason': reason,
        'initial_capital': cfg.initial_capital,
        'ending_capital': None,
        'normalized_gross_return_pct': None,
        'normalized_net_return_pct': None,
        'maximum_drawdown_pct': None,
        'peak_equity': None,
        'minimum_equity': None,
        'accepted_trades': 0,
        'capacity_rejected_trades': 0,
        'total_realized_pnl': 0.0,
        'total_fees': 0.0,
        'total_slippage_impact': 0.0,
        'maximum_gross_exposure': 0.0,
        'realized_close_to_close_equity_curve': [],
        'equity_curve': [],
        'assumptions': _capital_assumptions(cfg),
    }

def _capital_assumptions(cfg):
    return {
        'initial_capital': cfg.initial_capital,
        'allocation_pct_per_trade': cfg.allocation_pct_per_trade,
        'max_gross_exposure_pct': cfg.max_gross_exposure_pct,
        'round_trip_fee_bps': cfg.round_trip_fee_bps,
        'slippage_bps': cfg.slippage_bps,
        'curve_type': 'realized_close_to_close_equity_curve',
        'opening_trade_does_not_increase_equity': True,
        'gross_exposure_reported_separately': True,
        'no_unrealized_mark_to_market_fabricated': True,
        'no_leverage': True,
        'not_actual_account_roi': True,
    }

def _capital(valid,cfg, source_blocked=False):
    if source_blocked:
        return _blocked_capital(cfg, 'source database or table unavailable')
    if not valid:
        return _blocked_capital(cfg, 'no valid reconciled closed trades')
    if any(not t.get('opened_dt') or not t.get('closed_dt') or t.get('return_pct') is None for t in valid):
        return _blocked_capital(cfg, 'timestamp or return quality insufficient')
    realized_capital=cfg.initial_capital
    peak=realized_capital; min_eq=realized_capital; max_dd=0.0
    allocs={}; curve=[]; close_curve=[]; fees=slip=realized_pnl=0.0; rej=0; max_exp=0.0
    events=[]
    for t in sorted(valid,key=lambda x:(x['opened_dt'],x['closed_dt'],str(x['trade_id']))):
        events.append(('open',t['opened_dt'],t)); events.append(('close',t['closed_dt'],t))
    for typ,ts,t in sorted(events,key=lambda e:(e[1], 0 if e[0]=='close' else 1, str(e[2]['trade_id']))):
        event_pnl=event_fee=event_slip=0.0
        if typ=='close' and t['trade_id'] in allocs:
            notional=allocs.pop(t['trade_id'])
            event_pnl=notional*t['return_pct']/100
            event_fee=notional*cfg.round_trip_fee_bps/10000
            event_slip=notional*cfg.slippage_bps/10000
            realized_capital += event_pnl - event_fee - event_slip
            realized_pnl += event_pnl; fees += event_fee; slip += event_slip
        elif typ=='open':
            notional=realized_capital*cfg.allocation_pct_per_trade/100
            exposure=sum(allocs.values()); max_allowed=realized_capital*cfg.max_gross_exposure_pct/100
            if exposure+notional<=max_allowed+1e-9:
                allocs[t['trade_id']]=notional
            else:
                rej+=1
        open_exposure=sum(allocs.values()); max_exp=max(max_exp,open_exposure)
        # realized close-to-close equity does not include reserved/open notional.
        realized_equity=realized_capital
        peak=max(peak,realized_equity); min_eq=min(min_eq,realized_equity)
        max_dd=max(max_dd,(peak-realized_equity)/peak*100 if peak else 0)
        point={'timestamp':ts.isoformat(),'event':typ,'trade_id':t['trade_id'],'realized_capital':_round(realized_capital),'realized_account_equity':_round(realized_equity),'reserved_notional':_round(open_exposure),'open_gross_exposure':_round(open_exposure),'available_unallocated_capacity':_round(max(0.0, realized_capital*cfg.max_gross_exposure_pct/100-open_exposure)),'realized_pnl':_round(event_pnl),'fees':_round(event_fee),'slippage':_round(event_slip)}
        curve.append(point)
        if typ=='close': close_curve.append(point)
    return {'scenario_status':'COMPLETED','initial_capital':cfg.initial_capital,'ending_capital':_round(realized_capital),'normalized_gross_return_pct':_round((realized_capital+fees+slip-cfg.initial_capital)/cfg.initial_capital*100),'normalized_net_return_pct':_round((realized_capital-cfg.initial_capital)/cfg.initial_capital*100),'maximum_drawdown_pct':_round(max_dd),'peak_equity':_round(peak),'minimum_equity':_round(min_eq),'accepted_trades':len(valid)-rej,'capacity_rejected_trades':rej,'total_realized_pnl':_round(realized_pnl),'total_fees':_round(fees),'total_slippage_impact':_round(slip),'maximum_gross_exposure':_round(max_exp),'assumptions':_capital_assumptions(cfg),'realized_close_to_close_equity_curve':close_curve,'equity_curve':curve}

def _ratio_pct(numer, denom):
    if denom is None or abs(denom) < 1e-12:
        return None
    return _round(numer / abs(denom) * 100)

def _concentration(valid):
    totals={}
    for t in valid:
        totals[t['symbol']]=totals.get(t['symbol'],0.0)+(t.get('return_pct') or 0.0)
    signed_total=sum(totals.values())
    gross_abs_total=sum(abs(v) for v in totals.values())
    ranked_abs=sorted(totals.items(), key=lambda kv: abs(kv[1]), reverse=True)
    def gross_topn(n):
        if gross_abs_total <= 0: return None
        return _round(sum(abs(v) for _,v in ranked_abs[:n]) / gross_abs_total * 100)
    def signed_topn(n): return _ratio_pct(sum(v for _,v in ranked_abs[:n]), signed_total)
    hhi=None
    if gross_abs_total:
        hhi=_round(sum((abs(v)/gross_abs_total)**2 for v in totals.values()))
    return {
        'method':'Concentration gates use gross absolute symbol contribution shares: sum(abs(symbol return contribution)) / total absolute valid return contribution. Signed contribution percentages are informational and null when signed total is zero.',
        'top_1_symbol_contribution_pct':gross_topn(1),
        'top_3_symbol_contribution_pct':gross_topn(3),
        'top_5_symbol_contribution_pct':gross_topn(5),
        'top_10_symbol_contribution_pct':gross_topn(10),
        'top_1_signed_return_contribution_pct':signed_topn(1),
        'top_3_signed_return_contribution_pct':signed_topn(3),
        'top_5_signed_return_contribution_pct':signed_topn(5),
        'top_10_signed_return_contribution_pct':signed_topn(10),
        'herfindahl_concentration':hhi,
        'symbol_event_return_sums':{k:_round(v) for k,v in sorted(totals.items())},
    }

def _bucket_stats(trades):
    s=_stats(trades)
    return {k:s[k] for k in ('trades','wins','losses','breakeven','winrate','mean_return','median_return','profit_factor','profit_factor_state','expectancy_per_closed_trade')} | {'event_return_sum': _round(sum(t.get('return_pct') or 0 for t in trades)), 'expectancy': s['expectancy_per_closed_trade']}

def _holding_bucket(t):
    if not t.get('opened_dt') or not t.get('closed_dt'): return 'UNAVAILABLE'
    h=(t['closed_dt']-t['opened_dt']).total_seconds()/3600
    if h<1: return '<1h'
    if h<4: return '1-4h'
    if h<24: return '4-24h'
    if h<72: return '1-3d'
    return '>3d'

def _breakdowns(valid):
    out={'symbol':{},'side':{},'score_bucket':{'enrichment_status':'UNAVAILABLE'},'regime':{'enrichment_status':'UNAVAILABLE'},'exit_reason':{},'holding_period_bucket':{},'calendar_day':{},'calendar_week':{}}
    groups=[('symbol',lambda t:t['symbol']),('side',lambda t:t['side_normalized']),('exit_reason',lambda t:t['exit_reason']),('holding_period_bucket',_holding_bucket),('calendar_day',lambda t:t['closed_dt'].date().isoformat() if t.get('closed_dt') else 'UNAVAILABLE'),('calendar_week',lambda t:f"{t['closed_dt'].isocalendar().year}-W{t['closed_dt'].isocalendar().week:02d}" if t.get('closed_dt') else 'UNAVAILABLE')]
    for name,fn in groups:
        vals=sorted({fn(t) for t in valid})
        for v in vals: out[name][str(v)]=_bucket_stats([t for t in valid if fn(t)==v])
    return out

def _overall_status(valid, stats, cap, concentration, overlap_pct, outlier_pct, status_counts, cfg, data_quality_blocked):
    if data_quality_blocked or cap.get('scenario_status')!='COMPLETED' or not valid:
        return 'BLOCKED_DATA_QUALITY'
    if len(valid) < cfg.min_valid_closed_trades:
        return 'REVIEW'
    if stats.get('expectancy_per_closed_trade',0) <= 0 or (stats.get('profit_factor_state') == 'FINITE' and stats.get('profit_factor') < cfg.min_profit_factor):
        return 'BLOCKED_NEGATIVE_EXPECTANCY'
    if cap.get('normalized_net_return_pct') is None or cap.get('normalized_net_return_pct') < cfg.min_cost_adjusted_normalized_return_pct:
        return 'BLOCKED_NEGATIVE_EXPECTANCY'
    if cap.get('maximum_drawdown_pct') is not None and cap.get('maximum_drawdown_pct') > cfg.max_realized_drawdown_pct:
        return 'BLOCKED_DRAWDOWN'
    top=concentration.get('top_1_symbol_contribution_pct')
    if top is not None and abs(top)>cfg.max_top_symbol_concentration_pct:
        return 'BLOCKED_CONCENTRATION'
    if outlier_pct is None:
        return 'REVIEW'
    if abs(outlier_pct)>cfg.max_outlier_contribution_pct:
        return 'BLOCKED_OUTLIER_DEPENDENCE'
    if overlap_pct is not None and abs(overlap_pct)>cfg.max_overlap_dependence_pct:
        return 'REVIEW'
    return 'PASS'

def generate_paper_economic_reconciliation(db_path='mamuyy_hunter.db', output_path=REPORT_PATH, equity_curve_path=EQUITY_PATH, overlap_path=OVERLAP_PATH, write_reports=True, config=None):
    cfg=config or EconomicAuditConfig(); conn=_connect_ro(db_path); rows=[]; cols=[]; warning=''; source_blocked=False
    if conn:
        try:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_paper_trades'").fetchone()
            if exists:
                cols=[r[1] for r in conn.execute('PRAGMA table_info(internal_paper_trades)').fetchall()]
                rows=conn.execute("SELECT * FROM internal_paper_trades WHERE UPPER(COALESCE(status,''))='CLOSED' ORDER BY id").fetchall()
            else:
                warning='internal_paper_trades table unavailable'; source_blocked=True
        finally: conn.close()
    else:
        warning='database unavailable'; source_blocked=True
    trades=[_trade(r,cfg) for r in rows]
    status_counts={k:0 for k in ('MATCH','SMALL_DIFFERENCE','MATERIAL_DIFFERENCE','CANNOT_RECOMPUTE','INVALID_CONTRACT')}
    for t in trades:
        status_counts[t.get('reconciliation_status','INVALID_CONTRACT')]=status_counts.get(t.get('reconciliation_status','INVALID_CONTRACT'),0)+1
    legacy=[_legacy_return(r) for r in rows]; wins=sum(1 for v in legacy if v>0)
    valid=[t for t in trades if t['valid']]
    accepted,rejected=_one_symbol(valid); overlap_rows=_overlap(valid,{t['trade_id'] for t in accepted})
    cap=_capital(valid,cfg,source_blocked or len(rows)==0)
    out=[t for t in valid if abs(t['return_pct'])>cfg.extreme_return_abs_pct]
    no_out=[t for t in valid if t not in out]
    stats=_stats(valid); no_out_stats=_stats(no_out)
    concentration=_concentration(valid)
    overlap_return=sum(t['return_pct'] for t in valid if any(r['trade_id']==t['trade_id'] and r['same_symbol_overlap_count']>0 for r in overlap_rows))
    legacy_sum=sum(legacy)
    overlap_return_pct=_ratio_pct(overlap_return, legacy_sum)
    overlap_trade_pct=_round(sum(1 for r in overlap_rows if r['same_symbol_overlap_count']>0)/len(valid)*100) if valid else None
    total_abs_valid_return=sum(abs(t['return_pct']) for t in valid)
    outlier_contribution_pct=_round(sum(abs(t['return_pct']) for t in out)/total_abs_valid_return*100) if valid and total_abs_valid_return>0 else None
    signed_outlier_contribution_pct=_ratio_pct(sum(t['return_pct'] for t in out), sum(t['return_pct'] for t in valid))
    excluded_row_count=sum(1 for t in trades if not t['valid'])
    data_quality_block_reasons=[]
    if source_blocked: data_quality_block_reasons.append('source_database_or_table_unavailable')
    if len(rows)==0: data_quality_block_reasons.append('zero_closed_rows')
    if len(valid)==0: data_quality_block_reasons.append('zero_valid_reconciled_rows')
    if excluded_row_count: data_quality_block_reasons.append('excluded_required_closed_rows')
    for status_name in ('MATERIAL_DIFFERENCE','CANNOT_RECOMPUTE','INVALID_CONTRACT'):
        if status_counts.get(status_name,0): data_quality_block_reasons.append(status_name.lower())
    reason_map={'missing_symbol':'missing_symbol','missing_side':'missing_side','missing_entry_price':'missing_entry_or_exit','missing_exit_price':'missing_entry_or_exit','missing_opened_at':'missing_timestamps','missing_closed_at':'missing_timestamps','invalid_timestamp_ordering':'invalid_timestamp_ordering','return_unavailable':'non_finite_or_unavailable_return','zero_or_negative_entry_price':'missing_entry_or_exit'}
    for t in trades:
        for reason in t['exclusion_reasons']:
            mapped=reason_map.get(reason)
            if mapped and mapped not in data_quality_block_reasons: data_quality_block_reasons.append(mapped)
    data_quality_blocked=bool(data_quality_block_reasons)
    overall=_overall_status(valid, stats, cap, concentration, overlap_trade_pct, outlier_contribution_pct, status_counts, cfg, data_quality_blocked)
    fields=['trade_id','symbol','opened_at','closed_at','return_pct','same_symbol_overlap_count','total_concurrent_count','overlap_cluster_id','duplicate_event_flag','included_in_one_symbol_policy']
    eqfields=['timestamp','event','trade_id','realized_capital','realized_account_equity','reserved_notional','open_gross_exposure','available_unallocated_capacity','realized_pnl','fees','slippage']
    thresholds={'minimum_valid_closed_trades':cfg.min_valid_closed_trades,'minimum_cost_adjusted_normalized_return_pct':cfg.min_cost_adjusted_normalized_return_pct,'minimum_profit_factor':cfg.min_profit_factor,'maximum_realized_drawdown_pct':cfg.max_realized_drawdown_pct,'maximum_top_symbol_concentration_pct':cfg.max_top_symbol_concentration_pct,'maximum_outlier_contribution_pct':cfg.max_outlier_contribution_pct,'maximum_overlap_dependence_pct':cfg.max_overlap_dependence_pct}
    economic_readiness={'overall_economic_readiness_status':overall,'thresholds':thresholds,'sample_adequacy':'PASS' if len(valid)>=cfg.min_valid_closed_trades else ('BLOCKED_DATA_QUALITY' if not valid else 'REVIEW'),'data_quality_adequacy':'BLOCKED_DATA_QUALITY' if data_quality_blocked else 'PASS','positive_expectancy':'PASS' if stats['expectancy_per_closed_trade']>0 else 'BLOCKED_NEGATIVE_EXPECTANCY','profit_factor':'PASS' if stats['profit_factor_state']=='NO_LOSSES' and stats['trades']>0 or (stats['profit_factor_state']=='FINITE' and stats['profit_factor'] is not None and stats['profit_factor']>=cfg.min_profit_factor) else 'BLOCKED_DATA_QUALITY' if stats['profit_factor_state']=='NO_TRADES' else 'BLOCKED_NEGATIVE_EXPECTANCY','normalized_scenario_return':'PASS' if cap.get('scenario_status')=='COMPLETED' and cap.get('normalized_net_return_pct') is not None and cap.get('normalized_net_return_pct')>=cfg.min_cost_adjusted_normalized_return_pct else 'BLOCKED_DATA_QUALITY' if cap.get('scenario_status')!='COMPLETED' else 'BLOCKED_NEGATIVE_EXPECTANCY','normalized_maximum_drawdown':'PASS' if cap.get('maximum_drawdown_pct') is not None and cap.get('maximum_drawdown_pct')<=cfg.max_realized_drawdown_pct else 'BLOCKED_DATA_QUALITY' if cap.get('maximum_drawdown_pct') is None else 'BLOCKED_DRAWDOWN','concentration':'PASS' if concentration.get('top_1_symbol_contribution_pct') is not None and abs(concentration.get('top_1_symbol_contribution_pct'))<=cfg.max_top_symbol_concentration_pct else 'REVIEW' if concentration.get('top_1_symbol_contribution_pct') is None else 'BLOCKED_CONCENTRATION','overlap_dependence':'PASS' if overlap_trade_pct is not None and overlap_trade_pct<=cfg.max_overlap_dependence_pct else 'REVIEW' if overlap_trade_pct is not None else 'BLOCKED_DATA_QUALITY','outlier_dependence':'REVIEW' if outlier_contribution_pct is None and valid else 'BLOCKED_DATA_QUALITY' if outlier_contribution_pct is None else 'PASS' if abs(outlier_contribution_pct)<=cfg.max_outlier_contribution_pct else 'BLOCKED_OUTLIER_DEPENDENCE','cost_adjusted_result':'PASS' if cap.get('normalized_net_return_pct') is not None and cap.get('normalized_net_return_pct')>=cfg.min_cost_adjusted_normalized_return_pct else 'BLOCKED_DATA_QUALITY' if cap.get('normalized_net_return_pct') is None else 'BLOCKED_NEGATIVE_EXPECTANCY'}
    if overall == 'BLOCKED_DATA_QUALITY':
        for gate in ('sample_adequacy','data_quality_adequacy','positive_expectancy','profit_factor','normalized_scenario_return','normalized_maximum_drawdown','concentration','overlap_dependence','outlier_dependence','cost_adjusted_result'):
            economic_readiness[gate]='BLOCKED_DATA_QUALITY'

    report={'phase':PHASE,'generated_at':_now(),'source_database':db_path,'source_table':'internal_paper_trades','governance':{'paper_only':True,'read_only_database_access':True,'writes_to_database':False,'writes_to_broker':False,'execution_allowed':False,'automatic_promotion_allowed':False,'readiness_advisory_only':True},'schema_audit':{'total_closed_rows':len(rows),'exact_columns_available':cols,'warning':warning},'legacy_metrics':{'closed_trade_count':len(rows),'legacy_closed_winrate_pct':_round(wins/len(legacy)*100 if legacy else 0),'legacy_event_return_sum_pct':_round(sum(legacy)),'legacy_average_trade_return_pct':_round(statistics.mean(legacy) if legacy else 0),'legacy_best_trade_return_pct':_round(max(legacy) if legacy else 0),'legacy_worst_trade_return_pct':_round(min(legacy) if legacy else 0),'legacy_metric_interpretation':LEGACY_INTERPRETATION},'reconciliation_summary':{'reconciliation_status_counts':status_counts,'data_quality_blocked':data_quality_blocked,'data_quality_block_reasons':data_quality_block_reasons,'material_mismatch_policy':'MATERIAL_DIFFERENCE rows are excluded from authoritative closed-trade statistics because stored pnl unit is not assumed proven; side-aware recomputed return is required to match within tolerance.','trades':[{k:v for k,v in t.items() if k not in ('opened_dt','closed_dt')} for t in trades],'valid_closed_trades':len(valid),'excluded_rows':excluded_row_count},'closed_trade_statistics':stats,'data_quality':{'missing_symbol':sum('missing_symbol'in t['exclusion_reasons'] for t in trades),'missing_side':sum('missing_side'in t['exclusion_reasons'] for t in trades),'missing_entry_price':sum('missing_entry_price'in t['exclusion_reasons'] for t in trades),'missing_exit_current_price':sum('missing_exit_price'in t['exclusion_reasons'] for t in trades),'missing_stored_pnl':sum(t['stored_return_pct'] is None for t in trades),'missing_opened_at':sum('missing_opened_at'in t['exclusion_reasons'] for t in trades),'missing_closed_at':sum('missing_closed_at'in t['exclusion_reasons'] for t in trades),'invalid_timestamp_ordering':sum('invalid_timestamp_ordering'in t['exclusion_reasons'] for t in trades),'zero_or_negative_entry_prices':sum('zero_or_negative_entry_price'in t['exclusion_reasons'] for t in trades),'non_finite_returns':sum(t['return_pct'] is None for t in trades),'extreme_return_count':len(out),'duplicate_primary_ids':len(rows)-len({t['trade_id'] for t in trades}),'duplicate_economic_events':sum(1 for r in overlap_rows if r['duplicate_event_flag']),'excluded_rows':[{'trade_id':t['trade_id'],'reasons':t['exclusion_reasons'] + (['material_return_mismatch'] if t['reconciliation_status']=='MATERIAL_DIFFERENCE' else [])} for t in trades if not t['valid']]},'overlap_audit':{'maximum_simultaneous_active_trades':max([r['total_concurrent_count'] for r in overlap_rows], default=0),'same_symbol_overlapping_trades':sum(1 for r in overlap_rows if r['same_symbol_overlap_count']>0),'same_symbol_overlap_trade_pct':overlap_trade_pct,'overlap_event_return_sum_pct':_round(overlap_return),'overlap_event_return_contribution_pct':overlap_return_pct,'denominator_method':'Overlap contribution divides overlapping signed event-return by absolute legacy event-return sum; null when denominator is zero.','overlap_csv':overlap_path},'one_symbol_policy_scenario':{'original_valid_trade_count':len(valid),'accepted_trade_count':len(accepted),'overlap_rejected_trade_count':len(rejected),'original_event_return_sum':_round(sum(t['return_pct'] for t in valid)),'filtered_event_return_sum':_round(sum(t['return_pct'] for t in accepted)),'original_winrate':stats['winrate'],'filtered_winrate':_stats(accepted)['winrate'],'original_profit_factor':stats['profit_factor'],'filtered_profit_factor':_stats(accepted)['profit_factor'],'overlap_inflation_delta':_round(sum(t['return_pct'] for t in valid)-sum(t['return_pct'] for t in accepted))},'equal_allocation_capital_scenario':cap,'outlier_analysis':{'outlier_count':len(out),'outlier_trade_ids':[t['trade_id'] for t in out],'outlier_symbols':sorted({t['symbol'] for t in out}),'outlier_contribution_pct':outlier_contribution_pct,'signed_outlier_return_contribution_pct':signed_outlier_contribution_pct,'return_contribution_with_outliers':_round(sum(t['return_pct'] for t in valid)),'return_contribution_without_outliers':_round(sum(t['return_pct'] for t in no_out)),'winrate_with_outliers':stats['winrate'],'winrate_without_outliers':no_out_stats['winrate'],'profit_factor_with_outliers':stats['profit_factor'],'profit_factor_without_outliers':no_out_stats['profit_factor']},'concentration':concentration,'breakdowns':_breakdowns(valid),'readiness':{'engineering_readiness':{'data_pipeline':'PASS','database_readability':'PASS' if not source_blocked else 'BLOCKED_DATA_QUALITY','audit_execution':'PASS','PAPER_ONLY_governance':'PASS','report_generation':'PASS'},'economic_readiness':economic_readiness,'overall_economic_readiness_status':overall,'execution_allowed':False,'automatic_promotion_allowed':False,'paper_only':True,'readiness_advisory_only':True},'terminology_recommendations':{'Net PnL':'Event Return Sum','Cumulative Shadow PnL':'Cumulative Event Return','add':['Capital-Normalized Scenario Return','Economic Readiness'],'retain':['Engineering Readiness']},'artifact_paths':{'json':output_path,'equity_curve_csv':equity_curve_path,'overlap_audit_csv':overlap_path}}
    if write_reports:
        _atomic_json(output_path, report); _write_csv(overlap_path, overlap_rows, fields); _write_csv(equity_curve_path, cap.get('equity_curve',[]), eqfields)
    return report

def format_paper_economic_reconciliation(report):
    lm=report.get('legacy_metrics',{}); cap=report.get('equal_allocation_capital_scenario',{})
    return '\n'.join(['PAPER ECONOMIC RECONCILIATION',f"Paper Only: {report.get('governance',{}).get('paper_only')}",f"Closed Trades: {lm.get('closed_trade_count',0)}",f"Legacy Event Return Sum: {lm.get('legacy_event_return_sum_pct')}%",f"Legacy Interpretation: {lm.get('legacy_metric_interpretation')}",f"Capital Scenario Status: {cap.get('scenario_status')}",f"Normalized Net Return: {cap.get('normalized_net_return_pct')}"])

if __name__=='__main__': print(format_paper_economic_reconciliation(generate_paper_economic_reconciliation()))
