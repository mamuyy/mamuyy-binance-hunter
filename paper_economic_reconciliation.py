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
    return {'trade_id':_get(row,'id'),'symbol':_get(row,'symbol') or 'UNKNOWN','side':_get(row,'side') or '', 'side_normalized':str(_get(row,'side','') or '').upper(),'status':str(_get(row,'status','') or '').upper(),'entry_price':_num(_get(row,'entry_price')),'exit_price':_num(_get(row,'exit_price')),'stored_return_pct':_round(stored),'recomputed_return_pct':_round(rec),'return_pct':rec,'return_difference_pct':_round(diff),'return_source':'recomputed_side_aware_entry_exit' if rec is not None else 'unavailable','reconciliation_status':status,'opened_at':opened,'closed_at':closed,'opened_dt':od,'closed_dt':cd,'exit_reason':_get(row,'exit_reason') or 'UNKNOWN','regime':_get(row,'regime'),'score':_num(_get(row,'confidence')),'exclusion_reasons':reasons,'valid':not reasons and status in ('MATCH','SMALL_DIFFERENCE','MATERIAL_DIFFERENCE')}

def _stats(trades):
    vals=[t['return_pct'] for t in trades if t.get('return_pct') is not None]
    wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
    gp=sum(wins); gn=sum(losses)
    seq=sorted(trades,key=lambda t:(str(t.get('closed_at') or ''), str(t.get('trade_id'))))
    mw=ml=cw=cl=0
    for t in seq:
        v=t.get('return_pct') or 0
        if v>0: cw+=1; cl=0
        elif v<0: cl+=1; cw=0
        else: cw=cl=0
        mw=max(mw,cw); ml=max(ml,cl)
    return {'trades':len(vals),'wins':len(wins),'losses':len(losses),'breakeven':sum(1 for v in vals if v==0),'winrate':_round(len(wins)/len(vals)*100 if vals else 0),'mean_return':_round(statistics.mean(vals) if vals else 0),'median_return':_round(statistics.median(vals) if vals else 0),'standard_deviation':_round(statistics.pstdev(vals) if len(vals)>1 else 0),'downside_deviation':_round(statistics.pstdev([min(0,v) for v in vals]) if len(vals)>1 else 0),'gross_positive_return':_round(gp),'gross_negative_return':_round(gn),'profit_factor': None if gn==0 else _round(gp/abs(gn)),'average_winner':_round(statistics.mean(wins) if wins else 0),'average_loser':_round(statistics.mean(losses) if losses else 0),'payoff_ratio': None if not losses or statistics.mean(losses)==0 else _round(statistics.mean(wins)/abs(statistics.mean(losses)) if wins else 0),'expectancy_per_closed_trade':_round(statistics.mean(vals) if vals else 0),'maximum_consecutive_wins':mw,'maximum_consecutive_losses':ml,'return_percentiles':{str(p):_round(_pct(vals,p)) for p in (5,25,50,75,95)},'best_trade': max(trades,key=lambda t:t.get('return_pct') if t.get('return_pct') is not None else -1e9, default=None),'worst_trade': min(trades,key=lambda t:t.get('return_pct') if t.get('return_pct') is not None else 1e9, default=None)}

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

def _capital(valid,cfg):
    if any(not t.get('opened_dt') or not t.get('closed_dt') or t.get('return_pct') is None for t in valid): return {'scenario_status':'BLOCKED_DATA_QUALITY','normalized_gross_return_pct':None,'normalized_net_return_pct':None,'equity_curve':[]}
    cap=cfg.initial_capital; peak=cap; min_eq=cap; max_dd=0; openpos=[]; curve=[]; fees=slip=0; rej=0; max_exp=0
    events=[]
    for t in sorted(valid,key=lambda x:(x['opened_dt'],x['closed_dt'],str(x['trade_id']))): events.append(('open',t['opened_dt'],t)); events.append(('close',t['closed_dt'],t))
    allocs={}
    for typ,ts,t in sorted(events,key=lambda e:(e[1], 0 if e[0]=='close' else 1, str(e[2]['trade_id']))):
        if typ=='close' and t['trade_id'] in allocs:
            notional=allocs.pop(t['trade_id']); gross=notional*t['return_pct']/100; f=notional*cfg.round_trip_fee_bps/10000; s=notional*cfg.slippage_bps/10000; cap+=gross-f-s; fees+=f; slip+=s
        elif typ=='open':
            notional=cap*cfg.allocation_pct_per_trade/100; exposure=sum(allocs.values()); max_allowed=cap*cfg.max_gross_exposure_pct/100
            if exposure+notional<=max_allowed+1e-9: allocs[t['trade_id']]=notional
            else: rej+=1
        equity=cap+sum(allocs.values()); peak=max(peak,equity); min_eq=min(min_eq,equity); max_dd=max(max_dd,(peak-equity)/peak*100 if peak else 0); max_exp=max(max_exp,sum(allocs.values()))
        curve.append({'timestamp':ts.isoformat(),'event':typ,'trade_id':t['trade_id'],'capital':_round(cap),'equity':_round(equity),'gross_exposure':_round(sum(allocs.values()))})
    return {'scenario_status':'COMPLETED','initial_capital':cfg.initial_capital,'ending_capital':_round(cap),'normalized_gross_return_pct':_round((cap+fees+slip-cfg.initial_capital)/cfg.initial_capital*100),'normalized_net_return_pct':_round((cap-cfg.initial_capital)/cfg.initial_capital*100),'maximum_drawdown_pct':_round(max_dd),'peak_equity':_round(peak),'minimum_equity':_round(min_eq),'accepted_trades':len(valid)-rej,'capacity_rejected_trades':rej,'total_fees':_round(fees),'total_slippage_impact':_round(slip),'maximum_gross_exposure':_round(max_exp),'assumptions':{'initial_capital':cfg.initial_capital,'allocation_pct_per_trade':cfg.allocation_pct_per_trade,'max_gross_exposure_pct':cfg.max_gross_exposure_pct,'round_trip_fee_bps':cfg.round_trip_fee_bps,'slippage_bps':cfg.slippage_bps,'no_leverage':True,'not_actual_account_roi':True},'equity_curve':curve}

