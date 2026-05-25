#!/usr/bin/env python3
"""Read-only Phase 2C nonlinear model exploration over fixed train/validation split."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / 'data/ml_calibration_matched_20260520.csv'
OUT_PATH = ROOT / 'logs/phase2c_nonlinear_model_exploration_report.json'
CONTEXT_LOGS = [
    ROOT / 'logs/phase2c_candidate_feature_sources_report.json',
    ROOT / 'logs/phase2c_volatility_momentum_ablation_report.json',
    ROOT / 'logs/phase2c_evidence_synthesis_report.json',
]
TRAIN_START = datetime.fromisoformat('2026-05-20').replace(tzinfo=timezone.utc)
TRAIN_END = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
VALID_START = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
TARGET = 0.24


def to_dt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(v):
    try:
        return None if v in (None, '') else float(v)
    except Exception:
        return None


def brier(p, y):
    return sum((pp - yy) ** 2 for pp, yy in zip(p, y)) / len(y) if y else None


def stats(values):
    if not values:
        return {'min': None, 'max': None, 'mean': None, 'std': None}
    m = sum(values) / len(values)
    v = sum((x - m) ** 2 for x in values) / len(values)
    return {'min': round(min(values), 6), 'max': round(max(values), 6), 'mean': round(m, 6), 'std': round(math.sqrt(v), 6)}


def context_logs():
    out = {}
    for p in CONTEXT_LOGS:
        if p.exists():
            try:
                out[p.name] = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                out[p.name] = {'parse_error': True}
    return out


def load_rows():
    if not CSV_PATH.exists():
        return [], []
    rows, prev = [], None
    with CSV_PATH.open('r', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        fields = list(rd.fieldnames or [])
        for raw in rd:
            wl = (raw.get('win_loss') or '').upper()
            if wl not in ('WIN', 'LOSS'):
                continue
            dt = to_dt(raw.get('signal_timestamp'))
            if not dt:
                continue
            r = {'dt': dt, 'y': 1 if wl == 'WIN' else 0}
            for k in fields:
                v = to_float(raw.get(k))
                if v is not None:
                    r[k] = v
            entry, sl, tp1, tp2 = (r.get('entry', 0.0), r.get('sl', 0.0), r.get('tp1', 0.0), r.get('tp2', 0.0))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            r['score_norm'] = (r.get('score', 50.0) - 50.0) / 50.0
            r['regime_score_norm'] = (r.get('matched_regime_score', 50.0) - 50.0) / 50.0
            r['delta_norm'] = min(r.get('regime_match_delta_seconds', 0.0), 1800.0) / 1800.0
            r['holding_norm'] = r.get('holding_candles', 0.0) / 20.0
            r['sl_dist'], r['tp1_dist'], r['tp2_dist'] = sl_dist, tp1_dist, tp2_dist
            r['rr1'] = tp1_dist / sl_dist if sl_dist else 0.0
            r['rr2'] = tp2_dist / sl_dist if sl_dist else 0.0
            if prev is None:
                r['cand_score_mom'] = r['cand_regime_score_mom'] = r['cand_rolling_return'] = r['cand_trend_slope'] = 0.0
            else:
                r['cand_score_mom'] = r.get('score', 0.0) - prev.get('score', 0.0)
                r['cand_regime_score_mom'] = r.get('matched_regime_score', 0.0) - prev.get('matched_regime_score', 0.0)
                pe = prev.get('entry', 0.0)
                r['cand_rolling_return'] = ((entry - pe) / pe) if pe else 0.0
                r['cand_trend_slope'] = r['cand_rolling_return']
            r['cand_atr_like'] = (sl_dist + tp1_dist + tp2_dist) / 3.0
            rows.append(r)
            prev = r
    rows = sorted(rows, key=lambda x: x['dt'])
    return rows, fields


def leakage_safe_numeric_fields(fields):
    bad = ('win', 'loss', 'outcome', 'label', 'target', 'pnl', 'profit', 'return', 'drawdown', 'exit', 'close', 'filled', 'realized')
    allow = {'entry', 'sl', 'tp1', 'tp2', 'score', 'matched_regime_score', 'regime_match_delta_seconds', 'holding_candles'}
    out = []
    for f in fields:
        fl = f.lower()
        if f in ('signal_timestamp', 'win_loss'):
            continue
        if any(b in fl for b in bad) and f not in allow:
            continue
        out.append(f)
    return out


def build_matrix(rows, features):
    return [[float(r.get(k, 0.0)) for k in features] for r in rows], [r['y'] for r in rows]


def run_model(name, model, train_rows, valid_rows, features, baseline):
    xtr, ytr = build_matrix(train_rows, features)
    xva, yva = build_matrix(valid_rows, features)
    model.fit(xtr, ytr)
    p_tr = [float(p[1]) for p in model.predict_proba(xtr)]
    p_va = [float(p[1]) for p in model.predict_proba(xva)]
    b_tr, b_va = brier(p_tr, ytr), brier(p_va, yva)
    st = stats(p_va)
    fi = None
    if hasattr(model, 'feature_importances_'):
        fi = sorted([{'feature': f, 'importance': round(float(i), 6)} for f, i in zip(features, model.feature_importances_)], key=lambda x: x['importance'], reverse=True)[:12]
    return {
        'model_name': name,
        'train_rows': len(train_rows),
        'validation_rows': len(valid_rows),
        'brier': round(b_va, 6),
        'improvement_vs_baseline': round(baseline - b_va, 6),
        'gap_to_0_24': round(b_va - TARGET, 6),
        'passes_target': b_va <= TARGET,
        'prediction_min': st['min'],
        'prediction_max': st['max'],
        'prediction_mean': st['mean'],
        'prediction_std': st['std'],
        'saturation_flag': bool((st['max'] is not None and st['max'] >= 0.99) or (st['min'] is not None and st['min'] <= 0.01) or (st['std'] is not None and st['std'] < 0.03)),
        'overfit_flag': bool((b_va - b_tr) > 0.015),
        'feature_importance': fi,
    }


def main():
    rows, fields = load_rows()
    train = [r for r in rows if TRAIN_START <= r['dt'] < TRAIN_END]
    valid = [r for r in rows if r['dt'] >= VALID_START]
    results = []

    report = {
        'mode': 'READ_ONLY_PHASE2C_NONLINEAR_MODEL_EXPLORATION',
        'baseline_brier': None,
        'model_results': [],
        'best_model': None,
        'passes_target': False,
        'gap_to_target_0_24': None,
        'interpretation': {'nonlinear_signal_found': False, 'likely_data_feature_limitation': True},
        'recommendation': 'B) collect more outcomes and richer feature sources',
        'phase2c_status': 'REVIEW_NOT_PASSED',
        'phase3_status': 'LOCKED',
        'real_execution_status': 'BLOCKED',
        'context_logs_loaded': context_logs(),
        'safety': {'db_write': False, 'execution_change': False, 'production_scoring_change': False, 'phase_3': False, 'real_execution': 'blocked'},
    }

    if not train or not valid:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(str(OUT_PATH))
        return

    from sklearn.ensemble import GradientBoostingClassifier, HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.tree import DecisionTreeClassifier

    core = ['score_norm', 'regime_score_norm', 'delta_norm', 'holding_norm', 'sl_dist', 'tp1_dist', 'tp2_dist', 'rr1', 'rr2']
    cand = ['cand_score_mom', 'cand_regime_score_mom', 'cand_rolling_return', 'cand_atr_like', 'cand_trend_slope']
    all_safe = sorted(set(core + cand + leakage_safe_numeric_fields(fields)))
    feature_sets = [('A_core', core), ('B_core_plus_proxy', sorted(set(core + cand))), ('C_all_numeric_pre_signal', all_safe)]

    # baseline
    bmodel = LogisticRegression(max_iter=1000, class_weight='balanced', random_state=42)
    base = run_model('LogisticBaseline', bmodel, train, valid, core, baseline=0.0)
    base['feature_set'] = 'A_core'
    base['improvement_vs_baseline'] = 0.0
    report['baseline_brier'] = base['brier']
    results.append(base)

    constructors = [
        ('LogisticBaseline', lambda: LogisticRegression(max_iter=1000, class_weight='balanced', random_state=42)),
        ('RandomForestClassifier', lambda: RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=8, random_state=42)),
        ('GradientBoostingClassifier', lambda: GradientBoostingClassifier(random_state=42)),
        ('HistGradientBoostingClassifier', lambda: HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, random_state=42)),
        ('DecisionTreeShallow', lambda: DecisionTreeClassifier(max_depth=3, min_samples_leaf=12, random_state=42)),
    ]
    try:
        import xgboost as xgb
        constructors.append(('XGBoostClassifier', lambda: xgb.XGBClassifier(n_estimators=250, max_depth=4, learning_rate=0.05, subsample=0.9, colsample_bytree=0.9, objective='binary:logistic', eval_metric='logloss', random_state=42)))
    except Exception:
        pass

    for fs_name, feats in feature_sets:
        for model_name, mk in constructors:
            if model_name == 'LogisticBaseline' and fs_name == 'A_core':
                continue
            res = run_model(model_name, mk(), train, valid, feats, baseline=report['baseline_brier'])
            res['feature_set'] = fs_name
            results.append(res)

    best = min(results, key=lambda x: x['brier'])
    report['model_results'] = results
    report['best_model'] = best
    report['passes_target'] = bool(best['passes_target'])
    report['gap_to_target_0_24'] = round(best['brier'] - TARGET, 6)
    report['interpretation'] = {
        'nonlinear_signal_found': bool(best['model_name'] != 'LogisticBaseline' and best['improvement_vs_baseline'] > 0.002),
        'likely_data_feature_limitation': bool(best['brier'] > TARGET),
    }
    report['recommendation'] = (
        'A) promote nonlinear model to deeper read-only validation' if best['passes_target'] else
        'C) pivot to label/target redesign' if best['improvement_vs_baseline'] < 0.001 else
        'B) collect more outcomes and richer feature sources'
    )
    report['phase2c_status'] = 'PASSED' if best['passes_target'] else 'REVIEW_NOT_PASSED'

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(str(OUT_PATH))


if __name__ == '__main__':
    main()
