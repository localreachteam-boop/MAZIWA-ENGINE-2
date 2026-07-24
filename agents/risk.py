"""
RISK — Protection & P&L Management (merged)
Combines: Protector + PLManager
"""
from datetime import datetime, timezone
import time
from datetime import timezone
from datetime import datetime
from collections import deque
class Protector:
    """
    Unalterable safety boundaries.
    No trade passes unless ALL checks clear.
    """

    def __init__(self, config=None):
        cfg = config or {}
        # Hard limits (cannot be overridden)
        self.daily_loss_limit = cfg.get("daily_loss_limit_pct", 0.02)     # 2%
        self.balance_floor_pct = cfg.get("balance_floor_pct", 0.80)       # never below 80% of start
        self.hourly_trade_cap = cfg.get("hourly_trade_cap", 50)           # max 50 trades/hour
        self.max_hourly_loss = cfg.get("max_hourly_loss", 0.01)           # 1% hourly loss limit
        self.cooldown_after_losses = cfg.get("cooldown_after_losses", 3)  # 3 losses → pause
        self.cooldown_seconds = cfg.get("cooldown_seconds", 60)           # 1 minute pause (exploration-friendly)
        self.max_rejection_rate = cfg.get("max_rejection_rate", 0.015)    # 1.5% rejection rate
        self.rejection_window = cfg.get("rejection_window", 200)          # over 200 ticks
        self.profit_lock_pct = cfg.get("profit_lock_pct", 0.03)          # lock at +3%
        self.max_concurrent = cfg.get("max_concurrent_trades", 1)
        self._data_quality = 1.0          # current data quality (0-1)
        self._session_start = 0           # session start time
        self._max_session_seconds = cfg.get("max_session_seconds", 14400)  # 4 hours
        self._daily_profit_target = cfg.get("daily_profit_target", 0.002)  # 0.2% = $20 on $10k

        # State
        self.start_balance = 0
        self.current_balance = 0
        self.peak_balance = 0
        self.hour_start = time.time()
        self.hour_trades = 0
        this_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        self.hour_start = this_hour.timestamp()
        self.hour_pnl = 0
        self.daily_pnl = 0
        self.daily_start_balance = 0

        self.consecutive_losses = 0
        self.cooldown_until = 0
        self.total_trades = 0
        self.total_rejections = 0
        self.recent_rejections = []   # (time, success)
        self.open_contracts = 0
        self.is_frozen = False
        self.freeze_reason = ""
        self.session_active = True

    def init(self, balance):
        """Initialize with starting balance."""
        self.start_balance = balance
        self.current_balance = balance
        self.peak_balance = balance
        self.daily_start_balance = balance
        self.hour_start = time.time()

    def update_balance(self, balance):
        """Track balance changes."""
        self.current_balance = balance
        if balance > self.peak_balance:
            self.peak_balance = balance

    def record_trade_attempt(self, success):
        """Record whether a trade API call succeeded or was rejected."""
        now = time.time()
        self.recent_rejections.append((now, not success))
        if not success:
            self.total_rejections += 1
        # Keep window
        cutoff = now - 60  # 60 second window
        self.recent_rejections = [(t, r) for t, r in self.recent_rejections if t > cutoff]

    def record_trade_result(self, profit):
        """Record completed trade result."""
        self.total_trades += 1
        self.hour_trades += 1
        self.daily_pnl += profit
        self.hour_pnl += profit

        if profit < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def set_open_contracts(self, count):
        self.open_contracts = count

    def check(self):
        """
        Master safety check. Returns (allowed, reason).
        If NOT allowed, the trade is BLOCKED no matter what.
        """
        now = time.time()
        bal = self.current_balance
        start = self.start_balance

        # ── 1. GLOBAL FREEZE ────────────────────────────
        if self.is_frozen:
            return False, f"FROZEN: {self.freeze_reason}"

        # ── 2. SESSION STOPPED ──────────────────────────
        if not self.session_active:
            return False, "SESSION_STOPPED"

        # ── 3. DAILY LOSS LIMIT ─────────────────────────
        daily_loss = self.daily_start_balance - bal
        daily_loss_pct = daily_loss / self.daily_start_balance if self.daily_start_balance > 0 else 0
        if daily_loss_pct >= self.daily_loss_limit:
            self.is_frozen = True
            self.freeze_reason = f"DAILY_LOSS_LIMIT: {daily_loss_pct*100:.2f}%"
            return False, self.freeze_reason

        # ── 4. BALANCE FLOOR ────────────────────────────
        floor = start * self.balance_floor_pct
        if bal < floor:
            self.is_frozen = True
            self.freeze_reason = f"BALANCE_FLOOR: ${bal:.2f} < ${floor:.2f}"
            return False, self.freeze_reason

        # ── 5. HOURLY TRADE CAP ─────────────────────────
        # Reset hourly counter
        hour_elapsed = now - self.hour_start
        if hour_elapsed > 3600:
            self.hour_start = now
            self.hour_trades = 0
            self.hour_pnl = 0

        if self.hour_trades >= self.hourly_trade_cap:
            return False, f"HOURLY_CAP: {self.hour_trades}/{self.hourly_trade_cap}"

        # ── 6. HOURLY LOSS LIMIT ────────────────────────
        if self.hour_pnl < 0:
            hour_loss_pct = abs(self.hour_pnl) / start if start > 0 else 0
            if hour_loss_pct >= self.max_hourly_loss:
                return False, f"HOURLY_LOSS: {hour_loss_pct*100:.2f}%"

        # ── 7. COOLDOWN AFTER LOSSES ────────────────────
        if self.consecutive_losses >= self.cooldown_after_losses:
            self.cooldown_until = now + self.cooldown_seconds
            self.consecutive_losses = 0  # reset after setting cooldown

        if now < self.cooldown_until:
            remaining = int(self.cooldown_until - now)
            return False, f"COOLDOWN: {remaining}s left"

        # ── 8. MAX CONCURRENT TRADES ────────────────────
        if self.open_contracts >= self.max_concurrent:
            return False, f"MAX_OPEN: {self.open_contracts}/{self.max_concurrent}"

        # ── 9. REJECTION RATE MONITOR ───────────────────
        if len(self.recent_rejections) >= 50:
            rejections = sum(1 for _, r in self.recent_rejections if r)
            rate = rejections / len(self.recent_rejections)
            if rate > self.max_rejection_rate:
                self.is_frozen = True
                self.freeze_reason = f"HIGH_REJECTION_RATE: {rate*100:.1f}%"
                return False, self.freeze_reason

        # ── 10. PROFIT LOCK ─────────────────────────────
        if start > 0:
            profit_pct = (bal - start) / start
            if profit_pct >= self.profit_lock_pct:
                return False, f"PROFIT_LOCKED: +{profit_pct*100:.2f}%"

        # ── 11. UNCERTAINTY-BASED EXPOSURE REDUCTION ────
        # If data quality is low or regime unknown, reduce max concurrent
        if hasattr(self, '_data_quality') and self._data_quality < 0.6:
            if self.open_contracts >= 1:
                return False, f"UNCERTAINTY_EXPOSURE: quality={self._data_quality:.2f}"

        # ── 12. SESSION DURATION LIMIT ──────────────────
        if hasattr(self, '_session_start') and self._session_start > 0:
            session_elapsed = time.time() - self._session_start
            max_session = getattr(self, '_max_session_seconds', 14400)  # 4 hours
            if session_elapsed > max_session:
                return False, f"SESSION_DURATION: {session_elapsed/3600:.1f}h > {max_session/3600:.1f}h"

        # ── 13. DAILY PROFIT TARGET ────────────────────
        if start > 0:
            daily_profit_pct = (bal - self.daily_start_balance) / self.daily_start_balance if self.daily_start_balance > 0 else 0
            profit_target = getattr(self, '_daily_profit_target', 0.05)  # 5%
            if daily_profit_pct >= profit_target:
                return False, f"PROFIT_TARGET_HIT: +{daily_profit_pct*100:.2f}%"

        # ── ALL CLEAR ───────────────────────────────────
        return True, "CLEAR"

    def emergency_stop(self, reason):
        """Manual emergency stop."""
        self.is_frozen = True
        self.freeze_reason = f"EMERGENCY: {reason}"
        self.session_active = False

    def resume(self):
        """Resume after manual intervention."""
        self.is_frozen = False
        self.freeze_reason = ""
        self.consecutive_losses = 0
        self.cooldown_until = 0
        self.session_active = True

    def get_status(self):
        daily_loss = self.daily_start_balance - self.current_balance
        daily_loss_pct = daily_loss / self.daily_start_balance if self.daily_start_balance > 0 else 0
        return {
            "frozen": self.is_frozen,
            "freeze_reason": self.freeze_reason,
            "session_active": self.session_active,
            "daily_loss_pct": round(daily_loss_pct * 100, 2),
            "daily_loss_limit_pct": round(self.daily_loss_limit * 100, 2),
            "balance_floor": round(self.start_balance * self.balance_floor_pct, 2),
            "hour_trades": self.hour_trades,
            "hourly_cap": self.hourly_trade_cap,
            "hour_pnl": round(self.hour_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
            "open_contracts": self.open_contracts,
            "total_trades": self.total_trades,
            "total_rejections": self.total_rejections,
            "peak_balance": round(self.peak_balance, 2),
            "drawdown_from_peak": round((self.peak_balance - self.current_balance) / self.peak_balance * 100, 2) if self.peak_balance > 0 else 0,
            "data_quality": round(self._data_quality, 2),
            "session_elapsed_hours": round((time.time() - self._session_start) / 3600, 2) if self._session_start > 0 else 0,
            "daily_profit_target_pct": round(self._daily_profit_target * 100, 2),
        }


class PLManager:
    """
    Professional P&L management. Prevents giving back profits.
    """

    def __init__(self, config=None):
        cfg = config or {}
        
        # ── Trailing Stop ──
        self.trailing_stop_pct = cfg.get('trailing_stop_pct', 0.015)   # 1.5% trailing — give room
        self.trailing_active = False
        self.trailing_high_water = 0
        
        # ── Daily Target ──
        self.daily_target = cfg.get('daily_target', 20.0)             # $20 daily profit target
        self.daily_target_hit = False
        self.daily_start_balance = 0
        
        # ── Win Rate Decay ──
        self.wr_window = cfg.get('wr_window', 20)                     # last 20 trades
        self.wr_min = cfg.get('wr_min', 0.40)                         # minimum 40% WR
        self.recent_results = deque(maxlen=self.wr_window)
        
        # ── Hourly Tracking ──
        self.hourly_pnl = {}                                           # hour -> pnl
        self.current_hour = -1
        self.max_hourly_loss = cfg.get('max_hourly_loss', -15.0)      # -$15 max per hour
        
        # ── Anti-Revenge ──
        self.last_loss_size = 0
        self.revenge_cooldown_until = 0
        self.big_loss_threshold = cfg.get('big_loss_threshold', 2.0)   # $2+ loss = big
        self.revenge_cooldown_sec = cfg.get('revenge_cooldown_sec', 300)  # 5 min
        
        # ── Equity Curve Guard ──
        self.equity_curve = deque(maxlen=50)                           # last 50 balances
        self.ma_period = cfg.get('ma_period', 20)                      # 20-trade MA
        self.below_ma_reduce = cfg.get('below_ma_reduce', 0.5)        # reduce 50% when below MA
        
        # ── Profit Lock ──
        self.profit_lock_pct = cfg.get('profit_lock_pct', 0.003)      # lock 0.3% of profits
        self.profit_lock_floor = 0
        
        # ── State ──
        self.session_start = time.time()
        self.last_trade_time = 0
        self.consecutive_losses = 0
        self.consecutive_wins = 0

    def set_market_context(self, session_quality=50, regime="UNKNOWN", trend_strength=0):
        self.session_quality = session_quality
        self.regime = regime
        self.trend_strength = trend_strength

    def on_trade(self, profit, balance, start_balance):
        """Called after every trade. Returns (action, reason, stake_multiplier)."""
        now = time.time()
        self.last_trade_time = now
        self.recent_results.append(profit)
        self.equity_curve.append(balance)
        
        # Track hourly
        hour = int(now / 3600)
        if hour != self.current_hour:
            self.current_hour = hour
            self.hourly_pnl[hour] = 0
        self.hourly_pnl[hour] = self.hourly_pnl.get(hour, 0) + profit
        
        # Update streaks
        if profit > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self.last_loss_size = abs(profit)
        
        multiplier = 1.0
        action = 'CONTINUE'
        reason = ''
        daily_pnl = balance - start_balance
        
        # ── SMART TRAILING STOP: adapt to market quality ──
        sq = getattr(self, 'session_quality', 50)
        if sq >= 80:
            smart_trail, smart_lock = 0.008, 0.005
        elif sq >= 60:
            smart_trail, smart_lock = 0.012, 0.003
        elif sq >= 40:
            smart_trail, smart_lock = 0.018, 0.002
        else:
            smart_trail, smart_lock = 0.025, 0.001
        
        # ── Check 1: Daily Target ──
        if daily_pnl >= self.daily_target:
            self.daily_target_hit = True
            return 'STOP', f'DAILY_TARGET: +${daily_pnl:.2f} >= ${self.daily_target}', 0
        
        # ── Check 2: SMART Trailing Stop ──
        if balance > self.trailing_high_water:
            self.trailing_high_water = balance
            self.trailing_active = True
        if self.trailing_high_water > start_balance:
            trail_loss = (self.trailing_high_water - balance) / self.trailing_high_water
            if trail_loss >= smart_trail:
                return 'STOP', f'TRAILING_STOP: {trail_loss*100:.1f}% from peak (smart={smart_trail*100:.1f}%)', 0
        
        # ── Check 3: SMART Profit Lock ──
        # Reset floor if no profit lock hit in 30 minutes (allow recovery)
        if self.profit_lock_floor > 0 and hasattr(self, '_last_lock_time'):
            if now - self._last_lock_time > 1800:
                self.profit_lock_floor = 0
        if daily_pnl > 0:
            lock_floor = start_balance + daily_pnl * (1 - smart_lock * 10)
            self.profit_lock_floor = max(self.profit_lock_floor, lock_floor)
            if balance < self.profit_lock_floor and daily_pnl > 1.0:
                self._last_lock_time = now
                gap = self.profit_lock_floor - balance
                if gap > 2.0:
                    # Deep below floor — hard stop
                    return 'STOP', f'PROFIT_LOCK: below locked floor ${self.profit_lock_floor:.2f} (gap=${gap:.2f})', 0
                else:
                    # Slightly below floor — reduce stake, keep trading
                    multiplier *= 0.3
                    reason = f'PROFIT_LOCK_REDUCE: floor=${self.profit_lock_floor:.2f} gap=${gap:.2f}'
                    action = 'REDUCE'
        
        # ── Check 4: Win Rate Decay ──
        if len(self.recent_results) >= self.wr_window:
            recent_wins = sum(1 for r in self.recent_results if r > 0)
            recent_wr = recent_wins / len(self.recent_results)
            if recent_wr < self.wr_min:
                multiplier *= 0.5
                reason = f'WR_DECAY: {recent_wr*100:.0f}% < {self.wr_min*100:.0f}%'
                action = 'REDUCE'
        
        # ── Check 5: Hourly Loss Limit ──
        hourly = self.hourly_pnl.get(self.current_hour, 0)
        if hourly < self.max_hourly_loss:
            return 'STOP', f'HOURLY_LOSS: ${hourly:.2f} < ${self.max_hourly_loss}', 0
        
        # ── Check 6: Anti-Revenge ──
        if self.consecutive_losses >= 2 and self.last_loss_size >= self.big_loss_threshold:
            if now < self.revenge_cooldown_until:
                remaining = int(self.revenge_cooldown_until - now)
                return 'STOP', f'ANTI_REVENGE: {remaining}s left after ${self.last_loss_size:.2f} loss', 0
            elif self.revenge_cooldown_until == 0:
                self.revenge_cooldown_until = now + self.revenge_cooldown_sec
                multiplier *= 0.3
                reason = f'ANTI_REVENGE: big loss ${self.last_loss_size:.2f}, reducing 70%'
                action = 'REDUCE'
        
        # ── Check 7: Equity Curve Guard ──
        if len(self.equity_curve) >= self.ma_period:
            ma = sum(list(self.equity_curve)[-self.ma_period:]) / self.ma_period
            if balance < ma:
                multiplier *= self.below_ma_reduce
                reason = f'EQUITY_MA: ${balance:.2f} < MA ${ma:.2f}'
                action = 'REDUCE'
        
        # ── Check 8: Session Duration ──
        session_hours = (now - self.session_start) / 3600
        if session_hours > 6:
            multiplier *= 0.5
            reason = f'SESSION: {session_hours:.1f}h > 6h'
            action = 'REDUCE'
        
        # ── Check 9: Streak-based adjustments ──
        if self.consecutive_wins >= 5:
            multiplier *= 1.5   # hot streak — ride it
        elif self.consecutive_wins >= 3:
            multiplier *= 1.25
        elif self.consecutive_losses >= 4:
            multiplier *= 0.2   # cold streak — protect
        elif self.consecutive_losses >= 3:
            multiplier *= 0.4
        elif self.consecutive_losses >= 2:
            multiplier *= 0.6
        
        if not reason:
            reason = f'ok (W:{self.consecutive_wins} L:{self.consecutive_losses})'
        
        return action, reason, multiplier

    def reset_daily(self, balance):
        """Reset daily tracking (call at midnight or new session)."""
        self.daily_start_balance = balance
        self.daily_target_hit = False
        self.trailing_high_water = balance
        self.profit_lock_floor = 0
        self.hourly_pnl = {}
        self.recent_results.clear()
        self.equity_curve.clear()

    def get_status(self):
        sq = getattr(self, 'session_quality', 50)
        daily_pnl = 0
        if self.trailing_high_water > 0 and self.daily_start_balance > 0:
            daily_pnl = self.trailing_high_water - self.daily_start_balance
        trail_from_peak = 0
        if self.trailing_high_water > 0:
            trail_from_peak = (self.trailing_high_water - (self.equity_curve[-1] if self.equity_curve else self.trailing_high_water)) / self.trailing_high_water * 100
        return {
            'trailing_active': self.trailing_active,
            'trailing_high_water': round(self.trailing_high_water, 2),
            'trail_from_peak_pct': round(trail_from_peak, 2),
            'daily_target': self.daily_target,
            'daily_target_hit': self.daily_target_hit,
            'daily_pnl': round(daily_pnl, 2),
            'recent_wr': round(sum(1 for r in self.recent_results if r > 0) / max(1, len(self.recent_results)) * 100, 1),
            'recent_trades': len(self.recent_results),
            'hourly_pnl': {str(h): round(v, 2) for h, v in self.hourly_pnl.items()},
            'profit_lock_floor': round(self.profit_lock_floor, 2),
            'session_quality': sq,
            'smart_trail_pct': round(0.008 if sq >= 80 else (0.012 if sq >= 60 else (0.018 if sq >= 40 else 0.025)) * 100, 1),
            'anti_revenge_active': time.time() < self.revenge_cooldown_until,
            'anti_revenge_remaining': max(0, int(self.revenge_cooldown_until - time.time())),
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses,
            'session_hours': round((time.time() - self.session_start) / 3600, 1),
            'locked_profits': round(max(0, self.profit_lock_floor - self.daily_start_balance), 2) if self.daily_start_balance else 0,
        }
