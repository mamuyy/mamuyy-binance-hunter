import json, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from config import config
from database import init_db
from json_utils import atomic_write_json
from symbol_validation import validate_symbol

BASE_URL = os.getenv('BINANCE_BASE_URL', 'https://fapi.binance.com')
INTERVAL = os.getenv('DATA_SYNC_INTERVAL', '15m')
REPORT = Path('reports/market_data_sync_report.json')

def _ms(dt): return int(dt.timestamp()*1000)
def _iso(ms): return datetime.fromtimestamp(int(ms)/1000, tz=timezone.utc).isoformat()

def fetch_exchange_info(base_url=BASE_URL):
    return requests.get(base_url + '/fapi/v1/exchangeInfo', timeout=config.request_timeout_seconds).json()

def _open_candidate_symbols(reports_dir=Path('reports')):
    syms=set()
    paths=[reports_dir/'binance_candidate_queue.json'] + list((reports_dir/'candidate_batches').glob('*.json')) if (reports_dir/'candidate_batches').exists() else [reports_dir/'binance_candidate_queue.json']
    for path in paths:
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('status') == 'OPEN' or path.name == 'binance_candidate_queue.json':
                syms |= {str(c.get('symbol','')).upper() for c in data.get('candidates', [])}
        except Exception: pass
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

def sync_market_data(db_path='mamuyy_hunter.db', base_url=BASE_URL, output=REPORT):
    init_db(db_path)
    exchange_info=fetch_exchange_info(base_url)
    symbols, rejected = build_universe(exchange_info)
    overlap=int(os.getenv('DATA_SYNC_OVERLAP_HOURS','3'))
    end=datetime.now(timezone.utc)
    inserted=0; errors={}; per={}
    with sqlite3.connect(db_path) as conn:
        for sym in symbols:
            row=conn.execute("SELECT MAX(timestamp) FROM historical_klines WHERE symbol=? AND interval=?", (sym, INTERVAL)).fetchone()
            latest=datetime.fromisoformat(row[0]) if row and row[0] else end-timedelta(hours=overlap)
            start=latest-timedelta(hours=overlap)
            params={'symbol': sym, 'interval': INTERVAL, 'startTime': _ms(start), 'endTime': _ms(end), 'limit': 1500}
            try:
                rows=requests.get(base_url+'/fapi/v1/klines', params=params, timeout=config.request_timeout_seconds).json()
                before=conn.total_changes
                for k in rows if isinstance(rows, list) else []:
                    conn.execute("""INSERT OR IGNORE INTO historical_klines(timestamp,symbol,interval,open,high,low,close,volume,quote_asset_volume,number_of_trades,taker_buy_base_asset_volume,taker_buy_quote_asset_volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (_iso(k[6]), sym, INTERVAL, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]), float(k[8]), float(k[9]), float(k[10])))
                conn.commit(); per[sym]={'requested_start': start.isoformat(), 'rows_received': len(rows) if isinstance(rows, list) else 0, 'inserted': conn.total_changes-before}; inserted += per[sym]['inserted']
                time.sleep(0.01)
            except Exception as exc:
                errors[sym]=str(exc)
    report={'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'LIGHTWEIGHT_KLINE_SYNC_ONLY', 'database_path': str(db_path), 'symbols': symbols, 'rejected_symbols': rejected, 'overlap_hours': overlap, 'candles_inserted': inserted, 'per_symbol': per, 'errors': errors, 'governance': {'paper_only': True, 'writes_to_broker': False, 'execution_allowed': False, 'automatic_promotion_allowed': False}}
    atomic_write_json(output, report)
    return report

def main():
    r=sync_market_data(config.database_url or config.database_path)
    print(f"Market data sync inserted={r['candles_inserted']} errors={len(r['errors'])}")
    return 1 if r['errors'] else 0
if __name__=='__main__': raise SystemExit(main())
