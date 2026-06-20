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

def test_open_event_does_not_increase_equity_and_exposure_separate(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T02:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1))
    curve=rep['equal_allocation_capital_scenario']['equity_curve']
    assert curve[0]['event']=='open'
    assert curve[0]['realized_account_equity']==10000
    assert curve[0]['realized_capital']==10000
    assert curve[0]['open_gross_exposure']==100
    assert curve[0]['reserved_notional']==100
    assert 'cash_equity' not in curve[0]
    assert curve[0]['available_unallocated_capacity']==9900
    assert curve[1]['realized_account_equity']>10000


def test_realized_close_to_close_drawdown_is_from_closed_equity(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,50,50,-50,80,'R','CLOSED','SL','2026-01-01T01:00:00'),(2,'2026-01-01T02:00:00','ETH','LONG',100,200,200,100,80,'R','CLOSED','TP','2026-01-01T03:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_realized_drawdown_pct=0.1, max_top_symbol_concentration_pct=999, max_outlier_contribution_pct=999))
    cap=rep['equal_allocation_capital_scenario']
    assert cap['realized_close_to_close_equity_curve']
    assert cap['maximum_drawdown_pct'] > 0
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_DRAWDOWN'


def test_empty_and_unavailable_database_are_blocked(tmp_path):
    rep,_=run(tmp_path,[])
    assert rep['equal_allocation_capital_scenario']['scenario_status']=='BLOCKED_DATA_QUALITY'
    assert rep['equal_allocation_capital_scenario']['normalized_net_return_pct'] is None
    assert rep['equal_allocation_capital_scenario']['maximum_drawdown_pct'] is None
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_DATA_QUALITY'
    missing=generate_paper_economic_reconciliation(str(tmp_path/'missing.db'), str(tmp_path/'m.json'), str(tmp_path/'meq.csv'), str(tmp_path/'mov.csv'), True)
    assert missing['equal_allocation_capital_scenario']['scenario_status']=='BLOCKED_DATA_QUALITY'
    assert missing['readiness']['overall_economic_readiness_status']=='BLOCKED_DATA_QUALITY'


def test_zero_valid_rows_does_not_complete_and_material_mismatch_counts(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,-25,80,'R','CLOSED','TP','2026-01-01T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1))
    assert rep['reconciliation_summary']['reconciliation_status_counts']['MATERIAL_DIFFERENCE']==1
    assert rep['reconciliation_summary']['valid_closed_trades']==0
    assert rep['equal_allocation_capital_scenario']['scenario_status']=='BLOCKED_DATA_QUALITY'
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_DATA_QUALITY'


def test_negative_normalized_return_does_not_pass(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,90,90,-10,80,'R','CLOSED','SL','2026-01-01T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, min_cost_adjusted_normalized_return_pct=0))
    assert rep['equal_allocation_capital_scenario']['normalized_net_return_pct'] < 0
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_NEGATIVE_EXPECTANCY'


def test_excessive_concentration_blocks_and_top_contributions(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,120,120,20,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-02T00:00:00','ETH','LONG',100,101,101,1,80,'R','CLOSED','TP','2026-01-02T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=50))
    c=rep['concentration']
    assert c['top_1_symbol_contribution_pct'] is not None
    assert c['top_3_symbol_contribution_pct'] is not None
    assert c['top_5_symbol_contribution_pct'] is not None
    assert c['top_10_symbol_contribution_pct'] is not None
    assert c['herfindahl_concentration'] is not None
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_CONCENTRATION'


def test_overlap_dependence_percentage_and_review(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T03:00:00'),(2,'2026-01-01T01:00:00','BTC','LONG',100,111,111,11,80,'R','CLOSED','TP','2026-01-01T04:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_overlap_dependence_pct=10, max_top_symbol_concentration_pct=999, max_outlier_contribution_pct=999))
    assert rep['overlap_audit']['same_symbol_overlap_trade_pct']==100
    assert rep['overlap_audit']['overlap_event_return_contribution_pct'] is not None
    assert rep['readiness']['overall_economic_readiness_status']=='REVIEW'


def test_holding_period_and_calendar_breakdowns(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,101,101,1,80,'R','CLOSED','TP','2026-01-01T00:30:00'),(2,'2026-01-08T00:00:00','ETH','LONG',100,102,102,2,80,'R','CLOSED','TP','2026-01-08T02:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1))
    for section in ('holding_period_bucket','calendar_day','calendar_week'):
        assert rep['breakdowns'][section]
        bucket=next(iter(rep['breakdowns'][section].values()))
        for key in ('trades','wins','losses','breakeven','winrate','mean_return','median_return','event_return_sum','profit_factor','expectancy'):
            assert key in bucket


def test_overall_readiness_is_deterministic_and_safety_locked(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,101,101,1,80,'R','CLOSED','TP','2026-01-01T01:00:00')]
    cfg=EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=999, max_outlier_contribution_pct=999)
    rep1,_=run(tmp_path,rows,cfg)
    rep2=generate_paper_economic_reconciliation(str(tmp_path/'t.db'), str(tmp_path/'r2.json'), str(tmp_path/'eq2.csv'), str(tmp_path/'ov2.csv'), True, cfg)
    assert rep1['readiness']['overall_economic_readiness_status']==rep2['readiness']['overall_economic_readiness_status']
    assert rep1['governance']['paper_only'] is True
    assert rep1['governance']['writes_to_broker'] is False
    assert rep1['governance']['execution_allowed'] is False
    assert rep1['governance']['automatic_promotion_allowed'] is False


