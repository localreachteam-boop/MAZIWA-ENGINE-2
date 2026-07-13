#!/bin/bash
# M-ALMIS Phone Health Scanner & Optimizer
# Scans CPU, RAM, storage, battery, thermal, security, processes

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; W='\033[1;37m'; NC='\033[0m'
WARN=0; CRIT=0; OK=0

check() {
    local status="$1" msg="$2"
    if [ "$status" = "OK" ]; then
        echo -e "  ${G}✓${NC} $msg"
        OK=$((OK+1))
    elif [ "$status" = "WARN" ]; then
        echo -e "  ${Y}⚠${NC} $msg"
        WARN=$((WARN+1))
    else
        echo -e "  ${R}✗${NC} $msg"
        CRIT=$((CRIT+1))
    fi
}

echo ""
echo -e "${W}╔══════════════════════════════════════════════╗${NC}"
echo -e "${W}║   M-ALMIS PHONE HEALTH SCAN                 ║${NC}"
echo -e "${W}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── BATTERY ──
echo -e "${C}── BATTERY ──${NC}"
BAT=$(termux-battery-status 2>/dev/null)
BAT_PCT=$(echo "$BAT" | python3 -c "import sys,json; print(json.load(sys.stdin)['percentage'])" 2>/dev/null || echo "0")
BAT_TEMP=$(echo "$BAT" | python3 -c "import sys,json; print(json.load(sys.stdin)['temperature'])" 2>/dev/null || echo "0")
BAT_STATUS=$(echo "$BAT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "UNKNOWN")

if [ "$BAT_PCT" -lt 20 ]; then
    check CRIT "Battery: ${BAT_PCT}% — CRITICAL (charge immediately)"
elif [ "$BAT_PCT" -lt 40 ]; then
    check WARN "Battery: ${BAT_PCT}% — Low (${BAT_STATUS})"
else
    check OK "Battery: ${BAT_PCT}% (${BAT_STATUS})"
fi

# Temperature
TEMP_INT=$(echo "$BAT_TEMP" | cut -d. -f1)
if [ "${TEMP_INT:-0}" -gt 45 ]; then
    check CRIT "Temperature: ${BAT_TEMP}°C — DANGER (throttling risk)"
elif [ "${TEMP_INT:-0}" -gt 38 ]; then
    check WARN "Temperature: ${BAT_TEMP}°C — Warm (reduce workload)"
else
    check OK "Temperature: ${BAT_TEMP}°C — Normal"
fi

# ── MEMORY ──
echo ""
echo -e "${C}── MEMORY ──${NC}"
MEM_TOTAL=$(free -m 2>/dev/null | awk '/Mem:/{print $2}')
MEM_USED=$(free -m 2>/dev/null | awk '/Mem:/{print $3}')
MEM_AVAIL=$(free -m 2>/dev/null | awk '/Mem:/{print $7}')
MEM_PCT=$((MEM_USED * 100 / MEM_TOTAL))
SWAP_USED=$(free -m 2>/dev/null | awk '/Swap:/{print $3}')
SWAP_TOTAL=$(free -m 2>/dev/null | awk '/Swap:/{print $2}')

if [ "$MEM_PCT" -gt 85 ]; then
    check CRIT "RAM: ${MEM_USED}/${MEM_TOTAL}MB (${MEM_PCT}%) — CRITICAL"
elif [ "$MEM_PCT" -gt 70 ]; then
    check WARN "RAM: ${MEM_USED}/${MEM_TOTAL}MB (${MEM_PCT}%) — Heavy"
else
    check OK "RAM: ${MEM_USED}/${MEM_TOTAL}MB (${MEM_PCT}%)"
fi

if [ "$MEM_AVAIL" -lt 500 ]; then
    check CRIT "Available RAM: ${MEM_AVAIL}MB — CRITICAL (OOM risk)"
elif [ "$MEM_AVAIL" -lt 1024 ]; then
    check WARN "Available RAM: ${MEM_AVAIL}MB — Low"
else
    check OK "Available RAM: ${MEM_AVAIL}MB"
fi

SWAP_PCT=$((SWAP_USED * 100 / SWAP_TOTAL))
if [ "$SWAP_PCT" -gt 70 ]; then
    check WARN "Swap: ${SWAP_USED}/${SWAP_TOTAL}MB (${SWAP_PCT}%) — Heavy swapping"
elif [ "$SWAP_PCT" -gt 40 ]; then
    check WARN "Swap: ${SWAP_USED}/${SWAP_TOTAL}MB (${SWAP_PCT}%)"
else
    check OK "Swap: ${SWAP_USED}/${SWAP_TOTAL}MB (${SWAP_PCT}%)"
fi

# ── CPU ──
echo ""
echo -e "${C}── CPU ──${NC}"
CORES=$(nproc 2>/dev/null || echo "?")
LOAD=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}')
check OK "CPU cores: ${CORES}, Load: ${LOAD}"

