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


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


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
    x = [[float(r.get(k, 0.0)) for k in features] for r in rows]
    y = [r['y'] for r in rows]
    return x, y


def fit_logistic(x, y, lr=0.05, epochs=1400, l2=0.05):
    if not x:
        return []
    m = len(x[0])
    w = [0.0] * (m + 1)
    n = len(y)
    for _ in range(epochs):
        g = [0.0] * (m + 1)
        for row, yy in zip(x, y):
            z = w[0] + sum(w[j + 1] * row[j] for j in range(m))
            p = sigmoid(z)
            e = p - yy
            g[0] += e
            for j in range(m):
                g[j + 1] += e * row[j]
        w[0] -= lr * (g[0] / n)
        for j in range(1, m + 1):
            w[j] -= lr * ((g[j] / n) + l2 * w[j])
    return w


def predict_logistic(weights, x):
    if not x:
        return []
    m = len(x[0])
    out = []
    for row in x:
        z = weights[0] + sum(weights[j + 1] * row[j] for j in range(m))
        p = sigmoid(z)
        out.append(min(0.99, max(0.01, p)))
    return out


def fit_stump(x, y):
    if not x or not x[0]:
        return None
    best = None
    for j in range(len(x[0])):
        vals = sorted(set(row[j] for row in x))
        if len(vals) == 1:
            thresholds = vals
        else:
            thresholds = [(vals[i] + vals[i + 1]) / 2.0 for i in range(len(vals) - 1)]
        for t in thresholds[:64]:
            left_idx = [i for i, row in enumerate(x) if row[j] <= t]
            right_idx = [i for i, row in enumerate(x) if row[j] > t]
            lp = (sum(y[i] for i in left_idx) / len(left_idx)) if left_idx else 0.5
            rp = (sum(y[i] for i in right_idx) / len(right_idx)) if right_idx else 0.5
            preds = [lp if row[j] <= t else rp for row in x]
            b = brier(preds, y)
            if best is None or b < best['brier']:
                best = {'feature_idx': j, 'threshold': t, 'left_p': min(0.99, max(0.01, lp)), 'right_p': min(0.99, max(0.01, rp)), 'brier': b}
    return best


def predict_stump(stump, x):
    if not stump:
        return [0.5 for _ in x]
    j = stump['feature_idx']
    t = stump['threshold']
    return [stump['left_p'] if row[j] <= t else stump['right_p'] for row in x]


def summarize_result(model_name, feature_set, train_rows, valid_rows, p_tr, y_tr, p_va, y_va, baseline, feature_importance=None):
    b_tr = brier(p_tr, y_tr)
    b_va = brier(p_va, y_va)
    st = stats(p_va)
    return {
        'model_name': model_name,
        'feature_set': feature_set,
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
        'feature_importance': feature_importance,
    }


