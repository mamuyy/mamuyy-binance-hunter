#!/usr/bin/env python3
"""Read-only Phase 2C rolling/expanding split validation."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJECT_DIR = Path('/home/ubuntu/mamuyy-binance-hunter')
PROJECT_DIR = DEFAULT_PROJECT_DIR if DEFAULT_PROJECT_DIR.exists() else Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / 'data/ml_calibration_matched_20260520.csv'
DIAG_PATH = PROJECT_DIR / 'logs/phase2c_brier_failure_diagnosis.json'
SUFF_PATH = PROJECT_DIR / 'logs/phase2c_data_sufficiency_report.json'
UNSTABLE_PATH = PROJECT_DIR / 'logs/phase2c_unstable_bucket_exclusion_report.json'
OUT_PATH = PROJECT_DIR / 'logs/phase2c_rolling_expanding_split_report.json'
TRAIN_START, TRAIN_END, VALID_START = '2026-05-20', '2026-05-23', '2026-05-23'
TARGET_BRIER = 0.24
EXPECTED_BRIER = 0.247938
EXPECTED_TOL = 0.001


def to_dt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(v, default=0.0):
    try:
        return default if v in ('', None) else float(v)
    except Exception:
        return default


def clamp(v, lo=0.01, hi=0.99):
    return max(lo, min(hi, v))


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def fit_logistic(features, labels, lr=0.04, epochs=2200, l2=0.06):
    n, m = len(labels), len(features[0])
    w = [0.0] * m
    for _ in range(epochs):
        grad = [0.0] * m
        for x, y in zip(features, labels):
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            err = p - y
            for j in range(m):
                grad[j] += err * x[j]
        for j in range(m):
            reg = 0.0 if j == 0 else l2 * w[j]
            w[j] -= lr * ((grad[j] / n) + reg)
    return w


def predict(w, x):
    return clamp(sigmoid(sum(wi * xi for wi, xi in zip(w, x))))


def brier(rows, key):
    if not rows:
        return None
    return sum((r[key] - r['y']) ** 2 for r in rows) / len(rows)


def prob_stats(rows, key):
    if not rows:
        return {'prediction_min': None, 'prediction_max': None, 'prediction_mean': None, 'prediction_std': None}
    vals = [r[key] for r in rows]
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    return {
        'prediction_min': round(min(vals), 6),
        'prediction_max': round(max(vals), 6),
        'prediction_mean': round(mean, 6),
        'prediction_std': round(math.sqrt(var), 6),
    }


def psi_like(train_rows, valid_rows):
    if not train_rows or not valid_rows:
        return None
    tc, vc = {}, {}
    for r in train_rows:
        tc[r['regime']] = tc.get(r['regime'], 0) + 1
    for r in valid_rows:
        vc[r['regime']] = vc.get(r['regime'], 0) + 1
    regs = set(tc) | set(vc)
    t_total, v_total = len(train_rows), len(valid_rows)
    out = 0.0
    for reg in regs:
        t = max(tc.get(reg, 0) / t_total, 1e-6)
        v = max(vc.get(reg, 0) / v_total, 1e-6)
        out += (v - t) * math.log(v / t)
    return out


def maybe_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def core_feat(r):
    return [1.0, r['score_norm'], r['regime_score_norm'], r['delta_norm'], r['holding_norm'], r['sl_dist'] * 100.0, r['tp1_dist'] * 100.0, r['tp2_dist'] * 100.0, r['rr1'], r['rr2']]


def load_rows():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open('r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            wl = row.get('win_loss') or ''
            if wl not in ('WIN', 'LOSS'):
                continue
            entry = to_float(row.get('entry'))
            sl = to_float(row.get('sl'))
            tp1 = to_float(row.get('tp1'))
            tp2 = to_float(row.get('tp2'))
            regime_score = to_float(row.get('matched_regime_score'))
            delta = to_float(row.get('regime_match_delta_seconds'))
            holding = to_float(row.get('holding_candles'))
            score = to_float(row.get('score'))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            rr1 = tp1_dist / sl_dist if sl_dist else 0.0
            rr2 = tp2_dist / sl_dist if sl_dist else 0.0
            rows.append({
                'signal_dt': to_dt(row.get('signal_timestamp')),
                'regime': row.get('matched_regime') or 'UNKNOWN',
                'score_norm': (score - 50.0) / 50.0,
                'regime_score_norm': (regime_score - 50.0) / 50.0,
                'delta_norm': min(delta, 1800.0) / 1800.0,
                'holding_norm': holding / 20.0 if holding else 1.0,
                'sl_dist': sl_dist,
                'tp1_dist': tp1_dist,
                'tp2_dist': tp2_dist,
                'rr1': rr1,
                'rr2': rr2,
                'y': 1 if wl == 'WIN' else 0,
            })
    return sorted([r for r in rows if r['signal_dt'] is not None], key=lambda x: x['signal_dt'])


def score_fold(train_rows, valid_rows):
    global_prob = sum(r['y'] for r in train_rows) / len(train_rows)
    labels = [r['y'] for r in train_rows]
    w_core = fit_logistic([core_feat(r) for r in train_rows], labels, lr=0.04, epochs=2200, l2=0.06)
    scored = []
    for r in valid_rows:
        x = dict(r)
        x['global_prob'] = clamp(global_prob)
        x['core_features_prob'] = predict(w_core, core_feat(r))
        scored.append(x)
    return scored


def fold_metrics(fold_id, train_rows, valid_rows):
    scored = score_fold(train_rows, valid_rows)
    core_brier_raw = brier(scored, 'core_features_prob')
    return {
        'fold_id': fold_id,
        'train_rows': len(train_rows),
        'validation_rows': len(valid_rows),
        'train_winrate': round(sum(r['y'] for r in train_rows) / len(train_rows), 6),
        'validation_winrate': round(sum(r['y'] for r in valid_rows) / len(valid_rows), 6),
        'regime_psi_like': round(psi_like(train_rows, valid_rows), 6),
        'brier_global_prob': round(brier(scored, 'global_prob'), 6),
        'brier_core_features_prob': round(core_brier_raw, 6),
        'brier_gap_to_0_24': round(core_brier_raw - TARGET_BRIER, 6),
        'passes_target': core_brier_raw <= TARGET_BRIER,
        'debug_prediction_stats': {
            'global_prob': prob_stats(scored, 'global_prob'),
            'core_features_prob': prob_stats(scored, 'core_features_prob'),
        },
        '_brier_core_precise': core_brier_raw,
    }


def aggregate(name, folds, fixed_brier_raw):
    if not folds:
        return {'strategy': name, 'folds': 0, 'has_data': False}
    vals = [f['_brier_core_precise'] for f in folds]
    sorted_pairs = sorted([(f['_brier_core_precise'], i) for i, f in enumerate(folds)], key=lambda x: x[0])
    avg = sum(vals) / len(vals)
    med = sorted(vals)[len(vals) // 2] if len(vals) % 2 else (sorted(vals)[len(vals)//2 - 1] + sorted(vals)[len(vals)//2]) / 2
    best_idx, worst_idx = sorted_pairs[0][1], sorted_pairs[-1][1]
    return {
        'strategy': name,
        'folds': len(folds),
        'average_brier_core_features_prob': round(avg, 6),
        'median_brier_core_features_prob': round(med, 6),
        'best_fold': {'fold_id': folds[best_idx]['fold_id'], 'brier_core_features_prob': round(folds[best_idx]['_brier_core_precise'], 6)},
        'worst_fold': {'fold_id': folds[worst_idx]['fold_id'], 'brier_core_features_prob': round(folds[worst_idx]['_brier_core_precise'], 6)},
        'pass_rate_pct_brier_lte_0_24': round(100.0 * sum(1 for v in vals if v <= TARGET_BRIER) / len(vals), 2),
        'avg_brier_delta_vs_fixed': round(avg - fixed_brier_raw, 6) if fixed_brier_raw is not None else None,
        'materially_better_than_fixed': bool(fixed_brier_raw is not None and (avg - fixed_brier_raw) <= -0.002),
    }


def main():
    rows = load_rows()
    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE_2C_ROLLING_EXPANDING_SPLIT',
        'fixed_split_reproduction_ok': False,
        'fixed_split_expected_brier': EXPECTED_BRIER,
        'fixed_split_actual_brier': None,
        'fixed_split_delta': None,
        'validation_error': None,
        'fixed_split_result': None,
        'rolling_results': [],
        'expanding_results': [],
        'aggregate_summary': {},
        'best_strategy': None,
        'passes_target': False,
        'recommendation': 'insufficient_data_or_missing_csv',
        'inputs': {
            'csv': str(CSV_PATH),
            'diagnosis_report_present': DIAG_PATH.exists(),
            'data_sufficiency_report_present': SUFF_PATH.exists(),
            'unstable_bucket_exclusion_report_present': UNSTABLE_PATH.exists(),
        },
        'context_reports': {
            'brier_failure_diagnosis': maybe_json(DIAG_PATH),
            'data_sufficiency': maybe_json(SUFF_PATH),
            'unstable_bucket_exclusion': maybe_json(UNSTABLE_PATH),
        },
        'safety': {'db_write': False, 'execution_change': False, 'production_scoring_change': False, 'phase_3': False, 'real_execution': 'blocked'},
    }
    if not rows:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'Report: {OUT_PATH}')
        return

    ts = datetime.fromisoformat(TRAIN_START).replace(tzinfo=timezone.utc)
    te = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc)
    vs = datetime.fromisoformat(VALID_START).replace(tzinfo=timezone.utc)
    fixed_train = [r for r in rows if ts <= r['signal_dt'] < te]
    fixed_valid = [r for r in rows if r['signal_dt'] >= vs]
    if not fixed_train or not fixed_valid:
        report['validation_error'] = 'fixed_split_has_no_data'
        report['recommendation'] = 'invalid_experiment_fix_reproduction_first'
    else:
        fixed = fold_metrics('fixed', fixed_train, fixed_valid)
        report['fixed_split_result'] = {k: v for k, v in fixed.items() if not k.startswith('_')}
        fixed_raw = fixed['_brier_core_precise']
        delta = fixed_raw - EXPECTED_BRIER
        report['fixed_split_actual_brier'] = round(fixed_raw, 6)
        report['fixed_split_delta'] = round(delta, 6)
        report['fixed_split_reproduction_ok'] = abs(delta) <= EXPECTED_TOL
        if not report['fixed_split_reproduction_ok']:
            report['validation_error'] = 'fixed_split_brier_mismatch_gt_0.001'
            report['recommendation'] = 'invalid_experiment_fix_reproduction_first'

        train_size, valid_size, step = 3000, 1000, 1000
        for i, start in enumerate(range(0, max(0, len(rows) - (train_size + valid_size)) + 1, step), 1):
            train = rows[start:start + train_size]
            valid = rows[start + train_size:start + train_size + valid_size]
            if len(train) < 300 or len(valid) < 100:
                continue
            m = fold_metrics(f'rolling_{i}', train, valid)
            m.update({'train_start': train[0]['signal_dt'].isoformat(), 'train_end': train[-1]['signal_dt'].isoformat(), 'valid_start': valid[0]['signal_dt'].isoformat(), 'valid_end': valid[-1]['signal_dt'].isoformat()})
            report['rolling_results'].append(m)

        for i, end_train in enumerate(range(3000, len(rows) - 1000 + 1, 1000), 1):
            train, valid = rows[:end_train], rows[end_train:end_train + 1000]
            if len(train) < 300 or len(valid) < 100:
                continue
            m = fold_metrics(f'expanding_{i}', train, valid)
            m.update({'train_start': train[0]['signal_dt'].isoformat(), 'train_end': train[-1]['signal_dt'].isoformat(), 'valid_start': valid[0]['signal_dt'].isoformat(), 'valid_end': valid[-1]['signal_dt'].isoformat()})
            report['expanding_results'].append(m)

        rolling_agg = aggregate('rolling', report['rolling_results'], fixed_raw)
        expanding_agg = aggregate('expanding', report['expanding_results'], fixed_raw)
        report['aggregate_summary'] = {'fixed': {'strategy': 'fixed', 'brier_core_features_prob': round(fixed_raw, 6)}, 'rolling': rolling_agg, 'expanding': expanding_agg}

        if report['fixed_split_reproduction_ok']:
            candidates = [('fixed', fixed_raw)]
            if rolling_agg.get('average_brier_core_features_prob') is not None:
                candidates.append(('rolling', rolling_agg['average_brier_core_features_prob']))
            if expanding_agg.get('average_brier_core_features_prob') is not None:
                candidates.append(('expanding', expanding_agg['average_brier_core_features_prob']))
            best = min(candidates, key=lambda x: x[1])
            report['best_strategy'] = {'name': best[0], 'brier_core_features_prob': round(best[1], 6)}
            report['passes_target'] = best[1] <= TARGET_BRIER
            report['recommendation'] = ('paper_only_followup_on_' + best[0] + '_split_behavior') if report['passes_target'] else 'target_not_met_keep_phase2c_blocked'
        else:
            report['recommendation'] = 'invalid_experiment_fix_reproduction_first'

        for container in (report['rolling_results'], report['expanding_results']):
            for f in container:
                f.pop('_brier_core_precise', None)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Fixed split core_features_prob Brier: {report["fixed_split_actual_brier"]}')
    print(f'Fixed reproduction OK: {report["fixed_split_reproduction_ok"]}')
    print(f'Report: {OUT_PATH}')


if __name__ == '__main__':
    main()
