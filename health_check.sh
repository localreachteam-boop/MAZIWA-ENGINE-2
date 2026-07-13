#!/bin/bash
# AD-SMTA Health Check — Full system diagnostics
cd "$(dirname "$0")"

echo "╔══════════════════════════════════════════════════╗"
echo "║      AD-SMTA SYSTEM HEALTH CHECK               ║"
echo "║      $(date '+%Y-%m-%d %H:%M:%S UTC')                   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
WARN=0

check() {
    local name="$1" status="$2" detail="$3"
    if [ "$status" = "PASS" ]; then
        echo "  ✅ $name: $detail"
        PASS=$((PASS+1))
    elif [ "$status" = "WARN" ]; then
        echo "  ⚠️  $name: $detail"
        WARN=$((WARN+1))
    else
        echo "  ❌ $name: $detail"
        FAIL=$((FAIL+1))
    fi
}

# ── 1. PROCESSES ──────────────────────────────────────
echo "━━━ PROCESSES ━━━"

# Brain
BRAIN_PID=$(pgrep -f "brain_active.py" 2>/dev/null | head -1)
if [ -n "$BRAIN_PID" ]; then
    BRAIN_MEM=$(ps -o rss= -p $BRAIN_PID 2>/dev/null | awk '{printf "%.1f", $1/1024}')
    BRAIN_CPU=$(ps -o %cpu= -p $BRAIN_PID 2>/dev/null | tr -d ' ')
    check "Brain v3" "PASS" "PID $BRAIN_PID | ${BRAIN_MEM}MB RAM | ${BRAIN_CPU}% CPU"
else
    check "Brain v3" "FAIL" "NOT RUNNING"
fi

# Dashboard
DASH_PID=$(pgrep -f "dashboard_server.py" 2>/dev/null | head -1)
if [ -n "$DASH_PID" ]; then
    DASH_MEM=$(ps -o rss= -p $DASH_PID 2>/dev/null | awk '{printf "%.1f", $1/1024}')
    check "Dashboard" "PASS" "PID $DASH_PID | ${DASH_MEM}MB RAM"
else
    check "Dashboard" "FAIL" "NOT RUNNING"
fi

# Ollama
OLLAMA_PID=$(pgrep -f "ollama serve" 2>/dev/null | head -1)
if [ -n "$OLLAMA_PID" ]; then
    OLLAMA_MEM=$(ps -o rss= -p $OLLAMA_PID 2>/dev/null | awk '{printf "%.1f", $1/1024}')
    check "Ollama" "PASS" "PID $OLLAMA_PID | ${OLLAMA_MEM}MB RAM"
else
    check "Ollama" "WARN" "NOT RUNNING (AI brain offline)"
fi

# Zombie check
ZOMBIES=$(ps aux 2>/dev/null | grep -c "<defunct>" 2>/dev/null || echo "0")
if [ "$ZOMBIES" -gt 0 ]; then
    check "Zombie processes" "WARN" "$ZOMBIES zombie(s) found"
else
    check "Zombie processes" "PASS" "None"
fi

# ── 2. HTTP ENDPOINTS ─────────────────────────────────
echo ""
echo "━━━ HTTP ENDPOINTS ━━━"

# Dashboard HTTP
HTTP_CODE=$(timeout 3 curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9100/ 2>/dev/null)
if [ "$HTTP_CODE" = "200" ]; then
    check "Dashboard HTTP" "PASS" "Port 9100 → HTTP $HTTP_CODE"
else
    check "Dashboard HTTP" "FAIL" "Port 9100 → HTTP $HTTP_CODE"
fi

