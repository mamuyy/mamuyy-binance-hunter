# CP-039B Production Dataset Validation

Validation run date: 2026-06-28 (UTC)

## Scope and Safety

- Mode: PAPER_ONLY / read-only validation.
- Actions not performed: retraining, calibration, model promotion, broker execution, or production artifact promotion.
- Dataset builder invoked with `use_production_universe=True`.
- Production database path checked: `mamuyy_hunter.db`.

## Primary Build: Threshold 75

- Verdict: `DATABASE_NOT_FOUND`
- Dataset rows: `0`
- `internal_paper_trades` contribution: `0` selected rows; details `{}`
- `historical_outcomes` contribution: `0` selected rows; details `{}`
- Excluded rows by reason: `{}`
- Label distribution: `{}`
- Timestamp range: `{'max': None, 'min': None}`
- `dataset_contract_version`: `CP-039.production_universe.v1`
- `dataset_build_hash`: `68d58b858ba1c5c87ec987a0780358e211119cab8b0e867aa9f91be5a201d35c`
- `production_score_threshold`: `75`
- `label_mapping_version`: `CP-039.label_mapping.v1`

## Forbidden Label Verification

- `EXECUTION_SIMULATED` labels present: `NO`
- `EXPIRED_ORPHANED` labels present: `NO`
- `OPEN` labels present: `NO`
- `UNKNOWN` labels present: `NO`

## Threshold Sensitivity Check: 75 to 85

- Threshold 75 rows: `0`
- Threshold 85 rows: `0`
- Expected monotonic size check (`rows_85 <= rows_75`): `PASS`
- Observation: Threshold 85 produced the same dataset size as threshold 75 because no production database was present, so both builds returned the empty DATABASE_NOT_FOUND contract.

## Threshold 85 Build Metadata

- Verdict: `DATABASE_NOT_FOUND`
- Dataset rows: `0`
- Excluded rows by reason: `{}`
- Label distribution: `{}`
- Timestamp range: `{'max': None, 'min': None}`
- `dataset_build_hash`: `cbb1eacb8181a044a4c5877a64b69410907f741244a017c0651902998fde3a9d`

## Final Validation Result

- Result: `BLOCKED_BY_MISSING_PRODUCTION_DATABASE`. The dataset contract path executed successfully, but no `mamuyy_hunter.db` file was available in the repository workspace, so the generated dataset is empty.

