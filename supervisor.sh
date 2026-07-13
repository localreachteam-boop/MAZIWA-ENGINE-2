#!/bin/bash
# AD-SMTA Process Supervisor - manages ollama + app.py with auto-restart
PROJECT_DIR="/root/Documents/Codex/2026-07-11/if-you-need-to-run-this"
PID_DIR="$PROJECT_DIR/.pids"
LOG_DIR="$PROJECT_DIR"
OLLAMA_BIN="/usr/local/bin/ollama"

mkdir -p "$PID_DIR"

_start_ollama() {
    if pgrep -f "ollama serve" >/dev/null 2>&1; then
        echo "[supervisor] Ollama already running"
        return 0
    fi
    echo "[supervisor] Starting Ollama..."
    OLLAMA_NUM_THREADS=4 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_HOST=127.0.0.1:11434 \
        setsid "$OLLAMA_BIN" serve > "$LOG_DIR/ollama.log" 2>&1 &
    echo $! > "$PID_DIR/ollama.pid"
    sleep 3
    if pgrep -f "ollama serve" >/dev/null 2>&1; then
        echo "[supervisor] Ollama started (PID: $(cat $PID_DIR/ollama.pid))"
        return 0
    else
        echo "[supervisor] Ollama failed to start"
        return 1
    fi
}

_start_app() {
    if pgrep -f "python3 -u app.py" >/dev/null 2>&1; then
        echo "[supervisor] App already running"
        return 0
    fi
    echo "[supervisor] Starting AD-SMTA..."
    cd "$PROJECT_DIR"
    setsid python3 -u app.py > "$LOG_DIR/app.log" 2>&1 &
    echo $! > "$PID_DIR/app.pid"
    sleep 2
    if pgrep -f "python3 -u app.py" >/dev/null 2>&1; then
        echo "[supervisor] App started (PID: $(cat $PID_DIR/app.pid))"
        return 0
    else
        echo "[supervisor] App failed to start"
        return 1
    fi
}

_stop() {
    echo "[supervisor] Stopping services..."
    killall -9 python3 2>/dev/null
    killall -9 ollama 2>/dev/null
    rm -f "$PID_DIR"/*.pid
    sleep 1
    echo "[supervisor] All stopped"
}

_restart() {
    _stop
    sleep 2
    _start_ollama
    sleep 3
    _start_app
}

_status() {
    echo "=== AD-SMTA Status ==="
    if pgrep -f "ollama serve" >/dev/null 2>&1; then
        echo "  Ollama:     RUNNING (PID $(pgrep -f 'ollama serve'))"
    else
        echo "  Ollama:     STOPPED"
    fi
    if pgrep -f "python3 -u app.py" >/dev/null 2>&1; then
        echo "  App:        RUNNING (PID $(pgrep -f 'python3 -u app.py'))"
    else
        echo "  App:        STOPPED"
    fi
    echo "  Dashboard:  http://localhost:9100"
    echo "  WebSocket:  ws://localhost:9101"
}

_monitor() {
    echo "[supervisor] Starting monitor (Ctrl+C to stop)..."
    while true; do
        # Check Ollama
        if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
            echo "[supervisor] $(date '+%H:%M:%S') Ollama crashed, restarting..."
            _start_ollama
        fi
        # Check App
        if ! pgrep -f "python3 -u app.py" >/dev/null 2>&1; then
            echo "[supervisor] $(date '+%H:%M:%S') App crashed, restarting..."
            sleep 3  # Wait for Ollama
            _start_app
        fi
        sleep 10
    done
}

case "$1" in
    start)   _start_ollama; sleep 3; _start_app ;;
    stop)    _stop ;;
    restart) _restart ;;
    status)  _status ;;
    monitor) _start_ollama; sleep 3; _start_app; _monitor ;;
    *)       echo "Usage: $0 {start|stop|restart|status|monitor}" ;;
esac
