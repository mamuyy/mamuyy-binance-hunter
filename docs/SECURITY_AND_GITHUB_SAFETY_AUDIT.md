# SECURITY AND GITHUB SAFETY AUDIT

Date: 2026-05-20 (UTC)
Repository: `mamuyy-binance-hunter`
Scope: security + permission audit only (no trading logic changes)

## Executive Summary
This repository is currently structured as a **paper/simulation-oriented scanner** that uses public Binance market data and Telegram notifications. No direct exchange order placement path was found in the audited code paths. Security posture is **moderate**: key secrets are not hardcoded in committed Python files, `.env` is ignored, and no GitHub Actions deployment pipeline is present in-repo.

Primary remaining risks are operational/governance gaps (missing explicit branch protection enforcement evidence, broad logging surfaces, and no documented least-privilege GitHub permissions baseline).

---

## 1) Secrets / tokens / credentials committed

### Findings
- `.env` is excluded by Git ignore rules (`.gitignore` includes `.env`).
- Telegram secrets are loaded from environment variables in code, not hardcoded values.
- Repository contains `.env.example` only (template), and no committed `.env` file found in audit scan.
- No PEM/private key files found in repository root scan window.

### Evidence
- `.gitignore` excludes `.env`, logs, DB artifacts, and backups.
- `config.py` reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` via `os.getenv`.
- `README.md` repeatedly instructs using `.env` and warns not to commit token.

### Risk Level
**Low (current code snapshot)**

### Recommended Fixes
1. Add automated secret scanning in CI (e.g., Gitleaks/TruffleHog) and enable GitHub Secret Scanning alerts.
2. Add pre-commit hooks to block committing tokens and `.env` files.
3. Rotate Telegram token immediately if previously exposed outside this snapshot.

---

## 2) GitHub Actions auto-deploy capability

### Findings
- No `.github/workflows/*.yml` files found in this repository snapshot.
- Therefore no in-repo evidence of automatic deploy from GitHub Actions.

### Risk Level
**Low (for accidental auto-deploy via this repo alone)**

### Recommended Fixes
1. Keep deployment workflows in separate protected infra repo if needed.
2. If workflows are later added, explicitly set minimal permissions and require environment approvals.
3. Disallow workflow changes without code-owner review.

---

## 3) Branch protection status / recommendation

### Findings
- Branch protection cannot be verified from local filesystem alone.
- No branch policy file in repo proving protection state.

### Risk Level
**Medium (governance uncertainty)**

### Recommended Branch Protection Baseline
For `main` (and any production branch), enforce:
1. Require pull request before merge.
2. Require at least 1–2 approvals.
3. Dismiss stale approvals on new commits.
4. Require status checks to pass (tests, lint, security scan).
5. Require branches up to date before merge.
6. Restrict force-push and branch deletion.
7. Require signed commits (optional but recommended).
8. Add CODEOWNERS for security-sensitive files (`config.py`, workflow files, bridge/integration modules).

---

## 4) Real-order / broker execution path audit

### Findings
- Scanner calls Binance public market-data endpoints (`/fapi/v1/ticker/24hr`, `/fapi/v1/klines`, `/fapi/v1/openInterest`, `/fapi/v1/fundingRate`) using HTTP GET only.
- No authenticated Binance trading endpoint usage found (`/order`, signed requests, API key header logic, broker SDK usage).
- TradingView webhook module constructs payload with explicit safety flags indicating paper-only and no exchange/broker execution.
- README repeatedly states no live order placement.

### Risk Level
**Low (based on audited paths)**

### Recommended Fixes
1. Add explicit denylist assertions/tests that fail CI if order endpoints (`/order`, `/batchOrders`, etc.) or broker SDK imports appear.
2. Gate any future execution connector behind `ENABLE_LIVE_EXECUTION=false` hard default plus separate privileged runtime.

---

## 5) Paper-only safety enforcement clarity

### Findings
- Webhook payload includes `mode: PAPER_ONLY` plus safety booleans (`paper_mode_only: true`, `broker_execution: false`, `exchange_order: false`, `public_endpoint: false`, `localhost_test_only: true`).
- CLI messages and README include clear paper-only and no real-order statements.

### Risk Level
**Low–Medium** (low in code intent; medium if future contributors add connectors without policy gates)

### Recommended Fixes
1. Add CI policy check preventing merge if `PAPER_ONLY` semantics are removed without security-owner approval.
2. Add immutable runtime guard in startup path: abort if any live-execution flag appears in env unless in dedicated secured environment.
3. Document explicit “no live execution in this repository” security policy section.

---

## 6) Log data sensitivity exposure

### Findings
- Signal logs include market metrics and analytics fields (not direct credentials).
- Telegram sender may print request exceptions; generally safe but could still leak request context in edge cases.
- Extensive CSV/DB logging footprint exists; if host permissions are weak, operational metadata could be exposed.

### Risk Level
**Medium**

### Recommended Fixes
1. Implement structured log redaction utility for any future secret-bearing fields.
2. Ensure file permissions on logs/DB are restricted (`chmod 600` where appropriate).
3. Add retention and secure deletion policy for logs/backups.
4. Avoid printing full exception payloads from HTTP clients in production mode.

---

## Safe GitHub Permission Setup (Recommended)

### Repository-level
- Default repository permissions for Actions: **Read repository contents**.
- Disable “Allow GitHub Actions to create and approve pull requests” unless explicitly needed.
- Limit who can push to protected branches.

### Workflow-level (if workflows are introduced)
Use explicit minimal permissions in every workflow file:

```yaml
permissions:
  contents: read
```

Add only required scopes per job (example):

```yaml
jobs:
  security:
    permissions:
      contents: read
      security-events: write
```

### Environment protections
- Use GitHub Environments for any deployment target.
- Require manual reviewers for environment secrets.
- Store all secrets in GitHub Secrets / Environment Secrets only (never in repo).

---

## Branch Protection Recommendation (Actionable Checklist)

1. Protect `main` immediately.
2. Require PR + 2 approvals for workflow/security/config changes.
3. Require passing checks: unit tests, lint, secret scan, dependency scan.
4. Require conversation resolution before merge.
5. Disable force push and branch deletion.
6. Add CODEOWNERS and require code-owner review on:
   - `.github/workflows/**`
   - `config.py`
   - `bridge_tradingview.py`
   - `main.py`

---

## Overall Risk Posture
- **Current technical execution risk (real trading): Low**
- **Secret exposure risk in snapshot: Low**
- **Governance/process risk (branch protection unknown): Medium**
- **Operational log/privacy risk: Medium**

Overall: **Low-to-Medium**, with primary improvements needed in GitHub governance and automated security controls.
