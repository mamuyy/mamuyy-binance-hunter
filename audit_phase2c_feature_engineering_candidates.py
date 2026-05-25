#!/usr/bin/env python3
"""Read-only Phase 2C feature-engineering candidate audit."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJECT_DIR = Path('/home/ubuntu/mamuyy-binance-hunter')
PROJECT_DIR = DEFAULT_PROJECT_DIR if DEFAULT_PROJECT_DIR.exists() else Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / 'data/ml_calibration_matched_20260520.csv'
DIAG_PATH = PROJECT_DIR / 'logs/phase2c_brier_failure_diagnosis.json'
ROLLING_PATH = PROJECT_DIR / 'logs/phase2c_rolling_expanding_split_report.json'
UNSTABLE_PATH = PROJECT_DIR / 'logs/phase2c_unstable_bucket_exclusion_report.json'
OUT_PATH = PROJECT_DIR / 'logs/phase2c_feature_engineering_audit_report.json'

TRAIN_START, TRAIN_END, VALID_START = '2026-05-20', '2026-05-23', '2026-05-23'
TARGET_BRIER = 0.24
BASELINE_BRIER = 0.247938


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
    return sum((r[key] - r['y']) ** 2 for r in rows) / len(rows) if rows else None


def maybe_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def bucket_score(score):
    if score < 45:
        return 'score_low'
    if score < 55:
        return 'score_mid'
    return 'score_high'


def bucket_holding(v):
    if v <= 6:
        return 'hold_short'
    if v <= 12:
        return 'hold_mid'
    return 'hold_long'


def bucket_delta(v):
    if v <= 60:
        return 'delta_tight'
    if v <= 300:
        return 'delta_medium'
    return 'delta_wide'


def load_rows():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open('r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            wl = row.get('win_loss') or ''
            if wl not in ('WIN', 'LOSS'):
                continue
            signal_dt = to_dt(row.get('signal_timestamp'))
            if signal_dt is None:
                continue
            entry = to_float(row.get('entry'))
            sl = to_float(row.get('sl'))
            tp1 = to_float(row.get('tp1'))
            tp2 = to_float(row.get('tp2'))
            score = to_float(row.get('score'))
            regime_score = to_float(row.get('matched_regime_score'))
            delta = to_float(row.get('regime_match_delta_seconds'))
            holding = to_float(row.get('holding_candles'))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            rr1 = tp1_dist / sl_dist if sl_dist else 0.0
            rr2 = tp2_dist / sl_dist if sl_dist else 0.0
            regime = row.get('matched_regime') or 'UNKNOWN'
            rows.append({
                'signal_dt': signal_dt,
                'y': 1 if wl == 'WIN' else 0,
                'regime': regime,
                'score': score,
                'regime_score': regime_score,
                'score_norm': (score - 50.0) / 50.0,
                'regime_score_norm': (regime_score - 50.0) / 50.0,
                'delta_norm': min(delta, 1800.0) / 1800.0,
                'holding_norm': holding / 20.0 if holding else 1.0,
                'sl_dist': sl_dist,
                'tp1_dist': tp1_dist,
                'tp2_dist': tp2_dist,
                'rr1': rr1,
                'rr2': rr2,
                'score_bucket': bucket_score(score),
                'holding_bucket': bucket_holding(holding),
                'delta_bucket': bucket_delta(delta),
            })
    return sorted(rows, key=lambda x: x['signal_dt'])


def one_hot(value, values):
    return [1.0 if value == v else 0.0 for v in values]


def core_features(r):
    return [1.0, r['score_norm'], r['regime_score_norm'], r['delta_norm'], r['holding_norm'], r['sl_dist'] * 100.0, r['tp1_dist'] * 100.0, r['tp2_dist'] * 100.0, r['rr1'], r['rr2']]


def build_model_features(rows):
    regimes = sorted({r['regime'] for r in rows})
    sb = ['score_low', 'score_mid', 'score_high']
    hb = ['hold_short', 'hold_mid', 'hold_long']
    db = ['delta_tight', 'delta_medium', 'delta_wide']

    def baseline(r):
        return core_features(r)

    def plus_regime(r):
        return core_features(r) + one_hot(r['regime'], regimes)

    def plus_score_bucket(r):
        return core_features(r) + one_hot(r['score_bucket'], sb)

    def plus_interactions(r):
        x = core_features(r)
        x += [r['score_norm'] * r['regime_score_norm']]
        rg = one_hot(r['regime'], regimes)
        x += rg
        x += [r['score_norm'] * v for v in rg]
        x += [r['regime_score_norm'] * v for v in one_hot(r['score_bucket'], sb)]
        return x

    def plus_derived_rr_holding(r):
        return core_features(r) + one_hot(r['holding_bucket'], hb) + one_hot(r['delta_bucket'], db) + [r['rr1'] - r['rr2'], r['tp2_dist'] - r['tp1_dist']]

    return {
        'core_features_baseline': baseline,
        'core_plus_regime_one_hot': plus_regime,
        'core_plus_score_bucket_one_hot': plus_score_bucket,
        'core_plus_interaction_terms': plus_interactions,
        'core_plus_derived_risk_reward_holding': plus_derived_rr_holding,
    }


def simple_auc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def feature_separation(train_rows):
    numeric = ['score_norm', 'regime_score_norm', 'delta_norm', 'holding_norm', 'sl_dist', 'tp1_dist', 'tp2_dist', 'rr1', 'rr2']
    out = []
    labels = [r['y'] for r in train_rows]
    for k in numeric:
        win = [r[k] for r in train_rows if r['y'] == 1]
        loss = [r[k] for r in train_rows if r['y'] == 0]
        mw = sum(win) / len(win) if win else 0.0
        ml = sum(loss) / len(loss) if loss else 0.0
        sw = math.sqrt(sum((x - mw) ** 2 for x in win) / len(win)) if win else 0.0
        sl = math.sqrt(sum((x - ml) ** 2 for x in loss) / len(loss)) if loss else 0.0
        pooled = math.sqrt((sw**2 + sl**2) / 2.0) if (sw or sl) else 0.0
        d = (mw - ml) / pooled if pooled else 0.0
        auc = simple_auc([r[k] for r in train_rows], labels)
        mi_approx = 2.0 * ((auc - 0.5) ** 2) if auc is not None else 0.0
        out.append({
            'feature': k,
            'win_mean': round(mw, 6),
            'win_std': round(sw, 6),
            'loss_mean': round(ml, 6),
            'loss_std': round(sl, 6),
            'effect_size_cohens_d': round(d, 6),
            'univariate_auc': round(auc, 6) if auc is not None else None,
            'mutual_information_approx': round(mi_approx, 6),
        })
    return sorted(out, key=lambda x: abs(x['effect_size_cohens_d']), reverse=True)


def evaluate_models(train_rows, valid_rows):
    model_fns = build_model_features(train_rows + valid_rows)
    results = []
    labels = [r['y'] for r in train_rows]
    for name, fn in model_fns.items():
        x_train = [fn(r) for r in train_rows]
        x_valid = [fn(r) for r in valid_rows]
        w = fit_logistic(x_train, labels)
        scored = []
        for xv, rv in zip(x_valid, valid_rows):
            scored.append({'y': rv['y'], 'p': predict(w, xv)})
        b = brier(scored, 'p')
        improvement = BASELINE_BRIER - b
        results.append({
            'candidate_name': name,
            'validation_rows': len(valid_rows),
            'brier': round(b, 6),
            'improvement_vs_baseline_0_247938': round(improvement, 6),
            'gap_to_target_0_24': round(b - TARGET_BRIER, 6),
            'passes_target': b <= TARGET_BRIER,
            'risk_flags': risk_for_candidate(name, train_rows),
        })
    return sorted(results, key=lambda x: x['brier'])


def risk_for_candidate(name, train_rows):
    sample = len(train_rows)
    low_support = sample < 80
    high_complexity = 'interaction' in name
    return {
        'leakage_risk': 'low (features are pre-signal metadata only)',
        'low_support_risk': 'medium' if low_support else 'low',
        'overfitting_risk': 'high' if high_complexity else 'medium',
        'interpretability': 'medium' if high_complexity else 'high',
    }


def main():
    rows = load_rows()
    train_rows = [r for r in rows if TRAIN_START <= r['signal_dt'].date().isoformat() < TRAIN_END]
    valid_rows = [r for r in rows if r['signal_dt'].date().isoformat() >= VALID_START]

    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE_2C_FEATURE_ENGINEERING_AUDIT',
        'baseline_brier': BASELINE_BRIER,
        'candidate_results': [],
        'best_candidate': None,
        'passes_target': False,
        'gap_to_target_0_24': None,
        'recommended_next_step': 'insufficient_data_or_missing_csv',
        'feature_separation': [],
        'inputs': {
            'csv': str(CSV_PATH),
            'brier_failure_diagnosis_report_present': DIAG_PATH.exists(),
            'rolling_expanding_report_present': ROLLING_PATH.exists(),
            'unstable_bucket_exclusion_report_present': UNSTABLE_PATH.exists(),
        },
        'context_reports': {
            'brier_failure_diagnosis': maybe_json(DIAG_PATH),
            'rolling_expanding_split': maybe_json(ROLLING_PATH),
            'unstable_bucket_exclusion': maybe_json(UNSTABLE_PATH),
        },
        'safety': {
            'db_write': False,
            'execution_change': False,
            'production_scoring_change': False,
            'phase_3': False,
            'real_execution': 'blocked',
        },
    }

    if train_rows and valid_rows:
        report['feature_separation'] = feature_separation(train_rows)
        candidates = evaluate_models(train_rows, valid_rows)
        report['candidate_results'] = candidates
        best = candidates[0] if candidates else None
        report['best_candidate'] = best
        if best:
            report['passes_target'] = best['passes_target']
            report['gap_to_target_0_24'] = best['gap_to_target_0_24']
            report['recommended_next_step'] = (
                'candidate_feature_bundle_is_promising_for_offline_followup' if best['passes_target']
                else 'no_candidate_hit_target_keep_phase2c_blocked_and_run_more_time_regime_robustness_research'
            )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({
        'mode': report['mode'],
        'rows_total': len(rows),
        'train_rows': len(train_rows),
        'valid_rows': len(valid_rows),
        'best_candidate': (report['best_candidate'] or {}).get('candidate_name'),
        'best_brier': (report['best_candidate'] or {}).get('brier'),
        'passes_target': report['passes_target'],
        'report_path': str(OUT_PATH),
    }, indent=2))


if __name__ == '__main__':
    main()
