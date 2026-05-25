#!/usr/bin/env python3
"""Read-only Phase 2C volatility/trend/momentum ablation + robustness validation."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / 'data/ml_calibration_matched_20260520.csv'
OUT_PATH = ROOT / 'logs/phase2c_volatility_momentum_ablation_report.json'
CONTEXT_LOGS = [
    ROOT / 'logs/phase2c_candidate_feature_sources_report.json',
    ROOT / 'logs/phase2c_feature_source_gap_report.json',
    ROOT / 'logs/phase2c_evidence_synthesis_report.json',
]
TRAIN_START = datetime.fromisoformat('2026-05-20').replace(tzinfo=timezone.utc)
TRAIN_END = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
VALID_START = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
TARGET = 0.24
B_FEATURES = ['cand_score_mom', 'cand_regime_score_mom', 'cand_rolling_return', 'cand_atr_like', 'cand_trend_slope']


def to_float(v, d=0.0):
    try:
        return d if v in (None, '') else float(v)
    except Exception:
        return d


def to_dt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def clamp(v, lo=0.01, hi=0.99):
    return max(lo, min(hi, v))


def fit_logistic(x, y, lr=0.04, epochs=2000, l2=0.06):
    n, m = len(y), len(x[0])
    w = [0.0] * m
    for _ in range(epochs):
        g = [0.0] * m
        for row, yy in zip(x, y):
            p = sigmoid(sum(a * b for a, b in zip(w, row)))
            e = p - yy
            for j in range(m):
                g[j] += e * row[j]
        for j in range(m):
            reg = 0.0 if j == 0 else l2 * w[j]
            w[j] -= lr * ((g[j] / n) + reg)
    return w


def stats(values):
    if not values:
        return {'min': None, 'max': None, 'mean': None, 'std': None}
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return {
        'min': round(min(values), 6),
        'max': round(max(values), 6),
        'mean': round(mean, 6),
        'std': round(math.sqrt(var), 6),
    }


def brier(preds):
    return sum((p - y) ** 2 for p, y in preds) / len(preds) if preds else None


def core(r):
    return [1.0, r['score_norm'], r['regime_score_norm'], r['delta_norm'], r['holding_norm'], r['sl_dist'] * 100.0, r['tp1_dist'] * 100.0, r['tp2_dist'] * 100.0, r['rr1'], r['rr2']]


def load_context_logs():
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
        return []
    rows = []
    prev = None
    with CSV_PATH.open('r', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        for raw in rd:
            wl = (raw.get('win_loss') or '').upper()
            if wl not in ('WIN', 'LOSS'):
                continue
            dt = to_dt(raw.get('signal_timestamp'))
            if not dt:
                continue
            entry, sl, tp1, tp2 = map(lambda k: to_float(raw.get(k)), ('entry', 'sl', 'tp1', 'tp2'))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            row = {
                'dt': dt,
                'y': 1 if wl == 'WIN' else 0,
                'score_norm': (to_float(raw.get('score')) - 50.0) / 50.0,
                'regime_score_norm': (to_float(raw.get('matched_regime_score')) - 50.0) / 50.0,
                'delta_norm': min(to_float(raw.get('regime_match_delta_seconds')), 1800.0) / 1800.0,
                'holding_norm': to_float(raw.get('holding_candles')) / 20.0,
                'sl_dist': sl_dist,
                'tp1_dist': tp1_dist,
                'tp2_dist': tp2_dist,
                'rr1': tp1_dist / sl_dist if sl_dist else 0.0,
                'rr2': tp2_dist / sl_dist if sl_dist else 0.0,
                'entry': entry,
                'score_raw': to_float(raw.get('score')),
                'regime_score_raw': to_float(raw.get('matched_regime_score')),
            }
            if prev is None:
                row['cand_score_mom'] = 0.0
                row['cand_regime_score_mom'] = 0.0
                row['cand_rolling_return'] = 0.0
                row['cand_trend_slope'] = 0.0
            else:
                row['cand_score_mom'] = row['score_raw'] - prev['score_raw']
                row['cand_regime_score_mom'] = row['regime_score_raw'] - prev['regime_score_raw']
                row['cand_rolling_return'] = (entry - prev['entry']) / prev['entry'] if prev['entry'] else 0.0
                row['cand_trend_slope'] = row['cand_rolling_return']
            row['cand_atr_like'] = (sl_dist + tp1_dist + tp2_dist) / 3.0
            rows.append(row)
            prev = row
    return sorted(rows, key=lambda r: r['dt'])


def standardize(train, valid, keys):
    for k in keys:
        vals = [r.get(k, 0.0) for r in train]
        mean = sum(vals) / len(vals) if vals else 0.0
        var = sum((v - mean) ** 2 for v in vals) / len(vals) if vals else 0.0
        std = math.sqrt(var) if var > 1e-12 else 1.0
        for r in train + valid:
            r[k + '_z'] = (r.get(k, 0.0) - mean) / std


def evaluate(train, valid, feature_keys, label, baseline_brier, *, l2=0.06, clip=(0.01, 0.99)):
    x_train = [core(r) + [r[k + '_z'] for k in feature_keys] for r in train]
    y_train = [r['y'] for r in train]
    w = fit_logistic(x_train, y_train, l2=l2)
    preds = []
    for r in valid:
        raw_p = sigmoid(sum(a * b for a, b in zip(w, core(r) + [r[k + '_z'] for k in feature_keys])))
        p = clamp(raw_p, clip[0], clip[1])
        preds.append((p, r['y']))
    pred_vals = [p for p, _ in preds]
    b = brier(preds)
    s = stats(pred_vals)
    return {
        'model': label,
        'train_rows': len(train),
        'validation_rows': len(valid),
        'features_used': ['core'] + feature_keys,
        'brier': round(b, 6),
        'improvement_vs_baseline': round(baseline_brier - b, 6),
        'gap_to_0_24': round(b - TARGET, 6),
        'passes_target': b <= TARGET,
        'prediction_min': s['min'],
        'prediction_max': s['max'],
        'prediction_mean': s['mean'],
        'prediction_std': s['std'],
        'saturation_flag': bool((s['max'] is not None and s['max'] >= 0.95) or (s['min'] is not None and s['min'] <= 0.05)),
        'degraded_flag': bool((b - baseline_brier) > 0.01),
        'regularization_l2': l2,
        'prediction_clip': [clip[0], clip[1]],
    }


def recommendation(best):
    if not best:
        return 'missing_or_insufficient_input_data'
    if best['passes_target']:
        return 'target_met_in_read_only_validation_keep_phase3_locked_pending_review_gate'
    if best['saturation_flag']:
        return 'best_model_below_target_and_saturated_increase_regularization_or_revisit_features'
    return 'best_model_below_target_continue_read_only_feature_iteration'


def main():
    rows = load_rows()
    train = [r for r in rows if TRAIN_START <= r['dt'] < TRAIN_END]
    valid = [r for r in rows if r['dt'] >= VALID_START]

    results = []
    baseline = None
    if train and valid:
        standardize(train, valid, B_FEATURES)
        baseline = evaluate(train, valid, [], 'A_core_baseline', baseline_brier=0.0)
        baseline['improvement_vs_baseline'] = 0.0
        results.append(baseline)
        base_brier = baseline['brier']

        singles = []
        for f in B_FEATURES:
            r = evaluate(train, valid, [f], f'B_single_{f}', baseline_brier=base_brier)
            results.append(r)
            singles.append(r)

        top_sorted = sorted(singles, key=lambda x: x['brier'])
        top2 = [x['features_used'][-1] for x in top_sorted[:2]]
        top3 = [x['features_used'][-1] for x in top_sorted[:3]]
        results.append(evaluate(train, valid, top2, 'C_top2_from_singles', baseline_brier=base_brier))
        results.append(evaluate(train, valid, top3, 'D_top3_from_singles', baseline_brier=base_brier))
        all_b = evaluate(train, valid, B_FEATURES, 'E_all_b_features', baseline_brier=base_brier)
        results.append(all_b)

        if all_b['saturation_flag'] or (all_b['prediction_max'] is not None and all_b['prediction_max'] >= 0.99):
            results.append(evaluate(train, valid, B_FEATURES, 'F_all_b_features_stronger_reg_and_clip', baseline_brier=base_brier, l2=0.15, clip=(0.03, 0.97)))

    best = min(results, key=lambda x: x['brier']) if results else None
    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE2C_VOLATILITY_MOMENTUM_ABLATION',
        'baseline_brier': baseline['brier'] if baseline else None,
        'candidate_results': results,
        'best_candidate': best,
        'passes_target': bool(best and best['passes_target']),
        'gap_to_target_0_24': round(best['brier'] - TARGET, 6) if best else None,
        'saturation_risk': bool(best and best['saturation_flag']),
        'degraded_models': [x['model'] for x in results if x['degraded_flag']],
        'recommendation': recommendation(best),
        'phase2c_status': 'PASSED' if best and best['passes_target'] else 'REVIEW_NOT_PASSED',
        'phase3_status': 'LOCKED',
        'real_execution_status': 'BLOCKED',
        'context_logs_loaded': load_context_logs(),
        'safety': {
            'db_write': False,
            'execution_change': False,
            'production_scoring_change': False,
            'phase_3': False,
            'real_execution': 'blocked',
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(str(OUT_PATH))


if __name__ == '__main__':
    main()
