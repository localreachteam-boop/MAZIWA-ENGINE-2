#!/bin/bash
# AD-SMTA Heartbeat — Health check + Discord notification
# Runs via cron every 5 minutes
cd "$(dirname "$0")"

# ── Discord Sender ─────────────────────────────────────
send_discord() {
    local TITLE="$1"
    local COLOR="$2"
    local BAL="$3" TRD="$4" WR="$5" PNL="$6" MKT="$7" STR="$8" REG="$9"
    local AG="${10}" PNL2="${11}" ERR="${12}" RAM="${13}"
    local WEBHOOK=$(grep DISCORD_WEBHOOK .env 2>/dev/null | cut -d= -f2-)
    if [ -z "$WEBHOOK" ]; then return; fi
    python3 -c "
import json, urllib.request
fields = {
    'Balance': '\$${BAL}',
    'Trades': '${TRD}',
    'Win Rate': '${WR}%',
    'P&L': '\$${PNL}',
    'Market': '${MKT}',
    'Strategy': '${STR}',
    'Regime': '${REG}',
    'Agents': '${AG}/7',
    'Panels': '${PNL2}',
    'RAM': '${RAM}MB',
    'Errors': '${ERR}',
}
payload = json.dumps({'embeds': [{'title': '${TITLE}', 'color': ${COLOR},
    'fields': [{'name': k, 'value': v, 'inline': True} for k, v in fields.items()],
    'footer': {'text': 'AD-SMTA Heartbeat'}
}]}).encode()
req = urllib.request.Request('${WEBHOOK}', data=payload,
    headers={'Content-Type': 'application/json', 'User-Agent': 'AD-SMTA/1.0'}, method='POST')
try: urllib.request.urlopen(req, timeout=10)
except: pass
" 2>/dev/null
}

# ── Collect Health Data ────────────────────────────────
NOW=$(date '+%Y-%m-%d %H:%M:%S')

BRAIN_PID=$(pgrep -f "brain_active.py" 2>/dev/null | head -1)
DASH_PID=$(pgrep -f "dashboard_server.py" 2>/dev/null | head -1)
OLLAMA_PID=$(pgrep -f "ollama serve" 2>/dev/null | head -1)

BRAIN_STATUS="❌ DOWN"; DASH_STATUS="❌ DOWN"; OLLAMA_STATUS="❌ DOWN"
[ -n "$BRAIN_PID" ] && BRAIN_STATUS="✅ UP"
[ -n "$DASH_PID" ] && DASH_STATUS="✅ UP"
[ -n "$OLLAMA_PID" ] && OLLAMA_STATUS="✅ UP"

HTTP_CODE=$(timeout 3 curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9100/ 2>/dev/null)

STATE=$(timeout 3 curl -s http://127.0.0.1:9100/state.json 2>/dev/null)
if [ -n "$STATE" ]; then
    BALANCE=$(echo "$STATE" | python3 -c "import json,sys; print(f'{json.load(sys.stdin).get(\"balance\",0):.2f}')" 2>/dev/null)
    TRADES=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('trades',0))" 2>/dev/null)
    WINS=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('wins',0))" 2>/dev/null)
    LOSSES=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('losses',0))" 2>/dev/null)
    WR=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('win_rate',0))" 2>/dev/null)
    PNL=$(echo "$STATE" | python3 -c "import json,sys; print(f'{json.load(sys.stdin).get(\"total_pnl\",0):+.2f}')" 2>/dev/null)
    MKT=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('selected_market','?'))" 2>/dev/null)
    STRAT=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('selected_strategy','?'))" 2>/dev/null)
    REGIME=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('regime','?'))" 2>/dev/null)
    AGENTS=$(echo "$STATE" | python3 -c "import json,sys; a=json.load(sys.stdin).get('agents',{}); print(sum(1 for v in a.values() if v is True))" 2>/dev/null)
    PANELS=$(echo "$STATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
panels=['protection','session','judge','brain_status','picker','strategy_health','alm_brain','market_list']
ok=sum(1 for p in panels if d.get(p))
print(f'{ok}/{len(panels)}')
" 2>/dev/null)
    TRADE_STR="${TRADES} (W:${WINS} L:${LOSSES})"
else
    BALANCE="?" TRADE_STR="?" WR="?" PNL="?" MKT="?" STRAT="?" REGIME="?" AGENTS="?" PANELS="?"
fi

BRAIN_RAM=$(ps -o rss= -p $BRAIN_PID 2>/dev/null | awk '{printf "%.0f", $1/1024}')
DASH_RAM=$(ps -o rss= -p $DASH_PID 2>/dev/null | awk '{printf "%.0f", $1/1024}')
TOTAL_RAM=$(( ${BRAIN_RAM:-0} + ${DASH_RAM:-0} ))
LOG_ERRORS=$(grep -c "ERROR\|Traceback" brain_active.log 2>/dev/null || echo "0")

# ── Determine Status ───────────────────────────────────
ALL_OK=true
[ -z "$BRAIN_PID" ] && ALL_OK=false
[ -z "$DASH_PID" ] && ALL_OK=false
[ "$HTTP_CODE" != "200" ] && ALL_OK=false

if $ALL_OK; then
    DC_TITLE="🟢 HEARTBEAT OK — $NOW"
    DC_COLOR=0x22c55e
else
    DC_TITLE="🔴 HEARTBEAT ALERT — $NOW"
    DC_COLOR=0xef4444
fi

# ── Log ────────────────────────────────────────────────
mkdir -p logs
echo "[$NOW] $([ $ALL_OK = true ] && echo OK || echo ALERT) | Bal:\$$BALANCE | T:$TRADES W:$WINS L:$LOSSES | $MKT $STRAT" >> logs/heartbeat.log
tail -500 logs/heartbeat.log > logs/heartbeat.log.tmp 2>/dev/null && mv logs/heartbeat.log.tmp logs/heartbeat.log 2>/dev/null

# ── Send Discord ───────────────────────────────────────
send_discord "$DC_TITLE" "$DC_COLOR" "$BALANCE" "$TRADE_STR" "$WR" "$PNL" "$MKT" "$STRAT" "$REGIME" "$AGENTS" "$PANELS" "$LOG_ERRORS" "$TOTAL_RAM"

echo "[heartbeat] $NOW — $([ $ALL_OK = true ] && echo OK || echo ALERT) | Bal:\$$BALANCE | T:$TRADES W:$WINS L:$LOSSES"
