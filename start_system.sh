#!/bin/bash
cd /root/Documents/Codex/2026-07-11/if-you-need-to-run-this
echo "Starting dashboard..."
nohup python3 -u dashboard_server.py > /tmp/alm_dashboard.log 2>&1 &
echo "Dashboard PID: $!"
echo "Starting brain..."
nohup python3 -u brain_active.py > /tmp/alm_brain.log 2>&1 &
echo "Brain PID: $!"
echo "System started. Dashboard: http://localhost:9102"
