import json, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from config import config
from database import init_db, sqlite_path
from json_utils import atomic_write_json
from symbol_validation import validate_symbol
from infrastructure_capacity import lightweight_sync_allowed

BASE_URL = os.getenv('BINANCE_BASE_URL', 'https://fapi.binance.com')
INTERVAL = os.getenv('DATA_SYNC_INTERVAL', '15m')
REPORT = Path('reports/market_data_sync_report.json')
MAX_PAGES = int(os.getenv('DATA_SYNC_MAX_PAGES_PER_SYMBOL', '10'))
LIMIT = int(os.getenv('DATA_SYNC_KLINE_LIMIT', '1500'))
DEFAULT_CORE_LOOKBACK_HOURS = int(os.getenv('DATA_SYNC_CORE_LOOKBACK_HOURS', '24'))

def _ms(dt): return int(dt.timestamp()*1000)
def _iso(ms): return datetime.fromtimestamp(int(ms)/1000, tz=timezone.utc).isoformat()
def _parse(v): return datetime.fromisoformat(str(v).replace('Z','+00:00'))

def _http_json(url: str, *, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    last = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=config.request_timeout_seconds)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP_{response.status_code}:{getattr(response, 'text', '')[:160]}")
            data = response.json()
            if isinstance(data, dict) and 'code' in data and int(data.get('code', 0)) < 0:
                raise RuntimeError(f"BINANCE_API_ERROR:{data.get('code')}:{data.get('msg')}")
            return data
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.25 * (2 ** attempt))
    raise RuntimeError(str(last))

def fetch_exchange_info(base_url=BASE_URL):
    data = _http_json(base_url + '/fapi/v1/exchangeInfo')
    if not isinstance(data, dict) or not isinstance(data.get('symbols'), list):
        raise RuntimeError('INVALID_EXCHANGE_INFO_RESPONSE')
    return data

def _load_open_batches(reports_dir=Path('reports')):
    paths=[reports_dir/'binance_candidate_queue.json']
    if (reports_dir/'candidate_batches').exists(): paths += list((reports_dir/'candidate_batches').glob('*.json'))
    batches=[]
    for path in paths:
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('status') == 'OPEN' or path.name == 'binance_candidate_queue.json': batches.append(data)
        except Exception: pass
    return batches

def _open_candidate_symbols(reports_dir=Path('reports')):
    syms=set()
    for data in _load_open_batches(reports_dir):
        syms |= {str(c.get('symbol','')).upper() for c in data.get('candidates', []) if c.get('symbol')}
    return syms

def build_universe(exchange_info=None):
    core={s.strip().upper() for s in os.getenv('DATA_SYNC_CORE_SYMBOLS','BTCUSDT,ETHUSDT').split(',') if s.strip()}
    universe=core | _open_candidate_symbols()
    valid=[]; rejected={}
    for sym in sorted(universe):
        res=validate_symbol(sym, exchange_info)
        if res.valid: valid.append(sym)
        else: rejected[sym]=res.reason
    return valid, rejected

def _candidate_earliest(symbol: str, reports_dir=Path('reports')) -> datetime | None:
    earliest=None
    for data in _load_open_batches(reports_dir):
        for c in data.get('candidates', []) or []:
            if str(c.get('symbol','')).upper() != symbol: continue
            try: ts=_parse(c.get('timestamp'))
            except Exception: continue
            earliest = ts if earliest is None or ts < earliest else earliest
    return earliest

def earliest_required_timestamp(conn, symbol: str, now: datetime, overlap_hours: int) -> datetime:
    row=conn.execute("SELECT MAX(timestamp) FROM historical_klines WHERE symbol=? AND interval=?", (symbol, INTERVAL)).fetchone()
    latest=_parse(row[0]) if row and row[0] else None
    candidates=[now - timedelta(hours=DEFAULT_CORE_LOOKBACK_HOURS)]
    if latest: candidates.append(latest - timedelta(hours=overlap_hours))
    cand=_candidate_earliest(symbol)
    if cand: candidates.append(cand)
    return min(candidates)

