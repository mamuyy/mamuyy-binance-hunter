import argparse, json, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from data_freshness_guard import check_freshness
from json_utils import atomic_write_json
from symbol_validation import validate_symbol

QUEUE_PATH=Path('reports/binance_candidate_queue.json'); OUTPUT_PATH=Path('reports/candidate_validation_report.json'); DB_PATH=Path('mamuyy_hunter.db'); HORIZONS_HOURS=[24,48,72]
def parse_ts(v): return datetime.fromisoformat(str(v).replace('Z','+00:00'))
def nearest_price_after(conn, symbol, target_ts):
    r=conn.execute('SELECT timestamp, close FROM historical_klines WHERE symbol=? AND timestamp>=? AND close IS NOT NULL ORDER BY timestamp ASC LIMIT 1', (symbol, target_ts.isoformat())).fetchone()
    return (str(r[0]), float(r[1])) if r else None

def validate_candidate(conn, item, freshness=None, now=None):
    freshness = freshness or {'validation_allowed': True, 'status': 'GREEN', 'reasons': []}
    now=now or datetime.now(timezone.utc); symbol=str(item.get('symbol','')).upper(); base=float(item.get('price') or 0); signal_ts=parse_ts(item.get('timestamp')); score=float(item.get('score') or 0); horizons={}
    symval=validate_symbol(symbol)
    global_block=not freshness.get('validation_allowed', False)
    for hours in HORIZONS_HOURS:
        target=signal_ts+timedelta(hours=hours); key=f'{hours}h'
        if now < target: horizons[key]={'status':'PENDING_NOT_MATURE','target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'return_pct':None,'direction_hit':None}; continue
        if not symval.valid: horizons[key]={'status':'BLOCKED_INVALID_SYMBOL','blocked_reason':symval.reason,'target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'return_pct':None,'direction_hit':None}; continue
        if global_block and freshness.get('status') in {'BLOCKED_STALE_DATA','BLOCKED_CAPACITY'}: horizons[key]={'status':'BLOCKED_STALE_DATA','blocked_reason':','.join(freshness.get('reasons',[])),'target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'return_pct':None,'direction_hit':None}; continue
        found=nearest_price_after(conn, symbol, target)
        if found is None or base<=0: horizons[key]={'status':'BLOCKED_MISSING_DATA','target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'return_pct':None,'direction_hit':None}; continue
        ots, op=found; ret=((op-base)/base)*100; horizons[key]={'status':'READY','target_timestamp':target.isoformat(),'observed_timestamp':ots,'observed_price':op,'return_pct':round(ret,4),'direction_hit': bool((score>=85)==(ret>0))}
    statuses=[h['status'] for h in horizons.values()]; ready=[h['direction_hit'] for h in horizons.values() if h['status']=='READY']
    cstatus='READY' if statuses and all(s=='READY' for s in statuses) else 'PARTIALLY_READY' if ready else 'PENDING' if any(s=='PENDING_NOT_MATURE' for s in statuses) else 'BLOCKED'
    return {'rank': item.get('rank'), 'symbol': symbol, 'signal_timestamp': item.get('timestamp'), 'base_price': base, 'score': score, 'predicted_direction': 'UP' if score>=85 else 'DOWN', 'regime_name': item.get('regime_name'), 'whale_activity': item.get('whale_activity'), 'horizons': horizons, 'direction_accuracy': round(sum(1 for x in ready if x)/len(ready)*100,2) if ready else None, 'status': cstatus, 'blocked_reason': next((h.get('blocked_reason') or h['status'] for h in horizons.values() if h['status'].startswith('BLOCKED')), None)}

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--input', default=str(QUEUE_PATH)); ap.add_argument('--output', default=str(OUTPUT_PATH)); ns=ap.parse_args(argv)
    data=json.loads(Path(ns.input).read_text(encoding='utf-8')); candidates=data.get('candidates', [])
    freshness=check_freshness(str(DB_PATH), Path(ns.input)); results=[]
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn: results=[validate_candidate(conn,c,freshness) for c in candidates]
    ready_h=sum(1 for r in results for h in r['horizons'].values() if h['status']=='READY')
    report={'phase':'Phase 9D.1A Candidate Validation','mode':'READ_ONLY_ANALYTICS','source_queue':ns.input,'source_db':str(DB_PATH),'candidate_count':len(results),'ready_count':sum(1 for r in results if r['status'] in {'READY','PARTIALLY_READY'}),'pending_count':sum(1 for r in results if r['status']=='PENDING'),'blocked_count':sum(1 for r in results if r['status']=='BLOCKED'),'ready_horizon_count':ready_h,'freshness_status':freshness.get('status'),'governance':{'paper_only':True,'writes_to_database':False,'writes_to_broker':False,'execution_allowed':False,'automatic_promotion_allowed':False},'results':results}
    atomic_write_json(ns.output, report); print(f"Report generated: {ns.output}")
if __name__=='__main__': main()
