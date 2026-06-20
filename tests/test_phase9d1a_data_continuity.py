import json, sqlite3, time, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

from database import init_db, insert_signal, insert_flow_log
from binance_candidate_queue_v1 import fetch_candidates, build_report, write_reports
from candidate_validator import main as validator_main
from market_data_sync import sync_market_data
from infrastructure_capacity import classify_usage, assert_heavy_job_allowed


def test_lineage_defaults_and_live_writes(tmp_path):
    db=tmp_path/'x.db'; init_db(str(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO signals(timestamp,symbol,score) VALUES(?,?,?)", ('2026-01-01T00:00:00+00:00','OLDUSDT',1))
    insert_signal({'timestamp':'2026-01-02T00:00:00+00:00','symbol':'BTCUSDT','score':90}, str(db))
    insert_flow_log({'timestamp':'2026-01-02T00:00:00+00:00','symbol':'BTCUSDT'}, str(db))
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT data_source FROM signals WHERE symbol='OLDUSDT'").fetchone()[0] == 'LEGACY_UNKNOWN'
        assert c.execute("SELECT data_source FROM signals WHERE symbol='BTCUSDT'").fetchone()[0] == 'LIVE_SCANNER'
        assert c.execute("SELECT data_source FROM flow_logs WHERE symbol='BTCUSDT'").fetchone()[0] == 'LIVE_SCANNER'


def test_queue_live_only_and_no_snapshot(tmp_path, monkeypatch):
    db=tmp_path/'q.db'; init_db(str(db)); now=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as c:
        for sym,src in [('BTCUSDT','LIVE_SCANNER'),('ETHUSDT','HISTORICAL_BACKFILL'),('BNBUSDT','LEGACY_UNKNOWN')]:
            c.execute("INSERT INTO signals(timestamp,symbol,price,score,pressure_score,squeeze_risk,funding_warning,data_source) VALUES(?,?,?,?,?,?,?,?)", (now,sym,100,90,80,'LOW','',src))
    candidates, diag=fetch_candidates(db, exchange_info={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]})
    assert [c['symbol'] for c in candidates] == ['BTCUSDT']
    assert diag['historical_rows_excluded'] == 1 and diag['legacy_rows_excluded'] == 1
    assert not (tmp_path/'tmp/mamuyy_hunter_candidate_queue_snapshot.db').exists()


def test_batch_archive_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report=build_report([], {'live_rows_considered':0,'historical_rows_excluded':0,'legacy_rows_excluded':0,'rejected_symbol_count':0,'rejection_reasons':{}})
    p=write_reports(report); m=p.stat().st_mtime_ns; time.sleep(0.001); p2=write_reports(report)
    assert p == p2 and p.stat().st_mtime_ns == m
    assert Path('reports/binance_candidate_queue.json').exists()


def test_validator_custom_paths_and_statuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); db=tmp_path/'mamuyy_hunter.db'; init_db(str(db))
    sig_dt=datetime.now(timezone.utc)-timedelta(hours=25); sig_ts=sig_dt.isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((sig_dt+timedelta(hours=24, minutes=5)).isoformat(),'BTCUSDT','15m',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(),'BTCUSDT','15m',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'ETHUSDT','15m',100))
    queue={'status':'OPEN','candidates':[{'rank':1,'symbol':'BTCUSDT','timestamp':sig_ts,'price':100,'score':90,'symbol_validation':{'symbol':'BTCUSDT','valid':True,'reason':None}},{'rank':2,'symbol':'ETHUSDT','timestamp':datetime.now(timezone.utc).isoformat(),'price':100,'score':90,'symbol_validation':{'symbol':'ETHUSDT','valid':True,'reason':None}}]}
    inp=tmp_path/'in.json'; out=tmp_path/'out.json'; inp.write_text(json.dumps(queue), encoding='utf-8')
    validator_main(['--input', str(inp), '--output', str(out)])
    data=json.loads(out.read_text())
    assert data['results'][0]['horizons']['24h']['status'] == 'READY'
    assert data['results'][1]['horizons']['24h']['status'] == 'PENDING_NOT_MATURE'