def generate_paper_economic_reconciliation(db_path='mamuyy_hunter.db', output_path=REPORT_PATH, equity_curve_path=EQUITY_PATH, overlap_path=OVERLAP_PATH, write_reports=True, config=None):
    cfg=config or EconomicAuditConfig(); conn=_connect_ro(db_path); rows=[]; cols=[]; warning=''
    if conn:
        try:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_paper_trades'").fetchone()
            if exists:
                cols=[r[1] for r in conn.execute('PRAGMA table_info(internal_paper_trades)').fetchall()]
                rows=conn.execute("SELECT * FROM internal_paper_trades WHERE UPPER(COALESCE(status,''))='CLOSED' ORDER BY id").fetchall()
            else: warning='internal_paper_trades table unavailable'
        finally: conn.close()
    else: warning='database unavailable'
    trades=[_trade(r,cfg) for r in rows]
    legacy=[_legacy_return(r) for r in rows]; wins=sum(1 for v in legacy if v>0); valid=[t for t in trades if t['valid']]
    accepted,rejected=_one_symbol(valid); overlap_rows=_overlap(valid,{t['trade_id'] for t in accepted})
    cap=_capital(valid,cfg); out=[t for t in valid if abs(t['return_pct'])>cfg.extreme_return_abs_pct]
    no_out=[t for t in valid if t not in out]
    fields=['trade_id','symbol','opened_at','closed_at','return_pct','same_symbol_overlap_count','total_concurrent_count','overlap_cluster_id','duplicate_event_flag','included_in_one_symbol_policy']
    eqfields=['timestamp','event','trade_id','capital','equity','gross_exposure']
    report={'phase':PHASE,'generated_at':_now(),'source_database':db_path,'source_table':'internal_paper_trades','governance':{'paper_only':True,'read_only_database_access':True,'writes_to_database':False,'writes_to_broker':False,'execution_allowed':False,'automatic_promotion_allowed':False,'readiness_advisory_only':True},'schema_audit':{'total_closed_rows':len(rows),'exact_columns_available':cols,'warning':warning},'legacy_metrics':{'closed_trade_count':len(rows),'legacy_closed_winrate_pct':_round(wins/len(legacy)*100 if legacy else 0),'legacy_event_return_sum_pct':_round(sum(legacy)),'legacy_average_trade_return_pct':_round(statistics.mean(legacy) if legacy else 0),'legacy_best_trade_return_pct':_round(max(legacy) if legacy else 0),'legacy_worst_trade_return_pct':_round(min(legacy) if legacy else 0),'legacy_metric_interpretation':LEGACY_INTERPRETATION},'reconciliation_summary':{'trades':[{k:v for k,v in t.items() if k not in ('opened_dt','closed_dt')} for t in trades],'valid_closed_trades':len(valid),'excluded_rows':sum(1 for t in trades if not t['valid'])},'closed_trade_statistics':_stats(valid),'data_quality':{'missing_symbol':sum('missing_symbol'in t['exclusion_reasons'] for t in trades),'missing_side':sum('missing_side'in t['exclusion_reasons'] for t in trades),'missing_entry_price':sum('missing_entry_price'in t['exclusion_reasons'] for t in trades),'missing_exit_current_price':sum('missing_exit_price'in t['exclusion_reasons'] for t in trades),'missing_stored_pnl':sum(t['stored_return_pct'] is None for t in trades),'missing_opened_at':sum('missing_opened_at'in t['exclusion_reasons'] for t in trades),'missing_closed_at':sum('missing_closed_at'in t['exclusion_reasons'] for t in trades),'invalid_timestamp_ordering':sum('invalid_timestamp_ordering'in t['exclusion_reasons'] for t in trades),'zero_or_negative_entry_prices':sum('zero_or_negative_entry_price'in t['exclusion_reasons'] for t in trades),'non_finite_returns':sum(t['return_pct'] is None for t in trades),'extreme_return_count':len(out),'duplicate_primary_ids':len(rows)-len({t['trade_id'] for t in trades}),'duplicate_economic_events':sum(1 for r in overlap_rows if r['duplicate_event_flag']),'excluded_rows':[{'trade_id':t['trade_id'],'reasons':t['exclusion_reasons']} for t in trades if not t['valid']]},'overlap_audit':{'maximum_simultaneous_active_trades':max([r['total_concurrent_count'] for r in overlap_rows], default=0),'same_symbol_overlapping_trades':sum(1 for r in overlap_rows if r['same_symbol_overlap_count']>0),'overlap_event_return_sum_pct':_round(sum(t['return_pct'] for t in valid if any(r['trade_id']==t['trade_id'] and r['same_symbol_overlap_count']>0 for r in overlap_rows))),'overlap_csv':overlap_path},'one_symbol_policy_scenario':{'original_valid_trade_count':len(valid),'accepted_trade_count':len(accepted),'overlap_rejected_trade_count':len(rejected),'original_event_return_sum':_round(sum(t['return_pct'] for t in valid)),'filtered_event_return_sum':_round(sum(t['return_pct'] for t in accepted)),'original_winrate':_stats(valid)['winrate'],'filtered_winrate':_stats(accepted)['winrate'],'original_profit_factor':_stats(valid)['profit_factor'],'filtered_profit_factor':_stats(accepted)['profit_factor'],'overlap_inflation_delta':_round(sum(t['return_pct'] for t in valid)-sum(t['return_pct'] for t in accepted))},'equal_allocation_capital_scenario':cap,'outlier_analysis':{'outlier_count':len(out),'outlier_trade_ids':[t['trade_id'] for t in out],'outlier_symbols':sorted({t['symbol'] for t in out}),'return_contribution_with_outliers':_round(sum(t['return_pct'] for t in valid)),'return_contribution_without_outliers':_round(sum(t['return_pct'] for t in no_out)),'winrate_with_outliers':_stats(valid)['winrate'],'winrate_without_outliers':_stats(no_out)['winrate'],'profit_factor_with_outliers':_stats(valid)['profit_factor'],'profit_factor_without_outliers':_stats(no_out)['profit_factor']},'breakdowns':{'symbol':{},'side':{},'score_bucket':{'enrichment_status':'UNAVAILABLE'},'regime':{'enrichment_status':'UNAVAILABLE'},'exit_reason':{},'holding_period_bucket':{},'calendar_day':{},'calendar_week':{}},'readiness':{'engineering_readiness':{'data_pipeline':'PASS','database_readability':'PASS' if conn or rows else 'REVIEW','audit_execution':'PASS','PAPER_ONLY_governance':'PASS','report_generation':'PASS'},'economic_readiness':{'sample_adequacy':'PASS' if len(valid)>=100 else 'REVIEW','data_quality_adequacy':'PASS' if not any(not t['valid'] for t in trades) else 'BLOCKED_DATA_QUALITY','positive_expectancy':'PASS' if _stats(valid)['expectancy_per_closed_trade']>0 else 'BLOCKED_NEGATIVE_EXPECTANCY','normalized_scenario_return':'PASS' if cap.get('scenario_status')=='COMPLETED' else 'BLOCKED_DATA_QUALITY','cost_adjusted_result':'REVIEW','concentration':'REVIEW','overlap_dependence':'REVIEW' if rejected else 'PASS','outlier_dependence':'BLOCKED_OUTLIER_DEPENDENCE' if out else 'PASS'},'execution_allowed':False,'automatic_promotion_allowed':False,'paper_only':True,'readiness_advisory_only':True},'terminology_recommendations':{'Net PnL':'Event Return Sum','Cumulative Shadow PnL':'Cumulative Event Return','add':['Capital-Normalized Scenario Return','Economic Readiness'],'retain':['Engineering Readiness']},'artifact_paths':{'json':output_path,'equity_curve_csv':equity_curve_path,'overlap_audit_csv':overlap_path}}
    # simple breakdowns
    for key,name in [('symbol','symbol'),('side','side_normalized'),('exit_reason','exit_reason')]:
        for val in sorted({t[name] for t in valid}): report['breakdowns'][key][str(val)]=_stats([t for t in valid if t[name]==val])
    if write_reports:
        _atomic_json(output_path, report); _write_csv(overlap_path, overlap_rows, fields); _write_csv(equity_curve_path, cap.get('equity_curve',[]), eqfields)
    return report

def format_paper_economic_reconciliation(report):
    lm=report.get('legacy_metrics',{}); cap=report.get('equal_allocation_capital_scenario',{})
    return '\n'.join(['PAPER ECONOMIC RECONCILIATION',f"Paper Only: {report.get('governance',{}).get('paper_only')}",f"Closed Trades: {lm.get('closed_trade_count',0)}",f"Legacy Event Return Sum: {lm.get('legacy_event_return_sum_pct')}%",f"Legacy Interpretation: {lm.get('legacy_metric_interpretation')}",f"Capital Scenario Status: {cap.get('scenario_status')}",f"Normalized Net Return: {cap.get('normalized_net_return_pct')}"])

if __name__=='__main__': print(format_paper_economic_reconciliation(generate_paper_economic_reconciliation()))
