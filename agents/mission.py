"""
MISSION MODULE — System Identity & Goal Tracking

The ALM Trading System mission:
  "Let collected data dictate system alignment.
   Self-test, adapt, and lock $20 daily profit.
   Trade only when, where, and why the data says it works."

Three core functions:
  1. Mission state — daily goal tracking, profit locking at $20
  2. Self-test evaluator — runs every N trades to measure performance
  3. Market auto-selector — uses timezone_history data to pick best market/hour combos
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

MISSION_FILE = Path(__file__).parent.parent / 'mission_state.json'

MISSION_OBJECTIVE = (
    "Collect data → Evaluate → Lock $20/day → Adapt market/hour selection → Repeat. "
    "No claims, only data-driven decisions. Protect capital at all costs."
)

# Session labels for naming
SESSION_LABELS = {
    range(0, 4):   'Asian Late',
    range(4, 7):   'Asian Early',
    range(7, 10):  'European Open',
    range(10, 13): 'European Mid',
    range(13, 16): 'US Open',
    range(16, 19): 'US Afternoon',
    range(19, 22): 'Evening',
    range(22, 24): 'Night',
}

def get_session_label(hour):
    for rng, label in SESSION_LABELS.items():
        if hour in rng:
            return label
    return 'Unknown'

def now_utc():
    return datetime.now(timezone.utc)


class Mission:
    """Tracks mission state: daily profit target, self-test scores, market alignment."""

    def __init__(self):
        self.state = self._load()
        self._ensure_today()
        self.adaptation_log = []

    def _load(self):
        try:
            if MISSION_FILE.exists():
                state = json.loads(MISSION_FILE.read_text())
                # Ensure self_test key exists (mission_tracker may overwrite without it)
                if 'self_test' not in state:
                    state['self_test'] = self._default_state()['self_test']
                return state
        except Exception:
            pass
        return self._default_state()

    def _default_state(self):
        return {
            "mission_objective": MISSION_OBJECTIVE,
            "version": 5,
            "created": now_utc().isoformat(),
            "daily_target": 20.0,
            "daily_profit_locked": False,
            "daily_profit": 0.0,
            "house_money_mode": False,
            "house_money_balance": 0.0,
            "self_test": {
                "runs": 0,
                "last_run": None,
                "last_score": None,
                "last_recommendation": None,
                "history": [],
            },
            "market_alignment": {
                "enabled_hours": {},
                "blocked_hours": {},
                "last_rebalance": None,
            },
            "adaptations": [],
        }

    def _ensure_today(self):
        """Reset daily state if new day."""
        today = now_utc().strftime('%Y-%m-%d')
        if self.state.get('today') != today:
            self.state['today'] = today
            self.state['daily_profit'] = 0.0
            self.state['daily_profit_locked'] = False
            self.state['house_money_mode'] = False
            self.state['house_money_balance'] = 0.0
            self.state['daily_trades'] = 0
            self.state['daily_session_pnl'] = 0.0
            self._save()

    def _save(self):
        try:
            MISSION_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception as e:
            print(f"  [MISSION] Save error: {e}", flush=True)

    def record_trade(self, profit, pnl_before_trade, balance):
        """Record a completed trade and check profit locks."""
        self._ensure_today()
        self.state['daily_trades'] = self.state.get('daily_trades', 0) + 1
        new_profit = self.state['daily_profit'] + profit
        self.state['daily_profit'] = round(new_profit, 2)
        self.state['daily_session_pnl'] = round(pnl_before_trade + profit, 2)

        # ── $20 DAILY PROFIT LOCK ──
        target = self.state.get('daily_target', 20.0)
        if not self.state['daily_profit_locked'] and new_profit >= target:
            self.state['daily_profit_locked'] = True
            self.state['house_money_mode'] = True
            self.state['house_money_balance'] = balance
            self.state['lock_reached_at'] = now_utc().isoformat()
            print(f"  🏆 MISSION: DAILY ${target:.0f} PROFIT LOCKED AT ${new_profit:.2f}! "
                  f"House money mode activated.", flush=True)
            return 'lock'
        return 'ok'

    def is_profit_locked(self):
        """Check if daily profit target has been hit."""
        self._ensure_today()
        return self.state.get('daily_profit_locked', False)

    def get_stake_multiplier(self):
        """Stake 50% of normal in house-money mode to keep building."""
        if self.is_profit_locked():
            return 0.5  # house money: half stake, keep growing
        return 1.0

    # ── SELF-TEST EVALUATOR ──
    def run_self_test(self, timezone_intel=None, agent_memory=None):
        """Self-test: evaluate system performance by hour, market, strategy.
        Returns recommendations for what to change.
        """
        self._ensure_today()
        results = []
        recommendations = []

        # 1. Evaluate hourly performance
        if timezone_intel and hasattr(timezone_intel, 'session_stats'):
            ss = timezone_intel.session_stats
            for hour_str in sorted(ss.keys(), key=lambda x: int(x)):
                h = int(hour_str)
                stats = ss[hour_str]
                trades = stats.get('trades', 0)
                pnl = stats.get('pnl', 0)
                wr = stats.get('wr', 0)
                if trades >= 3:
                    results.append({
                        'type': 'hour', 'key': hour_str,
                        'trades': trades, 'pnl': round(pnl, 2),
                        'wr': round(wr, 1),
                    })
                    if pnl < -3 and wr < 35:
                        recommendations.append(f"BLOCK hour {h}:00 — ${pnl:.2f}, WR={wr:.0f}%")

        # 2. Evaluate market/hour combos
        if timezone_intel and hasattr(timezone_intel, 'hourly_stats'):
            for market, hours in timezone_intel.hourly_stats.items():
                for h_str, stats in hours.items():
                    trades = stats.get('trades', 0)
                    pnl = stats.get('pnl', 0)
                    wr = stats.get('wr', 0)
                    if trades >= 3:
                        results.append({
                            'type': 'market_hour', 'market': market, 'hour': h_str,
                            'trades': trades, 'pnl': round(pnl, 2),
                            'wr': round(wr, 1),
                        })
                        if pnl < -3 and wr < 35:
                            recommendations.append(f"AVOID {market} at h{h_str} — ${pnl:.2f}")

        # 3. Evaluate strategy performance
        if agent_memory and isinstance(agent_memory, dict):
            strategies = agent_memory.get('strategies', {})
            for skey, sinfo in strategies.items():
                tp = sinfo.get('total_pnl', 0)
                tw = sinfo.get('total_trades', 0)
                wr = sinfo.get('win_rate', 0)
                if tw >= 5:
                    results.append({
                        'type': 'strategy', 'key': skey,
                        'trades': tw, 'pnl': round(tp, 2),
                        'wr': round(wr, 1),
                    })
                    if tp < -5:
                        recommendations.append(f"RETIRE {skey} — ${tp:.2f} PnL")

        # 4. Compute overall score
        total_trades = sum(r.get('trades', 0) for r in results)
        total_pnl = sum(r.get('pnl', 0) for r in results)
        positive_hours = sum(1 for r in results if r.get('pnl', 0) > 0)
        scored_items = [r for r in results if r.get('trades', 0) >= 3]
        overall_score = 0
        if scored_items:
            pnl_sum = sum(r['pnl'] for r in scored_items)
            max_possible = sum(abs(r['pnl']) for r in scored_items) or 1
            overall_score = max(-100, min(100, int((pnl_sum / max_possible) * 100 + 50)))

        # Update state
        self.state['self_test']['runs'] += 1
        self.state['self_test']['last_run'] = now_utc().isoformat()
        self.state['self_test']['last_score'] = overall_score
        self.state['self_test']['last_recommendation'] = recommendations[:10]
        self.state['self_test']['history'].append({
            'time': now_utc().isoformat(),
            'score': overall_score,
            'recommendations': len(recommendations),
            'results_sampled': len(results),
        })
        if len(self.state['self_test']['history']) > 50:
            self.state['self_test']['history'] = self.state['self_test']['history'][-50:]

        self._save()
        return overall_score, recommendations, results

    def get_self_test_results(self):
        return self.state.get('self_test', {})

    # ── MARKET AUTO-SELECTOR ──
    def auto_select_markets(self, timezone_intel, current_hour=None):
        """From timezone data, decide which markets to focus on right now.
        Returns list of (market, confidence, reason).
        """
        if current_hour is None:
            current_hour = int(time.strftime('%H'))

        candidates = []
        if not timezone_intel or not hasattr(timezone_intel, 'hourly_stats'):
            return candidates

        for market, hours in timezone_intel.hourly_stats.items():
            h_stats = hours.get(str(current_hour), {})
            trades = h_stats.get('trades', 0)
            pnl = h_stats.get('pnl', 0)
            wr = h_stats.get('wr', 0)

            if trades < 2:
                # Not enough data — still allow with neutral confidence
                candidates.append({
                    'market': market, 'confidence': 'neutral',
                    'reason': f'insufficient data ({trades}T)',
                    'score': 0.5
                })
                continue

            if pnl < -3.0 and wr < 35:
                # Bad data — block
                candidates.append({
                    'market': market, 'confidence': 'block',
                    'reason': f'${pnl:.2f} PnL, WR={wr:.0f}% in {trades}T',
                    'score': 0.0
                })
            elif pnl > 2.0 and wr >= 55:
                candidates.append({
                    'market': market, 'confidence': 'high',
                    'reason': f'${pnl:.2f} PnL, WR={wr:.0f}% in {trades}T',
                    'score': 1.0
                })
            elif pnl > 0 and wr >= 50:
                candidates.append({
                    'market': market, 'confidence': 'medium',
                    'reason': f'${pnl:.2f} PnL, WR={wr:.0f}% in {trades}T',
                    'score': 0.75
                })
            else:
                candidates.append({
                    'market': market, 'confidence': 'low',
                    'reason': f'${pnl:.2f} PnL, WR={wr:.0f}% in {trades}T',
                    'score': 0.25
                })

        # Sort by score descending
        candidates.sort(key=lambda x: -x['score'])
        return candidates

    def should_allow_market(self, market, hour, timezone_intel):
        """Check if a specific market should be traded at this hour.
        Returns (allowed, reason).
        """
        if not timezone_intel or not hasattr(timezone_intel, 'hourly_stats'):
            return True, 'no data yet'

        h_stats = timezone_intel.hourly_stats.get(market, {}).get(str(hour), {})
        trades = h_stats.get('trades', 0)
        pnl = h_stats.get('pnl', 0)
        wr = h_stats.get('wr', 0)

        if trades >= 3 and pnl < -3.0 and wr < 35:
            return False, f'BAD: {market} h{hour} ${pnl:.2f} WR={wr:.0f}% in {trades}T'
        return True, ''

    def get_status(self):
        """Get full mission status for dashboard."""
        self._ensure_today()
        st = self.state.get('self_test', {})
        return {
            'daily_profit': self.state.get('daily_profit', 0),
            'daily_target': self.state.get('daily_target', 20.0),
            'profit_locked': self.state.get('daily_profit_locked', False),
            'house_money_mode': self.state.get('house_money_mode', False),
            'house_money_balance': self.state.get('house_money_balance', 0),
            'daily_trades': self.state.get('daily_trades', 0),
            'self_test_runs': st.get('runs', 0),
            'self_test_score': st.get('last_score', None),
            'self_test_recommendations': (st.get('last_recommendation') or [])[:5],
            'adaptations': self.state.get('adaptations', [])[-5:],
        }
