# Phase 2.99 — Solo Operator Testnet Runbook

Status: PASS  
Mode: MANUAL SOLO OPERATOR / TESTNET ONLY  
Exchange: BINANCE FUTURES DEMO/TESTNET ONLY  
Real Binance: OFF  
Auto execution: OFF  
Cron and loops: OFF  

## Purpose

This runbook defines the safe operating procedure for one human operator managing MAMUYY Hunter in Binance Futures Demo/Testnet mode.

It covers:

- session startup
- read-only safety verification
- normal manually approved Testnet operation
- ambiguous entry handling
- close failure handling
- SSH interruption recovery
- emergency halt activation
- reduce-only recovery close
- evidence preservation
- end-of-session shutdown

This runbook does not authorize Real Binance execution or unattended trading.

## Non-Negotiable Safety Principles

1. **Live exchange state is the final source of truth.**
2. **Never replay an entry after a plan is consumed.**
3. **Never guess whether an order succeeded. Read the position first.**
4. **A recovery order must only reduce an existing Testnet position.**
5. **Only one operator may control an actual Testnet roundtrip at a time.**
6. **All execution gates stay unset except during the exact command that requires them.**
7. **A full roundtrip requires at least three remaining daily slots:** entry, primary close, and emergency-close reserve.
8. **A `SAFE_IDLE` verdict does not override insufficient daily capacity.**
9. **Any unresolved contradiction activates or preserves HALT.**
10. **Real Binance must remain disabled in every phase of this runbook.**

## Fixed Safety Scope

Required configuration:

```text
base_url = https://demo-fapi.binance.com
broker_mode = BINANCE_FUTURES_TESTNET_ONLY
REAL_BINANCE_ENABLED = false
ALLOW_REAL_BINANCE_ORDER = false
ALLOW_AUTO_TESTNET_ORDER = false
```

Prohibited:

- production Binance Futures URL
- Real Binance API execution
- cron-based execution
- infinite loops
- unattended entry
- automatic plan approval
- duplicate entry retry
- raw emergency entry
- removing HALT before flat verification

## 1. Start a Safe Operator Session

Connect to the VPS and enter the project:

```bash
cd ~/mamuyy-binance-hunter
source .venv/bin/activate
```

Disable all execution gates before inspection:

```bash
unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
unset ALLOW_MANUAL_TESTNET_APPROVAL
```

Do not unset `TESTNET_EXECUTION_HALT` merely to make a check pass. HALT must be investigated and cleared through the dedicated procedure below.

## 2. Mandatory Read-Only Startup Check

Run:

```bash
python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT \
  --telegram-preview

python3 manual_actual_testnet_roundtrip_controller.py --status

python3 binance_testnet_executor.py \
  --positions \
  --symbol ETHUSDT
```

Normal safe startup target:

```text
verdict = SAFE_IDLE
symbol_position_amt = 0
symbol_open_order_count = 0
other_nonzero_positions = []
final_flat_live_verified = true
execution_halt_active = false
execution_lock_active = false
real_binance_enabled = false
allow_auto_testnet_order = false
```

Daily capacity must also be checked:

```text
remaining_daily_order_slots >= 3
full_roundtrip_capacity_passed = true
```

When fewer than three slots remain, the system may still be `SAFE_IDLE`, but a new full roundtrip is not permitted.

## 3. Verdict Interpretation

### SAFE_IDLE

Meaning:

- no live position
- no open order
- no other symbol exposure
- evidence internally consistent
- active execution lock absent
- HALT inactive
- execution gates disabled

Operator action:

- remain idle, or
- prepare a future manually approved Testnet operation only when daily capacity and all prerequisites pass

### REVIEW_REQUIRED

Possible causes:

- missing or malformed evidence
- checksum missing or mismatched
- plan/result/state contradiction
- incomplete entry/close audit evidence
- live inspection unavailable

Operator action:

- do not enable execution gates
- inspect evidence files
- do not create or replay an entry
- escalate for code or evidence review

### HALTED

Possible causes:

- HALT file or environment flag active
- non-zero position after a completed plan
- open order remains
- another non-zero symbol position exists
- active execution lock exists
- production URL or Real Binance enabled
- auto execution enabled

Operator action:

