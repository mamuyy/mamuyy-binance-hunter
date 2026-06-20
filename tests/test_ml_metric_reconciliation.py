import json, os, sqlite3
from pathlib import Path

import pandas as pd

from ml_metric_reconciliation import (
    atomic_write_json, baseline_status, classification_metrics, leakage_status,
    producer_inventory, reconstruct_walkforward, run_ml_metric_reconciliation, segment_performance, write_csv,
)


def test_metrics_baselines_confusion_and_imbalance():
    m=classification_metrics([1,1,1,0],[1,1,0,1],[0,1])
    assert m['accuracy']==0.5
    assert round(m['balanced_accuracy'],4)==0.3333
    assert m['majority_class_baseline']==0.75
    assert len(m['confusion_matrix'])==2
    assert baseline_status(m, min_samples=1)['status']=='BLOCKED_BELOW_BASELINE'


def test_leakage_detection_modes():
    train=pd.DataFrame({'timestamp':['2024-01-02'], 'symbol':['BTC']})
    test=pd.DataFrame({'timestamp':['2024-01-01'], 'symbol':['BTC']})
    assert leakage_status(train,test)['status']=='BLOCKED_TEMPORAL_LEAKAGE'
    assert leakage_status(train, train.copy())['status'] in {'BLOCKED_TEMPORAL_LEAKAGE','BLOCKED_SPLIT_CONTAMINATION'}
    assert leakage_status(train,test,feature_cols=['pnl_percent'])['reasons'][-1]=='BLOCKED_TARGET_LEAKAGE'


def test_walkforward_aggregates_latest_and_worst(tmp_path):
    p=tmp_path/'walk.csv'
    pd.DataFrame([
        {'fold':1,'train_start':0,'train_end':1,'test_start':2,'test_end':3,'test_accuracy':0.4},
        {'fold':2,'train_start':2,'train_end':3,'test_start':4,'test_end':5,'test_accuracy':0.8},
    ]).to_csv(p,index=False)
    r=reconstruct_walkforward(p)
    assert r['fold_count']==2
    assert r['weighted_aggregate']==0.6 and r['unweighted_aggregate']==0.6
    assert r['latest_fold']['fold_id']==2
    assert r['worst_fold']['accuracy']==0.4


def test_atomic_json_and_deterministic_csv(tmp_path):
    j=tmp_path/'x.json'; atomic_write_json(j, {'b':1,'a':2})
    assert json.loads(j.read_text())=={'a':2,'b':1}
    c=tmp_path/'x.csv'; rows=[{'a':'2','b':'b'},{'a':'1','b':'a'}]
    write_csv(c, rows, ['a','b'])
    assert c.read_text().splitlines()==['a,b','1,a','2,b']


def test_inventory_missing_source_unreproducible(tmp_path):
    inv=producer_inventory(tmp_path/'missing.json', tmp_path/'missing.csv', tmp_path/'missing.db')
    assert any(i['reproducibility_status']=='SOURCE_MISSING' for i in inv)


def test_segments_sparse_review_and_model_version_separation():
    df=pd.DataFrame({'target':['WIN','LOSS'], 'regime_name':['A','B'], 'symbol':['BTC','ETH']})
    seg=segment_performance(df, min_samples=3)
    assert seg and all(s['readiness_status']=='REVIEW' for s in seg)


def test_full_audit_empty_data_governance_and_contract_separation(tmp_path, monkeypatch):
    (tmp_path/'paper_trades.csv').write_text('symbol,status,timestamp\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    report=run_ml_metric_reconciliation(output_dir='reports', db_path='missing.db', model_output_path='model_output.json', walkforward_path='walkforward_results.csv')
    assert report['governance']['paper_only'] is True
    assert report['governance']['execution_allowed'] is False
    assert report['governance']['automatic_promotion_allowed'] is False
    assert report['governance']['model_promotion_allowed'] is False
    ids={m['display_value']:m for m in report['metric_identity']}
    assert ids['32.81%']['contract_match_walkforward'] is False
    assert ids['64.38%']['contract_match_walkforward'] is True
    assert Path('reports/ml_metric_identity.csv').exists()
    assert Path('reports/ml_confusion_matrix.csv').read_text().startswith('actual_class')


def test_no_db_or_model_mutation(tmp_path, monkeypatch):
    db=tmp_path/'x.db'
    with sqlite3.connect(db) as c: c.execute('create table historical_outcomes (id integer)')
    model=tmp_path/'model_output.json'; model.write_text('{"accuracy":0.3281,"ai_confidence_score":65}', encoding='utf-8')
    mt=model.stat().st_mtime_ns; dbmt=db.stat().st_mtime_ns
    monkeypatch.chdir(tmp_path)
    run_ml_metric_reconciliation(output_dir='reports', db_path=str(db), model_output_path=str(model), walkforward_path='missing.csv')
    assert model.stat().st_mtime_ns==mt
    assert db.stat().st_mtime_ns==dbmt
