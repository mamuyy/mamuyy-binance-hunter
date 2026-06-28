# CP-042 Score >=95 Forward Validation / Paper-only Gate Audit

- Verdict: **REVIEW**
- Phase 3 status: **LOCKED**
- Classifier gate: **FROZEN**
- PAPER_ONLY: **true**
- Runtime changed: **false**
- Execution changed: **false**
- Registry changed: **false**

## Dataset Overview

- Rows: 1043
- Source distribution: `{'historical_outcomes': 597, 'internal_paper_trades': 446}`
- Target distribution: `{'LOSS': 554, 'TP1 HIT': 190, 'WIN': 299}`
- Score >=95 rows: 107

## Verdict Evidence

- IPT valid forward segments: 9
- IPT pass segments: 7
- IPT fail segments: 2
- IPT low-sample segments: 2
- Major contradiction: True

## Gate Policy Recommendation Draft

Draft only: retain score >=95 as a PAPER_ONLY candidate gate requiring source-aware IPT/live-like evidence, minimum rolling-window evidence, no automatic execution, no model promotion, and no Phase 3 unlock.

## Next Recommendation

Keep Phase 3 LOCKED and classifier promotion on HOLD. Continue PAPER_ONLY forward collection until live-like rolling windows provide sufficient source-aware evidence.
