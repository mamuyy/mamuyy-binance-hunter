#!/usr/bin/env bash
# scripts/start_hunter_stack.sh
# Start MAMUYY Hunter tmux stack after reboot or manual recovery.
# Idempotent — skips any session that is already running.
# PAPER_ONLY mode is enforced by the application; do NOT enable live execution here.

set -euo pipefail

REPO=/home/ubuntu/mamuyy-binance-hunter
VENV="$REPO/.venv/bin/activate"
LOG="$REPO/logs/start_hunter_stack.log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

cd "$REPO"
mkdir -p logs

log "=== start_hunter_stack.sh invoked ==="

# --- hunter (orchestrator) ---
if tmux has-session -t hunter 2>/dev/null; then
    log "Session 'hunter' already running — skipping."
else
    log "Starting 'hunter' session..."
    tmux new-session -d -s hunter \
        "cd $REPO && source $VENV && python main.py --orchestrator 2>&1 | tee -a logs/hunter_tmux.log"
    log "Session 'hunter' started."
fi

# --- dashboard (streamlit) ---
if tmux has-session -t dashboard 2>/dev/null; then
    log "Session 'dashboard' already running — skipping."
else
    log "Starting 'dashboard' session..."
    tmux new-session -d -s dashboard \
        "cd $REPO && source $VENV && streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 2>&1 | tee -a logs/dashboard_tmux.log"
    log "Session 'dashboard' started."
fi

# --- api (uvicorn) ---
if tmux has-session -t api 2>/dev/null; then
    log "Session 'api' already running — skipping."
else
    log "Starting 'api' session..."
    tmux new-session -d -s api \
        "cd $REPO && source $VENV && uvicorn hunter_api:app --host 127.0.0.1 --port 8502 2>&1 | tee -a logs/api_tmux.log"
    log "Session 'api' started."
fi

log "=== Stack startup complete ==="
echo ""
tmux ls
