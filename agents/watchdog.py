"""
SYSTEM WATCHDOG — Prevents what breaks the system

7 protections:
1. Deriv reconnection watchdog — detects stuck connections
2. COMPRESSION stall detector — forces action when all markets stall
3. Bunch WR circuit breaker — stops bunching when WR drops
4. Per-market daily loss limit — kills bleeding markets
5. Brain health monitor — detects brain death, triggers restart
6. Model fallback chain — rotates AI models on failure
7. Dashboard stale data detector — forces refresh on stale state
"""
import time
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime

BASEDIR = Path(__file__).parent.parent

class SystemWatchdog:
    """Monitors system health and prevents cascading failures."""

    def __init__(self):
        self.state = self._load()
        self._last_tick_time = time.time()
        self._last_trade_time = time.time()
        self._last_bunch_time = time.time()
        self._deriv_disconnect_count = 0
        self._deriv_last_ok = time.time()
        self._market_daily_pnl = {}  # market -> daily PnL
        self._bunch_win_count = 0
        self._bunch_loss_count = 0
        self._bunch_window_start = time.time()
        self._brain_pid = os.getpid()
        self._alerts = []

    def _load(self):
        f = BASEDIR / 'watchdog_state.json'
        if f.exists():
            try:
                return json.loads(f.read_text())
            except: pass
        return {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'market_pnl': {},
            'alerts': [],
            'restarts': 0,
            'deriv_disconnects': 0,
            'compression_stalls': 0,
            'bunch_circuit_breaks': 0,
        }

    def _save(self):
        try:
            (BASEDIR / 'watchdog_state.json').write_text(
                json.dumps(self.state, indent=2, default=str))
        except: pass

    # ═══ PROTECTION 1: Deriv Reconnection Watchdog ═══
    def check_deriv_health(self, connected, tick_age, last_tick_time):
        """Detect stuck Deriv connections. Returns (healthy, action, reason)."""
        now = time.time()
        
        if not connected:
            self._deriv_disconnect_count += 1
            self.state['deriv_disconnects'] = self.state.get('deriv_disconnects', 0) + 1
            if self._deriv_disconnect_count >= 3:
                return False, 'RESTART', f'Deriv disconnected {self._deriv_disconnect_count}x — restart needed'
            return False, 'WAIT', f'Deriv disconnected (#{self._deriv_disconnect_count})'
        
        # Connected but no ticks flowing
        time_since_tick = now - last_tick_time if last_tick_time else 999
        if time_since_tick > 60 and connected:
            return False, 'RECONNECT', f'No ticks for {time_since_tick:.0f}s despite connected'
        
        # Connection healthy
        if tick_age == 0:
            self._deriv_disconnect_count = 0
            self._deriv_last_ok = now
        
        return True, 'OK', 'Deriv healthy'

    # ═══ PROTECTION 2: COMPRESSION Stall Detector ═══
    def check_compression_stall(self, market_states, cycle):
        """Detect when ALL markets are stuck in COMPRESSION/WAIT."""
        if not market_states:
            return False, 'NO_DATA'
        
        compressed = sum(1 for s in market_states.values() 
                        if s in ('COMPRESSION', 'WAIT', 'CALM'))
        total = len(market_states)
        
        if compressed == total and total >= 4:
            # All markets compressed — check how long
            stall_key = 'compression_stall_start'
            if stall_key not in self.state:
                self.state[stall_key] = time.time()
                self._save()
                return False, 'STARTING'
            
            stall_duration = time.time() - self.state[stall_key]
            if stall_duration > 1800:  # 30 minutes stalled
                self.state['compression_stalls'] = self.state.get('compression_stalls', 0) + 1
                self._save()
                return True, 'STALLED'
            elif stall_duration > 600:  # 10 minutes
                return False, 'WATCHING'
        
        # Markets are moving — clear stall
        if stall_key in self.state:
            del self.state[stall_key]
            self._save()
        
        return False, 'MOVING'

    # ═══ PROTECTION 3: Bunch WR Circuit Breaker ═══
    def record_bunch_result(self, pnl, wins, losses):
        """Track bunch performance. Returns (circuit_open, reason)."""
        self._bunch_win_count += wins
        self._bunch_loss_count += losses
        
        total = self._bunch_win_count + self._bunch_loss_count
        
        # Reset window every hour
        if time.time() - self._bunch_window_start > 3600:
            self._bunch_win_count = wins
            self._bunch_loss_count = losses
            self._bunch_window_start = time.time()
            return False, 'WINDOW_RESET'
        
        if total < 5:
            return False, f'Collecting data ({total}/5)'
        
        wr = self._bunch_win_count / total
        
        # Circuit breaker thresholds
        if wr < 0.50 and total >= 8:
            self.state['bunch_circuit_breaks'] = self.state.get('bunch_circuit_breaks', 0) + 1
            self._save()
            return True, f'CIRCUIT OPEN: WR={wr:.0%} ({self._bunch_win_count}W/{self._bunch_loss_count}L)'
        elif wr < 0.60 and total >= 10:
            return True, f'CAUTION: WR={wr:.0%} — pausing bunches'
        
        return False, f'OK: WR={wr:.0%}'

    # ═══ PROTECTION 4: Per-Market Daily Loss Limit ═══
    def record_trade(self, market, profit):
        """Track per-market PnL. Returns (killed, reason)."""
        today = datetime.now().strftime('%Y-%m-%d')
        if self.state.get('date') != today:
            self.state['date'] = today
            self.state['market_pnl'] = {}
        
        mp = self.state.setdefault('market_pnl', {})
        mp[market] = mp.get(market, 0) + profit
        
        # Per-market daily loss limit: -$5
        if mp[market] < -5.0:
            return True, f'{market} daily limit: ${mp[market]:+.2f} (max -$5)'
        
        # Total daily loss limit: -$15
        total_daily = sum(mp.values())
        if total_daily < -15.0:
            return True, f'Total daily limit: ${total_daily:+.2f} (max -$15)'
        
        return False, f'{market}: ${mp[market]:+.2f} today'

    # ═══ PROTECTION 5: Brain Health Monitor ═══
    def check_brain_health(self, cycle, last_trade_time, trades_today):
        """Detect brain stuck/dead. Returns (healthy, action)."""
        now = time.time()
        
        # Brain hasn't traded in 30 minutes
        time_since_trade = now - last_trade_time
        if time_since_trade > 1800 and trades_today > 5:
            return False, 'STUCK'
        
        # Brain cycle not advancing (stuck in wait loop)
        # This is detected by comparing cycle count over time
        
        return True, 'OK'

    # ═══ PROTECTION 6: Model Fallback Chain ═══
    def check_model_health(self, model_status):
        """Check if current AI model is healthy. Returns (healthy, fallback)."""
        if not model_status:
            return False, 'openrouter'
        
        score = model_status.get('score', 0)
        consec_fails = model_status.get('consec_fails', 0)
        trade_wr = model_status.get('trade_wr', 0)
        
        if consec_fails >= 3:
            return False, 'rotate'
        
        if score < 30 and trade_wr < 40:
            return False, 'rotate'
        
        return True, 'keep'

    # ═══ PROTECTION 7: Dashboard Stale Data ═══
    def check_dashboard_state(self):
        """Check if dashboard state is stale. Returns (fresh, age_seconds)."""
        state_file = BASEDIR / 'trading_state.json'
        if not state_file.exists():
            return False, 999
        
        age = time.time() - os.path.getmtime(str(state_file))
        return age < 30, age

    # ═══ MASTER CHECK ═══
    def run_all_checks(self, cycle, brain_data):
        """Run all protections. Returns list of (severity, action, reason)."""
        alerts = []
        
        # 1. Deriv health
        if brain_data.get('deriv_connected') is not None:
            healthy, action, reason = self.check_deriv_health(
                brain_data['deriv_connected'],
                brain_data.get('tick_age', 0),
                brain_data.get('last_tick_time', time.time())
            )
            if not healthy:
                severity = 'CRITICAL' if action in ('RESTART', 'RECONNECT') else 'WARNING'
                alerts.append((severity, action, f'DERIV: {reason}'))
        
        # 2. Compression stall
        if brain_data.get('market_states'):
            stalled, reason = self.check_compression_stall(
                brain_data['market_states'], cycle)
            if stalled:
                alerts.append(('WARNING', 'DIVERSIFY', f'ALL MARKETS STALLED: {reason}'))
        
        # 3. Bunch circuit breaker
        if brain_data.get('bunch_result'):
            br = brain_data['bunch_result']
            circuit_open, reason = self.record_bunch_result(
                br.get('pnl', 0), br.get('wins', 0), br.get('losses', 0))
            if circuit_open:
                alerts.append(('CRITICAL', 'STOP_BUNCHES', f'BUNCH: {reason}'))
        
        # 4. Per-market loss limit
        if brain_data.get('last_trade'):
            lt = brain_data['last_trade']
            killed, reason = self.record_trade(
                lt.get('market', '?'), lt.get('profit', 0))
            if killed:
                alerts.append(('CRITICAL', 'KILL_MARKET', f'MARKET LIMIT: {reason}'))
        
        # 5. Brain health
        healthy, action = self.check_brain_health(
            cycle,
            brain_data.get('last_trade_time', time.time()),
            brain_data.get('trades_today', 0)
        )
        if not healthy:
            alerts.append(('WARNING', action, f'BRAIN: {action}'))
        
        # 6. Model health
        if brain_data.get('model_status'):
            healthy, action = self.check_model_health(brain_data['model_status'])
            if not healthy:
                alerts.append(('INFO', 'ROTATE_MODEL', f'MODEL: rotating to {action}'))
        
        # 7. Dashboard freshness
        fresh, age = self.check_dashboard_state()
        if not fresh:
            alerts.append(('INFO', 'REFRESH_DASHBOARD', f'Dashboard state stale: {age:.0f}s'))
        
        self._alerts = alerts
        self._save()
        return alerts

    def get_status(self):
        return {
            'market_pnl': self.state.get('market_pnl', {}),
            'alerts': self._alerts,
            'deriv_disconnects': self.state.get('deriv_disconnects', 0),
            'compression_stalls': self.state.get('compression_stalls', 0),
            'bunch_circuit_breaks': self.state.get('bunch_circuit_breaks', 0),
            'bunch_wr': round(self._bunch_win_count / max(self._bunch_win_count + self._bunch_loss_count, 1) * 100, 1),
        }