def test_market_sync_klines_only_idempotent(tmp_path, monkeypatch):
    db=tmp_path/'s.db'; init_db(str(db)); ms=int(datetime.now(timezone.utc).timestamp()*1000)
    ex={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]}
    k=[[ms-900000,'1','2','1','1.5','10',ms,'15','1','5','7','0']]
    def fake_get(url, **kwargs):
        m=Mock(); m.status_code=200; m.text=''; m.json.return_value = ex if 'exchangeInfo' in url else k; return m
    monkeypatch.setenv('DATA_SYNC_CORE_SYMBOLS','BTCUSDT')
    with patch('requests.get', fake_get):
        r1=sync_market_data(str(db), 'https://x', tmp_path/'r.json'); r2=sync_market_data(str(db), 'https://x', tmp_path/'r.json')
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM historical_klines').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM signals').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM flow_logs').fetchone()[0] == 0
    assert r1['overlap_hours'] == 3 and r2['candles_inserted'] == 0


def test_capacity_thresholds_and_block(monkeypatch):
    assert classify_usage(69.9) == 'GREEN'; assert classify_usage(70) == 'WATCH'; assert classify_usage(80) == 'WARNING'; assert classify_usage(85) == 'CRITICAL'; assert classify_usage(90) == 'BLOCK_HEAVY_JOBS'
    with patch('shutil.disk_usage', return_value=(100, 90, 10)):
        try:
            assert_heavy_job_allowed('.')
            assert False
        except RuntimeError:
            pass

from data_freshness_guard import check_freshness
from candidate_validator import validate_candidate, nearest_price_after
from symbol_validation import validate_symbol
from market_data_sync import main as sync_main


def _exchange_info():
    return {'symbols':[
        {'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'},
        {'symbol':'HALTUSDT','status':'BREAK','quoteAsset':'USDT','contractType':'PERPETUAL'},
        {'symbol':'BTCBUSD','status':'TRADING','quoteAsset':'BUSD','contractType':'PERPETUAL'},
        {'symbol':'BTCUSD_240628','status':'TRADING','quoteAsset':'USDT','contractType':'CURRENT_QUARTER'},
    ]}


def test_symbol_validation_fail_closed_and_reasons():
    ex=_exchange_info()
    assert validate_symbol('BTCUSDT', ex).valid
    assert validate_symbol('NOPEUSDT', ex).reason == 'SYMBOL_NOT_FOUND'
    assert validate_symbol('HALTUSDT', ex).reason == 'SYMBOL_NOT_TRADING'
    assert validate_symbol('BTCBUSD', ex).reason == 'UNSUPPORTED_QUOTE_ASSET'
    assert validate_symbol('BTCUSD_240628', ex).reason == 'UNSUPPORTED_CONTRACT'
    assert validate_symbol('SKHYNIXUSDT', ex).reason == 'POLICY_DENYLIST'
    assert validate_symbol('BTCUSDT', None).reason == 'EXCHANGE_INFO_UNAVAILABLE'


def test_queue_exchange_info_fail_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db=tmp_path/'q2.db'; init_db(str(db)); now=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO signals(timestamp,symbol,price,score,pressure_score,squeeze_risk,funding_warning,data_source) VALUES(?,?,?,?,?,?,?,?)", (now,'BTCUSDT',100,90,80,'LOW','','LIVE_SCANNER'))
    with patch('requests.get', side_effect=RuntimeError('offline')):
        candidates, diag=fetch_candidates(db, exchange_info=None)
    assert candidates == []
    assert diag['rejection_reasons'].get('EXCHANGE_INFO_CACHE_MISSING') == 1


def test_freshness_stale_missing_and_future_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); db=tmp_path/'f.db'; init_db(str(db))
    stale=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(); future=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (stale,'BTCUSDT','15m',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (future,'BTCUSDT','15m',100))
    q=tmp_path/'q.json'; q.write_text(json.dumps({'candidates':[{'symbol':'BTCUSDT'},{'symbol':'ETHUSDT'}]}), encoding='utf-8')
    report=check_freshness(str(db), q, max_age_minutes=30)
    assert report['status'] == 'BLOCKED_MISSING_SYMBOL'
    assert not report['validation_allowed']
    assert 'ETHUSDT' in report['missing_symbols']
    assert report['future_timestamp_count'] == 1
    q.write_text(json.dumps({'candidates':[{'symbol':'BTCUSDT'}]}), encoding='utf-8')
    report=check_freshness(str(db), q, max_age_minutes=30)
    assert report['status'] == 'BLOCKED_STALE_DATA'
    assert not report['validation_allowed']


