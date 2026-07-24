"""
GOAL MANAGER — Dynamic Objective System (Stage 1: Goal)
Adapts trading objectives based on performance, drawdown, and session state.

Instead of static goals, the GoalManager shifts priorities:
  - PRESERVATION: protect capital when losing
  - RECOVERY: climb back from drawdown
  - OPTIMAL: normal operation
  - GROWTH: push when winning
"""
import time
from collections import defaultdict


class GoalManager:
    """
    Manages dynamic goals that adapt to current performance.
    Goal = (objective, target, constraints, priority)
    """

    MODE_PRESERVATION = "PRESERVATION"
    MODE_RECOVERY = "RECOVERY"
    MODE_OPTIMAL = "OPTIMAL"
    MODE_GROWTH = "GROWTH"
    MODE_EXPLORATION = "EXPLORATION"

    def __init__(self):
        self.current_mode = self.MODE_OPTIMAL
        self.start_balance = 0
        self.current_balance = 0
        self.peak_balance = 0
        self.session_pnl = 0
        self.total_trades = 0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.daily_loss = 0
        self.daily_trades = 0
        self.last_mode_change = 0
        self.mode_history = []
        self.goals = self._default_goals()
        self.active_goal = None
        self.last_evaluation = 0
        self.evaluation_interval = 30  # seconds

    def _default_goals(self):
        return {
            self.MODE_PRESERVATION: {
                "objective": "Protect capital",
                "max_stake_multiplier": 0.5,
                "max_trades_per_hour": 5,
                "min_confidence": 5,
                "allowed_contracts": ["DIGITDIFF"],
                "cooldown_seconds": 120,
                "max_daily_loss_pct": 0.005,
                "priority": 100,
            },
            self.MODE_RECOVERY: {
                "objective": "Recover from drawdown — diversify markets and strategies",
                "max_stake_multiplier": 0.7,
                "max_trades_per_hour": 8,
                "min_confidence": 1,
                "allowed_contracts": None,
                "cooldown_seconds": 45,
                "max_daily_loss_pct": 0.01,
                "priority": 80,
            },
            self.MODE_OPTIMAL: {
                "objective": "Steady profitable operation",
                "max_stake_multiplier": 1.0,
                "max_trades_per_hour": 15,
                "min_confidence": 1,
                "allowed_contracts": None,  # all
                "cooldown_seconds": 20,
                "max_daily_loss_pct": 0.02,
                "priority": 50,
            },
            self.MODE_GROWTH: {
                "objective": "Maximize gains during strong performance",
                "max_stake_multiplier": 1.5,
                "max_trades_per_hour": 25,
                "min_confidence": 1,
                "allowed_contracts": None,
                "cooldown_seconds": 10,
                "max_daily_loss_pct": 0.025,
                "priority": 30,
            },
            self.MODE_EXPLORATION: {
                "objective": "Gather data on new strategies/markets",
                "max_stake_multiplier": 0.3,
                "max_trades_per_hour": 10,
                "min_confidence": 0,
                "allowed_contracts": None,
                "cooldown_seconds": 30,
                "max_daily_loss_pct": 0.01,
                "priority": 60,
            },
        }

    def init(self, balance):
        self.start_balance = balance
        self.current_balance = balance
        self.peak_balance = balance

    def update(self, balance, pnl, consecutive_losses, consecutive_wins, total_trades, daily_loss):
        """Update state and potentially switch goals."""
        self.current_balance = balance
        self.peak_balance = max(self.peak_balance, balance)
        self.session_pnl = pnl
        self.consecutive_losses = consecutive_losses
        self.consecutive_wins = consecutive_wins
        self.total_trades = total_trades
        self.daily_loss = daily_loss

        old_mode = self.current_mode
        self._evaluate_mode()

        if old_mode != self.current_mode:
            self.mode_history.append({
                "from": old_mode,
                "to": self.current_mode,
                "balance": round(balance, 2),
                "pnl": round(pnl, 2),
                "time": int(time.time() * 1000),
            })
            if len(self.mode_history) > 20:
                self.mode_history = self.mode_history[-20:]

        self.active_goal = self.goals.get(self.current_mode, self.goals[self.MODE_OPTIMAL])
        return self.current_mode != old_mode

    def _evaluate_mode(self):
        """Determine the best mode based on current conditions."""
        if self.start_balance <= 0:
            return

        drawdown_pct = (self.peak_balance - self.current_balance) / self.peak_balance if self.peak_balance > 0 else 0
        pnl_pct = self.session_pnl / self.start_balance if self.start_balance > 0 else 0

        # PRESERVATION: big drawdown or deep loss
        if drawdown_pct > 0.03 or pnl_pct < -0.02:
            self.current_mode = self.MODE_PRESERVATION
            return

        # RECOVERY: moderate drawdown or loss streak
        if drawdown_pct > 0.015 or self.consecutive_losses >= 3 or pnl_pct < -0.01:
            self.current_mode = self.MODE_RECOVERY
            return

        # GROWTH: strong winning streak and above peak
        if (self.consecutive_wins >= 5 and pnl_pct > 0.01
                and self.current_balance >= self.peak_balance * 0.99):
            self.current_mode = self.MODE_GROWTH
            return

        # EXPLORATION: early session, few trades
        if self.total_trades < 10:
            self.current_mode = self.MODE_EXPLORATION
            return

        # OPTIMAL: default
        self.current_mode = self.MODE_OPTIMAL

    def get_mode(self):
        return self.current_mode

    def get_goal(self):
        return self.active_goal or self.goals[self.MODE_OPTIMAL]

    def check_trade_allowed(self, confidence, contract_type, trades_this_hour):
        """Check if a trade is allowed under current goal."""
        goal = self.get_goal()
        if confidence < goal.get("min_confidence", 0):
            return False, f"Confidence {confidence} < min {goal['min_confidence']}"
        if trades_this_hour >= goal.get("max_trades_per_hour", 999):
            return False, f"Hourly trade cap reached ({trades_this_hour})"
        allowed = goal.get("allowed_contracts")
        if allowed and contract_type not in allowed:
            return False, f"Contract {contract_type} not allowed in {self.current_mode}"
        return True, "ok"

    def get_stake_multiplier(self):
        """Get stake multiplier for current mode."""
        goal = self.get_goal()
        return goal.get("max_stake_multiplier", 1.0)

    def get_cooldown(self):
        """Get cooldown seconds for current mode."""
        goal = self.get_goal()
        return goal.get("cooldown_seconds", 20)

    def get_status(self):
        return {
            "mode": self.current_mode,
            "goal": self.active_goal.get("objective", "?") if self.active_goal else "?",
            "stake_multiplier": self.get_stake_multiplier(),
            "cooldown": self.get_cooldown(),
            "drawdown_pct": round((self.peak_balance - self.current_balance) / max(self.peak_balance, 1) * 100, 2),
            "mode_history": self.mode_history[-5:],
            "total_modes": len(self.mode_history),
        }
