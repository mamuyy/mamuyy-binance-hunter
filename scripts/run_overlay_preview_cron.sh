#!/usr/bin/env bash
set -euo pipefail

# Preview-only cron helper for the ML Signal Overlay Telegram flow.
# This script intentionally never exports ALLOW_OVERLAY_TELEGRAM_SEND and uses
# --dry-run for the sender bridge, so it cannot perform a real Telegram send.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs/cron"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/ml_overlay_preview_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

unset ALLOW_OVERLAY_TELEGRAM_SEND

{
  echo "[$(date -u --iso-8601=seconds)] ML Overlay preview-only cron start"
  echo "[$(date -u --iso-8601=seconds)] Generating PAPER_ONLY overlay preview for BEATUSDT"
  python3 ml_signal_overlay_v1.py --symbol BEATUSDT --telegram-preview --dry-run
  echo
  echo "[$(date -u --iso-8601=seconds)] Evaluating Telegram bridge policy in dry-run mode"
  python3 send_ml_overlay_to_telegram.py --send --dry-run
  echo "[$(date -u --iso-8601=seconds)] ML Overlay preview-only cron complete"
} >> "$LOG_FILE" 2>&1

echo "$LOG_FILE"
