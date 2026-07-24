#!/bin/bash
cd /root/Documents/Codex/2026-07-11/if-you-need-to-run-this

while true; do
    if ! pgrep -f "dashboard_server.py" >/dev/null 2>&1; then
        PYTHONUNBUFFERED=1 python3 dashboard_server.py >>/tmp/alm_dashboard.log 2>&1 &
        echo "[$(date '+%H:%M:%S')] Dashboard restarted" >> /tmp/watchdog.log
    fi
    if ! pgrep -f "brain_active.py" >/dev/null 2>&1; then
        PYTHONUNBUFFERED=1 python3 brain_active.py >>/tmp/alm_brain.log 2>&1 &
        echo "[$(date '+%H:%M:%S')] Brain restarted" >> /tmp/watchdog.log
    fi
    sleep 15
done
