"""
FINAL DECISION ENGINE — The Judge
Answers 8 critical questions before every trade.
TRADE is only authorized when ALL answers are satisfactory.
"""
import time


class Judge:
    """
    Master decision gate. Answers:
    1. What market has the best opportunity?
    2. What strategy fits this market?
    3. Is the strategy currently healthy?
    4. Has it been validated by simulation?
    5. What risk level is acceptable?
    6. Should we trade or wait?
    7. What regime is the market in?
    8. Is capital protection satisfied?
    """

    TRADE = "TRADE"
    TEST = "TEST"
    OPTIMIZE = "OPTIMIZE"
    ROTATE = "ROTATE"
    WAIT = "WAIT"

    def __init__(self):
        self.decisions = []
        self.cooldown_until = 0
        self.last_decision = None

    def evaluate(self, context):
        """
        Master evaluation. context dict must contain:
          - market: selected market symbol
          - market_type: type of market
          - strategy: best strategy dict from Brain
          - regime: current regime classification
          - sim_result: simulation result (or None)
          - strategy_health: health score from Strategist
          - risk_clearance: bool from Protector
          - protector_status: full protector status
          - memory_stats: memory summary
          - signal: current signal data

        Returns: {decision, reason, confidence, answers: [...]}
        """
        answers = []
        decision = self.TRADE
        reasons = []

        market = context.get("market", "unknown")
        strategy = context.get("strategy", {})
        regime = context.get("regime", "UNKNOWN")
        sim_result = context.get("sim_result")
        health = context.get("strategy_health", 0)
        risk_clear = context.get("risk_clearance", False)
        protector = context.get("protector_status", {})
        signal = context.get("signal", {})
        memory = context.get("memory_stats", {})

        sname = strategy.get("strategy", "none")
        ev = strategy.get("expected_value", 0)
        prob = strategy.get("probability", 0)
        payout = strategy.get("payout", 0.95)
        edge = strategy.get("edge", 0)

        # Q1: What market has the best opportunity?
        q1 = self._q1_market_opportunity(market, signal, memory)
        answers.append(q1)
        if not q1["pass"]:
            decision = self.ROTATE
            reasons.append(q1["reason"])

        # Q2: What strategy fits this market?
        q2 = self._q2_strategy_fit(strategy, regime)
        answers.append(q2)
        if not q2["pass"]:
            decision = self.WAIT
            reasons.append(q2["reason"])

        # Q3: Is the strategy currently healthy?
        q3 = self._q3_strategy_health(health, strategy)
        answers.append(q3)
        if not q3["pass"]:
            if health < 20:
                decision = self.OPTIMIZE
            else:
                decision = self.WAIT
            reasons.append(q3["reason"])

        # Q4: Has it been validated by simulation?
        q4 = self._q4_simulation(sim_result, strategy)
        answers.append(q4)
        if not q4["pass"]:
            if sim_result is None:
                decision = self.TEST
            else:
                decision = self.WAIT
            reasons.append(q4["reason"])

        # Q5: What risk level is acceptable?
        q5 = self._q5_risk_level(protector, strategy)
        answers.append(q5)
        if not q5["pass"]:
            decision = self.WAIT
            reasons.append(q5["reason"])

        # Q6: Should we trade or wait?
        q6 = self._q6_timing(protector, self.cooldown_until)
        answers.append(q6)
        if not q6["pass"]:
            decision = self.WAIT
            reasons.append(q6["reason"])

        # Q7: What regime is the market in?
        q7 = self._q7_regime(regime, strategy)
        answers.append(q7)
        if not q7["pass"]:
            decision = self.ROTATE
            reasons.append(q7["reason"])

        # Q8: Is capital protection satisfied?
        q8 = self._q8_capital_protection(risk_clear, protector)
        answers.append(q8)
        if not q8["pass"]:
            decision = self.WAIT
            reasons.append(q8["reason"])

        # Override: if risk says no, always WAIT
        if not risk_clear:
            decision = self.WAIT
            if "RISK_BLOCKED" not in reasons:
                reasons.append("RISK_BLOCKED")

        # Confidence score
        passed = sum(1 for a in answers if a["pass"])
        confidence = passed / len(answers) if answers else 0

        # Build explainability summary
        failed_questions = [a for a in answers if not a["pass"]]
        passed_questions = [a for a in answers if a["pass"]]
        evidence_summary = []
        for a in answers:
            if "evidence" in a:
                evidence_summary.append({"question": a["q"], "passed": a["pass"], "evidence": a["evidence"]})

        explainability = {
            "decision": decision,
            "reason": "; ".join(reasons) if reasons else "ALL_CHECKS_PASSED",
            "confidence": round(confidence, 4),
            "passed_count": len(passed_questions),
            "failed_count": len(failed_questions),
            "failed_questions": [{"q": a["q"], "reason": a["reason"]} for a in failed_questions],
            "evidence": evidence_summary,
        }

        result = {
            "decision": decision,
            "reason": "; ".join(reasons) if reasons else "ALL_CHECKS_PASSED",
            "confidence": round(confidence, 4),
            "market": market,
            "strategy": sname,
            "regime": regime,
            "ev": ev,
            "prob": prob,
            "answers": answers,
            "explainability": explainability,
            "time": int(time.time() * 1000),
        }

        self.decisions.append(result)
        if len(self.decisions) > 100:
            self.decisions = self.decisions[-100:]
        self.last_decision = result

        return result

    # ── Question Implementations ────────────────────────

    def _q1_market_opportunity(self, market, signal, memory):
        if market == "unknown" or market is None:
            return {"q": "MARKET_OPPORTUNITY", "pass": False, "reason": "NO_MARKET_SELECTED"}
        if not signal:
            return {"q": "MARKET_OPPORTUNITY", "pass": False, "reason": "NO_SIGNAL_DATA"}
        ticks = signal.get("ticks_collected", 0)
        if ticks < 30:
            return {"q": "MARKET_OPPORTUNITY", "pass": False, "reason": f"INSUFFICIENT_DATA: {ticks}ticks"}
        return {"q": "MARKET_OPPORTUNITY", "pass": True, "reason": f"{market} active"}

    def _q2_strategy_fit(self, strategy, regime):
        if not strategy or strategy.get("direction") == "NONE":
            return {"q": "STRATEGY_FIT", "pass": False, "reason": "NO_STRATEGY", "evidence": {}}
        ev = strategy.get("expected_value", 0)
        if ev <= 0:
            return {"q": "STRATEGY_FIT", "pass": False, "reason": f"NEGATIVE_EV: {ev:.4f}",
                    "evidence": {"ev": ev, "regime": regime}}
        return {"q": "STRATEGY_FIT", "pass": True, "reason": f"EV={ev:.4f}",
                "evidence": {"ev": ev, "regime": regime, "strategy": strategy.get("strategy", "?")}}

    def _q3_strategy_health(self, health_score, strategy):
        if health_score is None:
            return {"q": "STRATEGY_HEALTH", "pass": True, "reason": "NEW_STRATEGY_NO_HISTORY",
                    "evidence": {"health": None}}
        if health_score < 15:
            return {"q": "STRATEGY_HEALTH", "pass": False, "reason": f"HEALTH={health_score:.0f}/100",
                    "evidence": {"health": health_score, "threshold": 15}}
        return {"q": "STRATEGY_HEALTH", "pass": True, "reason": f"HEALTH={health_score:.0f}/100",
                "evidence": {"health": health_score}}

    def _q4_simulation(self, sim_result, strategy):
        trades = strategy.get("trades", 0) if isinstance(strategy, dict) else 0
        if sim_result is None:
            if trades < 3:
                return {"q": "SIMULATION", "pass": False, "reason": "NEW_STRATEGY_NO_SIM: requires simulation first",
                        "evidence": {"trades": trades, "required": "simulation_before_live"}}
            return {"q": "SIMULATION", "pass": True, "reason": "SKIPPED_NO_SIM_DATA"}
        if not sim_result.get("approved", False):
            ror = sim_result.get("sim_risk_of_ruin", 100)
            return {"q": "SIMULATION", "pass": False, "reason": f"SIM_FAILED RoR={ror}%",
                    "evidence": {"risk_of_ruin": ror, "sim_win_rate": sim_result.get("sim_win_rate", 0)}}
        return {"q": "SIMULATION", "pass": True, "reason": f"SIM_APPROVED WR={sim_result.get('sim_win_rate', 0)}%",
                "evidence": {"sim_win_rate": sim_result.get("sim_win_rate", 0), "approved": True}}

    def _q5_risk_level(self, protector, strategy):
        frozen = protector.get("frozen", False)
        if frozen:
            return {"q": "RISK_LEVEL", "pass": False, "reason": f"FROZEN: {protector.get('freeze_reason', '?')}",
                    "evidence": {"frozen": True, "reason": protector.get("freeze_reason", "?")}}
        dd = protector.get("drawdown_from_peak", 0)
        if dd > 3:
            return {"q": "RISK_LEVEL", "pass": False, "reason": f"HIGH_DRAWDOWN: {dd}%",
                    "evidence": {"drawdown": dd, "threshold": 3}}
        hour_trades = protector.get("hour_trades", 0)
        cap = protector.get("hourly_cap", 20)
        if hour_trades >= cap:
            return {"q": "RISK_LEVEL", "pass": False, "reason": f"HOURLY_CAP: {hour_trades}/{cap}",
                    "evidence": {"hour_trades": hour_trades, "cap": cap}}
        return {"q": "RISK_LEVEL", "pass": True, "reason": "RISK_CLEAR",
                "evidence": {"drawdown": dd, "hour_trades": hour_trades}}

    def _q6_timing(self, protector, cooldown_until):
        now = time.time()
        if now < cooldown_until:
            remaining = int(cooldown_until - now)
            return {"q": "TIMING", "pass": False, "reason": f"COOLDOWN: {remaining}s",
                    "evidence": {"remaining_seconds": remaining}}
        consec = protector.get("consecutive_losses", 0)
        if consec >= 3:
            return {"q": "TIMING", "pass": False, "reason": f"LOSS_STREAK: {consec}",
                    "evidence": {"consecutive_losses": consec, "limit": 3}}
        return {"q": "TIMING", "pass": True, "reason": "READY",
                "evidence": {"consecutive_losses": consec}}

    def _q7_regime(self, regime, strategy):
        if regime == "UNKNOWN":
            return {"q": "REGIME", "pass": True, "reason": "REGIME_UNKNOWN_PROCEEDING"}
        strat_name = strategy.get("strategy", "")
        # Digit strategies need digit anomaly regime
        if "DIGIT" in strat_name and regime not in ("DIGIT_ANOMALY", "UNKNOWN"):
            return {"q": "REGIME", "pass": False, "reason": f"MISMATCH: DIGIT_STRAT in {regime}"}
        # Trend strategies need momentum
        if "TREND" in strat_name and regime not in ("MOMENTUM_EXPANSION", "UNKNOWN"):
            return {"q": "REGIME", "pass": False, "reason": f"MISMATCH: TREND_STRAT in {regime}"}
        return {"q": "REGIME", "pass": True, "reason": f"REGIME={regime}"}

    def _q8_capital_protection(self, risk_clear, protector):
        if not risk_clear:
            return {"q": "CAPITAL_PROTECTION", "pass": False, "reason": "RISK_VETO",
                    "evidence": {"risk_clear": False}}
        daily_loss = protector.get("daily_loss_pct", 0)
        limit = protector.get("daily_loss_limit_pct", 2)
        if daily_loss >= limit * 0.8:
            return {"q": "CAPITAL_PROTECTION", "pass": False, "reason": f"NEAR_DAILY_LIMIT: {daily_loss}%",
                    "evidence": {"daily_loss": daily_loss, "limit": limit, "threshold_pct": 80}}
        return {"q": "CAPITAL_PROTECTION", "pass": True, "reason": "CAPITAL_SAFE",
                "evidence": {"daily_loss": daily_loss, "limit": limit}}

    def set_cooldown(self, seconds):
        self.cooldown_until = time.time() + seconds

    def get_status(self):
        recent = self.decisions[-10:] if self.decisions else []
        trade_count = sum(1 for d in recent if d["decision"] == self.TRADE)
        wait_count = sum(1 for d in recent if d["decision"] == self.WAIT)
        return {
            "total_decisions": len(self.decisions),
            "recent_trades": trade_count,
            "recent_waits": wait_count,
            "last_decision": self.last_decision["decision"] if self.last_decision else "NONE",
            "last_reason": self.last_decision["reason"] if self.last_decision else "",
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
        }
