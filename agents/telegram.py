"""
TELEGRAM — All notification, logging & health functions (merged)
Uses urllib (no requests dependency).
"""
import json, time, os, urllib.request, urllib.parse
from pathlib import Path

# Load from .env file
_ENV_FILE = Path(__file__).parent.parent / ".env"
_ENV = {}
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            _ENV[k.strip()] = v.strip().strip('"').strip("'")

BOT_TOKEN = _ENV.get("TG_BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = _ENV.get("TG_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
RATE_LIMIT = 2
_last_send = {}
_last_rates = {}

def _send(text, parse_mode="HTML"):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except:
        return False

def _load_config():
    config = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip().strip('"').strip("'")
    return config

def send_message(text, parse_mode="HTML", silent=False):
    return _send(text, parse_mode)


def _rate_limited(key, min_interval=RATE_LIMIT):
    """Check if we should skip this message type due to rate limiting."""
    now = time.time()
    last = _last_send.get(key, 0)
    if now - last < min_interval:
        return True
    _last_send[key] = now
    return False

def notify_trade(profit, market, strategy, balance, win, stake=0.35):
    """Send trade result notification."""
    if _rate_limited("trade", 5):
        return
    icon = "✅" if win else "❌"
    color = "🟢" if win else "🔴"
    text = (
        f"<b>{icon} Trade {color}</b>\n\n"
        f"<b>Market:</b> {market}\n"
        f"<b>Strategy:</b> {strategy}\n"
        f"<b>Stake:</b> ${stake:.2f}\n"
        f"<b>Result:</b> {'WIN' if win else 'LOSS'}\n"
        f"<b>P&L:</b> {'+'if profit>0 else ''}{profit:.4f}\n"
        f"<b>Balance:</b> ${balance:.2f}\n"
        f"<b>Time:</b> {time.strftime('%H:%M:%S')}"
    )
    send_message(text)

def notify_session_summary(balance, total_trades, wins, losses, pnl, win_rate):
    """Send session summary notification."""
    if _rate_limited("summary", 30):
        return
    emoji = "🟢" if pnl >= 0 else "🔴"
    text = (
        f"📊 <b>SESSION SUMMARY</b> {emoji}\n\n"
        f"<b>Balance:</b> ${balance:.2f}\n"
        f"<b>Trades:</b> {total_trades}\n"
        f"<b>Wins:</b> {wins} | <b>Losses:</b> {losses}\n"
        f"<b>Win Rate:</b> {win_rate:.1f}%\n"
        f"<b>P&L:</b> {'+'if pnl>=0 else ''}{pnl:.4f}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_message(text)

def notify_escalation(cloud_model, reason, consecutive_losses):
    """Send escalation alert."""
    if _rate_limited("escalation", 10):
        return
    text = (
        f"🔥 <b>ESCALATION ALERT</b>\n\n"
        f"<b>Trigger:</b> {consecutive_losses} consecutive losses\n"
        f"<b>Escalated to:</b> {cloud_model}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Time:</b> {time.strftime('%H:%M:%S')}"
    )
    send_message(text, silent=False)

def notify_market_rotation(old_market, new_market, reason=""):
    """Send market rotation notification."""
    if _rate_limited("rotation", 15):
        return
    text = (
        f"🔄 <b>MARKET ROTATION</b>\n\n"
        f"<b>From:</b> {old_market}\n"
        f"<b>To:</b> {new_market}\n"
        f"<b>Reason:</b> {reason or 'Strategy rotation'}\n"
        f"<b>Time:</b> {time.strftime('%H:%M:%S')}"
    )
    send_message(text)

def notify_error(error_msg, context=""):
    """Send error notification."""
    if _rate_limited("error", 10):
        return
    text = (
        f"⚠️ <b>SYSTEM ERROR</b>\n\n"
        f"<b>Error:</b> {error_msg[:200]}\n"
        f"<b>Context:</b> {context}\n"
        f"<b>Time:</b> {time.strftime('%H:%M:%S')}"
    )
    send_message(text)

def notify_daily_report(balance, total_pnl, trades, win_rate, markets_traded, best_strategy):
    """Send end of day report."""
    emoji = "🟢" if total_pnl >= 0 else "🔴"
    text = (
        f"📈 <b>DAILY REPORT</b> {emoji}\n\n"
        f"<b>Date:</b> {time.strftime('%Y-%m-%d')}\n"
        f"<b>Balance:</b> ${balance:.2f}\n"
        f"<b>Trades:</b> {trades}\n"
        f"<b>Win Rate:</b> {win_rate:.1f}%\n"
        f"<b>P&L:</b> {'+'if total_pnl>=0 else ''}{total_pnl:.4f}\n"
        f"<b>Markets:</b> {markets_traded}\n"
        f"<b>Best Strategy:</b> {best_strategy}\n"
        f"<b>Time:</b> {time.strftime('%H:%M:%S')}"
    )
    send_message(text)

def notify_startup(balance, mode, agents):
    """Send system startup notification."""
    text = (
        f"🚀 <b>ALM SYSTEM STARTED</b>\n\n"
        f"<b>Balance:</b> ${balance:.2f}\n"
        f"<b>Mode:</b> {mode}\n"
        f"<b>Agents:</b> {agents}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_message(text)

def get_status():
    """Return Telegram notifier status."""
    return {
        "configured": bool(TG_TOKEN and TG_CHAT_ID),
        "token_set": bool(TG_TOKEN),
        "chat_id_set": bool(TG_CHAT_ID),
        "last_sent": dict(_last_sent),
    }

def _load():
    global _activity, _initialized
    if _initialized:
        return
    try:
        if LOG_FILE.exists():
            _activity = json.loads(LOG_FILE.read_text())
    except:
        _activity = []
    _initialized = True

def _save():
    try:
        LOG_FILE.write_text(json.dumps(_activity[-MAX_ENTRIES:], default=str))
    except: pass

def log_command(command, user="user", response_preview="", status="ok", chat_id=""):
    """Log a Telegram command received."""
    _load()
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "type": "command",
        "command": command,
        "user": user,
        "chat_id": chat_id,
        "response_len": len(response_preview),
        "response_preview": response_preview[:120],
        "status": status,
    }
    _activity.append(entry)
    _save()

def log_response(command, success=True, chars=0, duration=0):
    """Log a Telegram response sent."""
    _load()
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "type": "response",
        "command": command,
        "success": success,
        "chars": chars,
        "duration_ms": round(duration * 1000),
    }
    _activity.append(entry)
    _save()

def log_system_event(event, detail="", level="info"):
    """Log a system event (trade, escalation, rotation, etc)."""
    _load()
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "type": "system",
        "event": event,
        "detail": detail[:200],
        "level": level,
    }
    _activity.append(entry)
    _save()

