"""
PROTECTOR — Hard Account Protection Layer
Circuit breakers, balance floors, trade caps, emergency stops.
This is the LAST gate before any trade executes.
"""
import time
from datetime import datetime, timezone


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
        self.hourly_trade_cap = cfg.get("hourly_trade_cap", 20)           # max 20 trades/hour
        self.max_hourly_loss = cfg.get("max_hourly_loss", 0.01)           # 1% hourly loss limit
        self.cooldown_after_losses = cfg.get("cooldown_after_losses", 3)  # 3 losses → pause
        self.cooldown_seconds = cfg.get("cooldown_seconds", 120)          # 2 minute pause
        self.max_rejection_rate = cfg.get("max_rejection_rate", 0.015)    # 1.5% rejection rate
        self.rejection_window = cfg.get("rejection_window", 200)          # over 200 ticks
        self.profit_lock_pct = cfg.get("profit_lock_pct", 0.03)          # lock at +3%
        self.max_concurrent = cfg.get("max_concurrent_trades", 1)
        self._data_quality = 1.0          # current data quality (0-1)
        self._session_start = 0           # session start time
        self._max_session_seconds = cfg.get("max_session_seconds", 14400)  # 4 hours
        self._daily_profit_target = cfg.get("daily_profit_target", 0.05)  # 5%

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
