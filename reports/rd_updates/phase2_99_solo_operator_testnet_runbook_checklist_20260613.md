# Phase 2.99 Operator Checklist

Use this compact checklist together with the full Solo Operator Testnet Runbook.

## Start

- Enter the project virtual environment.
- Disable all execution gates.
- Do not clear HALT without investigation.
- Run the Phase 2.98 full read-only supervisor.
- Confirm position zero, open orders zero, and Real Binance OFF.
- Confirm at least three remaining daily slots before any new roundtrip.

## During an Authorized Testnet Roundtrip

- Use a fresh unexpired plan.
- Review plan symbol, side, quantity, notional, and Testnet URL.
- Enable gates only for the exact execution command.
- Execute the plan once.
- Immediately disable gates.
- Never replay a consumed plan.

## Incident

- Activate HALT for ambiguous entry, close failure, unexpected position, or contradictory evidence.
- Read the live Testnet position before any decision.
- Do not send a second entry.
- Use only one bounded reduce-only recovery attempt when a live non-zero position exists.
- Keep HALT active until flat state and evidence are reviewed.

## Finish

- Disable all execution gates.
- Run the full read-only supervisor.
- Confirm final position zero and open orders zero.
- Preserve evidence and checksums.
- Do not leave an unresolved `REVIEW_REQUIRED` or `HALTED` state unattended.