def test_validator_honors_all_freshness_blocks_and_tolerance(tmp_path, monkeypatch):
    db=tmp_path/'v.db'; init_db(str(db)); signal_ts=datetime.now(timezone.utc)-timedelta(hours=25); target=signal_ts+timedelta(hours=24)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((target+timedelta(minutes=16)).isoformat(),'BTCUSDT','15m',101))
        item={'symbol':'BTCUSDT','timestamp':signal_ts.isoformat(),'price':100,'score':90,'symbol_validation':{'symbol':'BTCUSDT','valid':True,'reason':None}}
        monkeypatch.setenv('CANDIDATE_VALIDATION_MAX_OBSERVATION_LAG_MINUTES','20')
        ready=validate_candidate(c, item, {'validation_allowed':True,'status':'GREEN','reasons':[]})
        assert ready['horizons']['24h']['status'] == 'READY'
        assert ready['horizons']['24h']['observed_lag_minutes'] == 16
        monkeypatch.setenv('CANDIDATE_VALIDATION_MAX_OBSERVATION_LAG_MINUTES','10')
        blocked=validate_candidate(c, item, {'validation_allowed':True,'status':'GREEN','reasons':[]})
        assert blocked['horizons']['24h']['status'] == 'BLOCKED_MISSING_DATA'
        stale=validate_candidate(c, item, {'validation_allowed':False,'status':'BLOCKED_STALE_DATA','reasons':['GLOBAL_STALE_DATA']})
        assert stale['horizons']['24h']['status'] == 'BLOCKED_STALE_DATA'
        missing=validate_candidate(c, item, {'validation_allowed':False,'status':'BLOCKED_MISSING_SYMBOL','missing_symbols':['BTCUSDT'],'reasons':['MISSING_CANDIDATE_SYMBOLS']})
        assert missing['horizons']['24h']['blocked_reason'] == 'BLOCKED_MISSING_SYMBOL'
        cap=validate_candidate(c, item, {'validation_allowed':False,'status':'BLOCKED_CAPACITY','reasons':['CAPACITY_BLOCK']})
        assert cap['horizons']['24h']['blocked_reason'] == 'BLOCKED_CAPACITY'


def test_sync_pagination_retry_incomplete_candidate_earliest_and_capacity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); db=tmp_path/'sync.db'; init_db(str(db))
    old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    Path('reports').mkdir(); Path('reports/binance_candidate_queue.json').write_text(json.dumps({'status':'OPEN','candidates':[{'symbol':'BTCUSDT','timestamp':old}]}), encoding='utf-8')
    monkeypatch.setenv('DATA_SYNC_CORE_SYMBOLS','BTCUSDT'); monkeypatch.setenv('DATA_SYNC_MAX_PAGES_PER_SYMBOL','2')
    import market_data_sync as mds
    mds.MAX_PAGES=2; mds.LIMIT=2
    ex={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]}
    base_ms=int((datetime.now(timezone.utc)-timedelta(days=2)).timestamp()*1000)
    calls={'n':0}
    def fake_get(url, **kwargs):
        calls['n']+=1; resp=Mock(); resp.status_code=200; resp.text=''; resp.raise_for_status=lambda: None
        if 'exchangeInfo' in url: resp.json.return_value=ex; return resp
        if calls['n']==2: raise RuntimeError('temporary')
        start=kwargs['params']['startTime']; resp.json.return_value=[[start,'1','2','1','1','1',start+900000,'1','1','1','1','0'],[start+900001,'1','2','1','1','1',start+1800000,'1','1','1','1','0']]; return resp
    with patch('requests.get', fake_get):
        r=sync_market_data(str(db), 'https://x', tmp_path/'r.json')
    assert r['status'] == 'INCOMPLETE_SYNC'
    assert r['per_symbol']['BTCUSDT']['requested_start'][:10] == old[:10]
    assert calls['n'] >= 3
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM signals').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM flow_logs').fetchone()[0] == 0
    with patch('shutil.disk_usage', return_value=(100, 99, 1)):
        r=sync_market_data(str(db), 'https://x', tmp_path/'r2.json')
    assert r['status'] == 'BLOCKED_CAPACITY'


