#!/usr/bin/env bash
# Safe monthly retrain wrapper — uses flock, checks orchestrator cycle state.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$REPO_DIR/logs/monthly_retrain.log"
LOCK="/tmp/retrain_safe.lock"
DIAG_LOG="$REPO_DIR/logs/orchestrator_diagnostics.log"
TMUX_SESSION="hunter"
WINDOW_MINUTES=30

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

# Prevent concurrent runs
exec 9>"$LOCK"
if ! flock -n 9; then
    log "[SKIP] Another retrain_safe.sh is already running (lock held). Exiting."
    exit 0
fi

log "[START] retrain_safe.sh invoked"

# Check tmux session exists
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    log "[SKIP] tmux session '$TMUX_SESSION' not found. Orchestrator may be down. Exiting."
    exit 0
fi
log "[OK] tmux session '$TMUX_SESSION' is running"

# Detect mid-cycle: engine_start without a subsequent cycle_end in last WINDOW_MINUTES
CUTOFF_TS=$(date -u -d "-${WINDOW_MINUTES} minutes" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null \
    || date -u -v-${WINDOW_MINUTES}M '+%Y-%m-%dT%H:%M:%S')  # GNU / BSD fallback

RECENT_LINES=$(awk -v cutoff="$CUTOFF_TS" '
    /\"timestamp\":/ {
        match($0, /"timestamp": "([^"]+)"/, a)
        if (a[1] >= cutoff) print
    }
' "$DIAG_LOG" 2>/dev/null || true)

HAS_ENGINE_START=$(echo "$RECENT_LINES" | grep -c '"event_type": "engine_start"' || true)
HAS_CYCLE_END=$(echo "$RECENT_LINES" | grep -c '"event_type": "cycle_end"' || true)

log "[INFO] Last ${WINDOW_MINUTES}m: engine_start=$HAS_ENGINE_START  cycle_end=$HAS_CYCLE_END"

if [ "$HAS_ENGINE_START" -gt 0 ] && [ "$HAS_CYCLE_END" -eq 0 ]; then
    log "[SKIP] Orchestrator appears mid-cycle (engine_start seen, no cycle_end). Skipping retrain to avoid interference."
    exit 0
fi

log "[RUN] Orchestrator is idle or between cycles. Starting retrain."
cd "$REPO_DIR"
"$REPO_DIR/.venv/bin/python" main.py --retrain-model >> "$LOG" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    log "[DONE] Retrain completed successfully (exit 0)."
else
    log "[ERROR] Retrain exited with code $EXIT_CODE."
fi

exit $EXIT_CODE
