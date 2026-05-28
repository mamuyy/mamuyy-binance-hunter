# Canonical R&D Status

Last updated: 2026-05-28
Canonical mode: `PAPER_ONLY`
Execution gate: **LOCKED** — no Phase 3, no real execution, no broker order path, no auto-promotion.

This document is the single source of truth for current R&D status after auditing the latest repository commit history, `docs/`, `dashboard.py`, `telegram.py`, and `scripts/`. It supersedes older roadmap interpretations when they imply repeating completed research or moving toward live execution.

## Audit snapshot

- Latest audited branch in this checkout: `work` at `6589a4c` (`Merge pull request #53 from mamuyy/codex/polish-telegram-governance-intelligence-message`).
- Latest observed main-line history is dominated by governance intelligence, dashboard/Telegram presentation, cached report loading, and Phase 2 research closure.
- `reports/` is intentionally not a tracked source-of-truth directory in this checkout; it is ignored as generated/local research output. Treat missing tracked reports as normal unless a specific runtime artifact is required for a review.
- The previous source-of-truth doc still points future sessions to GitHub `main` as technical truth, but this status file is now the canonical R&D interpretation for what should and should not be repeated.


## 2026-05-28 Update — Phase 2.5 Governance Stack

Phase 2.5 governance hardening is complete and remains bounded to `PAPER_ONLY` operations. The completed governance work added the canonical R&D status, portfolio risk-budget governance, promotion scorecard governance, governance self-auditing, and emergency-brake context alignment. The final governance audit reports **100% consistency**, **HEALTHY** status, **0 conflicts**, and **no violations**.

### Completed

- Portfolio Risk Budget Layer.
- Promotion Scorecard Engine.
- Governance Audit Layer.
- Brake Context Alignment.

### Current governance stack

| Layer | Current state | Interpretation |
| --- | --- | --- |
| Risk Budget | `FREEZE NEW ALLOCATION` | No new allocation increases while the paper-only governance stack observes stability. |
| Promotion Scorecard | `FREEZE` | No strategy promotion is allowed. |
| Governance Audit | `HEALTHY` / `100%` consistency | Current canonical governance conflicts are reduced to `0` with no violations. |

### Decision

- Continue `PAPER_ONLY`.
- No allocation increase.
- No strategy promotion.
- No Phase 3.
- No real execution.
- No broker routing.

### Next

- Run the Daily Governance Ops Window.
- Observe stability over multiple days.
- Track whether governance conflicts remain `0`.
- Track whether the risk budget improves naturally from `FREEZE` toward `DEFENSIVE` or `NORMAL` without forced promotion.

## Canonical status taxonomy

| Status | Meaning |
| --- | --- |
| `DONE` | Implemented or documented enough to stop rerunning as baseline R&D. |
| `LIVE` | Active in paper/runtime observability only; not live trading. |
| `REVIEW` | Evidence exists but requires manual governance review before any promotion. |
| `OBSERVATION` | Monitor-only / telemetry-only; no mutation authority. |
| `NEXT` | Correct next work item. |
| `OBSOLETE` | Do not repeat unless anti-repeat criteria are met. |

## Completed items (`DONE`)

