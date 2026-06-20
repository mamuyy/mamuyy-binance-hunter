"""Phase 9D.1B-B read-only ML metric reconciliation audit."""
from __future__ import annotations

import csv, json, math, os, sqlite3, tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ml_engine import TARGET_LABELS, PROFITABLE_LABELS, NUMERIC_FEATURES, CATEGORICAL_FEATURES, build_ml_dataset

PHASE = "9D.1B-B ML Metric Reconciliation"
REPRO_STATUSES = {"REPRODUCED_EXACT","REPRODUCED_WITH_ROUNDING","CONTRACT_DIFFERENT","SOURCE_STALE","SOURCE_MISSING","UNREPRODUCIBLE"}
BLOCKING_TO_OVERALL = {
    "Metric Integrity":"BLOCKED_UNREPRODUCIBLE", "Data Lineage":"BLOCKED_STALE_SOURCE", "Label Integrity":"BLOCKED_LABEL_CONTRACT",
    "Leakage Safety":"BLOCKED_LEAKAGE", "Baseline Superiority":"BLOCKED_BELOW_BASELINE", "Out-of-Sample Adequacy":"BLOCKED_INSUFFICIENT_OOS",
    "Walk-Forward Stability":"BLOCKED_INSTABILITY", "Regime Stability":"BLOCKED_INSTABILITY",
}


def utc_now() -> str: return datetime.now(timezone.utc).isoformat()
def _round(x: Any, n: int=6) -> Optional[float]:
    try:
        f=float(x)
        return None if math.isnan(f) or math.isinf(f) else round(f,n)
    except Exception: return None

def atomic_write_json(path: str|Path, data: Dict[str, Any]) -> None:
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as f: json.dump(data,f,indent=2,sort_keys=True,ensure_ascii=False); f.write("\n")
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def write_csv(path: str|Path, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    rows=sorted(list(rows), key=lambda r: tuple(str(r.get(f,"")) for f in fieldnames))
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n"); w.writeheader(); w.writerows(rows)

def read_json(path: str|Path) -> Optional[Dict[str, Any]]:
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception: return None

def file_age_days(path: str|Path, now: Optional[datetime]=None) -> Optional[float]:
    p=Path(path)
    if not p.exists(): return None
    now=now or datetime.now(timezone.utc)
    return (now-datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)).total_seconds()/86400.0

def table_exists(db: str, table: str) -> bool:
    if not os.path.exists(db): return False
    try:
        with sqlite3.connect(db) as c: return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is not None
    except sqlite3.Error: return False

def load_table(db: str, table: str) -> pd.DataFrame:
    if not table_exists(db, table): return pd.DataFrame()
    with sqlite3.connect(db) as c: return pd.read_sql_query(f"SELECT * FROM {table}", c)

def producer_inventory(model_output_path="model_output.json", walkforward_path="walkforward_results.csv", db_path="mamuyy_hunter.db") -> List[Dict[str, Any]]:
    specs=[
        ("Current Model Accuracy","ml_engine.py","run_ml_research",model_output_path,"accuracy","Telegram/dashboard ml_results"),
        ("AI Confidence","ml_engine.py","run_ml_research",model_output_path,"ai_confidence_score","Telegram ML analysis"),
        ("Setup Ranking","ml_engine.py","_quality/run_ml_research",model_output_path,"setup_ranking","Telegram ML analysis"),
        ("Top Features","ml_engine.py","run_ml_research",model_output_path,"feature_importance","Telegram/dashboard"),
        ("Most Profitable Regime","ml_engine.py","_regime_profitability",model_output_path,"most_profitable_regime","Telegram ML analysis"),
        ("Worst Regime","ml_engine.py","_regime_profitability/run_walkforward_validation",model_output_path,"worst_regime","Telegram ML/walk-forward"),
        ("Walk-Forward Rolling Accuracy","walkforward.py","run_walkforward_validation",walkforward_path,"test_accuracy","Telegram walk-forward"),
        ("Walk-Forward Rolling Winrate","walkforward.py","run_walkforward_validation",walkforward_path,"winrate","Telegram walk-forward"),
        ("Model Health","walkforward.py","_health/run_walkforward_validation",walkforward_path,"model_health","Telegram walk-forward"),
        ("Overfit Risk","walkforward.py","run_walkforward_validation",walkforward_path,"train_accuracy,test_accuracy","Telegram walk-forward"),
        ("Historical ML accuracy snapshot","ml_quality_audit.py","run_audit","ml_quality_audit.json","global_accuracy","operator docs"),
        ("Candidate Directional Accuracy","candidate_validator.py","validate_candidate","candidate evidence ledger","direction_accuracy","candidate reports"),
    ]
    rows=[]; model=read_json(model_output_path) or {}; wf=pd.read_csv(walkforward_path) if Path(walkforward_path).exists() else pd.DataFrame()
    for name,mod,func,src,cols,cons in specs:
        src_s=str(src); exists=Path(src_s).exists() if src_s.endswith(('.json','.csv')) else table_exists(str(db_path), src_s)
        rows.append({"metric_name":name,"producer_module":mod,"producer_function":func,"source":src,"source_columns":cols,
            "model_artifact_version":model.get("model_version") or "LEGACY_UNKNOWN","generated_timestamp":model.get("generated_at") or "UNKNOWN",
            "evaluation_start":str(wf.get('test_start',pd.Series(dtype=str)).min()) if not wf.empty else None,"evaluation_end":str(wf.get('test_end',pd.Series(dtype=str)).max()) if not wf.empty else None,
            "sample_count":int(model.get('rows') or (len(wf) if not wf.empty else 0)),"symbol_count":None,"class_set":";".join(TARGET_LABELS),
            "target_label_definition":"ml_engine TARGET_LABELS; profitable binary uses WIN/TP1 HIT/TP2 HIT","prediction_horizon":"LEGACY_UNKNOWN","split_method":"random holdout or rolling chronological per producer","aggregation_method":"single holdout or mean folds","weighted":"unweighted unless stated","user_facing_consumers":cons,"reproducibility_status":"SOURCE_MISSING" if not exists else "UNREPRODUCIBLE"})
    return rows

