"""
OPTIMIZER — Strategy Ranking, Competition & Efficiency (merged)
Combines: CompetitionEngine + BotScorer + EfficiencyAgent
"""
from collections import defaultdict
import os
import json
import time
import math
from pathlib import Path
COMPETITION_LOG = Path(__file__).parent.parent / "competition_log.json"

class CompetitionEngine:
    """
    Manages strategy head-to-head competitions.
    
    Each competition round:
    - Picks N strategies for a given market
    - Tracks their performance side by side
    - After enough data, declares a winner
    - Updates the global strategy ranking
    """

    def __init__(self):
        self.rounds = {}           # round_id -> round dict
        self.rankings = {}         # market -> [strategy_rankings]
        self.history = []          # completed rounds
        self.next_round_id = 1
        self.min_trades_per_strat = 10
        self.max_rounds = 20
        self.round_timeout = 600   # seconds before auto-complete
        self.active_rounds = []    # currently running round IDs
        self.max_active_rounds = 2
        self._load()

    def _load(self):
        """Load competition history from disk."""
        if COMPETITION_LOG.exists():
            try:
                data = json.loads(COMPETITION_LOG.read_text())
                self.rounds = data.get("rounds", {})
                self.rankings = data.get("rankings", {})
                self.history = data.get("history", [])
                self.next_round_id = data.get("next_round_id", 1)
                self.active_rounds = data.get("active_rounds", [])
            except Exception:
                pass

    def _save(self):
        """Persist competition data."""
        data = {
            "rounds": self.rounds,
            "rankings": self.rankings,
            "history": self.history,
            "next_round_id": self.next_round_id,
            "active_rounds": self.active_rounds,
        }
        try:
            COMPETITION_LOG.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass

    def start_competition(self, market, strategies, context=None):
        """
        Start a new competition round.
        
        market: e.g. "R_75"
        strategies: list of strategy names to compete
        context: optional market/regime info
        
        Returns: round dict
        """
        if len(self.active_rounds) >= self.max_active_rounds:
            return None

        if len(strategies) < 2:
            return None

        # Check we don't already have this market competing
        for rid in self.active_rounds:
            r = self.rounds.get(rid, {})
            if r.get("market") == market and r.get("status") == "running":
                return None

        round_id = str(self.next_round_id)
        self.next_round_id += 1

        competitors = {}
        for strat in strategies:
            competitors[strat] = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "pnl_history": [],
                "score": 0.0,
            }

        competition_round = {
            "id": round_id,
            "market": market,
            "strategies": strategies,
            "competitors": competitors,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "completed_at": None,
            "winner": None,
            "regime": context.get("regime", "UNKNOWN") if context else "UNKNOWN",
            "total_trades": 0,
        }

        self.rounds[round_id] = competition_round
        self.active_rounds.append(round_id)
        self._save()
        return competition_round

    def record_trade(self, round_id, strategy, win, pnl):
        """Record a trade result for a strategy in a competition round."""
        rnd = self.rounds.get(str(round_id))
        if not rnd or rnd["status"] != "running":
            return

        competitors = rnd.get("competitors", {})
        if strategy not in competitors:
            return

        comp = competitors[strategy]
        comp["trades"] += 1
        if win:
            comp["wins"] += 1
        else:
            comp["losses"] += 1
        comp["total_pnl"] += pnl
        comp["pnl_history"].append(pnl)

        # Keep only last 50 PnL entries
        if len(comp["pnl_history"]) > 50:
            comp["pnl_history"] = comp["pnl_history"][-50:]

        comp["score"] = self._calc_score(comp)
        rnd["total_trades"] += 1
        rnd["updated_at"] = time.time()

        # Check if round should complete
        self._check_round_completion(round_id)
        self._save()

    def _calc_score(self, comp):
        """
        Calculate competition score for a competitor.
        
        Score = (win_rate × 0.3) + (profit_factor × 0.3) + (ev × 0.2) + (consistency × 0.2)
        """
        trades = comp.get("trades", 0)
        if trades < 3:
            return 0.0

        wins = comp.get("wins", 0)
        pnl = comp.get("total_pnl", 0.0)
        history = comp.get("pnl_history", [])

        # Win rate component
        wr = wins / trades

        # Profit factor
        gross_profit = sum(p for p in history if p > 0)
        gross_loss = abs(sum(p for p in history if p < 0))
        pf = gross_profit / max(gross_loss, 0.001)

        # Expected value per trade
        ev = pnl / trades

        # Consistency (lower variance = better)
        if len(history) >= 5:
            mean_pnl = sum(history) / len(history)
            variance = sum((p - mean_pnl) ** 2 for p in history) / len(history)
            std_dev = math.sqrt(variance)
            consistency = 1.0 / (1.0 + std_dev * 10)
        else:
            consistency = 0.5

        score = (wr * 0.3) + (min(pf, 3.0) / 3.0 * 0.3) + (min(max(ev, -1), 1) * 0.2 + 0.5 * 0.2) + (consistency * 0.2)
        return round(score, 4)

    def _check_round_completion(self, round_id):
        """Check if a round should be auto-completed."""
        rnd = self.rounds.get(str(round_id))
        if not rnd or rnd["status"] != "running":
            return

        now = time.time()

        # Timeout check
        if now - rnd["created_at"] > self.round_timeout:
            self._complete_round(round_id)
            return

        # All competitors have enough data
        all_ready = all(
            comp["trades"] >= self.min_trades_per_strat
            for comp in rnd.get("competitors", {}).values()
        )
        if all_ready:
            self._complete_round(round_id)

    def _complete_round(self, round_id):
        """Complete a competition round and determine winner."""
        rnd = self.rounds.get(str(round_id))
        if not rnd or rnd["status"] != "running":
            return

        rnd["status"] = "completed"
        rnd["completed_at"] = time.time()

        # Sort competitors by score
        competitors = rnd.get("competitors", {})
        ranked = sorted(competitors.items(), key=lambda x: x[1].get("score", 0), reverse=True)

        if ranked:
            rnd["winner"] = ranked[0][0]
            rnd["ranking"] = [
                {
                    "strategy": name,
                    "score": comp.get("score", 0),
                    "trades": comp.get("trades", 0),
                    "wr": round(comp.get("wins", 0) / max(comp.get("trades", 1), 1) * 100, 1),
                    "pnl": round(comp.get("total_pnl", 0), 4),
                }
                for name, comp in ranked
            ]

            # Update global rankings for this market
            market = rnd.get("market", "")
            if market:
                self.rankings[market] = rnd["ranking"]

        # Move to history
        self.history.append({
            "id": rnd["id"],
            "market": rnd.get("market", ""),
            "winner": rnd.get("winner", ""),
            "regime": rnd.get("regime", ""),
            "strategies": rnd.get("strategies", []),
            "total_trades": rnd.get("total_trades", 0),
            "completed_at": rnd.get("completed_at"),
        })
        if round_id in self.active_rounds:
            self.active_rounds.remove(round_id)

    def pick_competitors(self, market, available_strategies, performance, count=4):
        """
        Select the best strategies to pit against each other.
        
        Strategy selection logic:
        - Include the current champion (highest EV)
        - Include 1-2 challengers (good but not best)
        - Include 1 dark horse (low trades but promising)
        """
        if len(available_strategies) < 2:
            return []

        scored = []
        for strat in available_strategies:
            key = f"{market}:{strat}"
            perf = performance.get(key, {})
            trades = perf.get("trades", 0)
            ev = perf.get("ev", 0)
            wr = perf.get("win_rate", 0)

            # Score for competition selection
            if trades < 5:
                select_score = 0.5  # dark horse bonus
            else:
                select_score = ev * 2 + wr * 0.5

            scored.append({
                "strategy": strat,
                "select_score": select_score,
                "trades": trades,
            })

        scored.sort(key=lambda x: x["select_score"], reverse=True)

        selected = []
        # Take top 2 (champion + strong challenger)
        for s in scored[:2]:
            selected.append(s["strategy"])

        # Add 1-2 mid-tier
        mid = [s for s in scored[2:5] if s["trades"] >= 3]
        if mid:
            selected.append(mid[0]["strategy"])
        if len(mid) > 1:
            selected.append(mid[1]["strategy"])

        # If not enough, pad from remaining
        if len(selected) < 3:
            for s in scored:
                if s["strategy"] not in selected:
                    selected.append(s["strategy"])
                    if len(selected) >= 3:
                        break

        return selected[:count]

    def get_rankings(self, market=None):
        """Get strategy rankings, optionally filtered by market."""
        if market:
            return self.rankings.get(market, [])
        return self.rankings

    def get_status(self):
        """Get competition status for dashboard."""
        active = []
        for rid in self.active_rounds:
            rnd = self.rounds.get(rid, {})
            if rnd.get("status") == "running":
                elapsed = time.time() - rnd.get("created_at", time.time())
                active.append({
                    "id": rnd["id"],
                    "market": rnd.get("market", ""),
                    "strategies": rnd.get("strategies", []),
                    "total_trades": rnd.get("total_trades", 0),
                    "regime": rnd.get("regime", ""),
                    "elapsed_sec": int(elapsed),
                })

        recent = self.history[-5:] if self.history else []

        return {
            "active_rounds": len(active),
            "active": active,
            "completed_rounds": len(self.history),
            "rankings": {
                m: r[:3] for m, r in self.rankings.items()
            },
            "recent_results": recent,
        }

    def get_champion(self, market):
        """Get the current champion strategy for a market."""
        rankings = self.rankings.get(market, [])
        if rankings:
            return rankings[0].get("strategy", None)
        return None