def _fetch_kline_pages(base_url: str, symbol: str, start: datetime, end: datetime, max_pages: int) -> tuple[list[list[Any]], bool]:
    rows=[]; cursor=_ms(start); end_ms=_ms(end); pages=0
    while cursor <= end_ms and pages < max_pages:
        batch=_http_json(base_url+'/fapi/v1/klines', params={'symbol': symbol, 'interval': INTERVAL, 'startTime': cursor, 'endTime': end_ms, 'limit': LIMIT})
        if not isinstance(batch, list): raise RuntimeError('INVALID_KLINES_RESPONSE')
        pages += 1
        if not batch: break
        rows.extend(batch)
        next_cursor=int(batch[-1][0]) + 1
        if next_cursor <= cursor: break
        cursor = next_cursor
        if len(batch) < LIMIT: break
    complete = cursor > end_ms or (rows and _parse(_iso(rows[-1][6])) >= end - timedelta(minutes=20)) or not rows
    return rows, complete and pages < max_pages or complete

def sync_market_data(db_path='mamuyy_hunter.db', base_url=BASE_URL, output=REPORT):
    path=sqlite_path(str(db_path)); init_db(path)
    min_free=int(os.getenv('DATA_SYNC_MIN_FREE_BYTES','50000000'))
    allowed, capacity = lightweight_sync_allowed(db_path=path, min_free_bytes=min_free)
    if not allowed:
        report={'generated_at': datetime.now(timezone.utc).isoformat(), 'mode':'LIGHTWEIGHT_KLINE_SYNC_ONLY', 'status':'BLOCKED_CAPACITY', 'database_path': path, 'errors': {'capacity':'INSUFFICIENT_FREE_SPACE'}, 'capacity': capacity, 'governance': {'paper_only': True, 'writes_to_broker': False, 'execution_allowed': False, 'automatic_promotion_allowed': False}}
        atomic_write_json(output, report); return report
    exchange_info=fetch_exchange_info(base_url)
    symbols, rejected = build_universe(exchange_info)
    overlap=int(os.getenv('DATA_SYNC_OVERLAP_HOURS','3'))
    end=datetime.now(timezone.utc); inserted=0; errors={}; per={}; incomplete=[]
    with sqlite3.connect(path) as conn:
        for sym in symbols:
            start=earliest_required_timestamp(conn, sym, end, overlap)
            try:
                rows, complete = _fetch_kline_pages(base_url, sym, start, end, MAX_PAGES)
                before=conn.total_changes
                for k in rows:
                    if not isinstance(k, list) or len(k) < 11: raise RuntimeError('INVALID_KLINE_ROW')
                    conn.execute("""INSERT OR IGNORE INTO historical_klines(timestamp,symbol,interval,open,high,low,close,volume,quote_asset_volume,number_of_trades,taker_buy_base_asset_volume,taker_buy_quote_asset_volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (_iso(k[6]), sym, INTERVAL, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]), float(k[8]), float(k[9]), float(k[10])))
                conn.commit(); per[sym]={'requested_start': start.isoformat(), 'rows_received': len(rows), 'inserted': conn.total_changes-before, 'complete': complete}; inserted += per[sym]['inserted']
                if not complete: incomplete.append(sym)
            except Exception as exc:
                errors[sym]=str(exc)
    status='INCOMPLETE_SYNC' if incomplete else 'ERROR' if errors else 'OK'
    report={'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'LIGHTWEIGHT_KLINE_SYNC_ONLY', 'status': status, 'database_path': path, 'symbols': symbols, 'rejected_symbols': rejected, 'overlap_hours': overlap, 'max_pages_per_symbol': MAX_PAGES, 'candles_inserted': inserted, 'per_symbol': per, 'incomplete_symbols': incomplete, 'errors': errors, 'capacity': capacity, 'governance': {'paper_only': True, 'writes_to_broker': False, 'execution_allowed': False, 'automatic_promotion_allowed': False}}
    atomic_write_json(output, report); return report

def main():
    r=sync_market_data(config.database_url or config.database_path)
    print(f"Market data sync status={r.get('status')} inserted={r.get('candles_inserted',0)} errors={len(r.get('errors',{}))}")
    return 0 if r.get('status') == 'OK' else 1
if __name__=='__main__': raise SystemExit(main())
