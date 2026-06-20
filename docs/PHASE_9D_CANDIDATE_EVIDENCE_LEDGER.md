# Phase 9D Candidate Evidence Ledger

## Purpose

Phase 9D adds an append-only research ledger for validated Binance candidate outcomes. Phase 9A, 9B, and 9C produce snapshot reports that can be regenerated. The ledger preserves each READY candidate horizon as cumulative historical evidence before any future candidate promotion or selection-policy gate is considered.

## Input and Outputs

Input:

- `reports/candidate_validation_report.json`

Outputs:

- `reports/candidate_evidence_ledger.jsonl`
- `reports/candidate_evidence_ledger_summary.json`

Each JSONL ledger line represents one validated candidate horizon, such as a 24h, 48h, or 72h result.

## Append-Only Design

`candidate_evidence_ledger.py` never rewrites the JSONL ledger. It loads existing evidence IDs, appends only new READY horizons, and leaves all prior ledger lines untouched. Existing malformed JSONL lines are counted and preserved; the tool does not silently repair or delete them.

The summary JSON is regenerated for the current run and cumulative ledger view. It is written through a temporary file and atomically replaced so summary refreshes do not risk partial output.

## Evidence ID and Deduplication

Each ledger record has a deterministic `evidence_id` generated from:

- `symbol`
- `signal_timestamp`
- `horizon`

The script hashes those values with SHA-256 and stores the first 16 hexadecimal characters. If that ID already exists in the ledger, the horizon is counted as `duplicate_skipped` and is not appended again. This makes repeated execution idempotent.

## READY and PENDING Outcomes

Only horizons with `status == "READY"` are appended. Horizons with `status == "PENDING"` are counted as `pending_skipped` and never written to the ledger. Phase 9D does not fabricate prices, returns, observed timestamps, or direction outcomes.

## Score Buckets

Phase 9D uses the same score buckets as Phase 9C:

- `95-100`
- `90-94`
- `85-89`
- `<85`

## How to Run

```bash
python candidate_evidence_ledger.py
```

The script prints a compact run report with candidates read, ready horizons, new evidence, duplicates, pending outcomes, total ledger records, and status.

## How to Run Tests

```bash
pytest tests/test_candidate_evidence_ledger.py
```

The tests use temporary directories and do not touch production `reports/` files.

## Governance Restrictions

Phase 9D is research-only and enforces these restrictions in the summary:

- `paper_only: true`
- `append_only: true`
- `writes_to_database: false`
- `writes_to_broker: false`
- `execution_allowed: false`
- `automatic_promotion_allowed: false`

The ledger performs no broker API calls, no Binance testnet orders, no real orders, no database inserts, updates, or deletes, no runtime routing changes, no scanner threshold changes, and no automatic candidate promotion or configuration updates.

## Why Phase 9D Must Exist Before Phase 9E

A future Phase 9E Candidate Policy Gate needs durable historical evidence rather than only the latest regenerated snapshot. The append-only ledger ensures candidate decisions can be audited across queue refreshes, validation reruns, and analytics updates. It prevents accidental loss of READY outcomes and keeps PENDING outcomes explicitly unrecorded until real validation data exists.
