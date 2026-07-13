#!/bin/bash
# AD-SMTA Service Manager
# Keeps Ollama and the bot running as background processes

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
OLLAMA_PID_FILE="$BASEDIR/ollama.pid"
BOT_PID_FILE="$BASEDIR/bot.pid"
LOG_DIR="$BASEDIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

status_ollama() {
    if [ -f "$OLLAMA_PID_FILE" ] && kill -0 $(cat "$OLLAMA_PID_FILE") 2>/dev/null; then
        return 0
    fi
    return 1
}

status_bot() {
    if [ -f "$BOT_PID_FILE" ] && kill -0 $(cat "$BOT_PID_FILE") 2>/dev/null; then
        return 0
    fi
    return 1
}

start_ollama() {
    if status_ollama; then
        echo -e "${GREEN}[OK]${NC} Ollama already running (PID $(cat $OLLAMA_PID_FILE))"
        return
    fi
    echo -e "${YELLOW}[...]${NC} Starting Ollama..."
    nohup /usr/local/bin/ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
    echo $! > "$OLLAMA_PID_FILE"
    sleep 3
    if status_ollama; then
        echo -e "${GREEN}[OK]${NC} Ollama started (PID $(cat $OLLAMA_PID_FILE))"
        # Check if model is available
        MODELS=$(curl -s http://localhost:11434/api/tags 2>/dev/null | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin).get('models',[])])" 2>/dev/null)
        if echo "$MODELS" | grep -q "qwen2.5:3b"; then
            echo -e "${GREEN}[OK]${NC} Model qwen2.5:3b available"
        else
            echo -e "${YELLOW}[...]${NC} Pulling qwen2.5:3b model..."
            nohup /usr/local/bin/ollama pull qwen2.5:3b > "$LOG_DIR/ollama_pull.log" 2>&1 &
            echo -e "${YELLOW}[...]${NC} Model pull running in background"
        fi
    else
        echo -e "${RED}[FAIL]${NC} Ollama failed to start. Check $LOG_DIR/ollama.log"
    fi
}

stop_ollama() {
    if status_ollama; then
        kill $(cat "$OLLAMA_PID_FILE") 2>/dev/null
        rm -f "$OLLAMA_PID_FILE"
        echo -e "${YELLOW}[STOP]${NC} Ollama stopped"
    else
        echo -e "${YELLOW}[OK]${NC} Ollama not running"
    fi
}

start_bot() {
    if status_bot; then
        echo -e "${GREEN}[OK]${NC} Bot already running (PID $(cat $BOT_PID_FILE))"
        return
    fi
    if ! status_ollama; then
        echo -e "${YELLOW}[...]${NC} Starting Ollama first..."
        start_ollama
        sleep 2
    fi
    echo -e "${YELLOW}[...]${NC} Starting AD-SMTA bot..."
    cd "$BASEDIR"
    nohup python3 -u app.py > "$LOG_DIR/bot.log" 2>&1 &
    echo $! > "$BOT_PID_FILE"
    sleep 2
    if status_bot; then
        echo -e "${GREEN}[OK]${NC} Bot started (PID $(cat $BOT_PID_FILE))"
    else
        echo -e "${RED}[FAIL]${NC} Bot failed to start. Check $LOG_DIR/bot.log"
    fi
}

stop_bot() {
    if status_bot; then
        kill $(cat "$BOT_PID_FILE") 2>/dev/null
        rm -f "$BOT_PID_FILE"
        echo -e "${YELLOW}[STOP]${NC} Bot stopped"
    else
        echo -e "${YELLOW}[OK]${NC} Bot not running"
    fi
}

restart_ollama() {
    stop_ollama
    sleep 1
    start_ollama
}

restart_bot() {
    stop_bot
    sleep 2
    start_bot
}

show_status() {
    echo ""
    echo "========================================="
    echo "  AD-SMTA Service Status"
    echo "========================================="
    if status_ollama; then
        echo -e "  Ollama:  ${GREEN}RUNNING${NC} (PID $(cat $OLLAMA_PID_FILE))"
    else
        echo -e "  Ollama:  ${RED}STOPPED${NC}"
    fi
    if status_bot; then
        echo -e "  Bot:     ${GREEN}RUNNING${NC} (PID $(cat $BOT_PID_FILE))"
    else
        echo -e "  Bot:     ${RED}STOPPED${NC}"
    fi
    # Check Ollama API
    if curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
        MODELS=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print(', '.join([m['name'] for m in json.load(sys.stdin).get('models',[])]))" 2>/dev/null)
        echo -e "  Models:  ${GREEN}$MODELS${NC}"
    else
        echo -e "  Models:  ${RED}unreachable${NC}"
    fi
    echo "========================================="
    echo ""
}

case "$1" in
    start)
        start_ollama
        start_bot
        show_status
        ;;
    stop)
        stop_bot
        stop_ollama
        show_status
        ;;
    restart)
        restart_bot
        show_status
        ;;
    status)
        show_status
        ;;
    start-ollama)
        start_ollama
        ;;
    stop-ollama)
        stop_ollama
        ;;
    start-bot)
        start_bot
        ;;
    stop-bot)
        stop_bot
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|start-ollama|stop-ollama|start-bot|stop-bot}"
        echo ""
        echo "  start        - Start Ollama + Bot"
        echo "  stop         - Stop Bot + Ollama"
        echo "  restart      - Restart Bot"
        echo "  status       - Show service status"
        echo "  start-ollama - Start Ollama only"
        echo "  stop-ollama  - Stop Ollama only"
        echo "  start-bot    - Start Bot only"
        echo "  stop-bot     - Stop Bot only"
        ;;
esac
