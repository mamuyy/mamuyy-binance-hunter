import json, sqlite3, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from config import config
from json_utils import atomic_write_json
from infrastructure_capacity import build_capacity_report
from database import sqlite_path
from interval_config import operational_kline_interval

def _parse(v): return datetime.fromisoformat(str(v).replace('Z','+00:00')) if v else None
def _age(ts, now): return round((now-ts).total_seconds()/60,2) if ts else None

def load_candidates(path=Path('reports/binance_candidate_queue.json')):
    try: return json.loads(Path(path).read_text(encoding='utf-8')).get('candidates', [])
    except Exception: return []

def check_freshness(db_path='mamuyy_hunter.db', candidate_path=Path('reports/binance_candidate_queue.json'), max_age_minutes=None):
    now=datetime.now(timezone.utc); max_age=max_age_minutes or int(os.getenv('DATA_FRESHNESS_MAX_AGE_MINUTES','30')); interval=operational_kline_interval()
    reasons=[]; missing=[]; stale=[]; future=0; latest=None; per={}
    candidates=load_candidates(candidate_path); symbols=sorted({str(c.get('symbol','')).upper() for c in candidates if c.get('symbol')})
    try:
        with sqlite3.connect(f'file:{sqlite_path(str(db_path))}?mode=ro', uri=True) as conn:
            conn.row_factory=sqlite3.Row
            latest=_parse(conn.execute('SELECT MAX(timestamp) FROM historical_klines WHERE interval=?', (interval,)).fetchone()[0])
            future=conn.execute('SELECT COUNT(*) FROM historical_klines WHERE interval=? AND timestamp > ?', (interval, now.isoformat())).fetchone()[0]
            for sym in symbols:
                ts=_parse(conn.execute('SELECT MAX(timestamp) FROM historical_klines WHERE symbol=? AND interval=?', (sym, interval)).fetchone()[0])
                per[sym]={'latest': ts.isoformat() if ts else None, 'age_minutes': _age(ts, now)}
                if ts is None: missing.append(sym)
                elif _age(ts, now) > max_age: stale.append(sym)
    except Exception as exc:
        reasons.append(f'DATABASE_UNREADABLE:{exc}')
    global_age=_age(latest, now)
    if latest is None: reasons.append('NO_GLOBAL_KLINES')
    elif global_age is not None and global_age > max_age: reasons.append('GLOBAL_STALE_DATA')
    if missing: reasons.append('MISSING_CANDIDATE_SYMBOLS')
    if stale: reasons.append('STALE_CANDIDATE_SYMBOLS')
    if future: reasons.append('FUTURE_TIMESTAMPS')
    cap=build_capacity_report(db_path=db_path)
    if cap['status']=='BLOCK_HEAVY_JOBS': reasons.append('CAPACITY_BLOCK')
    covered=len(symbols)-len(missing)
    coverage=round((covered/len(symbols))*100,2) if symbols else 100.0
    status='GREEN'
    if 'CAPACITY_BLOCK' in reasons: status='BLOCKED_CAPACITY'
    elif missing: status='BLOCKED_MISSING_SYMBOL'
    elif any(r in reasons for r in ['GLOBAL_STALE_DATA','NO_GLOBAL_KLINES','FUTURE_TIMESTAMPS','STALE_CANDIDATE_SYMBOLS']): status='BLOCKED_STALE_DATA'
    allowed=status == 'GREEN'
    report={'checked_at': now.isoformat(), 'database_path': str(db_path), 'global_latest_kline': latest.isoformat() if latest else None, 'interval': interval, 'global_age_minutes': global_age, 'candidate_count': len(symbols), 'covered_candidate_count': covered, 'coverage_percent': coverage, 'missing_symbols': missing, 'stale_symbols': stale, 'future_timestamp_count': future, 'capacity_status': cap['status'], 'status': status, 'validation_allowed': allowed, 'analytics_allowed': allowed, 'promotion_allowed': False, 'execution_allowed': False, 'reasons': reasons, 'per_symbol': per, 'governance': {'paper_only': True, 'writes_to_broker': False, 'execution_allowed': False, 'automatic_promotion_allowed': False}}
    return report

def main(output='reports/data_freshness_report.json'):
    r=check_freshness(config.database_url or config.database_path)
    atomic_write_json(output, r); print(f"Freshness status: {r['status']}")
    return 0 if r['validation_allowed'] else 1
if __name__=='__main__': raise SystemExit(main())