def test_backfill_lineage_helpers(tmp_path):
    from backfill import _insert_signal_if_missing, _insert_flow_if_missing
    db=tmp_path/'b.db'; init_db(str(db))
    with sqlite3.connect(db) as c:
        assert _insert_signal_if_missing(c, {'timestamp':'2026-01-01T00:00:00+00:00','symbol':'BTCUSDT','price':1,'score':1})
        assert _insert_flow_if_missing(c, {'timestamp':'2026-01-01T00:00:00+00:00','symbol':'BTCUSDT'})
        assert c.execute("SELECT data_source FROM signals").fetchone()[0] == 'HISTORICAL_BACKFILL'
        assert c.execute("SELECT data_source FROM flow_logs").fetchone()[0] == 'HISTORICAL_BACKFILL'


def test_queue_persists_rejected_symbol_details(tmp_path):
    db=tmp_path/'q3.db'; init_db(str(db)); now=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO signals(timestamp,symbol,price,score,pressure_score,squeeze_risk,funding_warning,data_source) VALUES(?,?,?,?,?,?,?,?)", (now,'NOPEUSDT',100,90,80,'LOW','','LIVE_SCANNER'))
    candidates, diag=fetch_candidates(db, exchange_info={'symbols':[]})
    assert candidates == []
    assert diag['rejected_symbols'][0]['symbol'] == 'NOPEUSDT'
    assert diag['rejected_symbols'][0]['reason'] == 'SYMBOL_NOT_FOUND'


def test_sync_paginates_more_than_1500_candles_and_no_data_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); db=tmp_path/'p.db'; init_db(str(db)); Path('reports').mkdir()
    monkeypatch.setenv('DATA_SYNC_CORE_SYMBOLS','BTCUSDT')
    import market_data_sync as mds
    mds.MAX_PAGES=3; mds.LIMIT=1500; mds.DEFAULT_CORE_LOOKBACK_HOURS=400
    ex={'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]}
    page_calls={'n':0}
    def page(start, count):
        return [[start+i*900000,'1','2','1','1','1',start+(i+1)*900000,'1','1','1','1','0'] for i in range(count)]
    def fake_get(url, **kwargs):
        resp=Mock(); resp.status_code=200; resp.text=''; resp.raise_for_status=lambda: None
        if 'exchangeInfo' in url: resp.json.return_value=ex; return resp
        page_calls['n'] += 1; start=kwargs['params']['startTime']
        resp.json.return_value=page(start, 1500 if page_calls['n'] == 1 else 10)
        return resp
    with patch('requests.get', fake_get):
        r=sync_market_data(str(db), 'https://x', tmp_path/'p.json')
    assert page_calls['n'] == 2
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM historical_klines').fetchone()[0] == 1510
    def empty_get(url, **kwargs):
        resp=Mock(); resp.status_code=200; resp.text=''; resp.raise_for_status=lambda: None
        resp.json.return_value=ex if 'exchangeInfo' in url else []
        return resp
    with patch('requests.get', empty_get):
        r=sync_market_data(str(tmp_path/'empty.db'), 'https://x', tmp_path/'e.json')
    assert r['status'] == 'INCOMPLETE_SYNC'
    assert 'BTCUSDT' in r['incomplete_symbols']


def test_legacy_batch_uses_fresh_exchange_cache_fallback(tmp_path, monkeypatch):
    from exchange_info_cache import write_exchange_info_cache
    monkeypatch.chdir(tmp_path); db=tmp_path/'mamuyy_hunter.db'; init_db(str(db)); Path('reports').mkdir()
    write_exchange_info_cache({'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]}, Path('reports/binance_futures_exchange_info_cache.json'))
    sig_dt=datetime.now(timezone.utc)-timedelta(hours=25)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((sig_dt+timedelta(hours=24, minutes=5)).isoformat(),'BTCUSDT','15m',101))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(),'BTCUSDT','15m',101))
    queue={'status':'OPEN','candidates':[{'symbol':'BTCUSDT','timestamp':sig_dt.isoformat(),'price':100,'score':90}]}
    inp=tmp_path/'legacy.json'; out=tmp_path/'legacy_out.json'; inp.write_text(json.dumps(queue), encoding='utf-8')
    with patch('requests.get', side_effect=RuntimeError('offline')):
        validator_main(['--input', str(inp), '--output', str(out)])
    data=json.loads(out.read_text())
    assert data['results'][0]['horizons']['24h']['status'] == 'READY'


