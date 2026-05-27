# Promotion Checklist (Week 1 Governance)

Mode: `PAPER_ONLY` (mandatory)

## Candidate Identity
- candidate_id: `<candidate-id>`
- owner: `<name>`
- date_utc: `<YYYY-MM-DD>`
- baseline_reference_commit: `<git-commit-sha>`

## Reproducibility
- reproducibility_command: ```bash
  <single command used to reproduce evaluation>
  ```
- dataset_window_utc: `<start_utc> -> <end_utc>`
- dataset_hash: `<hash/fingerprint>`
- config_hash: `<hash/fingerprint>`

## Status Label (choose one)
- [ ] `research_only`
- [ ] `shadow_candidate`
- [ ] `paper_candidate`
- [ ] `rejected`

## Anti-Repeat Evidence
- same window/hash seen before?: `<yes/no>`
- if yes, why this is new evidence?: `<reason>`

## Gate Results
- Research -> Shadow gate: `<PASS/FAIL>`
- Shadow -> Paper gate: `<PASS/FAIL>`
- Notes: `<metrics vs baseline, data integrity checks, stability window>`

## Safety Review
- safety_flags_detected: `<none | list>`
- guardian_halt_seen: `<yes/no>`
- data_lag_seen: `<yes/no>`
- db_lock_incident_seen: `<yes/no>`
- mitigation_summary: `<what was checked and why risk is contained>`

## Reason-Based Verdict
- verdict: `<promote/hold/reject>`
- rationale: `<concise reason linked to gates + safety>`
- follow_up_action: `<next concrete action>`

## PAPER_ONLY Confirmation (required)
- [ ] I confirm this candidate remains `PAPER_ONLY`.
- [ ] No live broker/order-routing connector was enabled.
- [ ] No real execution logic/path was activated.
