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
    candidates, diag=fetch_candidates(db)
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
    sig_ts=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(),'BTCUSDT','15m',100))
        c.execute("INSERT INTO historical_klines(timestamp,symbol,interval,close) VALUES(?,?,?,?)", ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'ETHUSDT','15m',100))
    queue={'status':'OPEN','candidates':[{'rank':1,'symbol':'BTCUSDT','timestamp':sig_ts,'price':100,'score':90},{'rank':2,'symbol':'ETHUSDT','timestamp':datetime.now(timezone.utc).isoformat(),'price':100,'score':90}]}
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
        m=Mock(); m.json.return_value = ex if 'exchangeInfo' in url else k; return m
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