def test_blocked_data_has_no_passing_data_dependent_subgates(tmp_path):
    rep,_=run(tmp_path,[])
    econ=rep['readiness']['economic_readiness']
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_DATA_QUALITY'
    for gate in ('sample_adequacy','data_quality_adequacy','positive_expectancy','profit_factor','normalized_scenario_return','normalized_maximum_drawdown','concentration','overlap_dependence','outlier_dependence','cost_adjusted_result'):
        assert econ[gate]=='BLOCKED_DATA_QUALITY'


def test_all_winning_sample_uses_no_losses_profit_factor_state(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,101,101,1,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-02T00:00:00','ETH','LONG',100,102,102,2,80,'R','CLOSED','TP','2026-01-02T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=999))
    assert rep['closed_trade_statistics']['profit_factor_state']=='NO_LOSSES'
    assert rep['readiness']['economic_readiness']['profit_factor']=='PASS'
    empty_dir=tmp_path/'empty'; empty_dir.mkdir(); empty,_=run(empty_dir,[])
    assert empty['closed_trade_statistics']['profit_factor_state']=='NO_TRADES'


def test_gross_absolute_concentration_and_signed_attribution_are_separate(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,130,130,30,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-02T00:00:00','ETH','LONG',100,80,80,-20,80,'R','CLOSED','SL','2026-01-02T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=50, max_outlier_contribution_pct=999))
    c=rep['concentration']
    assert c['top_1_symbol_contribution_pct']==60
    assert c['top_1_signed_return_contribution_pct']==300
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_CONCENTRATION'


def test_outlier_gate_uses_absolute_contribution_not_signed(tmp_path):
    rows=[(1,'2026-01-01T00:00:00','BTC','LONG',100,200,200,100,80,'R','CLOSED','TP','2026-01-01T01:00:00'),(2,'2026-01-02T00:00:00','ETH','LONG',100,50,50,-50,80,'R','CLOSED','SL','2026-01-02T01:00:00')]
    rep,_=run(tmp_path,rows,EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=999, max_outlier_contribution_pct=60))
    assert rep['outlier_analysis']['outlier_contribution_pct']==66.6667
    assert rep['outlier_analysis']['signed_outlier_return_contribution_pct']==200
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_OUTLIER_DEPENDENCE'


def test_excluded_closed_row_blocks_readiness_even_when_valid_trades_are_profitable(tmp_path):
    rows=[
        (1,'2026-01-01T00:00:00','BTC','LONG',100,110,110,10,80,'R','CLOSED','TP','2026-01-01T01:00:00'),
        (2,'2026-01-02T00:00:00','ETH','LONG',100,112,112,12,80,'R','CLOSED','TP','2026-01-02T01:00:00'),
        (3,'2026-01-03T00:00:00',None,'LONG',100,105,105,5,80,'R','CLOSED','TP','2026-01-03T01:00:00'),
    ]
    cfg=EconomicAuditConfig(min_valid_closed_trades=2, max_top_symbol_concentration_pct=100, max_outlier_contribution_pct=100, max_overlap_dependence_pct=100)
    rep,_=run(tmp_path,rows,cfg)
    assert rep['reconciliation_summary']['excluded_rows']==1
    assert rep['reconciliation_summary']['data_quality_blocked'] is True
    assert 'excluded_required_closed_rows' in rep['reconciliation_summary']['data_quality_block_reasons']
    econ=rep['readiness']['economic_readiness']
    assert econ['data_quality_adequacy']=='BLOCKED_DATA_QUALITY'
    assert rep['readiness']['overall_economic_readiness_status']=='BLOCKED_DATA_QUALITY'
    for gate in ('sample_adequacy','data_quality_adequacy','positive_expectancy','profit_factor','normalized_scenario_return','normalized_maximum_drawdown','concentration','overlap_dependence','outlier_dependence','cost_adjusted_result'):
        assert econ[gate] != 'PASS'


def test_zero_total_absolute_return_does_not_pass_outlier_dependence(tmp_path):
    rows=[
        (1,'2026-01-01T00:00:00','BTC','LONG',100,100,100,0,80,'R','CLOSED','FLAT','2026-01-01T01:00:00'),
        (2,'2026-01-02T00:00:00','ETH','LONG',100,100,100,0,80,'R','CLOSED','FLAT','2026-01-02T01:00:00'),
    ]
    cfg=EconomicAuditConfig(min_valid_closed_trades=1, max_top_symbol_concentration_pct=100)
    rep,_=run(tmp_path,rows,cfg)
    assert rep['outlier_analysis']['outlier_contribution_pct'] is None
    assert rep['readiness']['economic_readiness']['outlier_dependence']=='REVIEW'
    assert rep['readiness']['economic_readiness']['outlier_dependence'] != 'PASS'