- stop all normal operation
- preserve logs
- inspect live position and open orders
- use recovery procedure only when a live Testnet position actually exists

## 4. Preconditions for a New Manual Testnet Roundtrip

Every condition must pass:

- supervisor verdict is `SAFE_IDLE`
- live symbol position is zero
- open order count is zero
- no other non-zero position exists
- HALT inactive
- active lock absent
- Real Binance OFF
- auto execution OFF
- daily remaining slots at least three
- evidence checksum PASS
- plan from any earlier roundtrip is completed or otherwise reviewed
- operator is physically present and monitoring the terminal

A balance large enough to absorb the trade does not replace these checks.

## 5. Prepare a Fresh Plan

Preparation itself must not send an actual order.

```bash
python3 semi_auto_testnet_bridge.py \
  --overlay-report-path tests/fixtures/manual_approval_pass_ethusdt_long.json \
  --telegram-preview

python3 manual_testnet_approval_gate.py --prepare

python3 testnet_execution_safety_supervisor.py \
  --preflight \
  --symbol ETHUSDT \
  --telegram-preview

python3 manual_actual_testnet_roundtrip_controller.py \
  --prepare \
  --symbol ETHUSDT

python3 manual_actual_testnet_roundtrip_controller.py --status
```

Required prepared-plan properties:

```text
state = PREPARED
consumed = false
completed = false
expired = false
actual_testnet_only = true
remaining_daily_order_slots >= 3
```

A prepared plan must be reviewed before execution. Never copy an old plan ID or old payload hash into a new session.

## 6. Manually Approved Actual Testnet Roundtrip

This section is only for an authorized Testnet session with a fresh, unexpired plan and at least three remaining daily slots.

Retrieve the current plan credentials from the local plan file without publishing them:

```bash
PLAN_ID=$(python3 -c 'import json; print(json.load(open("logs/manual_actual_testnet_roundtrip_plan.json"))["actual_roundtrip_plan_id"])')
PLAN_SHA=$(python3 -c 'import json; print(json.load(open("logs/manual_actual_testnet_roundtrip_plan.json"))["actual_roundtrip_payload_sha256"])')
```

Enable only the two gates required for the exact execution window:

```bash
export ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP=1
export ALLOW_TESTNET_ORDER=true
```

Execute once:

```bash
python3 manual_actual_testnet_roundtrip_controller.py \
  --execute-roundtrip \
  --approve "$PLAN_ID" \
  --confirm-sha256 "$PLAN_SHA" \
  --confirm-action OPEN_AND_CLOSE_BINANCE_FUTURES_DEMO_POSITION
```

Immediately disable the gates, regardless of return code:

```bash
unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
```

Then run the post-operation verification:

```bash
python3 manual_actual_testnet_roundtrip_controller.py --status

python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT \
  --telegram-preview

python3 binance_testnet_executor.py \
  --positions \
  --symbol ETHUSDT
```

Never execute the same plan twice. A consumed plan forbids entry replay even when the previous terminal output was incomplete.

## 7. SSH Disconnect or Terminal Crash

### Rule

Do not reconnect and rerun `--execute-roundtrip`.

### Recovery Steps

Reconnect, enter the project, and disable gates:

```bash
cd ~/mamuyy-binance-hunter
source .venv/bin/activate

unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
```

Inspect read-only state:

```bash
python3 manual_actual_testnet_roundtrip_controller.py --status

python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT

python3 binance_testnet_executor.py \
  --positions \
  --symbol ETHUSDT
```

Decision:

- **Position zero, no open orders:** do not replay; preserve evidence and review the consumed plan.
- **Position non-zero:** activate HALT and follow the reduce-only recovery-close procedure.
- **State unclear:** keep HALT active and do not send any new entry.

## 8. Ambiguous Entry Result

Examples:

- subprocess return code non-zero
- API response missing
- SSH disconnect immediately after send
- entry status file incomplete
- position verification timeout

Procedure:

1. Do not retry the entry.
2. Disable all normal execution gates.
3. Activate HALT.
4. Query the live Testnet position.
5. Inspect controller status and order evidence.

Commands:

