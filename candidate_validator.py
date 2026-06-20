import argparse, json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from data_freshness_guard import check_freshness
from json_utils import atomic_write_json
from symbol_validation import validate_symbol, SymbolValidationResult
from database import sqlite_path
from exchange_info_cache import get_exchange_info
from interval_config import operational_kline_interval, interval_minutes
from candidate_batch_state import update_state_from_validation

QUEUE_PATH=Path('reports/binance_candidate_queue.json'); OUTPUT_PATH=Path('reports/candidate_validation_report.json'); DB_PATH=Path('mamuyy_hunter.db'); HORIZONS_HOURS=[24,48,72]

def parse_ts(v): return datetime.fromisoformat(str(v).replace('Z','+00:00'))

def max_observation_lag_minutes() -> int:
    explicit = os.getenv('CANDIDATE_VALIDATION_MAX_OBSERVATION_LAG_MINUTES')
    if explicit:
        return int(explicit)
    return interval_minutes() + 5

def nearest_price_after(conn, symbol, target_ts, max_lag_minutes: int | None = None, interval: str | None = None):
    lag = max_lag_minutes if max_lag_minutes is not None else max_observation_lag_minutes()
    latest_allowed = target_ts + timedelta(minutes=lag)
    use_interval = interval or operational_kline_interval()
    r=conn.execute('SELECT timestamp, close FROM historical_klines WHERE symbol=? AND interval=? AND timestamp>=? AND timestamp<=? AND close IS NOT NULL ORDER BY timestamp ASC LIMIT 1', (symbol, use_interval, target_ts.isoformat(), latest_allowed.isoformat())).fetchone()
    if not r: return None
    observed_ts = parse_ts(str(r[0])); observed_price = float(r[1])
    return str(r[0]), observed_price, round((observed_ts - target_ts).total_seconds()/60, 2)

def _validation_from_item(item: dict[str, Any], exchange_info: dict[str, Any] | None, exchange_reason: str | None = None) -> SymbolValidationResult:
    symbol = str(item.get('symbol','')).upper()
    evidence = item.get('symbol_validation') if isinstance(item.get('symbol_validation'), dict) else None
    if evidence and evidence.get('symbol') == symbol:
        return SymbolValidationResult(symbol, bool(evidence.get('valid')), evidence.get('reason'))
    if exchange_info is None:
        return SymbolValidationResult(symbol, False, exchange_reason or 'EXCHANGE_INFO_UNAVAILABLE')
    return validate_symbol(symbol, exchange_info)

def _blocked_status_from_freshness(freshness: dict[str, Any], symbol: str) -> tuple[str, str]:
    status = freshness.get('status')
    reasons = freshness.get('reasons', []) or []
    if status == 'BLOCKED_CAPACITY' or 'CAPACITY_BLOCK' in reasons:
        return 'BLOCKED_STALE_DATA', 'BLOCKED_CAPACITY'
    if status == 'BLOCKED_MISSING_SYMBOL' or symbol in set(freshness.get('missing_symbols', []) or []):
        return 'BLOCKED_MISSING_DATA', 'BLOCKED_MISSING_SYMBOL'
    return 'BLOCKED_STALE_DATA', status or ','.join(reasons) or 'FRESHNESS_BLOCKED'

