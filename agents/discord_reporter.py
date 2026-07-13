"""
DISCORD REPORTER — Webhook Alert Agent
Sends trade alerts, system status, and notifications to Discord via webhooks.
No bot token needed — just webhook URL.
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


class DiscordReporter:
    """
    Sends formatted alerts to Discord via webhook.
    
    Alert Types:
    - Trade executed (win/loss)
    - Strategy change
    - System status
    - Risk warnings
    - Market rotation
    - Session summary
    """

    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url
        self.enabled = bool(webhook_url)
        self.total_sent = 0
        self.errors = 0
        self.last_error = ""
        self.notes = []
        # Rate limiting: max 5 messages per minute
        self.send_history = []
        self.RATE_LIMIT = 5
        self.RATE_WINDOW = 60

    def _rate_check(self):
        """Check if we're within Discord rate limits."""
        now = time.time()
        self.send_history = [t for t in self.send_history if now - t < self.RATE_WINDOW]
        return len(self.send_history) < self.RATE_LIMIT

    def _send_webhook(self, payload):
        """Send a webhook to Discord."""
        if not self.enabled or not self.webhook_url:
            return False

        if not self._rate_check():
            return False

        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "AD-SMTA/1.0"},
                method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                # Discord returns 204 on success, urllib raises on non-200
                if e.code == 204:
                    pass  # Success
                else:
                    raise
            self.total_sent += 1
            self.send_history.append(time.time())
            return True
        except Exception as e:
            self.errors += 1
            self.last_error = str(e)
            return False

    def _color(self, r, g, b):
        return (r << 16) + (g << 8) + b

    def _ts(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ═══════════════════════════════════════════════════════
    # TRADE ALERTS
    # ═══════════════════════════════════════════════════════

    def trade_executed(self, market, contract, strategy, stake, direction=""):
        """Report a trade execution."""
        payload = {
            "embeds": [{
                "title": "📊 Trade Executed",
                "description": f"**{market}** | {contract} | {strategy}",
                "color": self._color(0, 229, 255),
                "fields": [
                    {"name": "Market", "value": market, "inline": True},
                    {"name": "Contract", "value": contract, "inline": True},
                    {"name": "Stake", "value": f"${stake:.2f}", "inline": True},
                    {"name": "Direction", "value": direction or "—", "inline": True},
                    {"name": "Strategy", "value": strategy, "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def trade_result(self, market, contract, stake, profit, balance, win_rate):
        """Report a trade result (win/loss)."""
        is_win = profit > 0
        color = self._color(34, 197, 94) if is_win else self._color(239, 68, 68)
        emoji = "✅ WIN" if is_win else "❌ LOSS"

        payload = {
            "embeds": [{
                "title": f"{emoji}: ${profit:+.2f}",
                "description": f"**{market}** | {contract}",
                "color": color,
                "fields": [
                    {"name": "P&L", "value": f"${profit:+.2f}", "inline": True},
                    {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                    {"name": "Win Rate", "value": f"{win_rate:.1f}%", "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def session_summary(self, balance, trades, wins, losses, pnl, win_rate, duration):
        """Report session summary."""
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        payload = {
            "embeds": [{
                "title": f"{pnl_emoji} Session Summary",
                "color": self._color(0, 229, 255) if pnl >= 0 else self._color(239, 68, 68),
                "fields": [
                    {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                    {"name": "Trades", "value": str(trades), "inline": True},
                    {"name": "Win Rate", "value": f"{win_rate:.1f}%", "inline": True},
                    {"name": "Wins", "value": str(wins), "inline": True},
                    {"name": "Losses", "value": str(losses), "inline": True},
                    {"name": "P&L", "value": f"${pnl:+.2f}", "inline": True},
                    {"name": "Duration", "value": duration, "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    # ═══════════════════════════════════════════════════════
    # SYSTEM ALERTS
    # ═══════════════════════════════════════════════════════

    def strategy_change(self, old_strategy, new_strategy, reason):
        """Report strategy rotation."""
        payload = {
            "embeds": [{
                "title": "🔄 Strategy Change",
                "color": self._color(245, 158, 11),
                "fields": [
                    {"name": "From", "value": old_strategy, "inline": True},
                    {"name": "To", "value": new_strategy, "inline": True},
                    {"name": "Reason", "value": reason, "inline": False},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def market_rotation(self, old_market, new_market, reason):
        """Report market rotation."""
        payload = {
            "embeds": [{
                "title": "🌍 Market Rotation",
                "color": self._color(168, 85, 247),
                "fields": [
                    {"name": "From", "value": old_market, "inline": True},
                    {"name": "To", "value": new_market, "inline": True},
                    {"name": "Reason", "value": reason, "inline": False},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def risk_warning(self, warning_type, message, severity="MEDIUM"):
        """Report a risk warning."""
        colors = {
            "LOW": self._color(34, 197, 94),
            "MEDIUM": self._color(245, 158, 11),
            "HIGH": self._color(239, 68, 68),
            "CRITICAL": self._color(220, 38, 38),
        }

        payload = {
            "embeds": [{
                "title": f"⚠️ Risk Warning: {warning_type}",
                "description": message,
                "color": colors.get(severity, self._color(245, 158, 11)),
                "fields": [
                    {"name": "Severity", "value": severity, "inline": True},
                    {"name": "Type", "value": warning_type, "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def system_status(self, status_data):
        """Report system status snapshot."""
        payload = {
            "embeds": [{
                "title": "📊 System Status",
                "color": self._color(59, 130, 246),
                "fields": [
                    {"name": "Balance", "value": f"${status_data.get('balance', 0):.2f}", "inline": True},
                    {"name": "Market", "value": status_data.get('market', '—'), "inline": True},
                    {"name": "Strategy", "value": status_data.get('strategy', '—'), "inline": True},
                    {"name": "ALM Brain", "value": status_data.get('alm_status', '—'), "inline": True},
                    {"name": "Phone", "value": f"BAT:{status_data.get('battery',0)}% TMP:{status_data.get('temp',0)}°C", "inline": True},
                    {"name": "Phase", "value": status_data.get('phase', '—'), "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def error_alert(self, error_type, message):
        """Report an error."""
        payload = {
            "embeds": [{
                "title": f"🔴 Error: {error_type}",
                "description": message[:2000],
                "color": self._color(220, 38, 38),
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System"}
            }]
        }
        return self._send_webhook(payload)

    def daily_report(self, date, balance, total_trades, wins, losses, pnl, best_trade, worst_trade):
        """Send daily summary report."""
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        payload = {
            "embeds": [{
                "title": f"{pnl_emoji} Daily Report — {date}",
                "color": self._color(0, 229, 255) if pnl >= 0 else self._color(239, 68, 68),
                "fields": [
                    {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
                    {"name": "Total Trades", "value": str(total_trades), "inline": True},
                    {"name": "Win Rate", "value": f"{wins/max(1,total_trades)*100:.1f}%", "inline": True},
                    {"name": "Wins / Losses", "value": f"{wins} / {losses}", "inline": True},
                    {"name": "Daily P&L", "value": f"${pnl:+.2f}", "inline": True},
                    {"name": "Best Trade", "value": f"${best_trade:+.2f}", "inline": True},
                    {"name": "Worst Trade", "value": f"${worst_trade:+.2f}", "inline": True},
                ],
                "timestamp": self._ts(),
                "footer": {"text": "AD-SMTA • ALM System • Daily Report"}
            }]
        }
        return self._send_webhook(payload)

    def get_status(self):
        return {
            "enabled": self.enabled,
            "webhook_configured": bool(self.webhook_url),
            "total_sent": self.total_sent,
            "errors": self.errors,
            "last_error": self.last_error,
        }