| Item | Canonical status | Evidence / interpretation |
| --- | --- | --- |
| Source-of-truth policy | `DONE` | Repository hierarchy and startup procedure exist; future sessions must inspect GitHub state before relying on old roadmap snapshots. |
| Phase 2 evidence package | `DONE` | Final Phase 2 summary closes calibration, regime filtering, drift, emergency brake, and transition-prediction findings as governance evidence. |
| Phase 2C calibration diagnosis | `DONE` | Hunter score is non-probabilistic; Brier-style probability calibration is not the primary optimization target. |
| Regime-aware filtering research | `DONE` | RISK OFF, low-score, and short-holding weak zones have been identified; filtering improves some metrics but reduces trade count. |
| Robustness / time-split diagnosis | `DONE` | Static filters degrade in the late split; market drift rather than another static threshold pass is the main interpretation. |
| Drift detection | `DONE` | Collapse/regime-shift evidence exists and supports defensive governance rather than further blind optimization. |
| Emergency brake simulation | `DONE` for research; `REVIEW` for governance adoption | Simulated brake behavior improved winrate, average PnL, and drawdown proxy, but remains recommendation-only. |
| Transition early-warning research | `DONE` for dashboard signal; `REVIEW` for predictive reliability | Useful as monitoring/risk-dashboard signal, not proven enough for autonomous transitions. |
| Anti-repeat governance | `DONE` | Duplicate experiment criteria, supersede logic, and reject rules are documented. |
| Continuation plan | `DONE` as roadmap | Two-week governance-first plan defines Research → Shadow → Paper → Live Locked lifecycle while prohibiting real execution. |
| Dashboard governance intelligence | `DONE` | Dashboard renders read-only Governance / Risk Intelligence, derives paper-only suggested action, and reads generated reports through cached helpers. |
| Telegram governance intelligence | `DONE` | Telegram formatter reports PAPER_ONLY, severity, action, report health, and read-only/no-live-trading reminder. |
| Report-generation scripts | `DONE` as tooling | Scripts exist for regime filtering, robustness, drift, emergency brake, transition prediction, duplicate detection, warning summaries, and ops reports. |
| Runtime report tracking policy | `DONE` | Generated `reports/`, `logs/`, database files, and data inputs are local/runtime artifacts, not tracked source-of-truth evidence in Git. |
| Portfolio Risk Budget Layer | `DONE` / `LIVE` | Phase 2.5 portfolio risk-budget governance is implemented as a paper-only allocation control layer; current state is `FREEZE NEW ALLOCATION`. |
| Promotion Scorecard Engine | `DONE` / `LIVE` | Phase 2.5 promotion scorecard governance is implemented as a paper-only promotion control engine; current state is `FREEZE`. |
| Governance Audit Layer | `DONE` / `LIVE` | Phase 2.5 self-auditing governance is implemented; latest result is `HEALTHY`, `100%` consistency, `0` conflicts, and no violations. |
| Brake Context Alignment | `DONE` | Emergency-brake context is aligned with risk-budget and promotion-scorecard governance boundaries. |

## Active items (`LIVE`, paper/observability only)

| Item | Canonical status | Boundary |
| --- | --- | --- |
| PAPER_ONLY runtime posture | `LIVE` | Active safety posture; it means simulation/alerts/dashboard only, not exchange execution. |
| Governance / Risk Intelligence panel | `LIVE` | Read-only dashboard surface. It may recommend `OBSERVE`, `HOLD`, `DEFENSIVE`, or `BRAKE REVIEW`, but it is not a command router. |
| Telegram governance intelligence message | `LIVE` | Read-only notification. It must remain explicit that governance signals are not live trading commands. |
| Daily/ops reporting scripts | `LIVE` if runtime reports exist; otherwise `OBSERVATION` | They summarize runtime artifacts and warning categories; they do not mutate strategy or place orders. |
| Portfolio Risk Budget governance | `LIVE` | Paper-only allocation guardrail; current state is `FREEZE NEW ALLOCATION` and does not route orders. |
| Promotion Scorecard governance | `LIVE` | Paper-only promotion guardrail; current state is `FREEZE` and does not auto-promote strategies. |
| Governance Audit self-audit | `LIVE` | Paper-only consistency audit; latest status is `HEALTHY` with `100%` consistency and `0` conflicts. |
| Resource monitoring | `OBSERVATION` | Host/database/log visibility only. It does not throttle, schedule, pause, trade, or deploy. |
| Dashboard/database analytics | `OBSERVATION` | UI/reporting layer only; expensive analytics are opt-in/cached and must not become execution gates without separate review. |

## Items requiring manual review (`REVIEW`)