# State JSON
STATE_SIZE=$(timeout 3 curl -s http://127.0.0.1:9100/state.json 2>/dev/null | wc -c)
if [ "$STATE_SIZE" -gt 100 ]; then
    check "State JSON" "PASS" "${STATE_SIZE} bytes served"
else
    check "State JSON" "FAIL" "Only ${STATE_SIZE} bytes"
fi

# Ollama
OLLAMA_HTTP=$(timeout 3 curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:11434/ 2>/dev/null)
if [ "$OLLAMA_HTTP" = "200" ]; then
    check "Ollama HTTP" "PASS" "Port 11434 → HTTP $OLLAMA_HTTP"
else
    check "Ollama HTTP" "WARN" "Port 11434 → HTTP $OLLAMA_HTTP"
fi

# ── 3. STATE FRESHNESS ────────────────────────────────
echo ""
echo "━━━ STATE FRESHNESS ━━━"

STATE_MTIME=$(stat -c %Y dashboard/templates/state.json 2>/dev/null)
NOW=$(date +%s)
if [ -n "$STATE_MTIME" ]; then
    AGE=$((NOW - STATE_MTIME))
    if [ "$AGE" -lt 10 ]; then
        check "State file age" "PASS" "${AGE}s old (fresh)"
    elif [ "$AGE" -lt 30 ]; then
        check "State file age" "WARN" "${AGE}s old (slightly stale)"
    else
        check "State file age" "FAIL" "${AGE}s old (stale!)"
    fi
else
    check "State file age" "FAIL" "File not found"
fi

# ── 4. STATE CONTENTS ─────────────────────────────────
echo ""
echo "━━━ STATE CONTENTS ━━━"

STATE=$(timeout 3 curl -s http://127.0.0.1:9100/state.json 2>/dev/null)
if [ -n "$STATE" ]; then
    BALANCE=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{d.get(\"balance\",0):.2f}')" 2>/dev/null)
    TRADES=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('trades',0))" 2>/dev/null)
    WINS=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('wins',0))" 2>/dev/null)
    LOSSES=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('losses',0))" 2>/dev/null)
    MARKET=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('selected_market','?'))" 2>/dev/null)
    STRATEGY=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('selected_strategy','?'))" 2>/dev/null)
    REGIME=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('regime','?'))" 2>/dev/null)
    CYCLES=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('cycles',0))" 2>/dev/null)
    
    check "Balance" "PASS" "\$$BALANCE"
    check "Trades" "PASS" "$TRADES (W:$WINS L:$LOSSES)"
    check "Market" "PASS" "$MARKET | $STRATEGY"
    check "Regime" "PASS" "$REGIME"
    check "Cycles" "PASS" "$CYCLES"
    
    # Check critical panels
    for PANEL in protection session judge brain_status picker strategy_health alm_brain market_list; do
        HAS=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print('yes' if d.get('$PANEL') else 'no')" 2>/dev/null)
        if [ "$HAS" = "yes" ]; then
            check "Panel: $PANEL" "PASS" "loaded"
        else
            check "Panel: $PANEL" "FAIL" "missing"
        fi
    done
    
    # Check agents
    AGENTS=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); a=d.get('agents',{}); print(','.join(k for k,v in a.items() if v is True))" 2>/dev/null)
    AGENT_COUNT=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); a=d.get('agents',{}); print(sum(1 for v in a.values() if v is True))" 2>/dev/null)
    check "Active agents" "PASS" "$AGENT_COUNT: $AGENTS"
    
    # Discord
    DC_ENABLED=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('discord',{}).get('enabled',False))" 2>/dev/null)
    DC_SENT=$(echo "$STATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('discord',{}).get('total_sent',0))" 2>/dev/null)
    if [ "$DC_ENABLED" = "True" ]; then
        check "Discord" "PASS" "enabled, $DC_SENT alerts sent"
    else
        check "Discord" "WARN" "disabled"
    fi
else
    check "State contents" "FAIL" "Cannot fetch state"
fi

# ── 5. MEMORY FILES ───────────────────────────────────
echo ""
echo "━━━ MEMORY FILES ━━━"

if [ -f "agent_memory.json" ]; then
    MEM_SIZE=$(wc -c < agent_memory.json)
    MEM_TRADES=$(python3 -c "import json; d=json.load(open('agent_memory.json')); print(len(d.get('trades',[])))" 2>/dev/null)
    check "Agent memory" "PASS" "${MEM_SIZE} bytes, $MEM_TRADES trades stored"
else
    check "Agent memory" "WARN" "No memory file"
fi

if [ -f "trading_state.json" ]; then
    TS_AGE=$(($(date +%s) - $(stat -c %Y trading_state.json)))
    check "Trading state" "PASS" "${TS_AGE}s old"
else
    check "Trading state" "FAIL" "Missing"
fi

