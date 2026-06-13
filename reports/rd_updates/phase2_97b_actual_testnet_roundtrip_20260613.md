# Phase 2.97B — Manual Actual Binance Futures Demo Roundtrip

Status: PASS
Mode: MANUAL ACTUAL TESTNET ROUNDTRIP
Exchange: BINANCE FUTURES DEMO/TESTNET ONLY
Real Binance: OFF
Auto execution: OFF
Final position: FLAT

## Purpose

Phase 2.97B validates that MAMUYY Hunter can safely perform one manually approved actual Binance Futures Demo roundtrip.

The lifecycle consists of:

1. Fresh advisory bridge result
2. Manual approval evidence
3. Read-only execution-safety preflight
4. Immutable actual roundtrip plan
5. Explicit operator confirmation
6. One actual Testnet entry
7. Live position verification
8. Mandatory MARKET reduce-only close
9. Final flat verification
10. Immutable audit and order evidence

This milestone does not enable automatic trading or Real Binance execution.

## Execution Summary

| Item                  | Result            |
| --------------------- | ----------------- |
| Symbol                | ETHUSDT           |
| Entry                 | BUY 0.013 MARKET  |
| Entry reduce-only     | false             |
| Entry attempted       | true              |
| Entry successful      | true              |
| Close                 | SELL 0.013 MARKET |
| Close reduce-only     | true              |
| Close attempted       | true              |
| Close successful      | true              |
| Position before close | 0.013             |
| Position after close  | 0                 |
| Emergency close used  | false             |
| Final live position   | 0.000             |
| Roundtrip evidence    | PASS              |

## Persistent Controller Evidence

* Plan consumed: true
* Plan completed: true
* Persistent state: COMPLETED
* Successful actual entry rows: 1
* Successful reduce-only close rows: 1
* Audit events: 7
* Final flat verification confirmed through executor order log and live position query

## Safety Controls Proven

* Binance Futures Demo base URL enforcement
* Broker mode locked to Testnet only
* Real Binance disabled
* Real Binance order gate disabled
* Auto Testnet execution disabled
* Explicit manual execution gate
* Explicit CLI confirmation phrase
* Immutable plan UUID and SHA256
* Ten-minute plan expiry
* Atomic consumed state before entry
* Entry replay prevention
* Exclusive filesystem lock
* Daily actual-order limit
* Three-slot roundtrip capacity policy
* Maximum one open position
* Maximum total exposure
* Open-order guard
* Live mark-price revalidation
* Exchange quantity-filter validation
* Minimum and maximum entry-notional policy
* Reduce-only close exemption from entry notional limits
* Live position-derived close side and quantity
* Maximum one entry attempt
* Maximum one primary close attempt
* Maximum one emergency close attempt
* Final-flat requirement
* Emergency halt mechanism
* Redacted audit logs

## Daily Capacity Result

The completed roundtrip consumed two successful Testnet actions:

1. Entry
2. Reduce-only close

Daily limit at validation time: 3 actual successful orders.

After completion:

* Actual successful actions used: 2
* Remaining slot: 1
* Another full roundtrip was not permitted because the controller requires three available slots before entry:

  * entry
  * primary close
  * emergency-close reserve

This restriction limits technical blast radius and is not based on account balance.

## Evidence Preservation

Evidence was copied into a timestamped directory under:

`evidence/phase2_97b_*`

The directory contains:

* actual roundtrip plan
* persistent state
* roundtrip audit JSONL
* Binance Testnet order JSONL
* SHA256 checksums

Do not commit raw evidence files when they contain:

* full UUIDs
* full SHA256 values
* exchange order IDs
* account-identifying fields
* credentials
* private infrastructure information

The R&D document should contain summarized and redacted evidence only.

## Status Observability Fix

The original `--status` implementation regenerated default execution metrics and overwrote the primary result view.

PR #107 corrected this by:

* preserving the authoritative result file
* writing a separate read-only status file
* reporting persistent state and plan lifecycle separately
* abbreviating UUID and SHA values
* prohibiting network, subprocess, order, plan, state, audit, and result mutation during status checks

VPS regression validation confirmed:

* result-file checksum unchanged after `--status`
* separate status JSON created
* persistent state remained COMPLETED
* plan remained consumed and completed
* final live ETHUSDT position remained 0.000

## Final Safety Posture

* Exchange: Binance Futures Demo/Testnet
* Real Binance enabled: false
* Real Binance order allowed: false
* Auto Testnet order: false/unset
* Manual actual roundtrip gate: false/unset after completion
* Testnet order gate: false/unset after completion
* Emergency halt: inactive
* Position: FLAT
* No cron
* No loop
* No unattended trading

## Interpretation

Phase 2.97B proves technical roundtrip capability in a controlled Testnet environment.

It does not prove:

* strategy profitability
* production readiness
* Real Binance readiness
* autonomous execution readiness
* permission to raise daily limits immediately

The milestone validates safe technical execution, mandatory close behavior, replay prevention, evidence preservation, and final-flat recovery.

## Next Recommended Phase

Phase 2.98 should remain read-only and focus on:

* Testnet daily-capacity status
* roundtrip evidence verification
* execution-history summary
* operator review status
* halt and flat-position checks
* no order submission

No additional actual Testnet order is required for Phase 2.98.

## Summary

Phase 2.97B is PASS.

MAMUYY Hunter successfully opened one small ETHUSDT Binance Futures Demo position, verified it, closed it through a mandatory MARKET reduce-only order, and returned to a confirmed flat position.

Real Binance and automatic execution remained disabled throughout the lifecycle.

## Security and Privacy Rules

Do not include:

* full plan UUID
* full request UUID
* full SHA256 payloads
* API keys
* API secrets
* Telegram credentials
* VPS IP address
* `.env` contents
* exchange account identifiers
* raw private evidence files

## Validation

Run only offline/read-only validation:

```bash
python3 -m py_compile \
  manual_actual_testnet_roundtrip_controller.py \
  actual_testnet_roundtrip_state.py \
  binance_testnet_executor.py

python3 -m unittest discover \
  -s tests \
  -p 'test_*.py'
```

Do not execute another actual roundtrip.
Do not enable execution gates.
Do not send an order.
Do not call `/order/test`.
