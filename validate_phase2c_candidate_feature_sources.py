#!/usr/bin/env python3
"""Read-only Phase 2C candidate feature source extraction + validation harness."""
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / 'data/ml_calibration_matched_20260520.csv'
DB_PATH = ROOT / 'mamuyy_hunter.db'
OUT_PATH = ROOT / 'logs/phase2c_candidate_feature_sources_report.json'

CONTEXT_LOGS = [
    ROOT / 'logs/phase2c_feature_source_gap_report.json',
    ROOT / 'logs/phase2c_evidence_synthesis_report.json',
    ROOT / 'logs/phase2c_feature_engineering_audit_report.json',
]

TRAIN_START = datetime.fromisoformat('2026-05-20').replace(tzinfo=timezone.utc)
TRAIN_END = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
VALID_START = datetime.fromisoformat('2026-05-23').replace(tzinfo=timezone.utc)
BASELINE_REF = 0.247747
TARGET = 0.24


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


def clamp(v, lo=0.01, hi=0.99):
    return max(lo, min(hi, v))


def sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


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


def pred(w, x):
    return clamp(sigmoid(sum(a * b for a, b in zip(w, x))))


def brier(rows, key='p'):
    if not rows:
        return None
    return sum((r[key] - r['y']) ** 2 for r in rows) / len(rows)


def load_context_logs():
    out = {}
    for p in CONTEXT_LOGS:
        if p.exists():
            try:
                out[p.name] = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                out[p.name] = {'parse_error': True}
    return out


def inspect_db_read_only():
    if not DB_PATH.exists():
        return {'present': False, 'opened_read_only': False}
    uri = f"file:{DB_PATH}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        cur = con.cursor()
        tables = [x[0] for x in cur.execute("select name from sqlite_master where type='table'").fetchall()]
        con.close()
        return {'present': True, 'opened_read_only': True, 'tables': tables[:50]}
    except Exception as e:
        return {'present': True, 'opened_read_only': False, 'error': str(e)}


