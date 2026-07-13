"""
STRATEGY LIBRARY & HEALTH MANAGER
Tracks all strategies, scores health, retires losers, promotes winners.
Maintains Champion Library + Failed Archive.
"""
import time
from collections import defaultdict


class Strategist:
    """
    Strategy lifecycle manager.
    HEALTH SCORE = historical_perf + recent_perf + market_compat - risk_penalty
    """

    HEALTH_STRONG = "STRONG"
    HEALTH_HEALTHY = "HEALTHY"
    HEALTH_UNCERTAIN = "UNCERTAIN"
    HEALTH_FAILING = "FAILING"
    HEALTH_NEW = "NEW"
    HEALTH_RETIRE = "RETIRE"

    def __init__(self, memory=None):
        self.strategies = {}   # key -> strategy info
        self.champions = []    # top performers
        self.failed_archive = []  # retired strategies with reasons
        self.last_eval = 0
        self.eval_interval = 30  # re-evaluate every 30 trades

        if memory:
            self._import_from_memory(memory)

    def _import_from_memory(self, memory):
        """Import existing data from Memory module."""
        if not hasattr(memory, 'data'):
            return
        for key, stats in memory.data.get('strategies', {}).items():
            trades = stats.get('trades', 0)
            wins = stats.get('wins', 0)
            losses = stats.get('losses', 0)
            total_profit = stats.get('total_profit', 0)
            wr = wins / trades if trades > 0 else 0

            self.strategies[key] = {
                "key": key,
                "market": stats.get('market', key.split(':')[0]),
                "name": stats.get('strategy', key.split(':')[1] if ':' in key else key),
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "winrate": round(wr, 4),
                "total_profit": round(total_profit, 2),
                "status": self._compute_health(wr, trades),
                "health_score": self._health_score(wr, trades, total_profit),
                "last_trade_time": stats.get('last_trade_time', 0),
                "best_streak": stats.get('best_streak', 0),
            }

    def _compute_health(self, winrate, trades):
        if trades < 3:
            return self.HEALTH_NEW
        if winrate < 0.30 and trades >= 5:
            return self.HEALTH_FAILING
        if winrate < 0.45:
            return self.HEALTH_UNCERTAIN
        if winrate < 0.60:
            return self.HEALTH_HEALTHY
        return self.HEALTH_STRONG

    def _health_score(self, winrate, trades, total_profit):
        """
        ALM STRATEGY SCORE:
        = Expected Value (EV)
        + Reliability (trade confidence)
        + Market Compatibility (profit factor)
        - Risk Penalty (drawdown, loss rate)
        """
        # Expected Value component (0-35 points)
        ev_score = winrate * 35

        # Reliability: trade confidence (0-25 points, caps at 50 trades)
        reliability = min(trades / 50, 1.0) * 25

        # Market Compatibility: profit factor (0-25 points)
        profit_factor = 0
        if total_profit > 0 and trades > 0:
            avg_win = total_profit / max(1, trades * winrate) if winrate > 0 else 0
            profit_factor = min(total_profit / 10, 1.0) * 25
        elif total_profit < 0:
            profit_factor = max(total_profit / 20, -1.0) * 10

        # Risk Penalty (-15 points max)
        loss_rate = 1 - winrate if trades > 0 else 0
        risk_penalty = loss_rate * 15 if trades >= 10 else 0

        # Bonus for large sample size (+5 points at 30+ trades)
        sample_bonus = 5 if trades >= 30 else (trades / 30 * 5) if trades >= 10 else 0

        score = ev_score + reliability + profit_factor - risk_penalty + sample_bonus
        return round(max(0, min(100, score)), 1)

    def get_strategy_recommendation(self, market, regime):
        """
        ALM: Recommend best strategy for a market+regime combo.
        Returns (strategy_key, health_score, action).
        """
        candidates = []
        for key, s in self.strategies.items():
            if s["market"] != market:
                continue
            if s["status"] in (self.HEALTH_FAILING, self.HEALTH_RETIRE):
                continue
            if s["trades"] < 2:
                continue
            candidates.append((key, s))

        if not candidates:
            return None, 0, "TEST"

        # Sort by health score
        candidates.sort(key=lambda x: x[1]["health_score"], reverse=True)
        best_key, best_s = candidates[0]

        if best_s["status"] == self.HEALTH_STRONG:
            return best_key, best_s["health_score"], "TRADE"
        elif best_s["status"] == self.HEALTH_HEALTHY:
            return best_key, best_s["health_score"], "TRADE"
        elif best_s["status"] == self.HEALTH_UNCERTAIN:
            return best_key, best_s["health_score"], "TEST"
        else:
            return best_key, best_s["health_score"], "OPTIMIZE"

    def record_trade(self, key, profit):
        """Record a trade outcome for a strategy."""
        if key not in self.strategies:
            parts = key.split(':', 1)
            self.strategies[key] = {
                "key": key,
                "market": parts[0] if len(parts) > 0 else "?",
                "name": parts[1] if len(parts) > 1 else key,
                "trades": 0, "wins": 0, "losses": 0,
                "winrate": 0, "total_profit": 0, "status": self.HEALTH_NEW,
                "health_score": 0, "last_trade_time": 0, "best_streak": 0,
            }

        s = self.strategies[key]
        s["trades"] += 1
        s["total_profit"] = round(s["total_profit"] + profit, 4)
        s["last_trade_time"] = int(time.time() * 1000)

        if profit > 0:
            s["wins"] += 1
            s["current_streak"] = s.get("current_streak", 0) + 1
            s["best_streak"] = max(s.get("best_streak", 0), s["current_streak"])
        else:
            s["losses"] += 1
            s["current_streak"] = 0

        wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
        s["winrate"] = round(wr, 4)
        s["status"] = self._compute_health(wr, s["trades"])
        s["health_score"] = self._health_score(wr, s["trades"], s["total_profit"])

        # Auto-archive failures
        if s["status"] == self.HEALTH_FAILING and s["trades"] >= 5:
            self._archive_failure(key, s)

    def _archive_failure(self, key, s):
        self.failed_archive.append({
            "key": key,
            "market": s["market"],
            "name": s["name"],
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "winrate": s["winrate"],
            "total_profit": s["total_profit"],
            "reason": f"WR={s['winrate']*100:.0f}% after {s['trades']}T",
            "archived_at": int(time.time()),
        })
        if len(self.failed_archive) > 30:
            self.failed_archive = self.failed_archive[-30:]

    def is_approved(self, key):
        """Is this strategy safe to trade?"""
        s = self.strategies.get(key)
        if s is None:
            return True  # new, allow testing
        return s["status"] not in (self.HEALTH_FAILING, self.HEALTH_RETIRE)

    def get_champions(self, top_n=5):
        """Get top performing strategies."""
        sorted_s = sorted(self.strategies.values(),
                          key=lambda x: x["health_score"], reverse=True)
        return [s for s in sorted_s if s["trades"] >= 3][:top_n]

    def get_worst(self, bottom_n=3):
        """Get worst performing strategies."""
        sorted_s = sorted(self.strategies.values(),
                          key=lambda x: x["health_score"])
        return [s for s in sorted_s if s["trades"] >= 5][:bottom_n]

    def should_retire(self, key):
        """Should this strategy be permanently retired?"""
        s = self.strategies.get(key)
        if s is None:
            return False
        # Retire if: 10+ trades, <25% win rate, or losing >20 units
        if s["trades"] >= 10 and s["winrate"] < 0.25:
            return True
        if s["total_profit"] < -20:
            return True
        return False

    def get_status(self):
        all_s = list(self.strategies.values())
        active = [s for s in all_s if s["trades"] >= 3 and s["status"] not in (self.HEALTH_FAILING, self.HEALTH_RETIRE)]
        return {
            "total_strategies": len(all_s),
            "active_strategies": len(active),
            "champions": len(self.get_champions()),
            "failed_archived": len(self.failed_archive),
            "best_health": max((s["health_score"] for s in all_s), default=0),
            "champion_list": self.get_champions(3),
            "failed_list": self.failed_archive[-3:],
        }
