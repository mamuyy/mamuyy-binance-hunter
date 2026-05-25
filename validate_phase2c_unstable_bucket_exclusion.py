#!/usr/bin/env python3
"""Read-only Phase 2C unstable bucket exclusion validation."""
import csv, json, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJECT_DIR = Path('/home/ubuntu/mamuyy-binance-hunter')
PROJECT_DIR = DEFAULT_PROJECT_DIR if DEFAULT_PROJECT_DIR.exists() else Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / 'data/ml_calibration_matched_20260520.csv'
DIAG_PATH = PROJECT_DIR / 'logs/phase2c_brier_failure_diagnosis.json'
SUFF_PATH = PROJECT_DIR / 'logs/phase2c_data_sufficiency_report.json'
OUT_PATH = PROJECT_DIR / 'logs/phase2c_unstable_bucket_exclusion_report.json'
TRAIN_START, TRAIN_END, VALID_START = '2026-05-20', '2026-05-23', '2026-05-23'
TARGET = 0.24


def to_dt(v):
    if not v: return None
    dt = datetime.fromisoformat(str(v).replace('Z','+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def to_float(v,d=0.0):
    try: return d if v in ('',None) else float(v)
    except Exception: return d

def clamp(v, lo=0.01, hi=0.99): return max(lo, min(hi, v))

def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z); return 1/(1+ez)
    ez = math.exp(z); return ez/(1+ez)

def bucket_name(score):
    if score < 20: return '00-19'
    if score < 40: return '20-39'
    if score < 60: return '40-59'
    if score < 80: return '60-79'
    return '80-100'

def brier(rows, k='pred'):
    return None if not rows else sum((r[k]-r['y'])**2 for r in rows)/len(rows)

def fit_logistic(features, labels, lr=0.04, epochs=2200, l2=0.06):
    n,m = len(labels), len(features[0]); w=[0.0]*m
    for _ in range(epochs):
        grad=[0.0]*m
        for x,y in zip(features, labels):
            p=sigmoid(sum(wi*xi for wi,xi in zip(w,x))); err=p-y
            for j in range(m): grad[j]+=err*x[j]
        for j in range(m):
            reg = 0.0 if j==0 else l2*w[j]
            w[j] -= lr*((grad[j]/n)+reg)
    return w

def predict(w,x): return clamp(sigmoid(sum(wi*xi for wi,xi in zip(w,x))))

def maybe_json(path):
    if not path.exists(): return None
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return None

def core_feat(r):
    return [1.0, r['score_norm'], r['regime_score_norm'], r['delta_norm'], r['holding_norm'], r['sl_dist']*100.0, r['tp1_dist']*100.0, r['tp2_dist']*100.0, r['rr1'], r['rr2']]

def load_rows():
    rows=[]
    if not CSV_PATH.exists(): return rows
    with CSV_PATH.open('r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            wl=row.get('win_loss') or ''
            if wl not in ('WIN','LOSS'): continue
            score=to_float(row.get('score')); entry=to_float(row.get('entry')); sl=to_float(row.get('sl')); tp1=to_float(row.get('tp1')); tp2=to_float(row.get('tp2'))
            regime_score=to_float(row.get('matched_regime_score')); delta=to_float(row.get('regime_match_delta_seconds')); holding=to_float(row.get('holding_candles'))
            sl_dist=abs((sl-entry)/entry) if entry else 0.0; tp1_dist=abs((tp1-entry)/entry) if entry else 0.0; tp2_dist=abs((tp2-entry)/entry) if entry else 0.0
            rr1=tp1_dist/sl_dist if sl_dist else 0.0; rr2=tp2_dist/sl_dist if sl_dist else 0.0
            rows.append({'signal_dt':to_dt(row.get('signal_timestamp')),'regime':row.get('matched_regime') or 'UNKNOWN','score':score,'bucket':bucket_name(score),'score_norm':(score-50.0)/50.0,'regime_score_norm':(regime_score-50.0)/50.0,'delta_norm':min(delta,1800.0)/1800.0,'holding_norm':holding/20.0 if holding else 1.0,'sl_dist':sl_dist,'tp1_dist':tp1_dist,'tp2_dist':tp2_dist,'rr1':rr1,'rr2':rr2,'y':1 if wl=='WIN' else 0})
    return rows

def per_regime(rows):
    out={}
    regs=sorted({r['regime'] for r in rows})
    for reg in regs:
        sub=[r for r in rows if r['regime']==reg]
        out[reg]={'rows':len(sub),'brier':round(brier(sub),6) if sub else None}
    return out

def main():
    rows=load_rows(); ts=datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc); te=datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc); vs=datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)
    train=[r for r in rows if r['signal_dt'] and ts<=r['signal_dt']<te]; valid=[r for r in rows if r['signal_dt'] and r['signal_dt']>=vs]

    report={'build_time_utc':datetime.now(timezone.utc).isoformat(),'mode':'READ_ONLY_PHASE_2C_UNSTABLE_BUCKET_EXCLUSION','sample_counts':{'train_rows':len(train),'validation_rows':len(valid)},'inputs':{'csv':str(CSV_PATH),'diagnosis_report_present':DIAG_PATH.exists(),'data_sufficiency_report_present':SUFF_PATH.exists()},'sparse_buckets_detected':[],'baseline_brier':None,'variant_results':{},'best_variant':None,'gap_to_target_0_24':None,'passes_target':False,'recommendation':'insufficient_data_or_missing_csv','safety':{'db_write':False,'execution_change':False,'production_scoring_change':False,'phase_3':False,'real_execution':'blocked'}}
    if not train or not valid:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True); OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8'); print(f'Report: {OUT_PATH}'); return

    labels=[r['y'] for r in train]; w=fit_logistic([core_feat(r) for r in train], labels)
    for r in valid: r['baseline_pred']=predict(w, core_feat(r)); r['pred']=r['baseline_pred']

    rb_train=defaultdict(list); b_train=defaultdict(list)
    for r in train: rb_train[f"{r['regime']}|{r['bucket']}"] .append(r); b_train[r['bucket']].append(r)
    global_prob=sum(labels)/len(labels)
    bucket_prob={k: sum(x['y'] for x in v)/len(v) for k,v in b_train.items()}
    sparse_keys=sorted([k for k,v in rb_train.items() if len(v)<20])
    report['sparse_buckets_detected']=[{'regime_bucket':k,'train_rows':len(rb_train[k])} for k in sparse_keys]

    baseline_rows=[dict(r, pred=r['baseline_pred']) for r in valid]
    report['baseline_brier']=round(brier(baseline_rows),6)
    report['variant_results']['baseline']={'rows':len(baseline_rows),'brier':round(brier(baseline_rows),6),'per_regime':per_regime(baseline_rows)}

    excl=[dict(r,pred=r['baseline_pred']) for r in valid if f"{r['regime']}|{r['bucket']}" not in sparse_keys]
    report['variant_results']['exclude_sparse_unstable']={'rows':len(excl),'brier':round(brier(excl),6) if excl else None,'per_regime':per_regime(excl)}

    neut=[]
    for r in valid:
        key=f"{r['regime']}|{r['bucket']}"; p=r['baseline_pred']
        if key in sparse_keys: p=bucket_prob.get(r['bucket'], global_prob)
        neut.append(dict(r,pred=clamp(p)))
    report['variant_results']['neutralize_sparse_to_bucket_or_global']={'rows':len(neut),'brier':round(brier(neut),6),'per_regime':per_regime(neut)}

    coll=[]
    for r in valid:
        key=f"{r['regime']}|{r['bucket']}"; p=r['baseline_pred']
        if key in sparse_keys and r['bucket']=='80-100':
            group=[x for x in train if x['regime']==r['regime'] and x['score']>=60]
            p=(sum(x['y'] for x in group)/len(group)) if group else bucket_prob.get('60-79', global_prob)
        coll.append(dict(r,pred=clamp(p)))
    report['variant_results']['collapse_sparse_highscore_80_100_to_60_100']={'rows':len(coll),'brier':round(brier(coll),6),'per_regime':per_regime(coll)}

    best=min(((k,v['brier']) for k,v in report['variant_results'].items() if v.get('brier') is not None), key=lambda x:x[1])
    report['best_variant']={'name':best[0],'brier':round(best[1],6)}
    gap=best[1]-TARGET
    report['gap_to_target_0_24']=round(gap,6); report['passes_target']=best[1]<=TARGET
    report['recommendation']='promote_read_only_followup_for_' + best[0] if report['passes_target'] else 'target_not_met_keep_phase2c_blocked'

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True); OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Baseline Brier: {report["baseline_brier"]}')
    print(f'Best Variant  : {report["best_variant"]["name"]} ({report["best_variant"]["brier"]})')
    print(f'Gap to 0.24   : {report["gap_to_target_0_24"]}')
    print(f'Report        : {OUT_PATH}')

if __name__ == '__main__':
    main()