def log_trade(market, strategy, profit, balance, win):
    """Log a trade execution."""
    _load()
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "type": "trade",
        "market": market,
        "strategy": strategy,
        "profit": profit,
        "balance": balance,
        "win": win,
    }
    _activity.append(entry)
    _save()

def log_brain_action(action, detail=""):
    """Log a brain decision/action."""
    _load()
    entry = {
        "time": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "type": "brain",
        "action": action,
        "detail": detail[:200],
    }
    _activity.append(entry)
    _save()

def get_recent(n=30):
    """Get recent activity entries."""
    _load()
    return _activity[-n:]

def get_summary():
    """Get activity summary."""
    _load()
    total_commands = sum(1 for e in _activity if e.get("type") == "command")
    total_responses = sum(1 for e in _activity if e.get("type") == "response")
    total_trades = sum(1 for e in _activity if e.get("type") == "trade")
    total_brain = sum(1 for e in _activity if e.get("type") == "brain")
    total_system = sum(1 for e in _activity if e.get("type") == "system")
    
    successful_responses = sum(1 for e in _activity if e.get("type") == "response" and e.get("success"))
    failed_responses = total_responses - successful_responses
    
    last_command = ""
    for e in reversed(_activity):
        if e.get("type") == "command":
            last_command = e.get("command", "")
            break
    
    return {
        "total_commands": total_commands,
        "total_responses": total_responses,
        "successful_responses": successful_responses,
        "failed_responses": failed_responses,
        "total_trades": total_trades,
        "total_brain_actions": total_brain,
        "total_system_events": total_system,
        "last_command": last_command,
        "last_activity": _activity[-1] if _activity else None,
        "activity_count": len(_activity),
    }