class BotScorer:
    """
    Ranks all strategies using a formal scoring formula.
    Generates failure analysis reports.
    """
    def __init__(self):
        self.total_trades = 0
        self.strategies = {}
        self.reports = []

    def get_status(self):
        return {
            "phase": "active",
            "total_trades": self.total_trades,
            "strategies_tracked": len(self.strategies),
            "connected": True
        }

    def score_strategy(self, name, trades=0, wins=0, losses=0, pnl=0.0, ev=0.0):
        """Score a strategy based on performance metrics."""
        wr = (wins / trades * 100) if trades > 0 else 0
        score = 0.0
        # Win rate component (40%)
        score += (wr / 100.0) * 0.4
        # EV component (30%)
        score += min(max(ev, -1.0), 1.0) * 0.3
        # PnL component (20%)
        score += (1.0 if pnl > 0 else -0.5 if pnl < -2 else 0) * 0.2
        # Experience component (10%)
        score += min(trades / 20.0, 1.0) * 0.1
        self.strategies[name] = {"score": round(score, 4), "trades": trades, "wr": wr, "pnl": pnl}
        return round(score, 4)

    def rank_all(self):
        ranked = sorted(self.strategies.items(), key=lambda x: x[1]['score'], reverse=True)
        return ranked

    def get_elite(self, top_n=3):
        ranked = self.rank_all()
        return [{'strategy': k, **v} for k, v in ranked[:top_n] if v.get('trades', 0) >= 5]

    def generate_failure_report(self, key, perf):
        wr = perf.get('wins', 0) / max(perf.get('trades', 1), 1)
        ev = perf.get('ev', 0)
        report = {'strategy': key, 'wr': wr, 'ev': ev, 'trades': perf.get('trades', 0), 'status': 'FAILING'}
        self.reports.append(report)
        return report

