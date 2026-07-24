"""
MISSION TRACKER — Makes the brain aware of daily goals and trade requirements.

Tracks:
- Daily PnL vs $20 target
- How many trades needed to reach target
- When to use bunches (high confidence) vs singles (low confidence)
- Session allocation (how many bunches per session)
"""
import time
import json
from pathlib import Path
from datetime import datetime, timezone

MISSION_FILE = Path(__file__).parent.parent / 'mission_state.json'

DAILY_TARGET = 20.0  # $20 daily profit target
AVG_TRADE_PROFIT = 0.85  # average profit per winning trade
WIN_RATE = 0.55  # expected win rate
BUCH_SIZE_AVG = 5  # average bunch size


class MissionTracker:
    """Tracks daily mission progress and decides bunch vs single strategy."""
    
    def __init__(self):
        self.state = self._load()
        self._ensure_today()
    
    def _load(self):
        try:
            if MISSION_FILE.exists():
                return json.loads(MISSION_FILE.read_text())
        except: pass
        return self._default()
    
    def _default(self):
        return {
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'daily_target': DAILY_TARGET,
            'start_balance': 0,
            'current_pnl': 0,
            'trades_today': 0,
            'bunch_trades': 0,
            'single_trades': 0,
            'bunch_runs': 0,
            'bunch_wins': 0,
            'sessions_completed': 0,
            'mode': 'ACCUMULATE',  # ACCUMULATE, TARGET_CLOSE, HOUSE_MONEY, COOLDOWN
            'trades_needed': 0,
            'bunches_needed': 0,
            'estimated_completion': '',
        }
    
    def _ensure_today(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if self.state.get('date') != today:
            self.state = self._default()
            self._save()
    
    def _save(self):
        try:
            # Preserve self_test data from Mission class (same file, different writer)
            if MISSION_FILE.exists():
                try:
                    _existing = json.loads(MISSION_FILE.read_text())
                    if 'self_test' in _existing and 'self_test' not in self.state:
                        self.state['self_test'] = _existing['self_test']
                except: pass
            MISSION_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except: pass
    
    def update(self, pnl, balance, trades_today, bunch_runs=0, bunch_wins=0, bunch_trades=0, single_trades=0):
        """Update mission state with current data."""
        self.state['current_pnl'] = round(pnl, 2)
        self.state['start_balance'] = balance - pnl
        self.state['trades_today'] = trades_today
        self.state['bunch_runs'] = bunch_runs
        self.state['bunch_wins'] = bunch_wins
        self.state['bunch_trades'] = bunch_trades
        self.state['single_trades'] = single_trades
        
        # Calculate what's needed
        remaining = DAILY_TARGET - pnl
        if remaining <= 0:
            self.state['mode'] = 'HOUSE_MONEY'
            self.state['trades_needed'] = 0
            self.state['bunches_needed'] = 0
        else:
            # How many winning trades needed
            wins_needed = remaining / AVG_TRADE_PROFIT
            trades_needed = wins_needed / WIN_RATE
            bunches_needed = trades_needed / BUCH_SIZE_AVG
            
            self.state['trades_needed'] = max(0, round(trades_needed))
            self.state['bunches_needed'] = max(0, round(bunches_needed))
            
            # Mode selection
            if pnl >= DAILY_TARGET * 0.8:
                self.state['mode'] = 'TARGET_CLOSE'
            elif pnl >= DAILY_TARGET:
                self.state['mode'] = 'HOUSE_MONEY'
            elif pnl < -5:
                self.state['mode'] = 'COOLDOWN'
            else:
                self.state['mode'] = 'ACCUMULATE'
        
        self._save()
    
    def should_use_bunch(self, setup_score, consec_loss):
        """Decide whether to use bunch or single trade.
        Returns (use_bunch, reason).
        """
        mode = self.state.get('mode', 'ACCUMULATE')
        pnl = self.state.get('current_pnl', 0)
        remaining = DAILY_TARGET - pnl
        
        # House money: already hit target, trade singles to protect
        if mode == 'HOUSE_MONEY':
            return False, 'house_money_singles'
        
        # Cooldown: big loss, singles only
        if mode == 'COOLDOWN' or consec_loss >= 3:
            return False, 'cooldown_singles'
        
        # Target close: within 80% of target, use singles to finish
        if mode == 'TARGET_CLOSE' and remaining < DAILY_TARGET * 0.2:
            return False, 'target_close_singles'
        
        # High confidence setup: always bunch
        if setup_score >= 55:
            return True, f'high_score_{setup_score}'
        
        # Medium confidence: bunch if still need profit
        if setup_score >= 35 and remaining > 5:
            return True, f'medium_score_{setup_score}_need_${remaining:.0f}'
        
        # Low confidence or close to target: singles
        return False, f'low_score_{setup_score}'
    
    def get_session_allocation(self):
        """How many bunches to run in current session.
        Returns (max_bunches, max_singles, stake_multiplier).
        """
        mode = self.state.get('mode', 'ACCUMULATE')
        pnl = self.state.get('current_pnl', 0)
        remaining = DAILY_TARGET - pnl
        
        if mode == 'HOUSE_MONEY':
            return 2, 5, 0.75  # protect profits
        elif mode == 'TARGET_CLOSE':
            return 1, 3, 0.8  # finish carefully
        elif mode == 'COOLDOWN':
            return 0, 3, 0.5  # minimal
        else:  # ACCUMULATE
            bunches_needed = self.state.get('bunches_needed', 5)
            if remaining > 10:
                return min(bunches_needed, 8), 2, 1.0  # aggressive
            else:
                return min(bunches_needed, 4), 3, 1.0  # moderate
    
    def get_status(self):
        pnl = self.state.get('current_pnl', 0)
        remaining = DAILY_TARGET - pnl
        progress = min(100, (pnl / DAILY_TARGET) * 100) if DAILY_TARGET > 0 else 0
        
        bunch_runs = self.state.get('bunch_runs', 0)
        bunch_wins = self.state.get('bunch_wins', 0)
        bunch_wr = (bunch_wins / bunch_runs * 100) if bunch_runs > 0 else 0
        
        return {
            'daily_target': DAILY_TARGET,
            'current_pnl': round(pnl, 2),
            'remaining': round(remaining, 2),
            'progress_pct': round(progress, 1),
            'mode': self.state.get('mode', 'ACCUMULATE'),
            'trades_today': self.state.get('trades_today', 0),
            'trades_needed': self.state.get('trades_needed', 0),
            'bunches_needed': self.state.get('bunches_needed', 0),
            'bunch_runs': bunch_runs,
            'bunch_wr': round(bunch_wr, 1),
            'single_trades': self.state.get('single_trades', 0),
        }
