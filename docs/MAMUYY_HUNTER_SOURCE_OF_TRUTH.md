# MAMUYY Hunter — Source of Truth Policy

Last updated: 2026-05-27

This document is the canonical reference for how future ChatGPT/Codex sessions should determine the current state of the MAMUYY Hunter project.

## Repository

Official technical repository:

- `mamuyy/mamuyy-binance-hunter`
- Default branch: `main`

Do not confuse this repository with `mamuyy/MyTradingAgents`. MyTradingAgents may be a related experiment, but it is not the primary MAMUYY Hunter source of truth.

## Source-of-truth hierarchy

When sources conflict, use this hierarchy:

1. **GitHub main branch = Technical Truth**
   - Current implementation state
   - Latest commit/PR history
   - Existing scripts, reports, docs, and experiments
   - Actual phase work already implemented

2. **Roadmap / R&D docs = Governance Truth**
   - Strategic direction
   - Phase gates
   - PAPER_ONLY policy
   - Readiness criteria
   - Real execution restrictions

3. **VPS runtime = Runtime Truth**
   - Whether tmux services are alive
   - Whether cron jobs are running
   - Whether dashboard/processes are active
   - Current deployed runtime state

4. **Telegram telemetry = Observability Truth**
   - Runtime metrics
   - ML reports
   - Portfolio/exposure alerts
   - Health notifications

## Mandatory startup procedure for future sessions

Every future ChatGPT/Codex continuation should begin by checking the actual GitHub state, not by assuming an older roadmap snapshot.

Run:

```bash
cd /root/mamuyy-binance-hunter || cd /root/MAMUYY_Hunter || pwd
git remote -v
git fetch origin
git checkout main
git pull origin main
git log --oneline --decorate -10
git status
```

Then report:

- Current `HEAD` commit
- Latest 5–10 commits
- Whether working tree is clean
- Relevant newest files/reports/docs
- Actual technical phase based on repo contents
- Next action that does not repeat completed work

## Important rule

If the roadmap says a task is not started, but GitHub main already contains a merged implementation or report for that task, treat the task as **implemented technically**, but still check whether the **governance gate** is passed.

Implementation progress and governance readiness are different.

Example:

- GitHub may contain advanced Phase 4 research scripts.
- Governance may still enforce PAPER_ONLY and block real execution.

This is not a contradiction. It means research can advance while execution remains locked.

## Standing safety policy

Until a formal readiness gate says otherwise:

- Mode remains `PAPER_ONLY`.
- Do not enable real execution.
- Do not connect broker execution.
- Do not bypass risk gates.
- Do not treat research-phase scripts as production deployment approval.

## Current known GitHub reference from connector check

As of the latest connector check on 2026-05-27, the official repository was identified as:

- `mamuyy/mamuyy-binance-hunter`

A latest observed commit was:

- `6bea306185ea68116d92d00370a8e4d170c48de0`
- Message: `feat: add phase4 nonlinear model exploration`

Future sessions must still run `git pull` and `git log -10`, because this document may become outdated as the repository advances.