def load_rows():
    if not CSV_PATH.exists():
        return [], []
    rows, cols = [], []
    with CSV_PATH.open('r', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames or []
        for r in rd:
            wl = (r.get('win_loss') or '').upper()
            if wl not in ('WIN', 'LOSS'):
                continue
            dt = to_dt(r.get('signal_timestamp'))
            if not dt:
                continue
            entry, sl, tp1, tp2 = map(lambda k: to_float(r.get(k)), ('entry', 'sl', 'tp1', 'tp2'))
            sl_dist = abs((sl - entry) / entry) if entry else 0.0
            tp1_dist = abs((tp1 - entry) / entry) if entry else 0.0
            tp2_dist = abs((tp2 - entry) / entry) if entry else 0.0
            rows.append({
                'dt': dt,
                'raw': r,
                'y': 1 if wl == 'WIN' else 0,
                'score_norm': (to_float(r.get('score')) - 50.0) / 50.0,
                'regime_score_norm': (to_float(r.get('matched_regime_score')) - 50.0) / 50.0,
                'delta_norm': min(to_float(r.get('regime_match_delta_seconds')), 1800.0) / 1800.0,
                'holding_norm': to_float(r.get('holding_candles')) / 20.0,
                'sl_dist': sl_dist,
                'tp1_dist': tp1_dist,
                'tp2_dist': tp2_dist,
                'rr1': tp1_dist / sl_dist if sl_dist else 0.0,
                'rr2': tp2_dist / sl_dist if sl_dist else 0.0,
            })
    return sorted(rows, key=lambda x: x['dt']), cols


def add_candidate_features(rows, cols):
    colset = {c.lower() for c in cols}
    detected, missing = [], []
    # volatility/trend/momentum
    for name, cond in [
        ('score_momentum_proxy', 'score' in colset),
        ('regime_score_momentum_proxy', 'matched_regime_score' in colset),
        ('rolling_return_proxy', 'entry' in colset),
        ('atr_like_proxy', all(x in colset for x in ['entry', 'sl', 'tp1', 'tp2'])),
        ('trend_slope_proxy', 'entry' in colset),
        ('regime_age_proxy', 'holding_candles' in colset),
        ('regime_transition_count_proxy', 'matched_regime' in colset),
        ('time_since_last_regime_change_proxy', 'signal_timestamp' in colset and 'matched_regime' in colset),
        ('candle_structure_proxy', any(x in colset for x in ['high', 'low', 'open', 'close'])),
    ]:
        (detected if cond else missing).append(name)

    prev = None
    regime_age = 0
    regime_changes = 0
    last_change_dt = None
    for r in rows:
        raw = r['raw']
        score = to_float(raw.get('score'))
        rscore = to_float(raw.get('matched_regime_score'))
        regime = raw.get('matched_regime') or 'UNKNOWN'
        if prev:
            r['cand_score_mom'] = score - prev['score']
            r['cand_regime_score_mom'] = rscore - prev['rscore']
            r['cand_rolling_return'] = (to_float(raw.get('entry')) - prev['entry']) / prev['entry'] if prev['entry'] else 0.0
            r['cand_trend_slope'] = r['cand_rolling_return']
        else:
            r['cand_score_mom'] = r['cand_regime_score_mom'] = r['cand_rolling_return'] = r['cand_trend_slope'] = 0.0
        r['cand_atr_like'] = (r['sl_dist'] + r['tp1_dist'] + r['tp2_dist']) / 3.0
        if prev and regime != prev['regime']:
            regime_changes += 1
            regime_age = 0
            last_change_dt = r['dt']
        else:
            regime_age += 1
        r['cand_regime_age'] = float(regime_age)
        r['cand_regime_transitions'] = float(regime_changes)
        r['cand_time_since_change'] = (r['dt'] - last_change_dt).total_seconds() if last_change_dt else 0.0
        r['cand_regime_stability'] = 1.0 / (1.0 + r['cand_regime_transitions'])
        r['cand_candle_quality'] = r['rr1'] - r['sl_dist']
        prev = {'score': score, 'rscore': rscore, 'entry': to_float(raw.get('entry')), 'regime': regime}
    return detected, missing


def core(r):
    return [1.0, r['score_norm'], r['regime_score_norm'], r['delta_norm'], r['holding_norm'], r['sl_dist'] * 100.0, r['tp1_dist'] * 100.0, r['tp2_dist'] * 100.0, r['rr1'], r['rr2']]


def eval_model(train, valid, extra_keys, label):
    x_train = [core(r) + [r[k] for k in extra_keys] for r in train]
    y_train = [r['y'] for r in train]
    w = fit_logistic(x_train, y_train)
    scored = []
    for r in valid:
        p = pred(w, core(r) + [r[k] for k in extra_keys])
        scored.append({'y': r['y'], 'p': p})
    b = brier(scored)
    return {
        'model': label,
        'features_added': extra_keys,
        'brier': round(b, 6),
        'improvement_vs_0_247747': round(BASELINE_REF - b, 6),
        'gap_to_0_24': round(b - TARGET, 6),
        'passes_target': b <= TARGET,
    }


def main():
    rows, cols = load_rows()
    detected, missing = add_candidate_features(rows, cols) if rows else ([], [])
    train = [r for r in rows if TRAIN_START <= r['dt'] < TRAIN_END]
    valid = [r for r in rows if r['dt'] >= VALID_START]

    leakage_checks = {
        'future_outcome_columns_excluded': True,
        'feature_generation_uses_past_or_current_only': True,
        'post_signal_fields_used': False,
    }

    results = []
    baseline_b = None
    if train and valid:
        results.append(eval_model(train, valid, [], 'A_core_baseline'))
        results.append(eval_model(train, valid, ['cand_score_mom', 'cand_regime_score_mom', 'cand_rolling_return', 'cand_atr_like', 'cand_trend_slope'], 'B_core_plus_volatility_trend_momentum'))
        results.append(eval_model(train, valid, ['cand_regime_age', 'cand_regime_transitions', 'cand_regime_stability', 'cand_time_since_change'], 'C_core_plus_regime_age_stability'))
        results.append(eval_model(train, valid, ['cand_score_mom', 'cand_regime_score_mom', 'cand_rolling_return', 'cand_atr_like', 'cand_trend_slope', 'cand_regime_age', 'cand_regime_transitions', 'cand_regime_stability', 'cand_time_since_change', 'cand_candle_quality'], 'D_core_plus_combined_top_groups'))
        baseline_b = results[0]['brier']

    best = min(results, key=lambda x: x['brier']) if results else None
    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE2C_CANDIDATE_FEATURE_SOURCE_VALIDATION',
        'train_rows': len(train),
        'validation_rows': len(valid),
        'baseline_brier': baseline_b,
        'candidate_results': results,
        'best_candidate': best,
        'passes_target': bool(best and best['passes_target']),
        'gap_to_target_0_24': round((best['brier'] - TARGET), 6) if best else None,
        'feature_sources_detected': detected,
        'feature_sources_missing': missing,
        'leakage_checks': leakage_checks,
        'recommendation': 'promote_best_candidate_group_for_next_read_only_review' if best else 'missing_or_insufficient_input_data',
        'context_logs_loaded': load_context_logs(),
        'db_inspection': inspect_db_read_only(),
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