class EfficiencyAgent:
    """
    Monitors strategy value and kills waste.

    Key insight: 60% win rate can still lose money if reward/risk < 1.
    This agent catches that and kills bad strategies BEFORE they drain capital.
    """

    PHASE_EXPLORE = "EXPLORATION"
    PHASE_EVALUATE = "EVALUATION"
    PHASE_EXPLOIT = "EXPLOITATION"

    def __init__(self):
        self.phase = self.PHASE_EXPLORE
        self.phase_start = time.time()
        self.total_trades = 0
        self.min_explore_trades = 30
        self.min_eval_trades = 5
        self._load_history()

        # Strategy tracking
        self.strategy_stats = defaultdict(lambda: {
            'trades': 0, 'wins': 0, 'losses': 0,
            'total_pnl': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'reward_risk': 0.0, 'edge': 0.0, 'strategy_value': 0.0,
            'status': 'ACTIVE', 'killed_at': 0, 'kill_reason': '',
            'first_trade': 0, 'last_trade': 0,
        })

        # Contract tracking
        self.contract_stats = defaultdict(lambda: {
            'trades': 0, 'wins': 0, 'losses': 0,
            'total_pnl': 0.0, 'payout': 0.0,
            'reward_risk': 0.0, 'edge': 0.0,
        })

        # Resource accounting
        self.compute_used = 0
        self.api_calls = 0
        self.capital_risked = 0.0
        self.information_gained = 0.0
        self.profit_potential = 0.0

        # Kill log
        self.kills = []

    def _load_history(self):
        import json, os
        mem_file = os.path.join(os.path.dirname(__file__), '..', 'agent_memory.json')
        try:
            with open(mem_file) as f:
                mem = json.load(f)
            strat_data = mem.get('strategies', {})
            for key, stats in strat_data.items():
                # Key format: "market:strategy"
                parts = key.split(':', 1)
                strategy = parts[1] if len(parts) > 1 else key
                trades = stats.get('trades', 0)
                wins = stats.get('wins', 0)
                losses = stats.get('losses', 0)
                pnl = stats.get('total_profit', 0)
                ss = self.strategy_stats[strategy]
                ss['trades'] = trades
                ss['wins'] = wins
                ss['losses'] = losses
                ss['total_pnl'] = pnl
                self.total_trades += trades
                self._update_strategy_value(strategy)
                # Auto-kill based on historical data
                if trades >= 2 and wins == 0:
                    ss['status'] = 'KILLED'
                    ss['kill_reason'] = f"Historical: 0% WR after {trades} trades"
                elif trades >= 5 and pnl < -0.5:
                    ss['status'] = 'KILLED'
                    ss['kill_reason'] = f"Historical: Negative PnL ({pnl:+.2f}) after {trades} trades"
                elif trades >= 5 and wins/trades < 0.30:
                    ss['status'] = 'KILLED'
                    ss['kill_reason'] = f"Historical: Low WR ({wins}/{trades}) after 5+ trades"
        except Exception:
            pass

    def record_trade(self, strategy, contract, stake, profit, market):
        """Record a trade and update all metrics."""
        self.total_trades += 1
        self.capital_risked += stake

        # Strategy stats
        ss = self.strategy_stats[strategy]
        ss['trades'] += 1
        ss['total_pnl'] = round(ss['total_pnl'] + profit, 4)
        if ss['first_trade'] == 0:
            ss['first_trade'] = time.time()
        ss['last_trade'] = time.time()

        if profit > 0:
            ss['wins'] += 1
            ss['avg_win'] = round(ss['total_pnl'] / ss['wins'], 4) if ss['wins'] > 0 else 0
        else:
            ss['losses'] += 1
            ss['avg_loss'] = round(ss['total_pnl'] / ss['losses'], 4) if ss['losses'] > 0 else 0

        # Recalculate reward/risk
        if ss['wins'] > 0 and ss['losses'] > 0:
            avg_win = sum(1 for _ in range(ss['wins'])) and ss['total_pnl'] / ss['trades']
            # Better: track actual avg win and avg loss separately
            ss['reward_risk'] = round(abs(ss['avg_win'] / ss['avg_loss']), 3) if ss['avg_loss'] != 0 else 0

        # Contract stats
        cs = self.contract_stats[contract]
        cs['trades'] += 1
        cs['total_pnl'] = round(cs['total_pnl'] + profit, 4)
        if profit > 0:
            cs['wins'] += 1
        else:
            cs['losses'] += 1

        # Calculate strategy value
        self._update_strategy_value(strategy)

        return self.get_status()

    def _update_strategy_value(self, strategy):
        ss = self.strategy_stats[strategy]
        t = ss['trades']
        if t < 2:
            # Negative if already losing, neutral if no data
            ss['strategy_value'] = 0.0 if ss['total_pnl'] >= 0 else -0.2
            ss['edge'] = 0
            return

        wins = ss['wins']
        losses = ss['losses']
        wr = wins / t if t > 0 else 0
        pnl = ss['total_pnl']

        expected_profit = pnl / t
        learning_value = min(t / 50, 1.0) * 0.1
        resource_waste = max(0, -pnl) / max(1, t) * 0.5

        # FIX: Properly calculate reward/risk from actual win/loss amounts
        if wins > 0 and losses > 0:
            # We need actual avg win and avg loss, not estimates from total pnl
            # Use: avg_win = total_pnl * wr / (wr + (1-wr)*rr_approx) 
            # Better: just use the sign-based approach
            avg_win = (pnl / t) * (1 / max(wr, 0.01)) if wr > 0 else 0
            avg_loss = (pnl / t) * (1 / max(1-wr, 0.01)) if wr < 1 else 0
            if avg_loss != 0 and avg_win > 0:
                rr = abs(avg_win / avg_loss)
                ss['reward_risk'] = round(rr, 3)
                be_wr = 1 / (1 + rr) if rr > 0 else 0.5
                ss['edge'] = round(wr - be_wr, 4)
            else:
                ss['reward_risk'] = 0
                ss['edge'] = 0
        elif losses == 0 and wins > 0:
            # All wins — reward/risk is infinite, use edge based on WR
            ss['reward_risk'] = 999.0
            ss['edge'] = round(wr - 0.5, 4)  # edge over 50%
        else:
            ss['reward_risk'] = 0
            ss['edge'] = 0

        # Risk penalty: only for strategies with bad reward/risk AND negative pnl
        risk_penalty = 0
        if pnl < 0 and ss['reward_risk'] < 0.5:
            risk_penalty = 0.5

        sv = expected_profit + learning_value - resource_waste - risk_penalty
        ss['strategy_value'] = round(sv, 4)

    def evaluate_all(self):
        """Evaluate all strategies and kill/continue/promote."""
        decisions = []
        for strategy, ss in self.strategy_stats.items():
            if ss['status'] not in ('ACTIVE', 'TESTING'):
                continue
            if ss['trades'] < self.min_eval_trades:
                decisions.append((strategy, 'CONTINUE', f"Need {self.min_eval_trades - ss['trades']} more trades"))
                continue

            sv = ss['strategy_value']
            edge = ss['edge']
            rr = ss['reward_risk']
            t = ss['trades']
            wr = ss.get('win_rate', ss.get('wins', 0) / max(t, 1))

            if sv < -0.3 or edge < -0.10 or (ss['trades'] >= 5 and wr < 0.20):
                # KILL: strategy is wasting resources
                ss['status'] = 'KILLED'
                ss['killed_at'] = time.time()
                reason = f"SV={sv:.3f} edge={edge:.4f} RR={rr:.2f}x"
                ss['kill_reason'] = reason
                self.kills.append({
                    'strategy': strategy, 'reason': reason,
                    'trades': t, 'pnl': ss['total_pnl'],
                    'time': int(time.time() * 1000),
                })
                decisions.append((strategy, 'KILL', reason))
            elif sv > 0.1 and edge > 0:
                # PROMOTE: strategy is profitable
                decisions.append((strategy, 'PROMOTE', f"SV={sv:.3f} edge={edge:.4f}"))
            else:
                decisions.append((strategy, 'CONTINUE', f"SV={sv:.3f} edge={edge:.4f}"))

        return decisions

    # Hard blacklist — these strategies NEVER trade
    BLACKLISTED = {
        # ODD_BIAS and EVEN_BIAS are now main strategies (parity family)
    }

    def should_trade(self, strategy):
        if strategy in self.BLACKLISTED:
            return False, f"blacklisted: {strategy}"
        ss = self.strategy_stats.get(strategy)
        if ss is None:
            return True, "new_strategy"
        if ss['status'] == 'KILLED':
            return False, f"killed: {ss['kill_reason']}"
        # Auto-kill: 0% WR after 2+ trades
        if ss['trades'] >= 2 and ss['wins'] == 0:
            ss['status'] = 'KILLED'
            ss['kill_reason'] = f"0% WR after {ss['trades']} trades"
            return False, f"0% WR after {ss['trades']} trades"
        # Auto-kill: negative PnL after 5+ trades
        if ss['trades'] >= 5 and ss['total_pnl'] < -0.5:
            ss['status'] = 'KILLED'
            ss['kill_reason'] = f"Negative PnL ({ss['total_pnl']:+.2f}) after {ss['trades']} trades"
            return False, f"Negative PnL ({ss['total_pnl']:+.2f}) after {ss['trades']} trades"
        # Auto-kill: <30% WR after 5+ trades
        if ss['trades'] >= 5 and ss['wins'] / ss['trades'] < 0.30:
            ss['status'] = 'KILLED'
            ss['kill_reason'] = f"Low WR ({ss['wins']}/{ss['trades']}) after 5+ trades"
            return False, f"Low WR ({ss['wins']}/{ss['trades']})"
        if ss['trades'] >= self.min_eval_trades and ss['strategy_value'] < -0.5:
            return False, f"low_value: SV={ss['strategy_value']:.3f}"
        return True, "ok"

    def get_phase(self):
        """Determine current system phase."""
        if self.total_trades < self.min_explore_trades:
            self.phase = self.PHASE_EXPLORE
        elif self.total_trades < self.min_explore_trades * 3:
            self.phase = self.PHASE_EVALUATE
        else:
            self.phase = self.PHASE_EXPLOIT
        return self.phase

    def get_allocation(self):
        """Resource allocation based on phase."""
        phase = self.get_phase()
        if phase == self.PHASE_EXPLORE:
            return {'explore': 100, 'test': 0, 'exploit': 0}
        elif phase == self.PHASE_EVALUATE:
            return {'explore': 15, 'test': 85, 'exploit': 0}
        else:
            return {'explore': 5, 'test': 15, 'exploit': 80}

    def get_best_strategy(self):
        """Suggest the best strategy to use based on efficiency data."""
        best = None
        best_sv = -999
        for s, ss in self.strategy_stats.items():
            if ss['status'] == 'KILLED':
                continue
            if ss['trades'] >= 2 and ss['strategy_value'] > best_sv:
                best_sv = ss['strategy_value']
                best = s
        return best, best_sv

    def get_status(self):
        """Full status for dashboard."""
        phase = self.get_phase()
        alloc = self.get_allocation()

        # Strategy rankings
        rankings = []
        for s, ss in self.strategy_stats.items():
            rankings.append({
                'strategy': s,
                'level': 'A' if ss['strategy_value'] > 0.1 else ('B' if ss['strategy_value'] > -0.3 else 'C'),
                'score': round(ss['strategy_value'], 3),
                'wr': round(ss['wins'] / ss['trades'] * 100, 1) if ss['trades'] > 0 else 0,
                'pnl': round(ss['total_pnl'], 2),
                'trades': ss['trades'],
                'rr': round(ss['reward_risk'], 3),
                'edge': round(ss['edge'], 4),
                'status': ss['status'],
                'kill_reason': ss.get('kill_reason', ''),
            })
        rankings.sort(key=lambda x: -x['score'])

        return {
            'phase': phase,
            'phase_name': phase,
            'total_trades': self.total_trades,
            'allocation': alloc,
            'rankings': rankings,
            'kills': self.kills[-10:],
            'capital_risked': round(self.capital_risked, 2),
            'active_strategies': sum(1 for s in self.strategy_stats.values() if s['status'] == 'ACTIVE'),
            'killed_strategies': sum(1 for s in self.strategy_stats.values() if s['status'] == 'KILLED'),
        }
