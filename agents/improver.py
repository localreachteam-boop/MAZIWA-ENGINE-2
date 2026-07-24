"""
IMPROVER — Self-improvement & Research (merged)
Combines: ResearchDirector + SelfImprover
Closes the loop: analyze → propose → improve → retire
"""
import json
import time
import random
import re
from pathlib import Path
EXPERIMENT_LOG = Path(__file__).parent.parent / "experiment_log.json"

EXPERIMENT_TYPES = {
    "strategy_test": {
        "description": "Test a strategy on a market",
        "min_trades": 15,
        "budget_trades": 30,
    },
    "market_probe": {
        "description": "Explore a new market with existing strategies",
        "min_trades": 10,
        "budget_trades": 20,
    },
    "parameter_sweep": {
        "description": "Test different parameters on a declining strategy",
        "min_trades": 10,
        "budget_trades": 25,
    },
    "stress_test": {
        "description": "Stress test a strategy under harsh conditions",
        "min_trades": 20,
        "budget_trades": 40,
    },
}


WEIGHTS = {
    "knowledge_gap": 0.3,
    "potential_edge": 0.4,
    "cost": -0.1,
    "novelty": 0.2,
    "urgency": 0.1,
}

IMPROVEMENT_LOG = Path(__file__).parent.parent / "improvement_log.json"
class ResearchDirector:
    """
    Manages the experiment pipeline.
    Decides what to test, when to stop, what to archive.
    """

    def __init__(self):
        self.experiments = {}         # id -> experiment dict
        self.completed = []           # finished experiments
        self.archive = []             # retired experiments
        self.next_id = 1
        self.priority_queue = []      # sorted experiment IDs
        self.active_experiments = []  # currently running (max 3)
        self.max_active = 2
        self.last_proposal_time = 0
        self.proposal_interval = 300  # 5 min between experiments (was 120)
        self._load()

    def _load(self):
        """Load experiment history from disk."""
        if EXPERIMENT_LOG.exists():
            try:
                data = json.loads(EXPERIMENT_LOG.read_text())
                self.experiments = data.get("experiments", {})
                self.completed = data.get("completed", [])
                self.archive = data.get("archive", [])
                self.next_id = data.get("next_id", len(self.experiments) + 1)
            except Exception:
                pass

    def _save(self):
        """Persist experiment data."""
        data = {
            "experiments": self.experiments,
            "completed": self.completed,
            "archive": self.archive,
            "next_id": self.next_id,
        }
        try:
            EXPERIMENT_LOG.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass

    def cleanup_stale(self, max_age_hours=3600):
        """Complete experiments stuck for too long."""
        now = time.time()
        for eid in list(self.active_experiments):
            exp = self.experiments.get(eid)
            if not exp: continue
            age_hours = (now - exp.get('created_at', now)) / 3600
            trades = exp.get('trades_taken', 0)
            if age_hours > max_age_hours or (trades >= 5 and age_hours > 2):
                wr = exp.get('wins', 0) / max(trades, 1)
                ev = exp.get('total_pnl', 0) / max(trades, 1)
                if ev > 0.01:
                    exp['conclusion'] = f'COMPLETED: WR={wr:.0%} EV={ev:.4f}'
                else:
                    exp['conclusion'] = f'STALE: WR={wr:.0%} trades={trades}'
                exp['status'] = 'completed'
                exp['completed_at'] = now
                self.completed.append(exp)
                self.active_experiments.remove(eid)
        self._save()

    def propose_experiment(self, context):
        """
        Propose a new experiment based on current knowledge gaps.
        
        context = {
            "known_strategies": [...],
            "known_markets": [...],
            "strategy_performance": {strategy: {trades, wins, losses, ev}},
            "market_conditions": {market: regime},
            "active_experiments": [...],
        }
        Returns: experiment dict or None
        """
        now = time.time()
        if now - self.last_proposal_time < self.proposal_interval:
            return None

        if len(self.active_experiments) >= self.max_active:
            return None

        known_strategies = context.get("known_strategies", [])
        known_markets = context.get("known_markets", [])
        performance = context.get("strategy_performance", {})
        active_ids = set(self.active_experiments)

        # Find knowledge gaps — but skip if we already have an active or completed experiment for this combo
        gaps = self._find_knowledge_gaps(known_strategies, known_markets, performance)

        # Find strategies with declining performance (need investigation)
        declining = self._find_declining(performance)

        # Find untested combinations
        untested = self._find_untested_combos(known_strategies, known_markets, performance)

        # Build deduplication set: market+strategy combos already explored or running
        _active_combos = set()
        for _eid in self.active_experiments:
            _e = self.experiments.get(_eid, {})
            _active_combos.add(f"{_e.get('market','')}:{_e.get('strategy','')}")
        for _cid in self.completed:
            if isinstance(_cid, dict):
                _active_combos.add(f"{_cid.get('market','')}:{_cid.get('strategy','')}")

        # Build candidate experiments — skip duplicates
        candidates = []

        for gap in gaps:
            combo_key = f"{gap['market']}:{gap.get('strategy','')}"
            if combo_key in _active_combos:
                continue  # already running or completed — skip
            exp = self._create_experiment("market_probe", gap["market"], gap.get("strategy"), {
                "reason": f"Knowledge gap: {gap['reason']}",
                "knowledge_gap_score": gap["score"],
            })
            candidates.append(exp)

        for decl in declining:
            combo_key = f"{decl['market']}:{decl.get('strategy','')}"
            if combo_key in _active_combos:
                continue
            exp = self._create_experiment("parameter_sweep", decl["market"], decl["strategy"], {
                "reason": f"Declining performance: {decl['reason']}",
                "urgency": decl.get("urgency", 2.0),
            })
            candidates.append(exp)

        for combo in untested[:3]:
            combo_key = f"{combo['market']}:{combo['strategy']}"
            if combo_key in _active_combos:
                continue
            exp = self._create_experiment("strategy_test", combo["market"], combo["strategy"], {
                "reason": f"Untested combo: {combo['strategy']} × {combo['market']}",
                "novelty": 2.0,
            })
            candidates.append(exp)

        # Stress test any strategy with high win rate but low EV
        for strat, perf in performance.items():
            if perf.get("trades", 0) > 30:
                wr = perf.get("wins", 0) / max(perf.get("trades", 1), 1)
                ev = perf.get("ev", 0)
                if wr > 0.6 and ev < 0.01:
                    exp = self._create_experiment("stress_test", perf.get("market", ""), strat, {
                        "reason": f"High WR ({wr:.0%}) but low EV ({ev:.4f}) — investigate",
                    })
                    candidates.append(exp)

        if not candidates:
            return None

        # Score and pick best
        best = max(candidates, key=lambda e: e["priority_score"])
        self.experiments[str(self.next_id)] = best
        self.next_id += 1
        self.active_experiments.append(best["id"])
        self.last_proposal_time = now
        self._save()

        return best

    def _find_knowledge_gaps(self, strategies, markets, performance):
        """Find markets or strategies we know too little about."""
        gaps = []
        for market in markets:
            for strategy in strategies:
                key = f"{market}:{strategy}"
                perf = performance.get(key, {})
                trades = perf.get("trades", 0)
                if trades < 10:
                    gaps.append({
                        "market": market,
                        "strategy": strategy,
                        "reason": f"Only {trades} trades recorded",
                        "score": max(0, 3.0 - trades * 0.3),
                    })
        return sorted(gaps, key=lambda g: g["score"], reverse=True)[:3]

    def _find_declining(self, performance):
        """Find strategies whose recent performance is declining."""
        declining = []
        for key, perf in performance.items():
            if ":" in key:
                market, strategy = key.split(":", 1)
            else:
                market, strategy = "", key

            recent_wr = perf.get("recent_win_rate", perf.get("win_rate", 0))
            overall_wr = perf.get("win_rate", 0)

            if perf.get("trades", 0) > 15 and overall_wr > 0 and recent_wr < overall_wr * 0.8:
                gap = overall_wr - recent_wr
                declining.append({
                    "market": market,
                    "strategy": strategy,
                    "reason": f"WR dropped {overall_wr:.0%}→{recent_wr:.0%}",
                    "urgency": min(3.0, gap * 5 + 1),
                })
        return sorted(declining, key=lambda d: d["urgency"], reverse=True)[:2]

    def _find_untested_combos(self, strategies, markets, performance):
        """Find strategy×market combos that have never been tried."""
        untested = []
        for market in markets[:5]:
            for strategy in strategies[:5]:
                key = f"{market}:{strategy}"
                if key not in performance or performance[key].get("trades", 0) == 0:
                    untested.append({"market": market, "strategy": strategy})
        random.shuffle(untested)
        return untested

    def _create_experiment(self, exp_type, market, strategy, extras=None):
        """Create a new experiment entry."""
        cfg = EXPERIMENT_TYPES.get(exp_type, EXPERIMENT_TYPES["strategy_test"])
        exp = {
            "id": str(self.next_id),
            "type": exp_type,
            "market": market,
            "strategy": strategy,
            "description": cfg["description"],
            "min_trades": cfg["min_trades"],
            "budget_trades": cfg["budget_trades"],
            "trades_taken": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "priority_score": 0,
            "conclusion": None,
        }
        if extras:
            for k, v in extras.items():
                exp[k] = v

        # Score priority
        exp["priority_score"] = self._score_priority(exp, extras or {})
        return exp

    def _score_priority(self, experiment, extras):
        """Calculate experiment priority score."""
        score = 0.0
        score += extras.get("knowledge_gap_score", 0) * WEIGHTS["knowledge_gap"]
        score += extras.get("potential_edge", 1.0) * WEIGHTS["potential_edge"]
        score += extras.get("cost", 0.5) * WEIGHTS["cost"]
        score += extras.get("novelty", 1.0) * WEIGHTS["novelty"]
        score += extras.get("urgency", 1.0) * WEIGHTS["urgency"]
        # Boost experiments for markets with high weight
        if experiment.get("market", "").startswith("R_75"):
            score += 0.5
        return round(score, 3)

    def record_trade(self, experiment_id, win, pnl):
        """Record a trade result for an experiment."""
        exp = self.experiments.get(str(experiment_id))
        if not exp:
            return

        exp["trades_taken"] += 1
        if win:
            exp["wins"] += 1
        else:
            exp["losses"] += 1
        exp["total_pnl"] += pnl
        exp["updated_at"] = time.time()

        # Check completion criteria
        if exp["trades_taken"] >= exp["budget_trades"]:
            self._complete_experiment(experiment_id)
        elif exp["trades_taken"] >= exp["min_trades"]:
            # Check if conclusion is obvious
            wr = exp["wins"] / max(exp["trades_taken"], 1)
            ev = exp["total_pnl"] / max(exp["trades_taken"], 1)
            if wr < 0.3 or ev < -0.02:
                # Clearly failing — early stop
                exp["conclusion"] = f"FAILED: WR={wr:.0%} EV={ev:.4f}"
                self._complete_experiment(experiment_id)
            elif wr > 0.7 and ev > 0.03:
                # Clearly winning — promote early
                exp["conclusion"] = f"PROMOTED: WR={wr:.0%} EV={ev:.4f}"
                self._complete_experiment(experiment_id)

        self._save()

    def _complete_experiment(self, experiment_id):
        """Move experiment to completed."""
        exp = self.experiments.get(str(experiment_id))
        if not exp or exp["status"] != "running":
            return
        exp["status"] = "completed"
        exp["completed_at"] = time.time()

        wr = exp["wins"] / max(exp["trades_taken"], 1)
        ev = exp["total_pnl"] / max(exp["trades_taken"], 1)

        if not exp.get("conclusion"):
            if ev > 0.02:
                exp["conclusion"] = f"SUCCESS: WR={wr:.0%} EV={ev:.4f}"
            elif ev < -0.02:
                exp["conclusion"] = f"FAILED: WR={wr:.0%} EV={ev:.4f}"
            else:
                exp["conclusion"] = f"NEUTRAL: WR={wr:.0%} EV={ev:.4f}"

        self.completed.append(exp)
        if experiment_id in self.active_experiments:
            self.active_experiments.remove(experiment_id)

        # Archive failures
        if ev < -0.05:
            exp["status"] = "archived"
            self.archive.append(exp)

    def get_status(self):
        """Get current research status for dashboard."""
        active = []
        for eid in self.active_experiments:
            exp = self.experiments.get(eid, {})
            if exp.get("status") == "running":
                wr = exp["wins"] / max(exp["trades_taken"], 1) * 100
                active.append({
                    "id": exp["id"],
                    "type": exp["type"],
                    "market": exp.get("market", ""),
                    "strategy": exp.get("strategy", ""),
                    "trades": exp["trades_taken"],
                    "budget": exp["budget_trades"],
                    "wr": round(wr, 1),
                    "pnl": round(exp["total_pnl"], 4),
                })

        recent_completed = self.completed[-5:] if self.completed else []
        return {
            "active_count": len(active),
            "active": active,
            "completed_count": len(self.completed),
            "archived_count": len(self.archive),
            "recent_completed": [
                {
                    "id": e["id"],
                    "conclusion": e.get("conclusion", "?"),
                    "market": e.get("market", ""),
                    "strategy": e.get("strategy", ""),
                    "trades": e.get("trades_taken", 0),
                }
                for e in recent_completed
            ],
        }