def get_system_health():
    """Collect system health data."""
    health = {}
    
    # Dashboard state
    try:
        state = json.loads(STATE_FILE.read_text())
        health['balance'] = state.get('balance', 0)
        health['trades'] = state.get('trades', 0)
        health['win_rate'] = state.get('win_rate', 0)
        health['pnl'] = state.get('total_pnl', 0)
        health['cycles'] = state.get('cycles', 0)
        health['market'] = state.get('selected_market', '?')
        health['strategy'] = state.get('selected_strategy', '?')
        health['mode'] = state.get('trading_mode', '?')
        
        # C++ Engine
        cpp = state.get('cpp_engine', {})
        health['cpp_connected'] = cpp.get('connected', False)
        health['cpp_trades'] = cpp.get('trades_learned', 0)
        health['cpp_accuracy'] = cpp.get('accuracy', 0)
        pred = cpp.get('last_prediction', {})
        if pred:
            health['signal'] = 'UP ↑' if pred.get('signal') == 1 else 'DOWN ↓' if pred.get('signal') == -1 else 'NEUTRAL →'
            health['confidence'] = pred.get('confidence', 0)
            health['ev'] = pred.get('ev', 0)
        
        # ALM Brain
        ab = state.get('alm_brain', {})
        health['brain_connected'] = ab.get('connected', False)
        health['cloud_model'] = ab.get('cloud_model', '?')
        health['last_model'] = ab.get('last_model_used', '?')
        health['escalations'] = ab.get('escalation_count', 0)
        
        # Phone resources
        pr = state.get('phone_resources', {})
        health['ram_pct'] = pr.get('ram_pct', 0)
        health['swap_pct'] = pr.get('swap_pct', 0)
        health['disk_free'] = pr.get('disk_free', '?')
        health['cpu_load'] = pr.get('cpu_load', '?')
        health['processes'] = pr.get('processes', {})
        
    except Exception as e:
        health['error'] = str(e)
    
    return health

def send_health_report():
    """Send formatted health report to Telegram."""
    h = get_system_health()
    
    if 'error' in h:
        send_message(f"⚠️ <b>Health Check Error</b>\n\n{h['error']}")
        return
    
    emoji = "🟢" if h.get('pnl', 0) >= 0 else "🔴"
    brain = "🟢 ON" if h.get('brain_connected') else "🔴 OFF"
    cpp = "🟢 ON" if h.get('cpp_connected') else "🔴 OFF"
    
    signal = h.get('signal', '—')
    conf = h.get('confidence', 0)
    ev = h.get('ev', 0)
    
    text = (
        f"📊 <b>ALM HEALTH REPORT</b> {emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>Balance:</b> ${h.get('balance', 0):.2f}\n"
        f"📈 <b>Trades:</b> {h.get('trades', 0)} ({h.get('win_rate', 0):.1f}% WR)\n"
        f"📉 <b>P&L:</b> {'+'if h.get('pnl',0)>=0 else ''}{h.get('pnl', 0):.4f}\n"
        f"🔄 <b>Cycles:</b> {h.get('cycles', 0)}\n\n"
        f"🎯 <b>Market:</b> {h.get('market', '?')}\n"
        f"🧠 <b>Strategy:</b> {h.get('strategy', '?')}\n"
        f"⚙️ <b>Mode:</b> {h.get('mode', '?')}\n\n"
        f"🤖 <b>AI Brain:</b> {brain}\n"
        f"☁️ <b>Cloud:</b> {h.get('cloud_model', '?')}\n"
        f"🏠 <b>Last Model:</b> {h.get('last_model', '?')}\n"
        f"🔥 <b>Escalations:</b> {h.get('escalations', 0)}\n\n"
        f"⚡ <b>C++ Engine:</b> {cpp}\n"
        f"📊 <b>Signal:</b> {signal}\n"
        f"🎯 <b>Confidence:</b> {conf*100:.1f}%\n"
        f"💡 <b>EV:</b> {ev:.4f}\n"
        f"🧠 <b>Trained:</b> {h.get('cpp_trades', 0)} trades\n"
        f"📈 <b>Accuracy:</b> {h.get('cpp_accuracy', 0):.1f}%\n\n"
        f"📱 <b>Phone Resources</b>\n"
        f"💾 RAM: {h.get('ram_pct', 0)}%\n"
        f"💿 Swap: {h.get('swap_pct', 0)}%\n"
        f"💿 Disk: {h.get('disk_free', '?')}\n"
        f"⚡ CPU: {h.get('cpu_load', '?')}\n"
        f"🔄 Processes: {h.get('processes', {}).get('total', 0)}\n\n"
        f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    send_message(text)
    print(f"Health report sent at {time.strftime('%H:%M:%S')}")