| Item | Review decision needed | Current answer |
| --- | --- | --- |
| Emergency brake promotion | Continue observing the aligned paper-only brake context against the risk budget and promotion scorecard. | Alignment complete; candidate remains paper-only with no live deployment. |
| Early Warning Score | Decide whether there is enough pre-collapse predictive evidence for paper-only defensive review triggers. | Monitoring signal only; not autonomous. |
| Adaptive threshold governance | If thresholds move, require bounded ranges, reason logging, and freeze during instability. | Governance-only continuation, not fresh alpha search. |
| Runtime report completeness | Confirm whether VPS/local runtime has current `reports/*.json` and `logs/*.json/csv`. | Not resolved from tracked Git because generated reports are ignored. |

## Observation-only items (`OBSERVATION`)

- Missing tracked `reports/` is not a failure by itself because generated reports are ignored and local/runtime-specific.
- Dashboard and Telegram may show missing report health until runtime scripts generate local artifacts.
- Execution-engine research code is simulation/analytics code only; it is not evidence of broker integration approval.
- Advanced Phase 4 scripts in the repo do not override PAPER_ONLY or unlock execution.

## Deprecated / obsolete repeated tasks (`OBSOLETE`)

Do **not** repeat these as the next R&D step unless a new dataset window, new explicit hypothesis, new metric, or new baseline commit is documented under anti-repeat governance:

1. Re-running Phase 2C calibration solely to force `Brier <= 0.24`.
2. Treating the Hunter score as a calibrated probability target.
3. Blind threshold tuning on `score`, `holding_candles`, or static regime filters.
4. Repeating robustness/time-split diagnostics with the same dataset and same hypothesis.
5. Repeating temporal drift diagnosis without new runtime data or a new collapse hypothesis.
6. Re-running nonlinear / Phase 4 model exploration as a shortcut around failed governance gates.
7. Re-implementing dashboard governance intelligence already present in `dashboard.py`.
8. Re-implementing Telegram governance intelligence already present in `telegram.py`.
9. Re-creating anti-repeat, source-of-truth, or continuation-plan docs instead of updating this canonical status.
10. Treating generated local reports as tracked source-of-truth files.

## Tasks that must NOT be repeated

- Do not open Phase 3.
- Do not enable real execution.
- Do not connect broker/live exchange order placement.
- Do not add an auto-promotion path from research or paper to live.
- Do not bypass risk gates or PAPER_ONLY language in reports, dashboard, Telegram, or docs.
- Do not mutate scanner/strategy/execution behavior based only on the Phase 2 evidence package.
- Do not run heavy walkforward/calibration loops merely to rediscover already documented instability.
- Do not present emergency brake or early-warning labels as autonomous trading commands.

## Correct next phase (`NEXT`)

The correct next phase is **Governance Continuation / Paper-Only Defensive Risk Management**, not Phase 3.

Priority order:

1. Run the Daily Governance Ops Window.
2. Observe paper-only governance stability over multiple days.
3. Track whether governance conflicts remain `0`.
4. Track whether the risk budget improves naturally from `FREEZE` toward `DEFENSIVE` or `NORMAL`.
5. Use Early Warning Score only to trigger manual defensive review, not autonomous transitions.
6. Keep dashboard and Telegram governance intelligence aligned with this canonical status.
7. Update this file when status changes; do not fork competing roadmap docs.

## PAPER_ONLY status

`PAPER_ONLY` is mandatory and active.

Allowed:

- Offline research scripts.
- Local/generated reports.
- Dashboard observability.
- Telegram alerts and summaries.
- Shadow/paper simulations.
- Manual governance review artifacts.

Forbidden:

- Real order placement.
- Broker/exchange execution connector activation.
- Live position sizing changes.
- Auto-deployment from a research report.
- Phase 3 execution work.
- Any wording that implies a recommendation is a live trading command.

## Canonical conclusion

R&D has moved past alpha/calibration repetition and completed the Phase 2.5 paper-only governance stack. Phase 2 remains closed as an evidence package. The next valid work is daily governance operations, multi-day stability observation, conflict tracking, and natural risk-budget recovery review. No Phase 3, no real execution, and no broker routing are permitted from the current repository state.
