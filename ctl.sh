#!/bin/bash
# AD-SMTA Control Script
DIR="/root/Documents/Codex/2026-07-11/if-you-need-to-run-this"
PIDFILE="$DIR/bot.pid"

case "$1" in
    start)
        if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
            echo "Bot already running (PID $(cat $PIDFILE))"
        else
            nohup "$DIR/supervisor.sh" > /dev/null 2>&1 &
            echo "Supervisor started"
        fi
        ;;
    stop)
        if [ -f "$PIDFILE" ]; then
            kill $(cat "$PIDFILE") 2>/dev/null
            rm -f "$PIDFILE" "$DIR/supervisor.lock"
            echo "Bot stopped"
        fi
        # Kill any remaining
        pkill -f "supervisor.sh" 2>/dev/null
        pkill -f "python3.*app.py" 2>/dev/null
        pkill -f "alm_server" 2>/dev/null
        echo "All processes killed"
        ;;
    status)
        echo "=== AD-SMTA STATUS ==="
        if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
            echo "Bot: RUNNING (PID $(cat $PIDFILE))"
        else
            echo "Bot: STOPPED"
        fi
        python3 -c "
import json, os, time
sf = '$DIR/trading_state.json'
if os.path.exists(sf) and os.path.getsize(sf) > 10:
    age = time.time() - os.path.getmtime(sf)
    d = json.load(open(sf))
    ex = d.get('executor', {})
    print(f'  Cycles: {d.get(\"cycles\", 0)}')
    print(f'  Market: {d.get(\"selected_market\")} ({d.get(\"selected_type\")})')
    print(f'  Balance: \${d.get(\"balance\", 0):.2f}')
    print(f'  Trades: {d.get(\"trades\", 0)} Wins: {d.get(\"wins\", 0)} Losses: {d.get(\"losses\", 0)}')
    print(f'  Executor: {ex.get(\"status\")} trades={ex.get(\"trades_executed\")} cd={ex.get(\"cooldown\",{}).get(\"tier\")}')
    print(f'  Dashboard: http://$(hostname -I | awk \"{print \\$1}\"):9000')
    print(f'  State age: {age:.0f}s')
else:
    print('  No state data yet')
" 2>/dev/null
        echo "=== RECENT LOG ==="
        tail -5 "$DIR/bot.log" 2>/dev/null
        ;;
    logs)
        tail -50 "$DIR/bot.log" 2>/dev/null
        ;;
    exec-log)
        cat "$DIR/executor_debug.log" 2>/dev/null | tail -30
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs|exec-log}"
        ;;
esac