```bash
mkdir -p runtime
printf 'operator halt: ambiguous entry state\n' > runtime/TESTNET_EXECUTION_HALT

unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER

python3 manual_actual_testnet_roundtrip_controller.py --status
python3 binance_testnet_executor.py --positions --symbol ETHUSDT
```

Outcome:

- **Live position zero:** entry must not be replayed from the consumed plan. Review evidence before preparing any future fresh plan.
- **Live position non-zero:** execute only the approved reduce-only recovery-close path.

## 9. Primary Close Failure

A failed or ambiguous primary close is a risk-reduction event, not an invitation to send another entry.

Immediate actions:

```bash
mkdir -p runtime
printf 'operator halt: primary close failed or ambiguous\n' > runtime/TESTNET_EXECUTION_HALT

unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER

python3 binance_testnet_executor.py --positions --symbol ETHUSDT
python3 manual_actual_testnet_roundtrip_controller.py --status
```

If the live position is already zero, do not send a recovery close. Preserve HALT until evidence review confirms flat state and no open orders.

If the live position remains non-zero, use the controller recovery path below.

## 10. Reduce-Only Emergency Recovery Close

This procedure is allowed only when:

- exchange is Binance Futures Demo/Testnet
- live position is non-zero
- no new entry is being attempted
- operator has reviewed the current consumed plan
- recovery intends only to reduce the existing position

Retrieve the existing consumed plan credentials locally:

```bash
PLAN_ID=$(python3 -c 'import json; print(json.load(open("logs/manual_actual_testnet_roundtrip_plan.json"))["actual_roundtrip_plan_id"])')
PLAN_SHA=$(python3 -c 'import json; print(json.load(open("logs/manual_actual_testnet_roundtrip_plan.json"))["actual_roundtrip_payload_sha256"])')
```

Enable only recovery gates:

```bash
export ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE=1
export ALLOW_TESTNET_ORDER=true
```

Run one recovery attempt through the controller:

```bash
python3 manual_actual_testnet_roundtrip_controller.py \
  --recover-close \
  --approve "$PLAN_ID" \
  --confirm-sha256 "$PLAN_SHA" \
  --confirm-action REDUCE_ONLY_EMERGENCY_CLOSE
```

Immediately disable gates:

```bash
unset ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
```

Verify:

```bash
python3 binance_testnet_executor.py --positions --symbol ETHUSDT

python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT \
  --telegram-preview
```

Do not repeatedly invoke recovery. The controller is designed for bounded attempts and operator review.

## 11. Emergency HALT Management

### Activate HALT

Use HALT whenever execution state is ambiguous, a live position remains unexpectedly, close fails, or safety evidence contradicts live state.

```bash
mkdir -p runtime
printf 'operator halt: manual safety intervention required\n' > runtime/TESTNET_EXECUTION_HALT
```

Verify:

```bash
python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT
```

Expected verdict: `HALTED`.

### Clear HALT

HALT may be cleared only after all conditions are confirmed:

- live position zero
- open orders zero
- no other non-zero positions
- execution gates OFF
- active execution lock absent
- evidence reviewed
- operator has documented the recovery outcome

Clear and recheck:

```bash
rm -f runtime/TESTNET_EXECUTION_HALT
unset TESTNET_EXECUTION_HALT

python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT \
  --telegram-preview
```

Target after a clean recovery: `SAFE_IDLE`.

Never clear HALT merely because a command is blocked.

## 12. Active Lock Handling

The controller uses an advisory filesystem lock.

A lock file may remain present after execution even when no active process holds it. Therefore:

- file present alone is not a failure
- `execution_lock_active=true` means stop
- `execution_lock_stale_or_free=true` means the leftover file is not actively locked

Do not manually delete or rewrite the lock file during an active or ambiguous operation.

Use the Phase 2.98 supervisor as the lock-state authority.

## 13. Evidence Preservation

After every actual Testnet roundtrip or recovery event, preserve:

- plan JSON
- persistent state JSON
- execution result JSON
- status JSON
- roundtrip audit JSONL
- Testnet order JSONL
- operations supervisor result
- Telegram preview
- SHA256 checksums

Recommended snapshot pattern:

