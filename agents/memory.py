"""
MEMORY AGENT — Persistent Learning System
Remembers what works, forgets what doesn't.
Tracks per-market, per-strategy performance over time.
"""
import json
import time
import os
from collections import defaultdict
from pathlib import Path

MEMORY_FILE = Path(__file__).parent.parent / "agent_memory.json"


class Memory:
    """
    Persistent memory that survives restarts.
    Learns which market+strategy combos are profitable.
    """

    def __init__(self):
        self.data = self._load()
        if "markets" not in self.data:
            self.data["markets"] = {}
        if "strategies" not in self.data:
            self.data["strategies"] = {}
        if "trades" not in self.data:
            self.data["trades"] = []
        if "digit_history" not in self.data:
            self.data["digit_history"] = {}
        if "daily_stats" not in self.data:
            self.data["daily_stats"] = {}
        if "market_profiles" not in self.data:
            self.data["market_profiles"] = {}
        if "strategy_lifecycle" not in self.data:
            self.data["strategy_lifecycle"] = {}
        if "failure_patterns" not in self.data:
            self.data["failure_patterns"] = []
        if "simulation_results" not in self.data:
            self.data["simulation_results"] = []
        if "model_decisions" not in self.data:
            self.data["model_decisions"] = []
        if "ai_feedback" not in self.data:
            self.data["ai_feedback"] = {"approved": 0, "blocked": 0, "correct": 0, "incorrect": 0, "recent": []}
        if "session_memory" not in self.data:
            self.data["session_memory"] = []
        if "strategy_families" not in self.data:
            self.data["strategy_families"] = {}
        if "daily_drawdown" not in self.data:
            self.data["daily_drawdown"] = {}


    @property
    def strategies(self):
        return self.data.get("strategies", {})
    def _load(self):
        try:
            if MEMORY_FILE.exists():
                with open(MEMORY_FILE) as f:
                    return json.load(f)
        except:
            pass
        return {}

    def save(self):
        with open(MEMORY_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    # ── Digit Frequency Memory ─────────────────────────
    def record_digit(self, market, digit):
        """Record a digit occurrence for frequency tracking."""
        if market not in self.data["digit_history"]:
            self.data["digit_history"][market] = {str(d): 0 for d in range(10)}
            self.data["digit_history"][market]["_total"] = 0

        key = str(digit)
        if key not in self.data["digit_history"][market]:
            self.data["digit_history"][market][key] = 0

        self.data["digit_history"][market][key] += 1
        self.data["digit_history"][market]["_total"] += 1

    def get_digit_bias(self, market, min_samples=50):
        """
        Get digit frequency bias for a market.
        Returns dict of digit -> frequency, and the most biased digit.
        Only returns if we have enough samples.
        """
        hist = self.data["digit_history"].get(market, {})
        total = hist.get("_total", 0)

        if total < min_samples:
            return None, None, 0

        freqs = {}
        for d in range(10):
            count = hist.get(str(d), 0)
            freqs[d] = count / total

        # Find most overrepresented digit
        expected = 0.10
        best_digit = None
        best_bias = 0
        for d, freq in freqs.items():
            bias = freq - expected
            if bias > best_bias:
                best_bias = bias
                best_digit = d

        return freqs, best_digit, best_bias

    # ── Strategy Performance Memory ────────────────────
    def record_trade(self, market, strategy, contract_type, profit, stake, details=None):
        """Record a completed trade for learning."""
        trade = {
            "market": market,
            "strategy": strategy,
            "contract_type": contract_type,
            "profit": profit,
            "stake": stake,
            "edge": profit - (-stake) if profit > 0 else profit,
            "time": int(time.time() * 1000),
            "details": details or {},
        }
        self.data["trades"].append(trade)

        # Keep last 500 trades
        if len(self.data["trades"]) > 500:
            self.data["trades"] = self.data["trades"][-500:]

        # Update strategy stats
        key = f"{market}:{strategy}"
        if key not in self.data["strategies"]:
            self.data["strategies"][key] = {
                "market": market,
                "strategy": strategy,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_profit": 0,
                "best_streak": 0,
                "current_streak": 0,
                "last_trade_time": 0,
            }

        s = self.data["strategies"][key]
        s["trades"] += 1
        s["total_profit"] = round(s["total_profit"] + profit, 4)
        s["last_trade_time"] = trade["time"]

        if profit > 0:
            s["wins"] += 1
            s["current_streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["current_streak"])
        else:
            s["losses"] += 1
            s["current_streak"] = 0

        self.save()

    def get_strategy_score(self, market, strategy):
        """Get historical score for a market+strategy combo."""
        key = f"{market}:{strategy}"
        s = self.data["strategies"].get(key)
        if not s or s["trades"] < 3:
            return None  # not enough data

        win_rate = s["wins"] / s["trades"] if s["trades"] > 0 else 0
        avg_profit = s["total_profit"] / s["trades"]

        # Score combines win rate and average profit
        score = win_rate * 0.6 + min(1.0, max(0, avg_profit / 5)) * 0.4
        return {
            "score": round(score, 4),
            "win_rate": round(win_rate * 100, 1),
            "trades": s["trades"],
            "total_profit": round(s["total_profit"], 2),
            "avg_profit": round(avg_profit, 4),
            "best_streak": s["best_streak"],
        }

    def get_best_strategies(self, top_n=5):
        """Return top performing strategies across all markets."""
        scored = []
        for key, s in self.data["strategies"].items():
            if s["trades"] < 3:
                continue
            win_rate = s["wins"] / s["trades"]
            avg_profit = s["total_profit"] / s["trades"]
            score = win_rate * 0.6 + min(1.0, max(0, avg_profit / 5)) * 0.4
            scored.append({
                "market": s["market"],
                "strategy": s["strategy"],
                "score": round(score, 4),
                "win_rate": round(win_rate * 100, 1),
                "trades": s["trades"],
                "total_profit": round(s["total_profit"], 2),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_n]

    def get_worst_strategies(self, bottom_n=3):
        """Return worst performing strategies to avoid."""
        scored = []
        for key, s in self.data["strategies"].items():
            if s["trades"] < 5:
                continue
            win_rate = s["wins"] / s["trades"]
            avg_profit = s["total_profit"] / s["trades"]
            score = win_rate * 0.6 + min(1.0, max(0, avg_profit / 5)) * 0.4
            scored.append({
                "market": s["market"],
                "strategy": s["strategy"],
                "score": round(score, 4),
                "win_rate": round(win_rate * 100, 1),
                "trades": s["trades"],
                "total_profit": round(s["total_profit"], 2),
            })

        scored.sort(key=lambda x: x["score"])
        return scored[:bottom_n]

    # ── Daily Stats ────────────────────────────────────
    def record_daily(self, date_str, balance, trades, wins, losses, pnl):
        """Record daily summary."""
        self.data["daily_stats"][date_str] = {
            "balance": balance,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "pnl": round(pnl, 2),
        }
        self.save()

    def get_daily_stats(self, days=7):
        """Get last N days of stats."""
        dates = sorted(self.data["daily_stats"].keys(), reverse=True)
        return {d: self.data["daily_stats"][d] for d in dates[:days]}

    # ── Learning Adjustments ───────────────────────────
    def should_avoid(self, market, strategy):
        """Check if a strategy should be avoided based on history."""
        score = self.get_strategy_score(market, strategy)
        if score is None:
            return False  # not enough data
        # Avoid if win rate < 30% after 10+ trades
        if score["trades"] >= 5 and score["win_rate"] < 35:
            return True
        # Avoid if losing money consistently
        if score["trades"] >= 5 and score["total_profit"] < -10:
            return True
        return False

    def get_confidence_modifier(self, market, strategy):
        """
        Returns a multiplier for stake sizing based on historical performance.
        1.0 = neutral, <1.0 = reduce, >1.0 = increase (up to a cap).
        """
        score = self.get_strategy_score(market, strategy)
        if score is None:
            return 1.0  # no data, neutral

        if score["trades"] < 5:
            return 0.8  # slightly cautious with new combos

        if score["win_rate"] > 60 and score["total_profit"] > 0:
            return 1.2  # slightly more aggressive with winners

        if score["win_rate"] < 40:
            return 0.6  # reduce on losers

        return 1.0

    # ── ALM Market Profiles ──────────────────────────────
    def update_market_profile(self, market, regime, strategy, profit):
        """Update market profile with P&L tracking."""
        """Update market profile with what works and what doesn't."""
        if market not in self.data["market_profiles"]:
            self.data["market_profiles"][market] = {
                "best_strategies": {},
                "worst_strategies": {},
                "regime_history": [],
                "total_trades": 0,
                "total_profit": 0,
            }

        mp = self.data["market_profiles"][market]
        mp["total_trades"] += 1
        mp["total_profit"] = round(mp["total_profit"] + profit, 4)

        # Track strategy performance per market
        if strategy not in mp["best_strategies"]:
            mp["best_strategies"][strategy] = {"wins": 0, "losses": 0, "pnl": 0}
        sp = mp["best_strategies"][strategy]
        sp["pnl"] = round(sp["pnl"] + profit, 4)
        if profit > 0:
            sp["wins"] += 1
        else:
            sp["losses"] += 1

        # Track regime
        mp["regime_history"].append(regime)
        if len(mp["regime_history"]) > 100:
            mp["regime_history"] = mp["regime_history"][-100:]

    def get_market_profile(self, market):
        """Get market profile with best/worst strategies."""
        mp = self.data["market_profiles"].get(market)
        if not mp:
            return None

        # Rank strategies by PnL
        ranked = sorted(mp["best_strategies"].items(),
                       key=lambda x: x[1]["pnl"], reverse=True)
        best = [(s, d) for s, d in ranked if d["pnl"] > 0][:3]
        worst = [(s, d) for s, d in ranked[-3:] if d["pnl"] < 0]

        return {
            "market": market,
            "total_trades": mp["total_trades"],
            "total_profit": mp["total_profit"],
            "best_strategies": best,
            "worst_strategies": worst,
        }

    # ── ALM Strategy Lifecycle ──────────────────────────
    def record_strategy_lifecycle(self, strategy_key, action, reason=""):
        """Track strategy lifecycle: TEST -> OPTIMIZE -> TRADE -> RETIRE."""
        if strategy_key not in self.data["strategy_lifecycle"]:
            self.data["strategy_lifecycle"][strategy_key] = {
                "history": [],
                "current_status": "NEW",
                "trades": 0,
                "wins": 0,
                "total_pnl": 0.0,
            }

        lc = self.data["strategy_lifecycle"][strategy_key]
        lc["history"].append({
            "action": action,
            "reason": reason,
            "time": int(time.time() * 1000),
        })
        lc["current_status"] = action

        # Count trades and wins from reason string
        if action == "TRADE":
            lc["trades"] = lc.get("trades", 0) + 1
            if "Win" in reason or "+" in reason:
                lc["wins"] = lc.get("wins", 0) + 1
            # Extract P&L from reason
            import re
            pnl_match = re.search(r'\$([+-]?[\d.]+)', reason)
            if pnl_match:
                lc["total_pnl"] = lc.get("total_pnl", 0) + float(pnl_match.group(1))

        # Keep last 20 lifecycle events
        if len(lc["history"]) > 20:
            lc["history"] = lc["history"][-20:]

    def get_strategy_lifecycle(self, strategy_key):
        """Get lifecycle history for a strategy."""
        return self.data["strategy_lifecycle"].get(strategy_key)

    def is_strategy_retired(self, market, strategy):
        """Check if a strategy is retired for a market."""
        strat_key = f"{market}:{strategy}"
        lc = self.data["strategy_lifecycle"].get(strat_key, {})
        return lc.get("current_status") == "RETIRE"

    def get_strategy_ev(self, market, strategy):
        """Get expected value (EV) for a strategy: pnl / trades."""
        strat_key = f"{market}:{strategy}"
        s = self.data["strategies"].get(strat_key, {})
        trades = s.get("trades", 0)
        pnl = s.get("pnl", 0.0)
        return pnl / max(1, trades)

    # ── ALM Failure Pattern Detection ──────────────────

    def evaluate_and_retire(self):
        """Evaluate strategies and retire losers. Returns list of retired keys."""
        retired = []
        for key, s in list(self.data["strategies"].items()):
            trades = s.get("trades", 0)
            if trades < 5:
                continue
            losses = trades - s.get("wins", 0)
            loss_rate = losses / trades
            # Retire if: 5+ trades with 70%+ loss rate, OR 10+ trades with 60%+ loss rate
            should_retire = (trades >= 5 and loss_rate >= 0.70) or (trades >= 10 and loss_rate >= 0.60) or (trades >= 15 and loss_rate >= 0.55)
            ev = s.get("ev", 0)
            # Also retire if negative EV with enough data
            if trades >= 8 and ev < -0.01:
                should_retire = True
            if should_retire:
                self.data["strategy_lifecycle"][key] = {"current_status": "RETIRE", "history": [{"action": "RETIRE", "reason": f"{losses}/{trades} losses, EV={ev:.4f}", "time": int(time.time() * 1000)}]}
                retired.append({"key": key, "reason": f"{losses}/{trades} losses, EV={ev:.4f}"})
        self.save()
        return retired
    def detect_failure_patterns(self):
        """Analyze recent trades for failure patterns."""
        recent = self.data["trades"][-30:]
        if len(recent) < 10:
            return []

        patterns = []

        # Pattern 1: Same strategy losing on multiple markets
        strategy_losses = {}
        for t in recent:
            if t["profit"] < 0:
                s = t["strategy"]
                if s not in strategy_losses:
                    strategy_losses[s] = {"markets": set(), "count": 0}
                strategy_losses[s]["markets"].add(t["market"])
                strategy_losses[s]["count"] += 1

        for s, data in strategy_losses.items():
            if data["count"] >= 3 and len(data["markets"]) >= 2:
                patterns.append({
                    "type": "STRATEGY_WIDE_FAILURE",
                    "strategy": s,
                    "markets_affected": len(data["markets"]),
                    "losses": data["count"],
                    "action": "RETIRE",
                })

        # Pattern 2: consecutive losses on same market
        market_losses = {}
        for t in recent:
            if t["profit"] < 0:
                m = t["market"]
                if m not in market_losses:
                    market_losses[m] = 0
                market_losses[m] += 1

        for m, count in market_losses.items():
            if count >= 5:
                patterns.append({
                    "type": "MARKET_UNFAVORABLE",
                    "market": m,
                    "consecutive_losses": count,
                    "action": "ROTATE",
                })

        # Pattern 3: declining win rate over time
        if len(recent) >= 20:
            first_half = recent[:10]
            second_half = recent[10:]
            wr1 = sum(1 for t in first_half if t["profit"] > 0) / len(first_half)
            wr2 = sum(1 for t in second_half if t["profit"] > 0) / len(second_half)
            if wr1 > 0.50 and wr2 < 0.35:
                patterns.append({
                    "type": "DECLINING_PERFORMANCE",
                    "early_wr": round(wr1 * 100, 1),
                    "recent_wr": round(wr2 * 100, 1),
                    "action": "OPTIMIZE",
                })

        self.data["failure_patterns"] = patterns
        return patterns

    # ── ALM Simulation Memory ──────────────────────────
    def record_simulation(self, strategy_key, results):
        """Store simulation results AND update strategy stats with EV/sims."""
        entry = {
            "strategy": strategy_key,
            "simulations": results.get("simulations", 0),
            "win_rate": results.get("win_rate", 0),
            "expected_value": results.get("expected_value", 0),
            "max_drawdown": results.get("max_drawdown", 0),
            "profit_factor": results.get("profit_factor", 0),
            "time": int(time.time() * 1000),
        }
        self.data["simulation_results"].append(entry)
        if len(self.data["simulation_results"]) > 100:
            self.data["simulation_results"] = self.data["simulation_results"][-100:]
        # KEY FIX: write EV + simulations into strategy stats so retirement works
        sims = results.get("simulations", 0)
        ev = results.get("expected_value", 0)
        wr = results.get("win_rate", 0)
        if sims >= 5 and strategy_key in self.data["strategies"]:
            self.data["strategies"][strategy_key]["ev"] = ev
            self.data["strategies"][strategy_key]["simulations"] = sims
            self.data["strategies"][strategy_key]["sim_win_rate"] = wr
            self.data["strategies"][strategy_key]["sim_time"] = int(time.time() * 1000)

    def get_simulation_history(self, strategy_key=None):
        """Get simulation results, optionally filtered by strategy."""
        if strategy_key:
            return [s for s in self.data["simulation_results"] if s["strategy"] == strategy_key]
        return self.data["simulation_results"][-10:]

    def get_memory_summary(self):
        """Summary for dashboard."""
        total_trades = len(self.data["trades"])
        total_strategies = len(self.data["strategies"])
        active_strategies = sum(1 for s in self.data["strategies"].values() if s["trades"] >= 3)
        markets_tracked = len(self.data["digit_history"])
        best = self.get_best_strategies(3)
        patterns = self.detect_failure_patterns()

        return {
            "total_trades": total_trades,
            "total_strategies": total_strategies,
            "active_strategies": active_strategies,
            "markets_tracked": markets_tracked,
            "best_strategies": best,
            "failure_patterns": patterns,
            "market_profiles": len(self.data.get("market_profiles", {})),
            "simulations": len(self.data.get("simulation_results", [])),
            "memory_size": len(json.dumps(self.data)),
        }
    # ── Adaptive Self-Tuning ────────────────────────────

    def _adaptive_tuning(self):
        """Self-adjust parameters based on recent performance."""
        recent = self.data["trades"][-20:]
        if len(recent) < 10:
            return {}

        wins = sum(1 for t in recent if t["profit"] > 0)
        total = len(recent)
        win_rate = wins / total
        total_profit = sum(t["profit"] for t in recent)

        adjustments = {}

        if win_rate < 0.40:
            adjustments["min_edge_boost"] = 0.02
            adjustments["confidence_boost"] = 0.05
            adjustments["stake_reduction"] = 0.8
        elif win_rate > 0.55:
            adjustments["min_edge_boost"] = -0.005
            adjustments["confidence_boost"] = -0.02
            adjustments["stake_reduction"] = 1.1

        if total_profit < -5:
            adjustments["stake_reduction"] = min(adjustments.get("stake_reduction", 1.0), 0.7)
            adjustments["cooldown_boost"] = 30

        if total_profit > 5 and win_rate > 0.50:
            adjustments["stake_reduction"] = max(adjustments.get("stake_reduction", 1.0), 0.95)

        return adjustments

    def get_adjustments(self):
        """Get current adaptive adjustments."""
        if not hasattr(self, '_cached_adjustments'):
            self._cached_adjustments = {}
            self._adjustment_counter = 0
        self._adjustment_counter += 1
        if self._adjustment_counter % 10 == 0:
            self._cached_adjustments = self._adaptive_tuning()
        return self._cached_adjustments

    # ================================================================
    # CONTINUOUS EVOLUTION ENGINE
    # AMIOS Agent #16: Continuous Evolution Engine
    # Observe -> Find weakness -> Create improvement -> Simulate
    # -> Compare -> Deploy if better
    # ================================================================

    def evolve(self, current_strategies=None):
        """
        Continuous evolution loop:
        1. Observe recent performance
        2. Find weaknesses
        3. Create improvement hypothesis
        4. Compare against current
        5. Return improvement recommendations

        Never changes a working system without evidence.
        """
        recent = self.data["trades"][-30:] if self.data["trades"] else []
        if len(recent) < 5:
            return {"status": "INSUFFICIENT_DATA", "recommendations": []}

        recommendations = []
        weaknesses = self._find_weaknesses(recent)

        for weakness in weaknesses:
            improvement = self._create_improvement(weakness)
            if improvement:
                recommendations.append(improvement)

        # Self-assessment
        assessment = self._self_assess(recent)

        self.data["evolution_history"] = self.data.get("evolution_history", [])
        self.data["evolution_history"].append({
            "time": int(time.time() * 1000),
            "weaknesses_found": len(weaknesses),
            "recommendations": len(recommendations),
            "assessment": assessment,
        })
        if len(self.data["evolution_history"]) > 100:
            self.data["evolution_history"] = self.data["evolution_history"][-100:]

        self.save()

        return {
            "status": "EVOLVED" if recommendations else "STABLE",
            "assessment": assessment,
            "weaknesses": weaknesses,
            "recommendations": recommendations,
            "trades_analyzed": len(recent),
        }

    def _find_weaknesses(self, recent_trades):
        """Identify weaknesses in recent performance."""
        weaknesses = []

        # Weakness 1: Declining win rate
        if len(recent_trades) >= 10:
            first_half = recent_trades[:len(recent_trades)//2]
            second_half = recent_trades[len(recent_trades)//2:]
            wr1 = sum(1 for t in first_half if t["profit"] > 0) / len(first_half)
            wr2 = sum(1 for t in second_half if t["profit"] > 0) / len(second_half)
            if wr1 > 0.50 and wr2 < 0.40:
                weaknesses.append({
                    "type": "DECLINING_WIN_RATE",
                    "early_wr": round(wr1 * 100, 1),
                    "recent_wr": round(wr2 * 100, 1),
                    "severity": "HIGH",
                    "action": "ROTATE_MARKET_OR_STRATEGY",
                })

        # Weakness 2: Market underperformance
        market_pnl = {}
        for t in recent_trades:
            m = t.get("market", "unknown")
            if m not in market_pnl:
                market_pnl[m] = 0
            market_pnl[m] += t["profit"]

        for m, pnl in market_pnl.items():
            if pnl < -3 and len([t for t in recent_trades if t.get("market") == m]) >= 3:
                weaknesses.append({
                    "type": "MARKET_UNDERPERFORMANCE",
                    "market": m,
                    "pnl": round(pnl, 2),
                    "severity": "MEDIUM",
                    "action": "ROTATE_MARKET",
                })

        # Weakness 3: Contract type failure
        contract_pnl = {}
        for t in recent_trades:
            c = t.get("contract_type", "unknown")
            if c not in contract_pnl:
                contract_pnl[c] = {"pnl": 0, "trades": 0, "wins": 0}
            contract_pnl[c]["pnl"] += t["profit"]
            contract_pnl[c]["trades"] += 1
            if t["profit"] > 0:
                contract_pnl[c]["wins"] += 1

        for c, stats in contract_pnl.items():
            if stats["trades"] >= 3:
                wr = stats["wins"] / stats["trades"]
                if wr < 0.35:
                    weaknesses.append({
                        "type": "CONTRACT_FAILURE",
                        "contract": c,
                        "win_rate": round(wr * 100, 1),
                        "pnl": round(stats["pnl"], 2),
                        "severity": "HIGH",
                        "action": "ROTATE_CONTRACT",
                    })

        # Weakness 4: Stake sizing too aggressive
        total_loss = sum(t["profit"] for t in recent_trades if t["profit"] < 0)
        total_wins = sum(t["profit"] for t in recent_trades if t["profit"] > 0)
        if total_loss < -10 and abs(total_loss) > total_wins * 1.5:
            weaknesses.append({
                "type": "AGGRESSIVE_STAKING",
                "total_loss": round(total_loss, 2),
                "total_wins": round(total_wins, 2),
                "severity": "HIGH",
                "action": "REDUCE_STAKE",
            })

        # Weakness 5: Trading too frequently
        if len(recent_trades) >= 10:
            timestamps = [t.get("time", 0) for t in recent_trades[-10:]]
            if len(timestamps) >= 2:
                time_span = (timestamps[-1] - timestamps[0]) / 1000  # ms to seconds
                if time_span < 300:  # 10 trades in < 5 minutes
                    weaknesses.append({
                        "type": "OVERTRADING",
                        "trades_per_minute": round(10 / max(1, time_span / 60), 1),
                        "severity": "MEDIUM",
                        "action": "INCREASE_COOLDOWN",
                    })

        return weaknesses

    def _create_improvement(self, weakness):
        """Create an improvement hypothesis based on a weakness."""
        wtype = weakness.get("type", "")

        if wtype == "DECLINING_WIN_RATE":
            return {
                "weakness": wtype,
                "improvement": "Rotate to different market/strategy family",
                "expected_impact": "Restore win rate above 50%",
                "confidence": 0.7,
                "apply_when": "current_market is underperforming",
            }

        if wtype == "MARKET_UNDERPERFORMANCE":
            return {
                "weakness": wtype,
                "improvement": f"Reduce exposure to {weakness.get('market', '?')}, rotate to higher-karma market",
                "expected_impact": "Stop bleeding on underperforming market",
                "confidence": 0.8,
                "apply_when": f"market={weakness.get('market')}",
            }

        if wtype == "CONTRACT_FAILURE":
            return {
                "weakness": wtype,
                "improvement": f"Rotate away from {weakness.get('contract', '?')} to different contract family",
                "expected_impact": "Avoid repeated losses on failing contract type",
                "confidence": 0.75,
                "apply_when": f"contract={weakness.get('contract')}",
            }

        if wtype == "AGGRESSIVE_STAKING":
            return {
                "weakness": wtype,
                "improvement": "Reduce stake multiplier by 30%, increase cooldown between trades",
                "expected_impact": "Reduce loss severity while maintaining upside",
                "confidence": 0.65,
                "apply_when": "total_loss > total_wins * 1.5",
            }

        if wtype == "OVERTRADING":
            return {
                "weakness": wtype,
                "improvement": "Increase minimum rest between trades from 15s to 60s",
                "expected_impact": "More deliberate, higher-quality trade selection",
                "confidence": 0.6,
                "apply_when": "trades_per_minute > 10",
            }

        return None

    def _self_assess(self, recent_trades):
        """Self-assessment of system performance."""
        if not recent_trades:
            return {"grade": "N/A", "score": 0}

        total_pnl = sum(t["profit"] for t in recent_trades)
        wins = sum(1 for t in recent_trades if t["profit"] > 0)
        total = len(recent_trades)
        win_rate = wins / total if total > 0 else 0

        # Score: -100 to +100
        score = 0
        score += win_rate * 50  # up to 50 points for win rate
        score += min(25, max(-25, total_pnl * 2))  # up to 25 points for PnL
        if total >= 10:
            score += 10  # bonus for sufficient sample
        if win_rate < 0.35:
            score -= 20  # penalty for poor performance

        score = max(-100, min(100, score))

        if score > 30:
            grade = "STRONG"
        elif score > 10:
            grade = "HEALTHY"
        elif score > -10:
            grade = "UNCERTAIN"
        elif score > -30:
            grade = "WEAK"
        else:
            grade = "FAILING"

        return {
            "grade": grade,
            "score": round(score, 1),
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "trades_analyzed": total,
        }

    def get_evolution_history(self, limit=10):
        """Get recent evolution events."""
        return self.data.get("evolution_history", [])[-limit:]

    # ═══════════════════════════════════════════════════════
    # MODEL DECISION FEEDBACK LOOP
    # ═══════════════════════════════════════════════════════

    def record_model_decision(self, decision, market, strategy, reason, context=None):
        """Record an AI model decision (approve/block) for later feedback."""
        entry = {
            "decision": decision,  # "approve" or "block"
            "market": market,
            "strategy": strategy,
            "reason": reason,
            "context": context or {},
            "time": int(time.time() * 1000),
            "outcome": None,  # filled in after trade result
        }
        self.data["model_decisions"].append(entry)
        if len(self.data["model_decisions"]) > 200:
            self.data["model_decisions"] = self.data["model_decisions"][-200:]
        # Update counts
        if decision == "approve":
            self.data["ai_feedback"]["approved"] += 1
        elif decision == "block":
            self.data["ai_feedback"]["blocked"] += 1
        self.save()

    def record_model_feedback(self, profit, market, strategy):
        """After trade completes, check if AI decision was correct."""
        # Find most recent approve decision for this market/strategy
        fb = self.data["ai_feedback"]
        recent = fb.get("recent", [])

        # Match the last approve decision
        matched = None
        for d in reversed(self.data["model_decisions"]):
            if d["decision"] == "approve" and d["outcome"] is None:
                if d["market"] == market or d["strategy"] == strategy:
                    matched = d
                    break

        if matched:
            correct = (profit > 0)
            matched["outcome"] = "correct" if correct else "incorrect"
            matched["profit"] = profit
            matched["feedback_time"] = int(time.time() * 1000)
            if correct:
                fb["correct"] += 1
            else:
                fb["incorrect"] += 1

            fb["recent"].append({
                "market": market,
                "strategy": strategy,
                "profit": profit,
                "correct": correct,
                "time": int(time.time() * 1000),
            })
            if len(fb["recent"]) > 50:
                fb["recent"] = fb["recent"][-50:]
            self.save()
            return correct
        return None

    def get_model_accuracy(self):
        """Get AI model decision accuracy."""
        fb = self.data.get("ai_feedback", {})
        approved = fb.get("approved", 0)
        correct = fb.get("correct", 0)
        total_evaluated = correct + fb.get("incorrect", 0)
        accuracy = (correct / total_evaluated * 100) if total_evaluated > 0 else 0
        return {
            "approved": approved,
            "blocked": fb.get("blocked", 0),
            "correct": correct,
            "incorrect": fb.get("incorrect", 0),
            "accuracy_pct": round(accuracy, 1),
            "total_evaluated": total_evaluated,
        }

    # ═══════════════════════════════════════════════════════
    # SESSION MEMORY — carry across restarts
    # ═══════════════════════════════════════════════════════

    def record_session(self, session_id, pnl, balance, trades, wins, losses,
                       best_market, best_strategy, mode, duration_seconds):
        """Persist session summary so new sessions start with knowledge."""
        entry = {
            "session_id": session_id,
            "pnl": round(pnl, 4),
            "balance": round(balance, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
            "best_market": best_market,
            "best_strategy": best_strategy,
            "mode": mode,
            "duration_sec": round(duration_seconds, 1),
            "time": int(time.time() * 1000),
        }
        self.data["session_memory"].append(entry)
        # Keep last 50 sessions
        if len(self.data["session_memory"]) > 50:
            self.data["session_memory"] = self.data["session_memory"][-50:]
        self.save()

    def get_session_insights(self, limit=10):
        """Get insights from past sessions to avoid repeating mistakes."""
        sessions = self.data.get("session_memory", [])[-limit:]
        if not sessions:
            return None

        total_pnl = sum(s["pnl"] for s in sessions)
        avg_wr = sum(s["win_rate"] for s in sessions) / len(sessions)
        best_markets = {}
        for s in sessions:
            m = s.get("best_market", "")
            if m:
                best_markets[m] = best_markets.get(m, 0) + 1
        top_market = max(best_markets, key=best_markets.get) if best_markets else None

        return {
            "sessions": len(sessions),
            "total_pnl": round(total_pnl, 4),
            "avg_win_rate": round(avg_wr, 1),
            "best_market": top_market,
            "last_mode": sessions[-1].get("mode", "?") if sessions else "?",
        }

    # ═══════════════════════════════════════════════════════
    # STRATEGY FAMILIES — aggregate family-level stats
    # ═══════════════════════════════════════════════════════

    def update_strategy_families(self):
        """Aggregate strategy stats by family (DIGIT_DIFF, DIGIT_MATCH, etc.)."""
        families = {}
        for key, strat in self.data.get("strategies", {}).items():
            if strat.get("status") == "RETIRED":
                continue
            strat_name = strat.get("strategy", "")
            # Extract family: "DIGIT_DIFF_5" -> "DIGIT_DIFF", "DIGIT_MATCH_3" -> "DIGIT_MATCH"
            parts = strat_name.split("_")
            if len(parts) >= 2:
                family = "_".join(parts[:2])
            else:
                family = strat_name

            if family not in families:
                families[family] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "total_profit": 0, "strategies": 0,
                }
            f = families[family]
            f["trades"] += strat.get("trades", 0)
            f["wins"] += strat.get("wins", 0)
            f["losses"] += strat.get("losses", 0)
            f["total_profit"] = round(f["total_profit"] + strat.get("total_profit", 0), 4)
            f["strategies"] += 1

        # Calculate family win rates
        for family, f in families.items():
            total = f["wins"] + f["losses"]
            f["win_rate"] = round(f["wins"] / total * 100, 1) if total > 0 else 0
            f["total_profit"] = round(f["total_profit"], 4)

        self.data["strategy_families"] = families
        self.save()
        return families

    def get_family_insights(self):
        """Get which strategy families are performing best."""
        families = self.data.get("strategy_families", {})
        if not families:
            self.update_strategy_families()
            families = self.data.get("strategy_families", {})

        ranked = sorted(
            families.items(),
            key=lambda x: (x[1].get("total_profit", 0), x[1].get("win_rate", 0)),
            reverse=True,
        )
        return [{"family": k, **v} for k, v in ranked]

    # ═══════════════════════════════════════════════════════
    # DAILY DRAWDOWN TRACKING
    # ═══════════════════════════════════════════════════════

    def record_daily_drawdown(self, pnl, balance, daily_limit=10.0):
        """Track daily drawdown and return if limit breached."""
        today = time.strftime("%Y-%m-%d")
        if today not in self.data["daily_drawdown"]:
            self.data["daily_drawdown"][today] = {
                "start_balance": balance,
                "min_balance": balance,
                "max_loss": 0,
                "trades": 0,
                "breached": False,
            }

        dd = self.data["daily_drawdown"][today]
        dd["trades"] += 1
        dd["min_balance"] = min(dd["min_balance"], balance)
        dd["max_loss"] = round(dd["start_balance"] - dd["min_balance"], 4)

        if dd["max_loss"] >= daily_limit and not dd["breached"]:
            dd["breached"] = True
            dd["breach_time"] = int(time.time() * 1000)
            self.save()
            return True  # breached

        self.save()
        return False  # ok

    def is_daily_limit_breached(self):
        """Check if today's daily drawdown limit is already breached."""
        today = time.strftime("%Y-%m-%d")
        dd = self.data["daily_drawdown"].get(today, {})
        return dd.get("breached", False)

    def get_daily_stats(self):
        """Get today's trading stats."""
        today = time.strftime("%Y-%m-%d")
        return self.data["daily_drawdown"].get(today, {})

    # ═══════════════════════════════════════════════════════
    # PERSISTENT KNOWLEDGE — consolidate all knowledge
    # ═══════════════════════════════════════════════════════

    def save_knowledge_snapshot(self, brain_notes=None, model_log=None):
        """Save a full knowledge snapshot so system restarts with context."""
        snapshot = {
            "time": int(time.time() * 1000),
            "model_accuracy": self.get_model_accuracy(),
            "session_insights": self.get_session_insights(),
            "family_insights": self.get_family_insights()[:5],
            "daily_stats": self.get_daily_stats(),
            "brain_notes": (brain_notes or [])[-10:],
            "model_log": (model_log or [])[-10:],
        }
        self.data["knowledge_snapshot"] = snapshot
        self.save()

    def get_knowledge_snapshot(self):
        """Get latest knowledge snapshot for system restart."""
        return self.data.get("knowledge_snapshot", {})