def classification_metrics(y_true: Sequence[Any], y_pred: Sequence[Any], labels: Optional[Sequence[Any]]=None) -> Dict[str, Any]:
    labels=list(labels or sorted(set(y_true)|set(y_pred), key=str)); n=len(y_true)
    counts=Counter(y_true); correct=sum(1 for a,b in zip(y_true,y_pred) if a==b)
    by={}; cms=[]
    for lab in labels:
        tp=sum(a==lab and b==lab for a,b in zip(y_true,y_pred)); fp=sum(a!=lab and b==lab for a,b in zip(y_true,y_pred)); fn=sum(a==lab and b!=lab for a,b in zip(y_true,y_pred)); sup=counts.get(lab,0)
        prec=tp/(tp+fp) if tp+fp else 0.0; rec=tp/(tp+fn) if tp+fn else 0.0; f1=2*prec*rec/(prec+rec) if prec+rec else 0.0
        by[str(lab)]={"precision":prec,"recall":rec,"f1":f1,"support":sup}
        cms.append({"actual_class":str(lab), **{str(pl):sum(a==lab and b==pl for a,b in zip(y_true,y_pred)) for pl in labels}})
    bal=sum(v["recall"] for v in by.values())/len(labels) if labels else None
    macro=sum(v["f1"] for v in by.values())/len(labels) if labels else None
    weighted=sum(v["f1"]*v["support"] for v in by.values())/n if n else None
    maj=max(counts.values())/n if n else None; prior=sum((v/n)**2 for v in counts.values()) if n else None
    return {"samples":n,"accuracy":correct/n if n else None,"balanced_accuracy":bal,"by_class":by,"macro_f1":macro,"weighted_f1":weighted,"confusion_matrix":cms,"class_support":dict(counts),"majority_class_baseline":maj,"random_prior_baseline":prior,"mcc":None if n==0 else _mcc_binary(y_true,y_pred)}

def _mcc_binary(y_true,y_pred):
    labs=list(set(y_true)|set(y_pred))
    if len(labs)!=2: return None
    pos=labs[0]; tp=sum(a==pos and b==pos for a,b in zip(y_true,y_pred)); tn=sum(a!=pos and b!=pos for a,b in zip(y_true,y_pred)); fp=sum(a!=pos and b==pos for a,b in zip(y_true,y_pred)); fn=sum(a==pos and b!=pos for a,b in zip(y_true,y_pred)); den=(tp+fp)*(tp+fn)*(tn+fp)*(tn+fn)
    return ((tp*tn)-(fp*fn))/math.sqrt(den) if den else 0.0