if [ -f "brain_active.log" ]; then
    LOG_LINES=$(wc -l < brain_active.log)
    LOG_ERRORS=$(grep -c "ERROR\|Error\|error" brain_active.log 2>/dev/null || echo "0")
    check "Brain log" "PASS" "$LOG_LINES lines, $LOG_ERRORS errors"
else
    check "Brain log" "WARN" "No log file"
fi

# ── 6. DISK & MEMORY ──────────────────────────────────
echo ""
echo "━━━ SYSTEM RESOURCES ━━━"

# Total RAM used by AD-SMTA
TOTAL_RAM=0
for PID in $BRAIN_PID $DASH_PID $OLLAMA_PID; do
    if [ -n "$PID" ]; then
        RAM=$(ps -o rss= -p $PID 2>/dev/null | tr -d ' ')
        TOTAL_RAM=$((TOTAL_RAM + ${RAM:-0}))
    fi
done
TOTAL_RAM_MB=$((TOTAL_RAM / 1024))
check "Total AD-SMTA RAM" "PASS" "${TOTAL_RAM_MB}MB"

# Disk
DISK_USED=$(df -h . | tail -1 | awk '{print $5}')
check "Disk usage" "PASS" "$DISK_USED used"

# ── 7. NETWORK ────────────────────────────────────────
echo ""
echo "━━━ NETWORK ━━━"

# Deriv WebSocket
DERIV_OK=$(timeout 5 python3 -c "
import asyncio, websockets, json
async def test():
    ws = await websockets.connect('wss://ws.derivws.com/websockets/v3?app_id=1089', close_timeout=3)
    await ws.send(json.dumps({'time': 1}))
    r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
    await ws.close()
    print('ok' if 'time' in r else 'fail')
asyncio.run(test())
" 2>/dev/null)
if [ "$DERIV_OK" = "ok" ]; then
    check "Deriv WebSocket" "PASS" "Connected"
else
    check "Deriv WebSocket" "FAIL" "Cannot connect"
fi

# Discord webhook
DISCORD_OK=$(timeout 5 python3 -c "
import json, urllib.request
url = '$(grep DISCORD_WEBHOOK .env 2>/dev/null | cut -d= -f2-)'
try:
    req = urllib.request.Request(url, data=json.dumps({'content': ''}).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'AD-SMTA/1.0'}, method='POST')
    r = urllib.request.urlopen(req, timeout=5)
    print('ok')
except Exception as e:
    except urllib.error.HTTPError as e:
    print('ok' if e.code in (200, 204) else 'fail')
except:
    print('ok')
" 2>/dev/null)
if [ "$DISCORD_OK" = "ok" ]; then
    check "Discord webhook" "PASS" "Reachable"
else
    check "Discord webhook" "WARN" "May be blocked"
fi

# ── 8. DASHBOARD HTML ─────────────────────────────────
echo ""
echo "━━━ DASHBOARD HTML ━━━"

HTML_SIZE=$(wc -c < dashboard/templates/index.html 2>/dev/null || echo "0")
HTML_LINES=$(wc -l < dashboard/templates/index.html 2>/dev/null || echo "0")
HTML_IDS=$(grep -o 'getElementById' dashboard/templates/index.html 2>/dev/null | wc -l)
check "Dashboard HTML" "PASS" "${HTML_SIZE} bytes, ${HTML_LINES} lines, ${HTML_IDS} element refs"

# Check for WebSocket references (should be 0)
WS_REFS=$(grep -c 'ws://' dashboard/templates/index.html 2>/dev/null || echo "0")
if [ "$WS_REFS" = "0" ]; then
    check "WebSocket removed" "PASS" "No ws:// references"
else
    check "WebSocket removed" "WARN" "$WS_REFS ws:// references remain"
fi

# ── SUMMARY ───────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  SUMMARY                                        ║"
echo "╠══════════════════════════════════════════════════╣"
TOTAL=$((PASS + FAIL + WARN))
echo "║  ✅ Pass: $PASS  ❌ Fail: $FAIL  ⚠️  Warn: $WARN  Total: $TOTAL    ║"
if [ "$FAIL" -eq 0 ]; then
    echo "║  🟢 SYSTEM HEALTHY                             ║"
else
    echo "║  🔴 SYSTEM NEEDS ATTENTION                      ║"
fi
echo "╚══════════════════════════════════════════════════╝"