def main():
    rows, fields = load_rows()
    train = [r for r in rows if TRAIN_START <= r['dt'] < TRAIN_END]
    valid = [r for r in rows if r['dt'] >= VALID_START]
    results = []
    skipped = []

    # optional dependencies
    sklearn_available = False
    xgboost_available = False
    sklearn_models = []
    xgb_ctor = None

    try:
        from sklearn.ensemble import GradientBoostingClassifier, HistGradientBoostingClassifier, RandomForestClassifier
        sklearn_available = True
        sklearn_models = [
            ('RandomForestClassifier', lambda: RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=8, random_state=42)),
            ('GradientBoostingClassifier', lambda: GradientBoostingClassifier(random_state=42)),
            ('HistGradientBoostingClassifier', lambda: HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, random_state=42)),
        ]
    except Exception:
        skipped.extend([
            {'model_name': 'RandomForestClassifier', 'reason': 'sklearn_not_installed'},
            {'model_name': 'GradientBoostingClassifier', 'reason': 'sklearn_not_installed'},
            {'model_name': 'HistGradientBoostingClassifier', 'reason': 'sklearn_not_installed'},
        ])

    try:
        import xgboost as xgb
        xgboost_available = True
        xgb_ctor = lambda: xgb.XGBClassifier(n_estimators=250, max_depth=4, learning_rate=0.05, subsample=0.9, colsample_bytree=0.9, objective='binary:logistic', eval_metric='logloss', random_state=42)
    except Exception:
        skipped.append({'model_name': 'XGBoostClassifier', 'reason': 'xgboost_not_installed'})

    report = {
        'mode': 'READ_ONLY_PHASE2C_NONLINEAR_MODEL_EXPLORATION',
        'dependency_status': {'sklearn_available': sklearn_available, 'xgboost_available': xgboost_available},
        'skipped_models': skipped,
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

    core = ['score_norm', 'regime_score_norm', 'delta_norm', 'holding_norm', 'sl_dist', 'tp1_dist', 'tp2_dist', 'rr1', 'rr2']
    cand = ['cand_score_mom', 'cand_regime_score_mom', 'cand_rolling_return', 'cand_atr_like', 'cand_trend_slope']
    all_safe = sorted(set(core + cand + leakage_safe_numeric_fields(fields)))
    feature_sets = [('A_core', core), ('B_core_plus_proxy', sorted(set(core + cand))), ('C_all_numeric_pre_signal', all_safe)]

    # dependency-free baseline logistic on A_core
    xtr, ytr = build_matrix(train, core)
    xva, yva = build_matrix(valid, core)
    w = fit_logistic(xtr, ytr)
    p_tr = predict_logistic(w, xtr)
    p_va = predict_logistic(w, xva)
    base = summarize_result('LogisticBaselineInternal', 'A_core', train, valid, p_tr, ytr, p_va, yva, baseline=0.0)
    base['improvement_vs_baseline'] = 0.0
    report['baseline_brier'] = base['brier']
    results.append(base)

    for fs_name, feats in feature_sets:
        xtr, ytr = build_matrix(train, feats)
        xva, yva = build_matrix(valid, feats)

        # dependency-free logistic for each feature set
        w = fit_logistic(xtr, ytr)
        results.append(summarize_result('LogisticBaselineInternal', fs_name, train, valid, predict_logistic(w, xtr), ytr, predict_logistic(w, xva), yva, report['baseline_brier']))

        # dependency-free stump sanity nonlinear exploration
        stump = fit_stump(xtr, ytr)
        fi = None
        if stump:
            fi = [{'feature': feats[stump['feature_idx']], 'importance': 1.0}]
        results.append(summarize_result('DecisionStumpThreshold', fs_name, train, valid, predict_stump(stump, xtr), ytr, predict_stump(stump, xva), yva, report['baseline_brier'], feature_importance=fi))

        if sklearn_available:
            for model_name, mk in sklearn_models:
                model = mk()
                model.fit(xtr, ytr)
                p_tr = [float(p[1]) for p in model.predict_proba(xtr)]
                p_va = [float(p[1]) for p in model.predict_proba(xva)]
                fim = None
                if hasattr(model, 'feature_importances_'):
                    fim = sorted([{'feature': f, 'importance': round(float(i), 6)} for f, i in zip(feats, model.feature_importances_)], key=lambda x: x['importance'], reverse=True)[:12]
                results.append(summarize_result(model_name, fs_name, train, valid, p_tr, ytr, p_va, yva, report['baseline_brier'], feature_importance=fim))

        if xgboost_available and xgb_ctor:
            model = xgb_ctor()
            model.fit(xtr, ytr)
            p_tr = [float(p) for p in model.predict_proba(xtr)[:, 1]]
            p_va = [float(p) for p in model.predict_proba(xva)[:, 1]]
            fim = None
            if hasattr(model, 'feature_importances_'):
                fim = sorted([{'feature': f, 'importance': round(float(i), 6)} for f, i in zip(feats, model.feature_importances_)], key=lambda x: x['importance'], reverse=True)[:12]
            results.append(summarize_result('XGBoostClassifier', fs_name, train, valid, p_tr, ytr, p_va, yva, report['baseline_brier'], feature_importance=fim))

    # remove duplicate A_core logistic second run by keeping first per tuple
    dedup = []
    seen = set()
    for r in results:
        key = (r['model_name'], r['feature_set'])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)

    best = min(dedup, key=lambda x: x['brier']) if dedup else None
    report['model_results'] = dedup
    report['best_model'] = best
    report['passes_target'] = bool(best and best['passes_target'])
    report['gap_to_target_0_24'] = round(best['brier'] - TARGET, 6) if best else None
    report['interpretation'] = {
        'nonlinear_signal_found': bool(best and best['model_name'] not in ('LogisticBaselineInternal',) and best['improvement_vs_baseline'] > 0.002),
        'likely_data_feature_limitation': bool((best is None) or (best['brier'] > TARGET)),
    }

    if not sklearn_available:
        report['recommendation'] = 'cannot_complete_full_nonlinear_test_without_optional_dependencies_collect_more_data_or_run_in_env_with_sklearn'
    else:
        report['recommendation'] = (
            'A) promote nonlinear model to deeper read-only validation' if best and best['passes_target'] else
            'C) pivot to label/target redesign' if best and best['improvement_vs_baseline'] < 0.001 else
            'B) collect more outcomes and richer feature sources'
        )

    report['phase2c_status'] = 'PASSED' if best and best['passes_target'] else 'REVIEW_NOT_PASSED'

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(str(OUT_PATH))


if __name__ == '__main__':
    main()
