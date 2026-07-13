#!/bin/bash
# AD-SMTA Quick Connect — paste this in any new chat
cd /root/Documents/Codex/2026-07-11/if-you-need-to-run-this
echo "=== AD-SMTA Quick Connect ==="
echo "Dashboard: http://localhost:9100"
echo "WebSocket: ws://localhost:9101"
echo ""
bash supervisor.sh status
echo ""
echo "Last trades:"
tail -3 trades.log 2>/dev/null | python3 -c "import json,sys
for line in sys.stdin:
    try:
        d=json.loads(line.strip().rstrip(','))
        if d.get('type')=='result':
            w='✅' if d.get('profit',0)>0 else '❌'
            print(f'  {w} {d.get(\"market\",\"?\")} {d.get(\"contract_type\",\"?\")} {d.get(\"strategy\",\"?\")} P&L:\${d.get(\"profit\",0):+.2f} Bal:\${d.get(\"balance\",0):.2f}')
    except: pass" 2>/dev/null
echo ""
echo "System state:"
curl -s http://127.0.0.1:9100/state.json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin)
print(f'  Balance: \${d.get(\"balance\",0):.2f}')
print(f'  Trades: {d.get(\"trades\",0)} (W:{d.get(\"wins\",0)} L:{d.get(\"losses\",0)})')
print(f'  Win Rate: {d.get(\"win_rate\",0)}%')
print(f'  P&L: \${d.get(\"total_pnl\",0):.2f}')
print(f'  Market: {d.get(\"selected_market\",\"none\")}')
print(f'  Strategy: {d.get(\"selected_strategy\",\"none\")}')
print(f'  Cycles: {d.get(\"cycles\",0)}')
print(f'  Discord: {d.get(\"discord\",{}).get(\"total_sent\",0)} sent, {d.get(\"discord\",{}).get(\"errors\",0)} errors')
print(f'  ALM Brain: {d.get(\"alm_brain\",{}).get(\"connected\",False)}')" 2>/dev/null
echo ""
echo "Quick commands:"
echo "  bash supervisor.sh status    — check services"
echo "  bash supervisor.sh restart   — restart everything"
echo "  bash supervisor.sh monitor   — auto-restart on crash"