# ── STORAGE ──
echo ""
echo -e "${C}── STORAGE ──${NC}"
DISK_PCT=$(df / 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%')
DISK_AVAIL=$(df -h / 2>/dev/null | tail -1 | awk '{print $4}')
if [ "${DISK_PCT:-0}" -gt 90 ]; then
    check CRIT "Disk: ${DISK_PCT}% used — CRITICAL"
elif [ "${DISK_PCT:-0}" -gt 75 ]; then
    check WARN "Disk: ${DISK_PCT}% used — ${DISK_AVAIL} free"
else
    check OK "Disk: ${DISK_PCT}% used — ${DISK_AVAIL} free"
fi

# ── PROCESSES ──
echo ""
echo -e "${C}── PROCESSES ──${NC}"
PROC_COUNT=$(ps -e 2>/dev/null | wc -l || echo "?")
check OK "Running processes: ${PROC_COUNT}"

# Memory hogs
LLAMA_MEM=$(ps -p $(pgrep -f "llama" 2>/dev/null | head -1) -o rss= 2>/dev/null | awk '{printf "%.0f", $1/1024}')
PYTHON_MEM=$(ps -p $(pgrep -f "app.py" 2>/dev/null | head -1) -o rss= 2>/dev/null | awk '{printf "%.0f", $1/1024}')
OLLAMA_MEM=$(ps -p $(pgrep -f "ollama serve" 2>/dev/null | head -1) -o rss= 2>/dev/null | awk '{printf "%.0f", $1/1024}')

if [ "${LLAMA_MEM:-0}" -gt 1500 ]; then
    check WARN "llama-server: ${LLAMA_MEM}MB — Heavy (reduce threads/model)"
else
    check OK "llama-server: ${LLAMA_MEM:-0}MB"
fi
check OK "Bot (python3): ${PYTHON_MEM:-0}MB"
check OK "Ollama daemon: ${OLLAMA_MEM:-0}MB"

LLAMA_THREADS=$(ps -T -p $(pgrep -f "llama-server" 2>/dev/null | head -1) 2>/dev/null | wc -l || echo "0")
if [ "${LLAMA_THREADS:-0}" -gt 12 ]; then
    check WARN "llama-server threads: ${LLAMA_THREADS} — Too many for mobile (use 4-8)"
else
    check OK "llama-server threads: ${LLAMA_THREADS}"
fi

# ── SECURITY ──
echo ""
echo -e "${C}── SECURITY ──${NC}"

# SSH
SSH_ROOT=$(grep "^PermitRootLogin" /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}')
SSH_PASS=$(grep "^PasswordAuthentication" /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}')
if [ "$SSH_ROOT" = "yes" ]; then
    check WARN "SSH: PermitRootLogin=yes — Security risk"
fi
if [ "$SSH_PASS" = "yes" ]; then
    check WARN "SSH: PasswordAuthentication=yes — Use key-only"
fi

# .env permissions
ENV_PERMS=$(ls -la /root/Documents/Codex/2026-07-11/if-you-need-to-run-this/.env 2>/dev/null | awk '{print $1}')
if echo "$ENV_PERMS" | grep -q "rw-r"; then
    check WARN ".env permissions: ${ENV_PERMS} — Too permissive (should be 600)"
else
    check OK ".env permissions: ${ENV_PERMS}"
fi

# Ollama exposure
OLLAMA_HOST=$(pgrep -f "ollama" >/dev/null 2>&1 && cat /proc/$(pgrep -f "ollama" | head -1)/cmdline 2>/dev/null | tr '\0' '\n' | grep "OLLAMA_HOST" || echo "")
if echo "$OLLAMA_HOST" | grep -q "0.0.0.0"; then
    check WARN "Ollama: Listening on 0.0.0.0 — Exposed to network"
else
    check OK "Ollama: Bound to localhost"
fi

# Network
WIFI=$(termux-wifi-connectioninfo 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"ssid\"]} ({d[\"rssi\"]}dBm)')" 2>/dev/null || echo "unknown")
check OK "Network: ${WIFI}"

# ── SUMMARY ──
echo ""
echo -e "${W}═══════════════════════════════════════════════${NC}"
echo -e "  ${G}OK: ${OK}${NC}  ${Y}WARN: ${WARN}${NC}  ${R}CRIT: ${CRIT}${NC}"
if [ $CRIT -gt 0 ]; then
    echo -e "  ${R}STATUS: CRITICAL — Immediate action needed${NC}"
elif [ $WARN -gt 0 ]; then
    echo -e "  ${Y}STATUS: WARNING — Some issues to address${NC}"
else
    echo -e "  ${G}STATUS: HEALTHY — All systems nominal${NC}"
fi
echo -e "${W}═══════════════════════════════════════════════${NC}"
echo ""
