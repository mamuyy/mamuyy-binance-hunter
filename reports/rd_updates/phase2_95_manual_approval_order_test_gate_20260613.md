# Phase 2.95 — Manual Approval Testnet Order-Test Gate

Status: PASS
Mode: MANUAL APPROVAL / ORDER-TEST ONLY
Real Binance: OFF
Auto execution: OFF
Actual order execution: DISABLED
Final position: FLAT

## Purpose

Phase 2.95 introduces a one-time manual approval gate between the Semi-Auto Testnet Bridge and Binance Futures Demo `/order/test`.

The gate validates a bridge proposal, generates an expiring approval request, freezes the execution payload with SHA256, sizes the proposal using live Binance Futures Demo mark price and exchange filters, and requires explicit manual confirmation before invoking `/order/test`.

This phase does not place an actual order and does not open a position.

## Final Validation Matrix

| Scenario                            |   Expected | Result |
| ----------------------------------- | ---------: | -----: |
| Prepare valid approval request      |   PREPARED |   PASS |
| Live mark-price sizing              | 20–25 USDT |   PASS |
| Quantity exchange-filter validation |       PASS |   PASS |
| Request ID confirmation             |      MATCH |   PASS |
| SHA256 confirmation                 |      MATCH |   PASS |
| Bridge payload confirmation         |      MATCH |   PASS |
| Manual-gated `/order/test`          |    SUCCESS |   PASS |
| Position after `/order/test`        |       FLAT |   PASS |
| Replay used request                 |    BLOCKED |   PASS |
| Replay executor invocation          |       NONE |   PASS |
| Gates after testing                 |      UNSET |   PASS |

## Successful Approval Evidence

* Symbol: ETHUSDT
* Side: BUY
* Order type: MARKET
* Approved quantity: 0.014
* Target notional: 22 USDT
* Live estimated notional: approximately 23.30 USDT
* Minimum notional: 20 USDT
* Maximum notional: 25 USDT
* Minimum-notional check: PASS
* Maximum-notional check: PASS
* Quantity filter: PASS
* Order-test result: SUCCESS
* Request used: true
* Position opened: false
* Actual order enabled: false
* Final ETHUSDT position: 0.000

## Replay Protection Evidence

The successful approval request was submitted a second time.

Expected and observed result:

* Status: BLOCKED
* Reason: request already used
* Approval passed: false
* Order attempted: false
* Order success: false
* Executor result: null
* Executor return code: null
* Position opened: false

This confirms that an approval request cannot be replayed.

## Safety Controls

* UUID request ID
* Canonical SHA256 approval payload
* Ten-minute request expiry
* One-time request usage
* Live mark-price sizing
* Exchange quantity-filter validation
* Minimum and maximum notional enforcement
* Approval-time mark-price revalidation
* Bridge-payload immutability check
* Manual approval environment gate
* Testnet order environment gate
* Execution halt switch
* Symbol allowlist
* No production Binance URL
* No real Binance support
* No cron
* No loop
* No automatic execution

## Final Safety Posture

* ALLOW_MANUAL_TESTNET_APPROVAL: unset
* ALLOW_TESTNET_ORDER: unset
* ALLOW_AUTO_TESTNET_ORDER: unset
* REAL_BINANCE_ENABLED: false
* ALLOW_REAL_BINANCE_ORDER: false
* BROKER_MODE: BINANCE_FUTURES_TESTNET_ONLY
* Actual order execution: disabled
* Final position: flat

## Summary

Phase 2.95 successfully validates a one-time human approval workflow for Binance Futures Demo `/order/test`.

The flow can safely transform a dry-run bridge proposal into a manually confirmed test request while preserving live-price sizing, notional limits, payload integrity, expiry, and replay protection.

This milestone does not enable actual testnet positions, automated trading, or real Binance execution.
