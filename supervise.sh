#!/bin/bash
LOG_DIR="/root/Documents/Codex/2026-07-11/if-you-need-to-run-this"
BOT_LOG="$LOG_DIR/bot.log"
SUPERVISOR_LOG="$LOG_DIR/supervisor.log"
PID_DIR="$LOG_DIR/.pids"
mkdir -p "$PID_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$SUPERVISOR_LOG"; }

is_alive() {
    [ -f "$PID_DIR/$1.pid" ] && kill -0 $(cat "$PID_DIR/$1.pid") 2>/dev/null
}

start_service() {
    local name="$1"; shift
    if is_alive "$name"; then return; fi
    cd "$LOG_DIR"
    "$@" >> "$BOT_LOG" 2>&1 &
    echo $! > "$PID_DIR/$name.pid"
    log "$name started (PID $!)"
}

stop_all() {
    log "Stopping..."
    for f in "$PID_DIR"/*.pid; do [ -f "$f" ] && kill $(cat "$f") 2>/dev/null; done
    rm -f "$PID_DIR"/*.pid
}

trap stop_all EXIT INT TERM
log "=== Supervisor started ==="

while true; do
    is_alive bot || start_service bot ./venv/bin/python -u app.py
    sleep 10
done