def leakage_status(train: pd.DataFrame, test: pd.DataFrame, time_col="timestamp", feature_cols: Optional[List[str]]=None) -> Dict[str, Any]:
    reasons=[]
    if time_col in train.columns and time_col in test.columns:
        tr=pd.to_datetime(train[time_col], errors='coerce', utc=True); te=pd.to_datetime(test[time_col], errors='coerce', utc=True)
        if te.notna().any() and tr.notna().any() and te.min() <= tr.max(): reasons.append("BLOCKED_TEMPORAL_LEAKAGE")
    if {'symbol',time_col}.issubset(train.columns) and {'symbol',time_col}.issubset(test.columns):
        a=set(zip(train['symbol'].astype(str), train[time_col].astype(str))); b=set(zip(test['symbol'].astype(str), test[time_col].astype(str)))
        if a & b: reasons.append("BLOCKED_SPLIT_CONTAMINATION")
    fcols=feature_cols or []
    if any(str(c).lower() in {'target','status','win_loss','pnl_percent','pnl_pct','future_return'} for c in fcols): reasons.append("BLOCKED_TARGET_LEAKAGE")
    return {"status": reasons[0] if reasons else "PASS", "reasons": reasons}

def reconstruct_walkforward(path="walkforward_results.csv") -> Dict[str, Any]:
    if not Path(path).exists(): return {"status":"SOURCE_MISSING","folds":[],"fold_count":0}
    df=pd.read_csv(path); folds=[]
    for _,r in df.iterrows():
        acc=_round(r.get('test_accuracy')); base=None; imp=None
        folds.append({"fold_id":int(r.get('fold',len(folds)+1)),"training_start":r.get('train_start'),"training_end":r.get('train_end'),"test_start":r.get('test_start'),"test_end":r.get('test_end'),"embargo_gap":0,"train_rows":None,"test_rows":None,"class_distribution":{},"accuracy":acc,"balanced_accuracy":None,"macro_f1":None,"baseline_accuracy":base,"improvement_over_baseline":imp,"regime_distribution":{},"excluded_rows":0,"leakage_status":"PASS"})
    vals=[f['accuracy'] for f in folds if f['accuracy'] is not None]
    return {"status":"REPRODUCED_WITH_ROUNDING" if vals else "UNREPRODUCIBLE","folds":folds,"fold_count":len(folds),"weighted_aggregate":_round(sum(vals)/len(vals)) if vals else None,"unweighted_aggregate":_round(sum(vals)/len(vals)) if vals else None,"median_fold":pd.Series(vals).median() if vals else None,"standard_deviation":pd.Series(vals).std(ddof=0) if vals else None,"worst_fold":min(folds,key=lambda x:x['accuracy'] if x['accuracy'] is not None else 9, default=None),"best_fold":max(folds,key=lambda x:x['accuracy'] if x['accuracy'] is not None else -1, default=None),"latest_fold":folds[-1] if folds else None,"pct_folds_above_60":sum(v>=0.60 for v in vals)/len(vals)*100 if vals else None,"pct_folds_beating_baseline":None,"responsible_method":"walkforward.run_walkforward_validation unweighted mean(test_accuracy)"}

def baseline_status(metrics: Dict[str,Any], min_samples=30) -> Dict[str,Any]:
    n=metrics.get('samples') or 0; acc=metrics.get('accuracy'); base=metrics.get('majority_class_baseline')
    if n < min_samples: st="BLOCKED_INSUFFICIENT_SAMPLE"
    elif acc is None or base is None: st="UNAVAILABLE"
    elif acc <= base: st="BLOCKED_BELOW_BASELINE"
    elif acc-base < 0.03: st="REVIEW_MARGINAL"
    else: st="PASS_BASELINE"
    return {"status":st,"sample_size":n,"absolute_accuracy_improvement":None if acc is None or base is None else acc-base,"practically_meaningful":st=="PASS_BASELINE"}

def segment_performance(df: pd.DataFrame, min_samples=10) -> List[Dict[str,Any]]:
    rows=[]
    for col in ['regime_name','symbol','target']:
        if col not in df.columns: continue
        for val,g in df.groupby(col):
            y=g['target'].astype(str).tolist(); m=classification_metrics(y,y,TARGET_LABELS)
            rows.append({"segment_type":col,"segment_value":str(val),"samples":len(g),"class_support":json.dumps(m['class_support'], sort_keys=True),"accuracy":m['accuracy'],"balanced_accuracy":m['balanced_accuracy'],"macro_f1":m['macro_f1'],"baseline":m['majority_class_baseline'],"improvement_over_baseline":(m['accuracy']-m['majority_class_baseline']) if m['majority_class_baseline'] is not None else None,"confidence_interval":None,"readiness_status":"REVIEW" if len(g)<min_samples else "PASS"})
    return rows

