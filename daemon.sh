#!/bin/bash
cd /root/Documents/Codex/2026-07-11/if-you-need-to-run-this
export PYTHONDONTWRITEBYTECODE=1

start_dashboard() {
    if ! pgrep -f "dashboard_server.py" > /dev/null 2>&1; then
        rm -rf agents/__pycache__
        python3 dashboard_server.py >> /tmp/dash_daemon.log 2>&1 &
        echo "$(date) Dashboard started PID $!" >> /tmp/daemon.log
    fi
}

start_brain() {
    if ! pgrep -f "brain_active.py" > /dev/null 2>&1; then
        rm -rf agents/__pycache__
        python3 -u brain_active.py >> /tmp/brain_daemon.log 2>&1 &
        echo "$(date) Brain started PID $!" >> /tmp/daemon.log
    fi
}

while true; do
    start_dashboard
    start_brain
    sleep 15
done