def validate_candidate(conn, item, freshness=None, now=None, exchange_info=None, exchange_reason=None):
    freshness = freshness or {'validation_allowed': True, 'status': 'GREEN', 'reasons': []}
    now=now or datetime.now(timezone.utc); symbol=str(item.get('symbol','')).upper(); base=float(item.get('price') or 0); signal_ts=parse_ts(item.get('timestamp')); score=float(item.get('score') or 0); horizons={}
    symval=_validation_from_item(item, exchange_info, exchange_reason)
    global_block=not freshness.get('validation_allowed', False)
    for hours in HORIZONS_HOURS:
        target=signal_ts+timedelta(hours=hours); key=f'{hours}h'
        if now < target: horizons[key]={'status':'PENDING_NOT_MATURE','target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'observed_lag_minutes':None,'return_pct':None,'direction_hit':None}; continue
        if not symval.valid: horizons[key]={'status':'BLOCKED_INVALID_SYMBOL','blocked_reason':symval.reason,'target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'observed_lag_minutes':None,'return_pct':None,'direction_hit':None}; continue
        if global_block:
            hstatus, reason = _blocked_status_from_freshness(freshness, symbol)
            horizons[key]={'status':hstatus,'blocked_reason':reason,'target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'observed_lag_minutes':None,'return_pct':None,'direction_hit':None}; continue
        found=nearest_price_after(conn, symbol, target)
        if found is None or base<=0: horizons[key]={'status':'BLOCKED_MISSING_DATA','blocked_reason':'NO_MARKET_DATA_WITHIN_TOLERANCE','target_timestamp':target.isoformat(),'observed_timestamp':None,'observed_price':None,'observed_lag_minutes':None,'return_pct':None,'direction_hit':None}; continue
        ots, op, lag=found; ret=((op-base)/base)*100; horizons[key]={'status':'READY','target_timestamp':target.isoformat(),'observed_timestamp':ots,'observed_price':op,'observed_lag_minutes':lag,'return_pct':round(ret,4),'direction_hit': bool((score>=85)==(ret>0))}
    statuses=[h['status'] for h in horizons.values()]; ready=[h['direction_hit'] for h in horizons.values() if h['status']=='READY']
    cstatus='READY' if statuses and all(s=='READY' for s in statuses) else 'PARTIALLY_READY' if ready else 'PENDING' if any(s=='PENDING_NOT_MATURE' for s in statuses) else 'BLOCKED'
    return {'rank': item.get('rank'), 'symbol': symbol, 'signal_timestamp': item.get('timestamp'), 'base_price': base, 'score': score, 'predicted_direction': 'UP' if score>=85 else 'DOWN', 'regime_name': item.get('regime_name'), 'whale_activity': item.get('whale_activity'), 'horizons': horizons, 'direction_accuracy': round(sum(1 for x in ready if x)/len(ready)*100,2) if ready else None, 'status': cstatus, 'blocked_reason': next((h.get('blocked_reason') or h['status'] for h in horizons.values() if h['status'].startswith('BLOCKED')), None)}

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--input', default=str(QUEUE_PATH)); ap.add_argument('--output', default=str(OUTPUT_PATH)); ns=ap.parse_args(argv)
    data=json.loads(Path(ns.input).read_text(encoding='utf-8')); candidates=data.get('candidates', [])
    freshness=check_freshness(str(DB_PATH), Path(ns.input)); results=[]
    needs_exchange = any(not isinstance(c.get('symbol_validation'), dict) for c in candidates)
    exchange_result = get_exchange_info(os.getenv('BINANCE_BASE_URL', 'https://fapi.binance.com')) if needs_exchange else None
    exchange_info = exchange_result.exchange_info if exchange_result else None
    exchange_reason = exchange_result.reason if exchange_result else None
    with sqlite3.connect(f'file:{sqlite_path(str(DB_PATH))}?mode=ro', uri=True) as conn: results=[validate_candidate(conn,c, freshness, exchange_info=exchange_info, exchange_reason=exchange_reason) for c in candidates]
    ready_h=sum(1 for r in results for h in r['horizons'].values() if h['status']=='READY')
    report={'phase':'Phase 9D.1A Candidate Validation', 'interval': operational_kline_interval(),'mode':'READ_ONLY_ANALYTICS','source_queue':ns.input,'source_db':str(DB_PATH),'candidate_count':len(results),'ready_count':sum(1 for r in results if r['status'] in {'READY','PARTIALLY_READY'}),'pending_count':sum(1 for r in results if r['status']=='PENDING'),'blocked_count':sum(1 for r in results if r['status']=='BLOCKED'),'ready_horizon_count':ready_h,'freshness_status':freshness.get('status'),'governance':{'paper_only':True,'writes_to_database':False,'writes_to_broker':False,'execution_allowed':False,'automatic_promotion_allowed':False},'results':results}
    atomic_write_json(ns.output, report)
    update_state_from_validation(report, ns.output, archive_path=ns.input)
    print(f"Report generated: {ns.output}")
if __name__=='__main__': main()
