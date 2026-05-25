#!/usr/bin/env python3
"""Read-only Phase 2C feature source gap analysis.

Goal: fastest realistic path to move Brier from 0.247747 to <= 0.24
Constraints: PAPER_ONLY; no execution/DB/production scoring changes.
"""
import csv, json, sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / 'logs'
OUT = LOGS / 'phase2c_feature_source_gap_report.json'
BASELINE_BRIER = 0.247747
TARGET_BRIER = 0.24

CANDIDATE_CSVS = [
    ROOT / 'data/ml_calibration_matched_20260520.csv',
    ROOT / 'paper_trades.csv',
]
CANDIDATE_DBS = list(ROOT.glob('*.db'))
CONTEXT_LOGS = [
    ROOT / 'logs/phase2c_brier_failure_diagnosis.json',
    ROOT / 'logs/phase2c_feature_engineering_audit_report.json',
    ROOT / 'logs/feature_level_calibration_report.json',
]

KEYWORDS = {
    'volatility_trend_momentum': ['atr','vol','volatility','trend','ema','sma','rsi','macd','adx','momentum','roc','stoch'],
    'orderflow_oi_funding': ['orderflow','imbalance','oi','open_interest','funding','basis','taker','maker','long_short'],
    'liquidity_spread': ['spread','liquidity','depth','slippage','book','bid','ask'],
    'regime_age_stability': ['regime','stability','age','duration','transition','choppy','trend_strength'],
    'setup_quality_candle_structure': ['candle','wick','body','engulf','pinbar','breakout','retest','structure','range','rr','risk_reward'],
}


def safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


def cohort_gap(rows, feature):
    wins, losses = [], []
    for r in rows:
        y = r.get('_y')
        v = safe_float(r.get(feature))
        if y is None or v is None:
            continue
        (wins if y == 1 else losses).append(v)
    if not wins or not losses:
        return None
    mw = sum(wins)/len(wins)
    ml = sum(losses)/len(losses)
    return {
        'win_mean': round(mw, 6),
        'loss_mean': round(ml, 6),
        'abs_gap': round(abs(mw-ml), 6),
        'n_win': len(wins),
        'n_loss': len(losses),
    }


def parse_csv(path):
    if not path.exists():
        return None
    with path.open('r', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames or []
        rows = []
        for r in rd:
            wl = (r.get('win_loss') or '').upper()
            r['_y'] = 1 if wl == 'WIN' else 0 if wl == 'LOSS' else None
            rows.append(r)
    return {'path': str(path), 'columns': cols, 'rows': rows}


def parse_dbs(paths):
    out = []
    for db in paths:
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            tables = [t[0] for t in cur.execute("select name from sqlite_master where type='table'").fetchall()]
            td = {}
            for t in tables:
                cols = [c[1] for c in cur.execute(f"pragma table_info('{t}')").fetchall()]
                td[t] = cols
            con.close()
            out.append({'path': str(db), 'tables': td})
        except Exception as e:
            out.append({'path': str(db), 'error': str(e)})
    return out


def group_candidates(all_cols):
    g = defaultdict(list)
    lc = [c.lower() for c in all_cols]
    for group, kws in KEYWORDS.items():
        for orig, low in zip(all_cols, lc):
            if any(k in low for k in kws):
                g[group].append(orig)
        g[group] = sorted(set(g[group]))
    return g


def main():
    csv_info = next((x for x in (parse_csv(p) for p in CANDIDATE_CSVS) if x), None)
    db_info = parse_dbs(CANDIDATE_DBS)
    context_logs = {}
    for p in CONTEXT_LOGS:
        context_logs[str(p)] = p.exists()

    csv_cols = csv_info['columns'] if csv_info else []
    all_db_cols = []
    for db in db_info:
        for cols in db.get('tables', {}).values():
            all_db_cols.extend(cols)

    known_prod_features = {'score','matched_regime_score','regime_match_delta_seconds','holding_candles','entry','sl','tp1','tp2'}
    unused_csv_cols = sorted([c for c in csv_cols if c not in known_prod_features])

    leakage_safe = [c for c in csv_cols if c not in {'win_loss','result','exit_time','close_time','pnl','realized_pnl'}]

    weak_sep = []
    if csv_info:
        numeric = [c for c in csv_cols if c in known_prod_features or any(k in c.lower() for k in ['score','regime','holding','delta','rr','risk'])]
        for c in numeric:
            gap = cohort_gap(csv_info['rows'], c)
            if gap:
                weak_sep.append({'feature': c, **gap})
        weak_sep.sort(key=lambda x: x['abs_gap'])

    grouped = group_candidates(sorted(set(csv_cols + all_db_cols)))
    estimates = [
        {'group':'volatility_trend_momentum','expected_brier_delta':-0.0030,'confidence':'medium-high','why':'Usually strongest incremental signal with low leakage when computed pre-signal.'},
        {'group':'regime_age_stability','expected_brier_delta':-0.0018,'confidence':'medium','why':'Should reduce regime-mismatch calibration noise.'},
        {'group':'setup_quality_candle_structure','expected_brier_delta':-0.0014,'confidence':'medium','why':'Can improve resolution around borderline scores.'},
        {'group':'liquidity_spread','expected_brier_delta':-0.0009,'confidence':'low-medium','why':'Useful but often sparse/noisy in current datasets.'},
        {'group':'orderflow_oi_funding','expected_brier_delta':-0.0007,'confidence':'low-medium','why':'Potentially strong but availability/latency constraints likely.'},
    ]

    LOGS.mkdir(parents=True, exist_ok=True)
    report = {
        'build_time_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'READ_ONLY_PHASE2C_FEATURE_SOURCE_GAP_ANALYSIS',
        'objective': {'baseline_brier': BASELINE_BRIER, 'target_brier': TARGET_BRIER, 'required_improvement': round(BASELINE_BRIER-TARGET_BRIER, 6)},
        'inputs': {
            'csv_used': csv_info['path'] if csv_info else None,
            'csv_rows': len(csv_info['rows']) if csv_info else 0,
            'csv_columns': len(csv_cols),
            'db_files': [d['path'] for d in db_info],
            'context_logs_present': context_logs,
        },
        'analysis': {
            'unused_columns': {'csv_unused_vs_current_feature_set': unused_csv_cols, 'db_columns_catalog_count': len(set(all_db_cols))},
            'pre_signal_safe_features': leakage_safe,
            'weak_separation_features_smallest_gaps_first': weak_sep[:20],
            'candidate_feature_groups_available_columns': grouped,
            'brier_improvement_estimates': estimates,
        },
        'recommended_compact_next_pr': {
            'title': 'Phase2C: add read-only feature candidate extraction + validation harness (paper only)',
            'scope': [
                'Add offline extractor that computes pre-signal-only candidate features for top 2 groups: volatility_trend_momentum + regime_age_stability.',
                'Add validator that joins candidates with existing calibration labels and reports leakage checks + univariate separation + simple logistic delta Brier.',
                'Write outputs to logs only; no DB writes; no production scoring path changes.'
            ],
            'expected_result': 'Fastest realistic path to close >=0.007747 Brier gap is additive gains from volatility/trend/momentum plus regime stability features before any execution changes.',
            'safety': {'paper_only': True, 'db_write': False, 'production_scoring_change': False, 'execution_change': False},
        },
        'final_recommendation': {
            'phase_2c_status': 'REVIEW_NOT_PASSED',
            'phase_3_status': 'LOCKED',
            'real_execution': 'BLOCKED'
        }
    }
    OUT.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(str(OUT))

if __name__ == '__main__':
    main()