```bash
EVIDENCE_DIR="evidence/operator_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_DIR"

cp logs/manual_actual_testnet_roundtrip_plan.json "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/manual_actual_testnet_roundtrip_state.json "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/manual_actual_testnet_roundtrip_result.json "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/manual_actual_testnet_roundtrip_status.json "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/manual_actual_testnet_roundtrip_audit.jsonl "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/binance_testnet_orders.jsonl "$EVIDENCE_DIR/" 2>/dev/null || true
cp logs/testnet_operations_evidence_supervisor_result.json "$EVIDENCE_DIR/" 2>/dev/null || true

(
  cd "$EVIDENCE_DIR"
  sha256sum * > SHA256SUMS
)
```

Do not commit raw private evidence when it includes full UUIDs, full hashes, exchange order IDs, account data, credentials, or infrastructure details.

## 14. End-of-Session Shutdown Checklist

Always finish with execution gates OFF:

```bash
unset ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP
unset ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE
unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
unset ALLOW_MANUAL_TESTNET_APPROVAL
```

Final checks:

```bash
python3 testnet_operations_evidence_supervisor.py \
  --full \
  --symbol ETHUSDT \
  --telegram-preview

python3 binance_testnet_executor.py \
  --positions \
  --symbol ETHUSDT
```

Required safe shutdown outcome:

```text
verdict = SAFE_IDLE
symbol_position_amt = 0
symbol_open_order_count = 0
other_nonzero_positions = []
final_flat_live_verified = true
real_binance_enabled = false
allow_auto_testnet_order = false
allow_testnet_order = false
allow_manual_actual_roundtrip = false
```

When the final verdict is `REVIEW_REQUIRED` or `HALTED`, do not close the incident merely by logging out. Preserve evidence and keep HALT active where appropriate.

## 15. Operator Decision Table

| Observation | Operator response |
|---|---|
| SAFE_IDLE, flat, 3+ slots | Manual preparation may begin |
| SAFE_IDLE, flat, fewer than 3 slots | Remain idle; reserve capacity |
| REVIEW_REQUIRED | No execution; inspect evidence |
| HALTED with flat position | Investigate cause; preserve HALT until review |
| HALTED with non-zero position | Use one bounded reduce-only recovery path |
| SSH lost before entry | Reconnect and inspect; do not assume entry |
| SSH lost after possible entry | Reconnect, HALT, query position; never replay |
| Entry failed and position zero | No replay from consumed plan |
| Primary close failed and position non-zero | HALT and controlled reduce-only recovery |
| Position zero after ambiguous close | Do not send another close; verify evidence |
| Active lock detected | Stop; another controller may be running |
| Stale/free lock file | Do not delete; continue only if all other checks pass |
| Real Binance or production URL detected | HALT and stop immediately |

## 16. Read-Only Training Drills

The operator may rehearse these without sending an order:

1. Run full operations supervisor.
2. Interpret `SAFE_IDLE`, `REVIEW_REQUIRED`, and `HALTED` fixtures in unit tests.
3. Inspect plan/status/result files.
4. Verify evidence checksums.
5. Confirm daily capacity.
6. Confirm lock file versus active lock behavior.
7. Practice activating HALT and verifying the supervisor detects it.
8. Clear HALT only after simulated flat-state review.

Do not use `/order/test` or an actual order merely to practice the runbook.

## 17. Security and Privacy Rules

Never expose:

- API key
- API secret
- Telegram token
- chat ID
- `.env` content
- VPS IP address in public reports
- full plan UUID
- full payload SHA256
- exchange order identifiers
- account identifiers
- raw private evidence

Normal console summaries should use redacted or abbreviated identifiers.

## Final Operational Posture

Phase 2.99 establishes the operating discipline for a single human Testnet operator.

Safe default state:

```text
Position: FLAT
Open orders: 0
HALT: OFF unless incident review is active
Real Binance: OFF
Auto execution: OFF
Execution gates: OFF
Cron: OFF
Loops: OFF
```

## Summary

Phase 2.99 is PASS as a documented Testnet operating procedure.

It does not increase the daily order limit, enable Real Binance, authorize autonomous execution, or bypass the safety supervisors.

Its purpose is to ensure that normal operation, interruption, ambiguity, failure, recovery, and shutdown are handled consistently by one operator without replaying entries or leaving unmanaged exposure.
