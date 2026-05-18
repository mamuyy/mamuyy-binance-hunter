#!/usr/bin/env bash
set -u

PROJECT_DIR="${MAMUYY_HUNTER_DIR:-$HOME/mamuyy-binance-hunter}"
LOG_FILE="$PROJECT_DIR/logs/monthly_retrain.log"

mkdir -p "$PROJECT_DIR/logs"
{
  echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') monthly retrain start ====="
  cd "$PROJECT_DIR" || exit 1
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python main.py --retrain-model
  python main.py --model-status
  echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') monthly retrain end ====="
  echo
} >> "$LOG_FILE" 2>&1
