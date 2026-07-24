"""
SUPERVISOR AGENT — Performance accountability, does NOT trade.

"You are a performance supervisor. Your job is to audit the trading agent,
detect declining performance, challenge weak decisions, request evidence,
and approve only changes supported by data. Do not force trades.
Optimize the system, not emotions."

Architecture:
  Trading Agent → trades, makes decisions
  Supervisor    → audits, challenges, approves/rejects changes

The supervisor creates pressure through accountability, not through
forcing more trades.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

SUPERVISOR_FILE = Path(__file__).parent.parent / 'supervisor_state.json'


class Supervisor:
    """
    Two-agent architecture: Supervisor challenges the Trading Brain.
    
    After every session:
    1. Review what worked, what failed
    2. Classify failure: strategy? timing? market? execution? wrong assumption?
    3. Benchmark: today vs yesterday, live vs simulation
    4. Approve only changes backed by data
    5. Roll back changes that reduce performance
    """

    def __init__(self):
        self.state = self._load()
        self._ensure_today()

    def _load(self):
        if SUPERVISOR_FILE.exists():
            try:
                return json.loads(SUPERVISOR_FILE.read_text())
            except Exception:
                pass
        return self._default()

    def _default(self):
        return {
            "version": 1,
            "today": self._today(),
            "sessions": [],
            "current_session": {
                "start_time": time.time(),
                "trades": [],
                "decisions": [],
                "changes_made": [],
                "rollbacks": [],
            },
            "benchmarks": {},
            "evolution_log": [],
            "approved_changes": [],
            "rejected_changes": [],
            "performance_history": [],
        }

    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_today(self):
        if self.state.get("today") != self._today():
            # Archive yesterday's session
            old_session = self.state.get("current_session", {})
            if old_session.get("trades"):
                self.state.setdefault("sessions", []).append({
                    "date": self.state.get("today", "?"),
                    "summary": self._summarize_session(old_session),
                    "session": old_session,
                })
                if len(self.state["sessions"]) > 14:
                    self.state["sessions"] = self.state["sessions"][-14:]
            self.state["today"] = self._today()
            self.state["current_session"] = {
                "start_time": time.time(),
                "trades": [],
                "decisions": [],
                "changes_made": [],
                "rollbacks": [],
            }
            self._save()

    def _save(self):
        try:
            SUPERVISOR_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def _summarize_session(self, session):
        trades = session.get("trades", [])
        if not trades:
            return {"trades": 0, "pnl": 0, "wr": 0}
        wins = sum(1 for t in trades if t.get("profit", 0) > 0)
        pnl = sum(t.get("profit", 0) for t in trades)
        return {
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "wr": round(wins / len(trades) * 100, 1),
            "pnl": round(pnl, 4),
            "duration_min": round((trades[-1].get("time", 0) - trades[0].get("time", 0)) / 60000, 1) if len(trades) > 1 else 0,
        }

    # ═══════════════════════════════════════════════════════
    # TRADE RECORDING
    # ═══════════════════════════════════════════════════════

    def record_trade(self, market, strategy, profit, stake, balance, context=None):
        """Record a trade for supervisor analysis."""
        trade = {
            "market": market,
            "strategy": strategy,
            "profit": profit,
            "stake": stake,
            "balance": balance,
            "time": int(time.time() * 1000),
            "context": context or {},
        }
        self.state["current_session"]["trades"].append(trade)
        self._save()

    def record_decision(self, decision, reason, evidence):
        """Record a trading decision for audit trail."""
        self.state["current_session"]["decisions"].append({
            "decision": decision,
            "reason": reason,
            "evidence": evidence,
            "time": int(time.time() * 1000),
        })
        self._save()

    def record_change(self, change_type, description, before, after):
        """Record a strategy change for approval tracking."""
        change = {
            "type": change_type,
            "description": description,
            "before": before,
            "after": after,
            "time": int(time.time() * 1000),
            "status": "pending",
        }
        self.state["current_session"]["changes_made"].append(change)
        self._save()

    # ═══════════════════════════════════════════════════════
    # SESSION REVIEW (self-criticism loop)
    # ═══════════════════════════════════════════════════════

    def review_session(self, session_pnl, session_trades, session_wr):
        """
        Post-session self-criticism.
        Returns: {verdict, failures, successes, root_cause, improvements}
        """
        session = self.state.get("current_session", {})
        trades = session.get("trades", [])

        if not trades:
            return {"verdict": "NO_DATA", "message": "No trades to review"}

        # Classify each trade outcome
        wins = [t for t in trades if t.get("profit", 0) > 0]
        losses = [t for t in trades if t.get("profit", 0) <= 0]

        # Failure analysis
        failures = []
        if session_pnl < 0:
            # Was it strategy failure?
            strat_losses = {}
            for t in losses:
                s = t.get("strategy", "?")
                strat_losses[s] = strat_losses.get(s, 0) + 1
            worst_strat = max(strat_losses, key=strat_losses.get) if strat_losses else None
            if worst_strat and strat_losses[worst_strat] >= 3:
                failures.append({
                    "type": "STRATEGY_FAILURE",
                    "strategy": worst_strat,
                    "losses": strat_losses[worst_strat],
                    "message": f"Strategy {worst_strat} had {strat_losses[worst_strat]} losses",
                })

            # Was it timing failure?
            market_losses = {}
            for t in losses:
                m = t.get("market", "?")
                market_losses[m] = market_losses.get(m, 0) + 1
            worst_market = max(market_losses, key=market_losses.get) if market_losses else None
            if worst_market and market_losses[worst_market] >= 3:
                failures.append({
                    "type": "MARKET_FAILURE",
                    "market": worst_market,
                    "losses": market_losses[worst_market],
                    "message": f"Market {worst_market} had {market_losses[worst_market]} losses",
                })

            # Was it overtrading?
            if len(trades) > 20:
                failures.append({
                    "type": "OVERTRADING",
                    "trades": len(trades),
                    "message": f"Too many trades ({len(trades)}) — quality over quantity",
                })

            # Was it execution failure?
            if session_wr < 45:
                failures.append({
                    "type": "LOW_WIN_RATE",
                    "wr": session_wr,
                    "message": f"Win rate {session_wr:.1f}% is below 45% threshold",
                })

        # Success analysis
        successes = []
        if session_pnl > 0:
            strat_wins = {}
            for t in wins:
                s = t.get("strategy", "?")
                strat_wins[s] = strat_wins.get(s, 0) + 1
            best_strat = max(strat_wins, key=strat_wins.get) if strat_wins else None
            if best_strat:
                successes.append({
                    "type": "STRATEGY_WORKS",
                    "strategy": best_strat,
                    "wins": strat_wins[best_strat],
                    "message": f"Strategy {best_strat} had {strat_wins[best_strat]} wins",
                })

        # Root cause determination
        root_cause = "UNKNOWN"
        if failures:
            types = [f["type"] for f in failures]
            if "STRATEGY_FAILURE" in types:
                root_cause = "BAD_STRATEGY"
            elif "MARKET_FAILURE" in types:
                root_cause = "BAD_MARKET"
            elif "OVERTRADING" in types:
                root_cause = "OVERTRADING"
            elif "LOW_WIN_RATE" in types:
                root_cause = "EXECUTION"
            else:
                root_cause = "MIXED"
        elif session_pnl > 0:
            root_cause = "WORKING"
        else:
            root_cause = "NOISE"

        # Generate improvements
        improvements = []
        for f in failures:
            if f["type"] == "STRATEGY_FAILURE":
                improvements.append({
                    "action": "RETIRED_STRATEGY",
                    "target": f["strategy"],
                    "reason": f["message"],
                    "evidence": f,
                })
            elif f["type"] == "MARKET_FAILURE":
                improvements.append({
                    "action": "AVOID_MARKET",
                    "target": f["market"],
                    "reason": f["message"],
                    "evidence": f,
                })
            elif f["type"] == "OVERTRADING":
                improvements.append({
                    "action": "REDUCE_TRADES",
                    "target": "MAX_TRADES",
                    "reason": f["message"],
                    "evidence": f,
                })

        verdict = "LOSING" if session_pnl < 0 else "WINNING" if session_pnl > 0 else "FLAT"

        result = {
            "verdict": verdict,
            "pnl": round(session_pnl, 4),
            "trades": len(trades),
            "wr": round(session_wr, 1),
            "failures": failures,
            "successes": successes,
            "root_cause": root_cause,
            "improvements": improvements,
            "timestamp": int(time.time() * 1000),
        }

        # Store review
        self.state.setdefault("performance_history", []).append(result)
        if len(self.state["performance_history"]) > 30:
            self.state["performance_history"] = self.state["performance_history"][-30:]

        self._save()
        return result

    # ═══════════════════════════════════════════════════════
    # BENCHMARK (today vs yesterday, live vs sim)
    # ═══════════════════════════════════════════════════════

    def benchmark(self, today_pnl, today_wr, today_trades):
        """
        Compare today vs recent performance.
        Returns: {trend, comparison, recommendation}
        """
        history = self.state.get("performance_history", [])
        if len(history) < 2:
            return {"trend": "INSUFFICIENT_DATA", "message": "Need at least 2 sessions to compare"}

        # Get yesterday's stats
        yesterday = history[-2] if len(history) >= 2 else None
        avg_pnl = sum(h.get("pnl", 0) for h in history[-5:]) / min(5, len(history))
        avg_wr = sum(h.get("wr", 0) for h in history[-5:]) / min(5, len(history))

        trend = "STABLE"
        comparison = {}

        if today_pnl > avg_pnl * 1.2:
            trend = "IMPROVING"
            comparison["pnl_vs_avg"] = f"+{(today_pnl - avg_pnl):.2f} vs avg"
        elif today_pnl < avg_pnl * 0.8:
            trend = "DECLINING"
            comparison["pnl_vs_avg"] = f"{(today_pnl - avg_pnl):.2f} vs avg"

        if today_wr > avg_wr + 5:
            trend = "IMPROVING"
            comparison["wr_vs_avg"] = f"+{(today_wr - avg_wr):.1f}% vs avg"
        elif today_wr < avg_wr - 5:
            trend = "DECLINING"
            comparison["wr_vs_avg"] = f"{(today_wr - avg_wr):.1f}% vs avg"

        # Recommendation
        recommendation = "CONTINUE"
        if trend == "DECLINING":
            recommendation = "REVIEW_STRATEGIES"
        elif trend == "IMPROVING":
            recommendation = "SCALE_UP"

        result = {
            "trend": trend,
            "today_pnl": round(today_pnl, 4),
            "today_wr": round(today_wr, 1),
            "avg_pnl": round(avg_pnl, 4),
            "avg_wr": round(avg_wr, 1),
            "comparison": comparison,
            "recommendation": recommendation,
            "sessions_compared": min(5, len(history)),
            "timestamp": int(time.time() * 1000),
        }

        self.state["benchmarks"] = result
        self._save()
        return result

    # ═══════════════════════════════════════════════════════
    # EVOLUTION (bad→cause→improve→backtest→keep/reject)
    # ═══════════════════════════════════════════════════════

    def propose_evolution(self, failure_analysis, memory):
        """
        Based on failure analysis, propose an improvement.
        Returns: {change_type, description, evidence, expected_impact}
        """
        improvements = failure_analysis.get("improvements", [])
        if not improvements:
            return None

        best = improvements[0]  # highest priority improvement

        # Get evidence from memory
        evidence = {}
        if best.get("target") and best["target"] in memory.strategies:
            strat = memory.strategies[best["target"]]
            evidence = {
                "trades": strat.get("trades", 0),
                "wr": strat.get("win_rate", 0),
                "pnl": strat.get("total_profit", 0),
            }

        proposal = {
            "change_type": best.get("action", "UNKNOWN"),
            "target": best.get("target", "?"),
            "description": best.get("reason", "?"),
            "evidence": evidence,
            "expected_impact": f"Eliminate {best.get('type', '?')} failures",
            "confidence": 0.7 if evidence.get("trades", 0) >= 5 else 0.4,
            "time": int(time.time() * 1000),
        }

        self.state.setdefault("evolution_log", []).append({
            "type": "PROPOSAL",
            "proposal": proposal,
            "time": int(time.time() * 1000),
        })
        self._save()
        return proposal

    def approve_change(self, proposal, result_improved):
        """Approve or reject a change based on backtest results."""
        status = "approved" if result_improved else "rejected"
        change = {
            **proposal,
            "status": status,
            "result_improved": result_improved,
            "time": int(time.time() * 1000),
        }

        if result_improved:
            self.state.setdefault("approved_changes", []).append(change)
        else:
            self.state.setdefault("rejected_changes", []).append(change)

        self.state.setdefault("evolution_log", []).append({
            "type": "DECISION",
            "change": change,
            "time": int(time.time() * 1000),
        })
        self._save()
        return status

    def rollback_check(self, current_pnl, previous_pnl, change_made):
        """
        If a change reduced performance, roll it back.
        Returns: (should_rollback, reason)
        """
        if previous_pnl is None:
            return False, "no previous data"

        performance_drop = current_pnl - previous_pnl
        if performance_drop < -1.0:  # lost more than $1 after change
            self.state["current_session"]["rollbacks"].append({
                "change": change_made,
                "reason": f"Performance dropped ${abs(performance_drop):.2f} after change",
                "time": int(time.time() * 1000),
            })
            self._save()
            return True, f"Performance dropped ${abs(performance_drop):.2f}"

        return False, "performance stable or improved"

    # ═══════════════════════════════════════════════════════
    # CHALLENGE (supervisor asks hard questions)
    # ═══════════════════════════════════════════════════════

    def challenge(self, trading_decision, context):
        """
        Supervisor challenges a trading decision.
        Returns: {approved, challenges, evidence_required}
        """
        challenges = []
        evidence_required = []

        # Challenge 1: Why this market?
        market = context.get("market", "?")
        if market not in ("R_75",):
            challenges.append(f"Market {market} is not in the profitable whitelist. What evidence supports trading it?")

        # Challenge 2: Why this strategy?
        strategy = context.get("strategy", "?")
        strat_pnl = context.get("strategy_pnl", 0)
        if strat_pnl < 0:
            challenges.append(f"Strategy {strategy} has PnL ${strat_pnl:.2f}. Why are we trading a losing strategy?")

        # Challenge 3: Why now?
        hour = context.get("hour", 0)
        if hour not in (2, 15, 18, 22):
            challenges.append(f"Hour {hour} is not in the profitable window. What changed?")

        # Challenge 4: Risk check
        daily_loss = context.get("daily_loss", 0)
        if daily_loss > 3:
            challenges.append(f"Daily loss is ${daily_loss:.2f}. Are we chasing losses?")

        # Challenge 5: Overtrading
        trades_today = context.get("trades_today", 0)
        if trades_today > 15:
            challenges.append(f"{trades_today} trades today. Are we overtrading?")

        approved = len(challenges) == 0

        result = {
            "approved": approved,
            "challenges": challenges,
            "evidence_required": evidence_required,
            "context": context,
            "time": int(time.time() * 1000),
        }

        self.state["current_session"]["decisions"].append({
            "decision": "CHALLENGE",
            "result": result,
            "time": int(time.time() * 1000),
        })
        self._save()
        return result

    # ═══════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════

    def get_status(self):
        session = self.state.get("current_session", {})
        trades = session.get("trades", [])
        pnl = sum(t.get("profit", 0) for t in trades) if trades else 0
        wins = sum(1 for t in trades if t.get("profit", 0) > 0) if trades else 0
        wr = round(wins / len(trades) * 100, 1) if trades else 0

        return {
            "session_trades": len(trades),
            "session_pnl": round(pnl, 4),
            "session_wr": wr,
            "decisions_today": len(session.get("decisions", [])),
            "changes_today": len(session.get("changes_made", [])),
            "rollbacks_today": len(session.get("rollbacks", [])),
            "total_sessions": len(self.state.get("sessions", [])),
            "approved_changes": len(self.state.get("approved_changes", [])),
            "rejected_changes": len(self.state.get("rejected_changes", [])),
            "benchmark": self.state.get("benchmarks", {}),
            "last_review": self.state.get("performance_history", [{}])[-1] if self.state.get("performance_history") else {},
        }