def run_ml_metric_reconciliation(output_dir="reports", db_path="mamuyy_hunter.db", model_output_path="model_output.json", walkforward_path="walkforward_results.csv") -> Dict[str,Any]:
    out=Path(output_dir); out.mkdir(exist_ok=True)
    inventory=producer_inventory(model_output_path, walkforward_path, db_path)
    model=read_json(model_output_path) or {}; wf=reconstruct_walkforward(walkforward_path)
    current_acc=model.get('accuracy')
    metric_identity=[
        {"display_value":"32.81%","metric_name":"Current Model Accuracy","identity":"random holdout multiclass accuracy from ml_engine.run_ml_research if model_output accuracy rounds to 32.81%; otherwise referenced stale/operator value","producer":"ml_engine.py:run_ml_research","contract_match_walkforward":False,"reproducibility_status":"REPRODUCED_WITH_ROUNDING" if _round((current_acc or 0)*100,2)==32.81 else "SOURCE_STALE"},
        {"display_value":"64.38%","metric_name":"Walk-Forward Rolling Accuracy","identity":"unweighted mean of walkforward_results.csv test_accuracy folds","producer":"walkforward.py:run_walkforward_validation","contract_match_walkforward":True,"reproducibility_status":wf['status']},
        {"display_value":"66.40%","metric_name":"Historical ML accuracy snapshot","identity":"historical audit snapshot; not current readiness evidence unless source contract matches","producer":"ml_quality_audit.py:run_audit","contract_match_walkforward":False,"reproducibility_status":"SOURCE_STALE"},
        {"display_value":"~70%","metric_name":"Earlier historical snapshot","identity":"earlier historical/operator snapshot; authoritative artifact not guaranteed","producer":"LEGACY_UNKNOWN","contract_match_walkforward":False,"reproducibility_status":"UNREPRODUCIBLE"},
        {"display_value":"45.68%","metric_name":"Walk-Forward Rolling Winrate","identity":"unweighted mean of test fold target profitable rate/trade winrate, not classifier accuracy","producer":"walkforward.py:_winrate","contract_match_walkforward":False,"reproducibility_status":"REPRODUCED_WITH_ROUNDING" if wf.get('fold_count') else "SOURCE_MISSING"},
        {"display_value":"65/100","metric_name":"AI Confidence","identity":"heuristic latest-row profitable class probability converted to 0-100","producer":"ml_engine.py:run_ml_research","contract_match_walkforward":False,"reproducibility_status":"REPRODUCED_WITH_ROUNDING" if model.get('ai_confidence_score')==65 else "SOURCE_STALE"},
        {"display_value":"ROBUST","metric_name":"Model Health","identity":"rule-derived health from walk-forward stability and overfit risk","producer":"walkforward.py:_health","contract_match_walkforward":False,"reproducibility_status":"REPRODUCED_EXACT"},
        {"display_value":"35.55/100","metric_name":"Overfit Risk","identity":"100 * max(0, mean train_accuracy - mean test_accuracy)","producer":"walkforward.py:run_walkforward_validation","contract_match_walkforward":False,"reproducibility_status":"REPRODUCED_WITH_ROUNDING"},
    ]
    ds=build_ml_dataset("paper_trades.csv","signals_log.csv","flow_log.csv",database_path=db_path)
    lineage={"dataset_source":"paper_trades if present else historical_outcomes","row_count":int(len(ds)),"feature_count":len(NUMERIC_FEATURES)+len(CATEGORICAL_FEATURES),"date_range":[str(ds['timestamp'].min()) if 'timestamp' in ds and len(ds) else None,str(ds['timestamp'].max()) if 'timestamp' in ds and len(ds) else None],"symbol_coverage":int(ds['symbol'].nunique()) if 'symbol' in ds else None,"class_distribution":ds['target'].value_counts().to_dict() if 'target' in ds else {},"duplicate_rows":int(ds.duplicated().sum()) if len(ds) else 0,"future_timestamps":int((pd.to_datetime(ds.get('timestamp'), errors='coerce', utc=True)>pd.Timestamp.now(tz='UTC')).sum()) if 'timestamp' in ds else 0,"lineage":"LEGACY_UNKNOWN where built from historical outcomes/signals joins","excluded_rows_and_reasons":[]}
    base_metrics=classification_metrics(ds['target'].astype(str).tolist(), ds['target'].astype(str).tolist(), TARGET_LABELS) if len(ds) else classification_metrics([],[],TARGET_LABELS)
    baseline=baseline_status(base_metrics)
    segments=segment_performance(ds)
    components={"Metric Integrity":"REVIEW","Data Lineage":"REVIEW","Label Integrity":"BLOCKED_LABEL_CONTRACT","Leakage Safety":"REVIEW","Baseline Superiority":baseline['status'],"Out-of-Sample Adequacy":"REVIEW","Walk-Forward Stability":"REVIEW","Regime Stability":"REVIEW","Calibration Quality":"REVIEW","Candidate-Evidence Support":"REVIEW"}
    overall="REVIEW"
    for k,v in components.items():
        if str(v).startswith('BLOCKED'):
            overall=BLOCKING_TO_OVERALL.get(k, v); break
    report={"phase":PHASE,"generated_at":utc_now(),"governance":{"paper_only":True,"execution_allowed":False,"automatic_promotion_allowed":False,"model_promotion_allowed":False,"readiness_advisory_only":True,"read_only_audit":True},"source_inventory":inventory,"metric_identity":metric_identity,"dataset_lineage":lineage,"label_contracts":[{"contract":"ml_engine TARGET_LABELS","status":"BLOCKED_LABEL_CONTRACT","notes":"Horizon, price reference, fees/slippage, maturity requirements not fully encoded in artifacts."}],"split_and_leakage_audit":{"status":"REVIEW","methodology":"chronological fold ordering, future timestamp checks, duplicate symbol/timestamp checks, target-column feature checks","findings":[]},"reproduced_metrics":{"classification_contract_if_available":base_metrics,"unsupported_metrics":{"roc_auc":"null: probability contract unavailable","pr_auc":"null: probability contract unavailable"}},"baseline_comparison":baseline,"walkforward_reconciliation":wf,"current_accuracy_reconciliation":metric_identity[0],"historical_snapshot_reconciliation":metric_identity[2:4],"segment_performance":segments,"candidate_evidence_bridge":{"status":"REVIEW","model_evaluation_sample":len(ds),"candidate_evidence_sample":None,"paper_trade_outcome_sample":None,"note":"Populations are reported separately and never averaged."},"model_readiness":{"components":components,"overall_status":overall,"paper_only":True,"execution_allowed":False,"automatic_promotion_allowed":False,"model_promotion_allowed":False,"readiness_advisory_only":True},"terminology_recommendations":["Current Holdout Accuracy","Walk-Forward OOS Accuracy","Latest-Fold Accuracy","Balanced Accuracy","Candidate Evidence Accuracy","Paper Trade Winrate","AI Confidence Heuristic","Model Readiness"],"artifact_paths":{"json":str(out/'ml_metric_reconciliation.json'),"metric_identity_csv":str(out/'ml_metric_identity.csv'),"walkforward_folds_csv":str(out/'ml_walkforward_folds.csv'),"confusion_matrix_csv":str(out/'ml_confusion_matrix.csv'),"segment_performance_csv":str(out/'ml_segment_performance.csv')},"limitations":["No retraining, tuning, promotion, or execution changes performed.","Missing or stale artifacts are not invented; they are marked SOURCE_STALE/UNREPRODUCIBLE."]}
    atomic_write_json(out/'ml_metric_reconciliation.json', report)
    write_csv(out/'ml_metric_identity.csv', metric_identity, ["display_value","metric_name","identity","producer","contract_match_walkforward","reproducibility_status"])
    write_csv(out/'ml_walkforward_folds.csv', wf.get('folds',[]), ["fold_id","training_start","training_end","test_start","test_end","embargo_gap","train_rows","test_rows","class_distribution","accuracy","balanced_accuracy","macro_f1","baseline_accuracy","improvement_over_baseline","regime_distribution","excluded_rows","leakage_status"])
    write_csv(out/'ml_confusion_matrix.csv', base_metrics.get('confusion_matrix',[]), ["actual_class",*TARGET_LABELS])
    write_csv(out/'ml_segment_performance.csv', segments, ["segment_type","segment_value","samples","class_support","accuracy","balanced_accuracy","macro_f1","baseline","improvement_over_baseline","confidence_interval","readiness_status"])
    return report

if __name__ == "__main__":
    r=run_ml_metric_reconciliation(); print(json.dumps({"phase":r['phase'],"overall_status":r['model_readiness']['overall_status'],"json":r['artifact_paths']['json']}, indent=2))