def test_candidate_batch_state_registry_sidecars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    diag={'live_rows_considered':0,'historical_rows_excluded':0,'legacy_rows_excluded':0,'rejected_symbol_count':0,'rejection_reasons':{},'rejected_symbols':[]}
    report=build_report([], diag)
    batch_path=write_reports(report)
    state_path=batch_path.with_name(batch_path.stem + '.state.json')
    registry_path=Path('reports/candidate_batches/registry.json')
    assert batch_path.exists() and state_path.exists() and registry_path.exists()
    registry=json.loads(registry_path.read_text())
    assert registry['batches'][0]['batch_id'] == report['batch_id']
    assert json.loads(state_path.read_text())['status'] == 'OPEN'


def test_strict_interval_filtering_for_freshness_and_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); db=tmp_path/'i.db'; init_db(str(db)); monkeypatch.setenv('CANDLE_INTERVAL','15m')
    signal_ts=datetime.now(timezone.utc)-timedelta(hours=25); target=signal_ts+timedelta(hours=24)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(),'BTCUSDT','1h',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),'BTCUSDT','15m',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((target+timedelta(minutes=5)).isoformat(),'BTCUSDT','1h',101))
        item={'symbol':'BTCUSDT','timestamp':signal_ts.isoformat(),'price':100,'score':90,'symbol_validation':{'symbol':'BTCUSDT','valid':True,'reason':None}}
        result=validate_candidate(c, item, {'validation_allowed':True,'status':'GREEN','reasons':[]})
    q=tmp_path/'q.json'; q.write_text(json.dumps({'candidates':[{'symbol':'BTCUSDT'}]}), encoding='utf-8')
    freshness=check_freshness(str(db), q, max_age_minutes=30)
    assert freshness['status'] == 'BLOCKED_STALE_DATA'
    assert result['horizons']['24h']['status'] == 'BLOCKED_MISSING_DATA'


def test_exchange_cache_metadata_ttl_malformed_and_queue_empty_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); Path('reports').mkdir(); db=tmp_path/'cache.db'; init_db(str(db)); now=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO signals(timestamp,symbol,price,score,pressure_score,squeeze_risk,funding_warning,data_source) VALUES(?,?,?,?,?,?,?,?)", (now,'BTCUSDT',100,90,80,'LOW','','LIVE_SCANNER'))
    stale=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
    Path('reports/binance_futures_exchange_info_cache.json').write_text(json.dumps({'cached_at':stale,'source':'test','cache_schema':'binance_futures_exchange_info_v1','exchange_info':{'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','contractType':'PERPETUAL'}]}}), encoding='utf-8')
    monkeypatch.setenv('EXCHANGE_INFO_CACHE_TTL_MINUTES','1')
    with patch('requests.get', side_effect=RuntimeError('offline')):
        candidates, diag=fetch_candidates(db, exchange_info=None)
    report=build_report(candidates, diag)
    assert candidates == []
    assert diag['exchange_info']['reason'] == 'EXCHANGE_INFO_CACHE_STALE'
    assert report['empty_reason'] == 'EXCHANGE_INFO_CACHE_STALE'
    Path('reports/binance_futures_exchange_info_cache.json').write_text('{bad json', encoding='utf-8')
    with patch('requests.get', side_effect=RuntimeError('offline')):
        candidates, diag=fetch_candidates(db, exchange_info=None)
    report=build_report(candidates, diag)
    assert diag['exchange_info']['reason'] == 'EXCHANGE_INFO_CACHE_MALFORMED'
    assert report['empty_reason'] == 'EXCHANGE_INFO_CACHE_MALFORMED'
