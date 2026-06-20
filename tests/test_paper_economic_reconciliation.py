import csv, json, sqlite3
from pathlib import Path
from paper_economic_reconciliation import EconomicAuditConfig, generate_paper_economic_reconciliation, LEGACY_INTERPRETATION

DDL='''CREATE TABLE internal_paper_trades(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, entry_price REAL, exit_price REAL, current_price REAL, pnl REAL, confidence REAL, regime TEXT, status TEXT, exit_reason TEXT, updated_at TEXT)'''

def make_db(tmp_path, rows):
    db=tmp_path/'t.db'
    con=sqlite3.connect(db); con.execute(DDL)
    con.executemany('INSERT INTO internal_paper_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    con.commit(); con.close(); return db

def run(tmp_path, rows, cfg=None):
    db=make_db(tmp_path, rows); before=db.read_bytes()
    rep=generate_paper_economic_reconciliation(str(db), str(tmp_path/'r.json'), str(tmp_path/'eq.csv'), str(tmp_path/'ov.csv'), True, cfg or EconomicAuditConfig())
    assert db.read_bytes()==before
    return rep,tmp_path

def test_closed_only_legacy_and_active_exclusions(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-01T00:00:00','ETH','LONG',100,200,200,100,80,'R','TP1 HIT','TP','2026-01-01T01:00:00'),(3,'2026-01-01T00:00:00','XRP','LONG',100,50,50,-50,80,'R','OPEN','TP','2026-01-01T01:00:00')]
    rep,_=run(tmp_path,rows)
    assert rep['legacy_metrics']['closed_trade_count']==1
    assert rep['legacy_metrics']['legacy_event_return_sum_pct']==10
    assert rep['legacy_metrics']['legacy_metric_interpretation']==LEGACY_INTERPRETATION

def test_long_short_and_mismatch_detection(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-01T02:00:00','ETH','SHORT',100,90,90,5,80,'R','CLOSED','TP','2026-01-01T03:00:00')]
    rep,_=run(tmp_path,rows)
    tr={t['trade_id']:t for t in rep['reconciliation_summary']['trades']}
    assert tr[1]['recomputed_return_pct']==10
    assert tr[2]['recomputed_return_pct']==10
    assert tr[2]['reconciliation_status']=='MATERIAL_DIFFERENCE'

def test_missing_exit_and_invalid_timestamps_excluded(tmp_path):
    rows=[(1,'2026-01-01T02:00:00','BTC','LONG',100,None,110,None,80,'R','CLOSED','TP','2026-01-01T01:00:00')]
    rep,_=run(tmp_path,rows)
    assert rep['reconciliation_summary']['valid_closed_trades']==0
    assert rep['data_quality']['missing_exit_current_price']==1
    assert rep['data_quality']['invalid_timestamp_ordering']==1

def test_duplicate_overlap_concurrency_and_one_symbol_policy(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T02:00:00'),(2,'2026-01-01T01:00:00','BTC','LONG',100,105,105,5,80,'R','CLOSED','TP','2026-01-01T03:00:00'),(3,'2026-01-01T01:30:00','ETH','LONG',100,90,90,-10,80,'R','CLOSED','SL','2026-01-01T04:00:00'),(4,'2026-01-01T01:30:00','ETH','LONG',100,90,90,-10,80,'R','CLOSED','SL','2026-01-01T04:00:00')]
    rep,p=run(tmp_path,rows)
    assert rep['overlap_audit']['same_symbol_overlapping_trades']>=4
    assert rep['overlap_audit']['maximum_simultaneous_active_trades']==4
    assert rep['one_symbol_policy_scenario']['accepted_trade_count']==2
    assert rep['one_symbol_policy_scenario']['overlap_rejected_trade_count']==2
    assert rep['data_quality']['duplicate_economic_events']==2
    ov=list(csv.DictReader(open(p/'ov.csv')))
    assert ov and set(ov[0]) >= {'trade_id','symbol','included_in_one_symbol_policy'}

def test_capital_scenario_fees_slippage_cap_and_drawdown(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T02:00:00'),(2,'2026-01-01T00:30:00','ETH','LONG',100,90,90,-10,80,'R','CLOSED','SL','2026-01-01T03:00:00')]
    cfg=EconomicAuditConfig(initial_capital=10000, allocation_pct_per_trade=60, max_gross_exposure_pct=100, round_trip_fee_bps=8, slippage_bps=15)
    rep,p=run(tmp_path,rows,cfg)
    cap=rep['equal_allocation_capital_scenario']
    assert cap['scenario_status']=='COMPLETED'
    assert cap['accepted_trades']==1 and cap['capacity_rejected_trades']==1
    assert cap['total_fees']==4.8 and cap['total_slippage_impact']==9.0
    assert cap['ending_capital']==10586.2 and cap['normalized_net_return_pct']==5.862
    assert cap['maximum_drawdown_pct']>=0
    assert list(csv.DictReader(open(p/'eq.csv')))

def test_outlier_metrics_governance_no_actual_roi_and_json_atomic(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,200,200,100,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-02T00:00:00','ETH','LONG',100,90,90,-10,80,'R','CLOSED','SL','2026-01-02T01:00:00')]
    rep,p=run(tmp_path,rows)
    assert rep['outlier_analysis']['outlier_count']==1
    assert rep['outlier_analysis']['return_contribution_without_outliers']==-10
    assert rep['governance']['paper_only'] is True
    assert rep['governance']['writes_to_broker'] is False
    assert rep['governance']['execution_allowed'] is False
    assert rep['governance']['automatic_promotion_allowed'] is False
    assert rep['equal_allocation_capital_scenario']['assumptions']['not_actual_account_roi'] is True
    assert json.load(open(p/'r.json'))['phase'].startswith('Phase 9D.1B-A')

def test_empty_database_behavior_and_deterministic_csv(tmp_path):
    rep,p=run(tmp_path,[])
    assert rep['legacy_metrics']['closed_trade_count']==0
    assert (p/'ov.csv').read_text().startswith('trade_id,symbol,opened_at')
    first=(p/'ov.csv').read_text(); generate_paper_economic_reconciliation(str(p/'t.db'), str(p/'r2.json'), str(p/'eq2.csv'), str(p/'ov.csv'), True)
    assert (p/'ov.csv').read_text()==first
