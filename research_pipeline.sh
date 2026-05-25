#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/ubuntu/mamuyy-binance-hunter"
PYTHON="$PROJECT_DIR/.venv/bin/python"
DB_PATH="$PROJECT_DIR/mamuyy_hunter.db"
LOG_DIR="$PROJECT_DIR/logs"
BACKUP_DIR="$PROJECT_DIR/manual_audit_backups"
LOCK_FILE="/tmp/mamuyy_research_pipeline.lock"

DAYS="${1:-3}"
MAX_LOAD="${MAX_LOAD:-4.0}"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
cd "$PROJECT_DIR"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "[$(timestamp)] $*" | tee -a "$LOG_DIR/research_pipeline.log"
}

current_load() {
  awk '{print $1}' /proc/loadavg
}

load_is_safe() {
  python3 - <<PY
load = float("$(current_load)")
max_load = float("$MAX_LOAD")
raise SystemExit(0 if load <= max_load else 1)
PY
}

heavy_process_running() {
  pgrep -f "main.py --orchestrator|main.py --backfill|main.py --label-outcomes|main.py --walkforward|main.py --retrain-model" >/dev/null 2>&1
}

post_check() {
  log "Post-check: historical_outcomes freshness"
  sqlite3 -header -column "$DB_PATH" "
  SELECT COUNT(*) AS outcomes, MAX(close_timestamp) AS last_close
  FROM historical_outcomes;
  " | tee -a "$LOG_DIR/research_pipeline.log"

  log "Post-check: latest ML rows"
  sqlite3 -header -column "$DB_PATH" "
  SELECT id,timestamp,accuracy,precision,recall,ai_confidence_score,setup_ranking
  FROM ml_results
  ORDER BY id DESC
  LIMIT 3;
  " | tee -a "$LOG_DIR/research_pipeline.log"
}

if ! command -v flock >/dev/null 2>&1; then
  echo "ERROR: flock not found. Install util-linux first."
  exit 1
fi

(
  flock -n 9 || {
    log "SKIP: another research pipeline is already running."
    exit 0
  }

  log "Research pipeline started. days=$DAYS max_load=$MAX_LOAD"

  if ! load_is_safe; then
    log "SKIP: system load too high. current_load=$(current_load), max_load=$MAX_LOAD"
    exit 0
  fi

  if heavy_process_running; then
    log "SKIP: heavy Hunter process is already running. Avoiding SQLite lock."
    exit 0
  fi

  BACKUP_FILE="$BACKUP_DIR/mamuyy_hunter_before_research_pipeline_$(date -u +%Y%m%d_%H%M%S).db"
  log "Creating DB backup: $BACKUP_FILE"
  cp "$DB_PATH" "$BACKUP_FILE"

  log "Step 1/3: backfill historical klines"
  "$PYTHON" main.py --backfill --days "$DAYS" >> "$LOG_DIR/research_pipeline.log" 2>&1

  log "Step 2/3: label historical outcomes"
  "$PYTHON" main.py --label-outcomes --days "$DAYS" >> "$LOG_DIR/research_pipeline.log" 2>&1

  log "Step 3/3: post-check only. ML/orchestrator refresh is intentionally NOT run here."
  post_check

  log "Research pipeline finished safely."

) 9>"$LOCK_FILE"
