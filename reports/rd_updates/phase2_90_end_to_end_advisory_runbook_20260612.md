# Phase 2.90 — End-to-End Advisory Runbook

Status: PASS
Mode: ADVISORY ONLY
Real Binance: OFF
Auto execution: OFF
Broker execution: DISABLED

## Purpose

This runbook defines the safe manual end-to-end advisory flow for MAMUYY Hunter after Phase 2.85. The flow is designed to generate ML overlay output, evaluate it through the semi-auto testnet bridge, send a manual-gated Telegram advisory message, and preserve evidence logs without placing any order.

## Flow

ML Overlay
→ Semi-Auto Testnet Bridge
→ Telegram Advisory
→ Evidence Logs
→ No Order

## Safety Rules

* Do not enable real Binance.
* Do not enable production broker API.
* Do not enable auto execution.
* Do not enable cron.
* Do not enable loops.
* Do not enable ALLOW_TESTNET_ORDER for this advisory flow.
* Keep ALLOW_AUTO_TESTNET_ORDER unset or false.
* Only set ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND=1 when manually sending a Telegram advisory message.
* Unset ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND immediately after send.

## Manual Advisory Commands

```bash
cd ~/mamuyy-binance-hunter
source .venv/bin/activate

unset ALLOW_TESTNET_ORDER
unset ALLOW_AUTO_TESTNET_ORDER
unset ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND

python3 ml_signal_overlay_v1.py --symbol HYPEUSDT --telegram-preview --dry-run

python3 semi_auto_testnet_bridge.py --allow-need-review --telegram-preview

python3 send_semi_auto_bridge_to_telegram.py
cat logs/semi_auto_bridge_telegram_send_result.json
```

## Manual Telegram Advisory Send

```bash
export ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND=1
python3 send_semi_auto_bridge_to_telegram.py --send --ignore-cooldown
unset ALLOW_SEMI_AUTO_BRIDGE_TELEGRAM_SEND

cat logs/semi_auto_bridge_telegram_send_result.json
```

## Expected Safety Fields

The final result must preserve:

```text
order_attempted = false
order_success = false
dry_run = true
broker_execution_enabled = false
real_binance_enabled = false
```

## Evidence Files

* logs/ml_signal_overlay_v1_report.json
* logs/ml_signal_overlay_telegram_preview.json
* logs/semi_auto_testnet_bridge_result.json
* logs/semi_auto_testnet_bridge_telegram_preview.json
* logs/semi_auto_bridge_telegram_send_result.json

## Interpretation

A BLOCKED result means the bridge correctly rejected a candidate.

A WOULD_ORDER result means the bridge advisory policy passed, but it still did not send any order.

A SENT result means Telegram advisory delivery succeeded, not that a trade was executed.

## Summary

Phase 2.90 standardizes the manual advisory workflow. It allows MAMUYY Hunter to produce ML-assisted Telegram alerts while preserving strict no-execution governance.

This milestone does not enable real trading, automatic trading, or testnet order automation.

## Safety Notes

* No Binance order should be sent.
* No testnet order should be sent.
* No real Binance execution.
* No production broker API.
* No cron.
* No loop.
* No automatic trading.
* Manual Telegram advisory gate only.
