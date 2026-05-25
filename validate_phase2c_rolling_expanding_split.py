#!/usr/bin/env python3
"""Read-only Phase 2C rolling/expanding split validation."""
import csv
import json
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


def clamp(v, lo=1e-6, hi=1 - 1e-6):
    return max(lo, min(hi, v))


def brier(rows, key):
    if not rows:
        return None
    return sum((clamp(r.get(key, 0.5)) - r['y']) ** 2 for r in rows) / len(rows)


def psi_like(train_rows, valid_rows):
    if not train_rows or not valid_rows:
        return None
    train_counts, valid_counts = {}, {}
    for r in train_rows:
        train_counts[r['regime']] = train_counts.get(r['regime'], 0) + 1
    for r in valid_rows:
        valid_counts[r['regime']] = valid_counts.get(r['regime'], 0) + 1
    regimes = set(train_counts) | set(valid_counts)
    t_total, v_total = len(train_rows), len(valid_rows)
    eps, score = 1e-9, 0.0
    for reg in regimes:
        p = train_counts.get(reg, 0) / t_total
        q = valid_counts.get(reg, 0) / v_total
        p, q = max(p, eps), max(q, eps)
        score += (p - q) * __import__('math').log(p / q)
    return score


def maybe_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_rows():
    rows = []
    if not CSV_PATH.exists():
        return rows
    with CSV_PATH.open('r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            wl = row.get('win_loss') or ''
            if wl not in ('WIN', 'LOSS'):
                continue
            rows.append({
                'signal_dt': to_dt(row.get('signal_timestamp')),
                'regime': row.get('matched_regime') or 'UNKNOWN',
                'y': 1 if wl == 'WIN' else 0,
                'global_prob': to_float(row.get('global_prob'), 0.5),
                'core_features_prob': to_float(row.get('core_features_prob'), 0.5),
            })
    return sorted([r for r in rows if r['signal_dt'] is not None], key=lambda x: x['signal_dt'])


def fold_metrics(train_rows, valid_rows):
    train_win = (sum(r['y'] for r in train_rows) / len(train_rows)) if train_rows else None
    valid_win = (sum(r['y'] for r in valid_rows) / len(valid_rows)) if valid_rows else None
    core_brier = brier(valid_rows, 'core_features_prob')
    return {
        'train_rows': len(train_rows),
        'validation_rows': len(valid_rows),
        'train_winrate': round(train_win, 6) if train_win is not None else None,
        'validation_winrate': round(valid_win, 6) if valid_win is not None else None,
        'regime_psi_like': round(psi_like(train_rows, valid_rows), 6) if train_rows and valid_rows else None,
        'brier_global_prob': round(brier(valid_rows, 'global_prob'), 6) if valid_rows else None,
        'brier_core_features_prob': round(core_brier, 6) if core_brier is not None else None,
        'brier_gap_to_0_24': round(core_brier - TARGET_BRIER, 6) if core_brier is not None else None,
        'passes_target': bool(core_brier is not None and core_brier <= TARGET_BRIER),
    }


def aggregate(name, folds, fixed_brier):
    vals = [f['brier_core_features_prob'] for f in folds if f.get('brier_core_features_prob') is not None]
    if not vals:
        return {'strategy': name, 'folds': len(folds), 'has_data': False}
    vals_sorted = sorted(vals)
    avg = sum(vals) / len(vals)
    med = vals_sorted[len(vals_sorted) // 2] if len(vals_sorted) % 2 else (vals_sorted[len(vals_sorted)//2 - 1] + vals_sorted[len(vals_sorted)//2]) / 2
    best = min(folds, key=lambda x: x['brier_core_features_prob'])
    worst = max(folds, key=lambda x: x['brier_core_features_prob'])
    pass_pct = 100.0 * sum(1 for v in vals if v <= TARGET_BRIER) / len(vals)
    delta_vs_fixed = avg - fixed_brier if fixed_brier is not None else None
    return {
        'strategy': name,
        'folds': len(folds),
        'average_brier_core_features_prob': round(avg, 6),
        'median_brier_core_features_prob': round(med, 6),
        'best_fold': {'fold_id': best['fold_id'], 'brier_core_features_prob': best['brier_core_features_prob']},
        'worst_fold': {'fold_id': worst['fold_id'], 'brier_core_features_prob': worst['brier_core_features_prob']},
        'pass_rate_pct_brier_lte_0_24': round(pass_pct, 2),
        'avg_brier_delta_vs_fixed': round(delta_vs_fixed, 6) if delta_vs_fixed is not None else None,
        'materially_better_than_fixed': bool(delta_vs_fixed is not None and delta_vs_fixed <= -0.002),
    }


def main():
    rows = load_rows()
    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE_2C_ROLLING_EXPANDING_SPLIT',
        'inputs': {
            'csv': str(CSV_PATH),
            'diagnosis_report_present': DIAG_PATH.exists(),
            'data_sufficiency_report_present': SUFF_PATH.exists(),
            'unstable_bucket_exclusion_report_present': UNSTABLE_PATH.exists(),
        },
        'fixed_split_result': None,
        'rolling_results': [],
        'expanding_results': [],
        'aggregate_summary': {},
        'best_strategy': None,
        'passes_target': False,
        'recommendation': 'insufficient_data_or_missing_csv',
        'context_reports': {
            'brier_failure_diagnosis': maybe_json(DIAG_PATH),
            'data_sufficiency': maybe_json(SUFF_PATH),
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
    report['fixed_split_result'] = fold_metrics(fixed_train, fixed_valid)

    train_size, valid_size, step = 3000, 1000, 1000
    for i, start in enumerate(range(0, max(0, len(rows) - (train_size + valid_size)) + 1, step), 1):
        train = rows[start:start + train_size]
        valid = rows[start + train_size:start + train_size + valid_size]
        if len(train) < 300 or len(valid) < 100:
            continue
        m = fold_metrics(train, valid)
        m['fold_id'] = f'rolling_{i}'
        m['train_start'] = train[0]['signal_dt'].isoformat()
        m['train_end'] = train[-1]['signal_dt'].isoformat()
        m['valid_start'] = valid[0]['signal_dt'].isoformat()
        m['valid_end'] = valid[-1]['signal_dt'].isoformat()
        report['rolling_results'].append(m)

    initial_train, valid_size, step = 3000, 1000, 1000
    fold_no = 1
    for end_train in range(initial_train, len(rows) - valid_size + 1, step):
        train = rows[:end_train]
        valid = rows[end_train:end_train + valid_size]
        if len(train) < 300 or len(valid) < 100:
            continue
        m = fold_metrics(train, valid)
        m['fold_id'] = f'expanding_{fold_no}'
        m['train_start'] = train[0]['signal_dt'].isoformat()
        m['train_end'] = train[-1]['signal_dt'].isoformat()
        m['valid_start'] = valid[0]['signal_dt'].isoformat()
        m['valid_end'] = valid[-1]['signal_dt'].isoformat()
        report['expanding_results'].append(m)
        fold_no += 1

    fixed_brier = report['fixed_split_result'].get('brier_core_features_prob') if report['fixed_split_result'] else None
    rolling_agg = aggregate('rolling', report['rolling_results'], fixed_brier)
    expanding_agg = aggregate('expanding', report['expanding_results'], fixed_brier)
    fixed_summary = {
        'strategy': 'fixed',
        'brier_core_features_prob': fixed_brier,
        'pass_rate_pct_brier_lte_0_24': 100.0 if fixed_brier is not None and fixed_brier <= TARGET_BRIER else 0.0,
    }
    report['aggregate_summary'] = {'fixed': fixed_summary, 'rolling': rolling_agg, 'expanding': expanding_agg}

    candidates = [
        ('fixed', fixed_brier),
        ('rolling', rolling_agg.get('average_brier_core_features_prob')),
        ('expanding', expanding_agg.get('average_brier_core_features_prob')),
    ]
    candidates = [(k, v) for k, v in candidates if v is not None]
    if candidates:
        best = min(candidates, key=lambda x: x[1])
        report['best_strategy'] = {'name': best[0], 'brier_core_features_prob': round(best[1], 6)}
        report['passes_target'] = best[1] <= TARGET_BRIER
        if report['passes_target']:
            report['recommendation'] = f'paper_only_followup_on_{best[0]}_split_behavior'
        elif best[0] in ('rolling', 'expanding') and best[1] < (fixed_brier or 9):
            report['recommendation'] = f'{best[0]}_shows_relative_improvement_but_target_not_met_keep_phase2c_blocked'
        else:
            report['recommendation'] = 'no_material_brier_improvement_keep_phase2c_blocked'

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Fixed split core_features_prob Brier: {fixed_brier}')
    print(f'Rolling folds: {len(report["rolling_results"])} | Expanding folds: {len(report["expanding_results"])}')
    print(f'Best strategy: {report["best_strategy"]}')
    print(f'Report: {OUT_PATH}')


if __name__ == '__main__':
    main()
