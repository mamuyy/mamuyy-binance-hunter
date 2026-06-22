# Binance Testnet Readiness Checkpoint

This checkpoint is a static, auditable documentation report for the Binance testnet integration state validated after `1247a7f Merge pull request #172`. It is documentation-only and does not change runtime behavior.

## Prior PR Summary

- PR170 fixed Binance testnet signed read-only header delivery.
- PR171 added local-only order preview validation.
- PR172 added safe allowlisted preview sample selection under `TESTNET_MAX_NOTIONAL_USDT`.

## Current Testnet Integration Status

| Area | Status |
| --- | --- |
| Public connectivity | `BINANCE_TESTNET_PUBLIC_PING_OK` |
| Signed read-only | `BINANCE_TESTNET_SIGNED_READ_ONLY_OK` |
| Account read | `BINANCE_TESTNET_SIGNED_READ_ONLY_OK` |
| Balance read | `BINANCE_TESTNET_SIGNED_READ_ONLY_OK` |
| Position read | `BINANCE_TESTNET_SIGNED_READ_ONLY_OK` |
| Order preview | `BINANCE_TESTNET_ORDER_PREVIEW_VALID` |
| Selected preview symbol | `HYPEUSDT` |
| Selected preview sample | `MARKET BUY`, `notional_usdt=5.0`, `leverage=1` |
| Order placement guard | `BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD` |
| ML readiness | `BLOCKED_BELOW_BASELINE` |

## Governance Lock State

- No order placement is enabled.
- No live orders are placed.
- No testnet orders are placed.
- No signed order endpoint is called by preview.
- Execution remains blocked by governance.
- `execution_allowed = False` remains preserved.
- `paper_only = True` remains preserved.
- `place_testnet_order` remains blocked.
- Order placement guards remain unchanged.
- Readiness gates remain unchanged and readiness remains locked.
- Model training, model inference, predictions, and threshold logic remain unchanged.

## Secret Handling

This checkpoint contains only non-secret status values. It does not include API keys, API secrets, signatures, or raw account balances.

## Next Recommended Step

Add a dry-run order intent journal for auditability. Do not proceed to real or testnet order placement yet.
