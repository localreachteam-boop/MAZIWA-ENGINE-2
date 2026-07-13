#!/bin/bash
# Continuous resource monitor for M-ALMIS
# Runs every 60 seconds, logs warnings

LOG="/root/Documents/Codex/2026-07-11/if-you-need-to-run-this/resource.log"

while true; do
    MEM_PCT=$(free -m 2>/dev/null | awk '/Mem:/{printf "%.0f", $3*100/$2}')
    LOAD=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}')
    TEMP=$(termux-battery-status 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['temperature'])" 2>/dev/null || echo "0")
    
    TS=$(date '+%H:%M:%S')
    
    # Log if resources are high
    if [ "${MEM_PCT:-0}" -gt 80 ]; then
        echo "[$TS] HIGH_MEM: ${MEM_PCT}%" >> "$LOG"
    fi
    if [ "$(echo "$LOAD > 4.0" | bc 2>/dev/null)" = "1" ]; then
        echo "[$TS] HIGH_LOAD: $LOAD" >> "$LOG"
    fi
    TEMP_INT=$(echo "$TEMP" | cut -d. -f1)
    if [ "${TEMP_INT:-0}" -gt 40 ]; then
        echo "[$TS] HIGH_TEMP: ${TEMP}°C" >> "$LOG"
    fi
    
    sleep 60
done