class SelfImprover:
    """
    Closes the cognitive loop by connecting evaluation back to planning.
    """

    def __init__(self):
        self.improvements = []
        self.last_improvement_cycle = 0
        self.improvement_interval = 25  # every 25 trades
        self.total_improvements = 0
        self.successful_improvements = 0
        self._load()

    def _load(self):
        if IMPROVEMENT_LOG.exists():
            try:
                data = json.loads(IMPROVEMENT_LOG.read_text())
                self.improvements = data.get("improvements", [])
                self.total_improvements = data.get("total", 0)
                self.successful_improvements = data.get("successful", 0)
            except:
                pass

    def _save(self):
        try:
            IMPROVEMENT_LOG.write_text(json.dumps({
                "improvements": self.improvements[-100:],
                "total": self.total_improvements,
                "successful": self.successful_improvements,
            }, indent=2, default=str))
        except:
            pass

    def should_run(self, total_trades):
        """Check if it's time to run improvement cycle."""
        return (total_trades > 0 and
                total_trades % self.improvement_interval == 0 and
                total_trades != self.last_improvement_cycle)

    def run_improvement_cycle(self, memory, strategist, scorer, competition):
        """
        Full improvement cycle. Returns list of actions taken.
        """
        self.last_improvement_cycle = memory.data.get("trades", []).__len__() if "trades" in memory.data else 0
        actions = []

        # 1. Analyze: what strategies are working vs failing?
        strategies = memory.strategies
        winners = []
        losers = []
        for key, stats in strategies.items():
            trades = stats.get("trades", 0)
            if trades < 5:
                continue
            ev = stats.get("total_profit", 0) / trades
            wr = stats.get("wins", 0) / trades
            if ev > 0.01 and wr > 0.55:
                winners.append({"key": key, "ev": ev, "wr": wr, "trades": trades})
            elif ev < -0.01 or wr < 0.35:
                losers.append({"key": key, "ev": ev, "wr": wr, "trades": trades})

        # 2. Decision: what to do about each
        for w in sorted(winners, key=lambda x: x["ev"], reverse=True)[:3]:
            actions.append({
                "type": "PROMOTE",
                "strategy": w["key"],
                "reason": f"EV={w['ev']:.4f}, WR={w['wr']:.0%}, {w['trades']}T",
                "confidence": min(0.9, 0.5 + w["trades"] / 50),
                "time": int(time.time() * 1000),
            })

        for l in sorted(losers, key=lambda x: x["ev"])[:3]:
            action_type = "RETIRE" if l["trades"] >= 15 and l["ev"] < -0.02 else "DEPRIORITIZE"
            actions.append({
                "type": action_type,
                "strategy": l["key"],
                "reason": f"EV={l['ev']:.4f}, WR={l['wr']:.0%}, {l['trades']}T",
                "confidence": 0.8 if l["trades"] >= 15 else 0.5,
                "time": int(time.time() * 1000),
            })

        # 3. Apply: update strategist health scores
        for action in actions:
            key = action["strategy"]
            if action["type"] in ("PROMOTE",):
                if key in strategist.strategies:
                    s = strategist.strategies[key]
                    if s["status"] == strategist.HEALTH_HEALTHY:
                        s["status"] = strategist.HEALTH_STRONG
                        s["health_score"] = min(100, s["health_score"] + 10)
            elif action["type"] == "RETIRE":
                if key in strategist.strategies:
                    strategist.strategies[key]["status"] = strategist.HEALTH_RETIRE

        # 4. Log improvement
        cycle_record = {
            "cycle": self.total_improvements + 1,
            "trades_analyzed": sum(s.get("trades", 0) for s in strategies.values()),
            "winners_found": len(winners),
            "losers_found": len(losers),
            "actions": actions,
            "time": int(time.time() * 1000),
        }
        self.improvements.append(cycle_record)
        self.total_improvements += 1
        if any(a["type"] == "PROMOTE" for a in actions):
            self.successful_improvements += 1
        self._save()

        return actions

    def get_status(self):
        recent = self.improvements[-3:] if self.improvements else []
        return {
            "total_cycles": self.total_improvements,
            "successful": self.successful_improvements,
            "last_cycle": self.last_improvement_cycle,
            "recent_actions": [
                {"type": a["type"], "strategy": a["strategy"], "reason": a["reason"]}
                for imp in recent
                for a in imp.get("actions", [])
            ][-10:],
        }
