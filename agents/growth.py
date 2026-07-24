"""
GROWTH ENGINE — Compounding, Streak Exploitation, Session Awareness

Fills the gaps between RiskManager (protection) and Mission (goals).
This module makes the system GROW, not just survive.

Key features:
  1. Compound Reinvestment — stake scales with growing balance
  2. Win Streak Exploitation — tiered compounding on consecutive wins
  3. Session-Aware Trading — trade proven market/hour combos only
  4. Drawdown Recovery — Kelly recalculation after losses
  5. Balance Growth Tracking — daily snapshots for equity curve
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

GROWTH_STATE_FILE = Path(__file__).parent.parent / 'growth_state.json'
BALANCE_SNAPSHOTS_FILE = Path(__file__).parent.parent / 'balance_snapshots.json'


class GrowthEngine:
    """Drives capital growth through compounding, streak exploitation, and session awareness."""

    def __init__(self):
        self.state = self._load()
        self.snapshots = self._load_snapshots()
        self._ensure_today()

    def _load(self):
        try:
            if GROWTH_STATE_FILE.exists():
                return json.loads(GROWTH_STATE_FILE.read_text())
        except Exception:
            pass
        return self._default()

    def _default(self):
        return {
            "version": 3,
            "compound_base_pct": 0.01,  # 1% of balance as base stake
            "compound_max_pct": 0.05,   # max 5% of balance per trade
            "streak_tiers": [
                {"min_wins": 2, "mult": 1.25},
                {"min_wins": 4, "mult": 1.50},
                {"min_wins": 6, "mult": 2.00},
                {"min_wins": 8, "mult": 2.50},
            ],
            "session_focus": {},  # market -> {hour -> {pnl, trades, wr}}
            "daily_snapshots": [],
            "total_compound_earned": 0.0,
            "streak_peak": 0,
            "best_streak_mult": 1.0,
        }

    def _ensure_today(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if self.state.get('today') != today:
            # Save yesterday's snapshot
            self._save_daily_snapshot()
            self.state['today'] = today
            self.state['daily_wins'] = 0
            self.state['daily_losses'] = 0
            self.state['daily_compound_earned'] = 0.0
            self._save()

    def _load_snapshots(self):
        try:
            if BALANCE_SNAPSHOTS_FILE.exists():
                return json.loads(BALANCE_SNAPSHOTS_FILE.read_text())
        except Exception:
            pass
        return {"snapshots": []}

    def _save(self):
        try:
            GROWTH_STATE_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception as e:
            print(f"  [GROWTH] Save error: {e}", flush=True)

    def _save_snapshots(self):
        try:
            # Keep last 90 days
            self.snapshots["snapshots"] = self.snapshots["snapshots"][-90:]
            BALANCE_SNAPSHOTS_FILE.write_text(json.dumps(self.snapshots, indent=2, default=str))
        except Exception as e:
            print(f"  [GROWTH] Snapshot save error: {e}", flush=True)

    def _save_daily_snapshot(self):
        """Save end-of-day balance snapshot."""
        if not self.state.get('last_balance'):
            return
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        snap = {
            "date": today,
            "balance": self.state['last_balance'],
            "pnl": self.state.get('last_pnl', 0),
            "trades": self.state.get('daily_trades_today', 0),
            "wins": self.state.get('daily_wins', 0),
            "losses": self.state.get('daily_losses', 0),
        }
        # Don't duplicate
        existing = [s for s in self.snapshots["snapshots"] if s.get("date") == today]
        if existing:
            existing[0].update(snap)
        else:
            self.snapshots["snapshots"].append(snap)
        self._save_snapshots()

    # ── COMPOUND REINVESTMENT ──
    def get_compound_stake(self, balance, base_stake):
        """Scale stake based on balance growth.
        When balance grows, stake grows proportionally.
        """
        start_balance = self.state.get('start_balance', balance)
        if start_balance <= 0:
            return base_stake

        growth_pct = (balance - start_balance) / start_balance
        compound_mult = 1.0 + max(0, growth_pct * 2)  # 2x the growth as multiplier

        # Cap at max
        max_mult = self.state.get('compound_max_mult', 3.0)
        compound_mult = min(compound_mult, max_mult)

        return round(base_stake * compound_mult, 2)

    def set_start_balance(self, balance):
        """Set starting balance for compound tracking."""
        if 'start_balance' not in self.state or self.state['start_balance'] == 0:
            self.state['start_balance'] = balance
            self._save()

    # ── WIN STREAK EXPLOITATION ──
    def get_streak_multiplier(self, consec_wins):
        """Get tiered multiplier based on win streak.
        2 wins = 1.25x, 4 wins = 1.50x, 6 wins = 2.00x, 8+ = 2.50x
        """
        mult = 1.0
        for tier in self.state.get('streak_tiers', []):
            if consec_wins >= tier['min_wins']:
                mult = tier['mult']

        if consec_wins > self.state.get('streak_peak', 0):
            self.state['streak_peak'] = consec_wins
            self.state['best_streak_mult'] = mult
            self._save()

        return mult

    # ── SESSION-AWARE TRADING ──
    def update_session_data(self, market, hour, profit, win):
        """Update session focus data with trade result."""
        if market not in self.state['session_focus']:
            self.state['session_focus'][market] = {}
        if str(hour) not in self.state['session_focus'][market]:
            self.state['session_focus'][market][str(hour)] = {
                'trades': 0, 'wins': 0, 'pnl': 0.0
            }
        s = self.state['session_focus'][market][str(hour)]
        s['trades'] += 1
        s['pnl'] = round(s['pnl'] + profit, 4)
        if win:
            s['wins'] += 1
        s['wr'] = round(s['wins'] / s['trades'] * 100, 1) if s['trades'] > 0 else 0
        self._save()

    def get_session_score(self, market, hour):
        """Score a market/hour combo based on collected data.
        Returns multiplier: >1.0 = good, <1.0 = bad, 0.0 = block.
        """
        sf = self.state.get('session_focus', {}).get(market, {}).get(str(hour))
        if not sf:
            return 1.0  # no data, neutral

        trades = sf.get('trades', 0)
        pnl = sf.get('pnl', 0)
        wr = sf.get('wr', 0)

        if trades < 3:
            return 1.0  # not enough data

        if pnl < -2.0 and wr < 35:
            return 0.0  # block: clearly losing
        if pnl < -1.0 and wr < 40:
            return 0.3  # heavily penalize
        if pnl > 1.0 and wr >= 60:
            return 1.5  # boost proven combos
        if pnl > 0 and wr >= 55:
            return 1.25  # mild boost

        return 1.0  # neutral

    def should_trade_session(self, market, hour):
        """Check if this market/hour combo should be traded."""
        score = self.get_session_score(market, hour)
        return score > 0, score

    # ── DRAWDOWN RECOVERY ──
    def get_drawdown_multiplier(self, balance, peak_balance):
        """Reduce stake during drawdown, restore during recovery."""
        if peak_balance <= 0:
            return 1.0

        drawdown_pct = (peak_balance - balance) / peak_balance

        if drawdown_pct > 0.03:
            return 0.25  # severe: 75% reduction
        elif drawdown_pct > 0.02:
            return 0.50  # moderate: 50% reduction
        elif drawdown_pct > 0.01:
            return 0.75  # mild: 25% reduction

        # Recovery bonus: if recovering from drawdown
        if hasattr(self, '_was_in_drawdown') and self._was_in_drawdown:
            if drawdown_pct < 0.005:
                self._was_in_drawdown = False
                return 1.25  # recovery boost

        if drawdown_pct > 0.005:
            self._was_in_drawdown = True

        return 1.0

    # ── COMBINED STAKE CALCULATION ──
    def calculate_optimal_stake(self, base_stake, balance, peak_balance,
                                 consec_wins, consec_losses, market, hour):
        """Master stake calculator: combines all growth factors.
        Returns (stake, breakdown_dict).
        """
        breakdown = {}
        stake = base_stake

        # 1. Compound reinvestment
        compound_mult = self.get_compound_stake(balance, 1.0) / base_stake if base_stake > 0 else 1.0
        stake *= compound_mult
        breakdown['compound'] = f"{compound_mult:.2f}x"

        # 2. Win streak exploitation
        streak_mult = self.get_streak_multiplier(consec_wins)
        stake *= streak_mult
        breakdown['streak'] = f"{streak_mult:.2f}x"

        # 3. Session awareness
        session_mult = self.get_session_score(market, hour)
        stake *= session_mult
        breakdown['session'] = f"{session_mult:.2f}x"

        # 4. Drawdown recovery
        dd_mult = self.get_drawdown_multiplier(balance, peak_balance)
        stake *= dd_mult
        breakdown['drawdown'] = f"{dd_mult:.2f}x"

        # Final clamp
        max_stake = balance * 0.05  # never more than 5% of balance
        stake = round(max(1.00, min(stake, max_stake)), 2)
        breakdown['final'] = f"${stake:.2f}"

        return stake, breakdown

    # ── TRACKING ──
    def record_trade(self, profit, balance, market, hour):
        """Record trade result for growth tracking."""
        self._ensure_today()
        self.state['last_balance'] = balance
        self.state['last_pnl'] = self.state.get('last_pnl', 0) + profit
        self.state['daily_trades_today'] = self.state.get('daily_trades_today', 0) + 1

        if profit > 0:
            self.state['daily_wins'] = self.state.get('daily_wins', 0) + 1
            self.state['daily_compound_earned'] = self.state.get('daily_compound_earned', 0) + profit
            self.state['total_compound_earned'] = self.state.get('total_compound_earned', 0) + profit
        else:
            self.state['daily_losses'] = self.state.get('daily_losses', 0) + 1

        # Update session data
        self.update_session_data(market, hour, profit, profit > 0)
        self._save()

    def get_growth_status(self):
        """Get full growth status for dashboard."""
        return {
            "balance": self.state.get('last_balance', 0),
            "start_balance": self.state.get('start_balance', 0),
            "daily_pnl": self.state.get('last_pnl', 0),
            "daily_trades": self.state.get('daily_trades_today', 0),
            "daily_wins": self.state.get('daily_wins', 0),
            "daily_losses": self.state.get('daily_losses', 0),
            "total_compound_earned": self.state.get('total_compound_earned', 0),
            "streak_peak": self.state.get('streak_peak', 0),
            "best_streak_mult": self.state.get('best_streak_mult', 1.0),
            "session_focus_markets": len(self.state.get('session_focus', {})),
        }
