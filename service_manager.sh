#!/bin/bash
# AD-SMTA Process Supervisor
# Manages Ollama + Bot + Dashboard with auto-restart and health checks
# Works in Docker containers where systemd is not PID 1

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASEDIR"
PID_DIR="$BASEDIR/pids"
mkdir -p "$PID_DIR"

# Service definitions
SERVICES=(
    "ollama|/usr/local/bin/ollama serve|5|ollama.log|http://localhost:11434/api/tags"
    "bot|python3 -u app.py|15|bot.log|http://127.0.0.1:9100/"
)

# Colors
G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'

is_running() {
    local pidfile="$PID_DIR/$1.pid"
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pidfile"
    fi
    return 1
}

start_service() {
    local name="$1" cmd="$2" restart_sec="$3" logfile="$4" health_url="$5"

    if is_running "$name"; then
        echo -e "  ${G}●${NC} $name: running (PID $(cat $PID_DIR/$name.pid))"
        return
    fi

    echo -e "  ${Y}○${NC} $name: starting..."

    # For bot, wait for ollama
    if [ "$name" = "bot" ] && ! is_running "ollama"; then
        echo -e "  ${Y}○${NC} $name: waiting for ollama..."
        start_service "ollama" "/usr/local/bin/ollama serve" "5" "ollama.log" "http://localhost:11434/api/tags"
        sleep 5
    fi

    cd "$BASEDIR"
    nohup $cmd > "$LOG_DIR/$logfile" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_DIR/$name.pid"

    # Wait for health check
    if [ -n "$health_url" ]; then
        local tries=0
        while [ $tries -lt 15 ]; do
            if curl -sf "$health_url" >/dev/null 2>&1; then
                echo -e "  ${G}●${NC} $name: running (PID $pid)"
                return
            fi
            sleep 1
            tries=$((tries + 1))
        done
        echo -e "  ${Y}●${NC} $name: started (PID $pid) - health check pending"
    else
        echo -e "  ${G}●${NC} $name: running (PID $pid)"
    fi
}

stop_service() {
    local name="$1"
    local pidfile="$PID_DIR/$name.pid"

    if ! is_running "$name"; then
        echo -e "  ${G}●${NC} $name: not running"
        return
    fi

    local pid=$(cat "$pidfile")
    echo -e "  ${Y}○${NC} $name: stopping (PID $pid)..."

    # Graceful stop first
    kill -TERM "$pid" 2>/dev/null
    local wait=0
    while kill -0 "$pid" 2>/dev/null && [ $wait -lt 10 ]; do
        sleep 1
        wait=$((wait + 1))
    done

    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        echo -e "  ${R}●${NC} $name: force killed"
    else
        echo -e "  ${G}●${NC} $name: stopped"
    fi
    rm -f "$pidfile"
}

health_check() {
    local name="$1" url="$2"
    if [ -z "$url" ]; then return 0; fi
    curl -sf "$url" >/dev/null 2>&1
}

supervisor_loop() {
    echo -e "${C}AD-SMTA Supervisor running (PID $$)${NC}"
    echo "Monitoring services every 30 seconds..."

    while true; do
        sleep 30

        # Check each service and restart if down
        for svc_def in "${SERVICES[@]}"; do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "$svc_def"

            if ! is_running "$name"; then
                echo -e "  ${R}[DEAD]${NC} $name - restarting..."
                start_service "$name" "$cmd" "$restart_sec" "$logfile" "$health_url"
            elif ! health_check "$name" "$health_url"; then
                echo -e "  ${Y}[UNHEALTHY]${NC} $name - restarting..."
                stop_service "$name"
                sleep 2
                start_service "$name" "$cmd" "$restart_sec" "$logfile" "$health_url"
            fi
        done
    done
}

show_status() {
    echo ""
    echo -e "${C}╔══════════════════════════════════════╗${NC}"
    echo -e "${C}║     AD-SMTA Service Status           ║${NC}"
    echo -e "${C}╚══════════════════════════════════════╝${NC}"
    echo ""

    for svc_def in "${SERVICES[@]}"; do
        IFS='|' read -r name cmd restart_sec logfile health_url <<< "$svc_def"
        if is_running "$name"; then
            local pid=$(cat "$PID_DIR/$name.pid")
            local mem=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1fMB", $1/1024}')
            echo -e "  ${G}●${NC} ${name}: ${G}RUNNING${NC} (PID $pid, ${mem:-?})"
        else
            echo -e "  ${R}●${NC} ${name}: ${R}STOPPED${NC}"
        fi
    done

    # Ollama models
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        local models=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print(', '.join([m['name'] for m in json.load(sys.stdin).get('models',[])]))" 2>/dev/null)
        echo -e "  ${G}●${NC} Models: ${G}${models}${NC}"
    fi

    # Dashboard
    if curl -sf http://127.0.0.1:9100/ >/dev/null 2>&1; then
        echo -e "  ${G}●${NC} Dashboard: ${G}http://0.0.0.0:9100${NC}"
    else
        echo -e "  ${R}●${NC} Dashboard: ${R}DOWN${NC}"
    fi

    # Show logs tail
    echo ""
    echo -e "${C}── Recent Logs ──${NC}"
    tail -3 "$LOG_DIR/bot.log" 2>/dev/null | sed 's/^/  /'
    echo ""
}

# Parse commands
case "${1:-status}" in
    start)
        echo -e "${C}Starting all services...${NC}"
        for svc_def in "${SERVICES[@]}"; do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "$svc_def"
            start_service "$name" "$cmd" "$restart_sec" "$logfile" "$health_url"
        done
        show_status
        ;;
    stop)
        echo -e "${C}Stopping all services...${NC}"
        # Stop in reverse order
        for ((i=${#SERVICES[@]}-1; i>=0; i--)); do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "${SERVICES[$i]}"
            stop_service "$name"
        done
        show_status
        ;;
    restart)
        echo -e "${C}Restarting all services...${NC}"
        for ((i=${#SERVICES[@]}-1; i>=0; i--)); do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "${SERVICES[$i]}"
            stop_service "$name"
        done
        sleep 3
        for svc_def in "${SERVICES[@]}"; do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "$svc_def"
            start_service "$name" "$cmd" "$restart_sec" "$logfile" "$health_url"
        done
        show_status
        ;;
    status)
        show_status
        ;;
    supervisor)
        # Run in foreground as supervisor daemon
        echo -e "${C}Starting supervisor daemon...${NC}"
        # Start all services first
        for svc_def in "${SERVICES[@]}"; do
            IFS='|' read -r name cmd restart_sec logfile health_url <<< "$svc_def"
            start_service "$name" "$cmd" "$restart_sec" "$logfile" "$health_url"
        done
        show_status
        # Then enter supervisor loop
        supervisor_loop &
        SUPERVISOR_PID=$!
        echo "$SUPERVISOR_PID" > "$PID_DIR/supervisor.pid"
        echo -e "${G}Supervisor daemon running (PID $SUPERVISOR_PID)${NC}"
        ;;
    *)
        echo "AD-SMTA Service Manager"
        echo ""
        echo "Usage: $0 {start|stop|restart|status|supervisor}"
        echo ""
        echo "  start      - Start Ollama + Bot"
        echo "  stop       - Stop all services"
        echo "  restart    - Restart all services"
        echo "  status     - Show service status"
        echo "  supervisor - Start with auto-restart monitoring"
        ;;
esac
