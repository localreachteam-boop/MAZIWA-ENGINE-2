
"""
SELF DIAGNOSTIC — Before killing a market, scan the system itself

The system should NOT blame the market for losses caused by:
  1. Overtrading (too many trades in sequence)
  2. Fatigue (consecutive losses, session too long)
  3. Network lag (tick delays, API slowness)
  4. Execution issues (slow bot, delayed entries)
  5. Strategy entry failures (bad signals, not bad markets)
  6. Wrong timezone session for this market
  7. Actual market decay vs system noise

Only if ALL system factors are healthy should we blame the market.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

DIAG_FILE = Path(__file__).parent.parent / 'self_diagnostic.json'

def now_ts():
    return time.time()

def now_utc():
    return datetime.now(timezone.utc)


class SelfDiagnostic:
    """Scans the system before blaming markets for losses."""

    def __init__(self):
        self.state = self._load()

    def _load(self):
        if DIAG_FILE.exists():
            try:
                state = json.loads(DIAG_FILE.read_text())
                # Reset stale session data on fresh brain start
                now = now_ts()
                session_age_h = (now - state.get('session_start', now)) / 3600
                if session_age_h > 1.0:
                    # Session is old — reset to fresh start
                    state['session_start'] = now
                    state['consecutive_losses'] = 0
                    state['consecutive_wins'] = 0
                    state['trade_timestamps'] = []  # clear stale overtrading metric
                    state['execution_latencies'] = []
                    state['network_failures'] = 0
                    state['last_tick_age'] = 0
                    # Keep strategy_results (they're useful history)
                    # Persist the reset immediately
                    try:
                        DIAG_FILE.write_text(json.dumps(state, indent=2, default=str))
                    except Exception:
                        pass
                return state
            except Exception:
                pass
        return self._default()

    def _default(self):
        return {
            "version": 2,
            "last_scan": 0,
            "trade_timestamps": [],      # last 50 trade times (epoch ms)
            "consecutive_losses": 0,
            "consecutive_wins": 0,
            "session_start": now_ts(),
            "last_tick_time": 0,
            "last_tick_age": 0,
            "network_failures": 0,
            "execution_latencies": [],    # last 20 execution times (ms)
            "strategy_results": {},       # strategy -> recent results
            "diagnostic_history": [],     # last 30 diagnostic scans
        }

    def _save(self):
        try:
            DIAG_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def record_trade(self, strategy, market, profit, execution_time_ms=None):
        """Record a trade for diagnostic analysis."""
        now = now_ts()

        # Track trade timestamps for overtrade detection
        self.state.setdefault("trade_timestamps", []).append(now)
        # Keep last 50
        self.state["trade_timestamps"] = self.state["trade_timestamps"][-50:]

        # Track consecutive losses/wins
        if profit > 0:
            self.state["consecutive_wins"] = self.state.get("consecutive_wins", 0) + 1
            self.state["consecutive_losses"] = 0
        else:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            self.state["consecutive_wins"] = 0

        # Track execution latency
        if execution_time_ms is not None:
            self.state.setdefault("execution_latencies", []).append(execution_time_ms)
            self.state["execution_latencies"] = self.state["execution_latencies"][-20:]

        # Track per-strategy results
        strat_key = f"{market}:{strategy}"
        if strat_key not in self.state.get("strategy_results", {}):
            self.state.setdefault("strategy_results", {})[strat_key] = {
                "trades": 0, "wins": 0, "losses": 0,
                "recent_5": [], "pnl": 0.0,
            }
        sr = self.state["strategy_results"][strat_key]
        sr["trades"] += 1
        sr["pnl"] = round(sr["pnl"] + profit, 4)
        if profit > 0:
            sr["wins"] += 1
        else:
            sr["losses"] += 1
        sr["recent_5"].append(profit)
        sr["recent_5"] = sr["recent_5"][-5:]

        self._save()

    def update_tick_health(self, tick_age, network_failures):
        """Update network/tick health from heartbeat."""
        self.state["last_tick_time"] = now_ts()
        self.state["last_tick_age"] = tick_age
        self.state["network_failures"] = network_failures
        self._save()

    def run_diagnostic(self, market, hour, heartbeat_data=None, session_stats=None):
        """
        Full system scan before blaming the market.
        Returns: (health_score 0-100, issues list, can_trade bool)
        
        health_score meaning:
          80-100: System healthy, loss is market's fault
          50-79: System partially degraded, reduce stake
          20-49: System degraded, pause
          0-19: System broken, don't trade
        """
        issues = []
        scores = {}
        now = now_ts()

        # ════ CHECK 1: Overtrading ════
        trade_times = self.state.get("trade_timestamps", [])
        recent_trades_5min = sum(1 for t in trade_times if now - t < 300)
        recent_trades_10min = sum(1 for t in trade_times if now - t < 600)
        recent_trades_30min = sum(1 for t in trade_times if now - t < 1800)

        if recent_trades_5min >= 8:
            scores["overtrading"] = 10
            issues.append(f"OVERTRADING: {recent_trades_5min} trades in 5min")
        elif recent_trades_5min >= 5:
            scores["overtrading"] = 30
            issues.append(f"Heavy trading: {recent_trades_5min} trades in 5min")
        elif recent_trades_10min >= 15:
            scores["overtrading"] = 25
            issues.append(f"Pace: {recent_trades_10min} trades in 10min")
        else:
            # No recent heavy trading — system is fine
            scores["overtrading"] = 90

        # ════ CHECK 2: Fatigue ════
        consec_losses = self.state.get("consecutive_losses", 0)
        session_duration = now - self.state.get("session_start", now)
        session_hours = session_duration / 3600

        if consec_losses >= 6:
            scores["fatigue"] = 10
            issues.append(f"FATIGUE: {consec_losses} consecutive losses")
        elif consec_losses >= 4:
            scores["fatigue"] = 25
            issues.append(f"Fatiguing: {consec_losses} consecutive losses")
        elif consec_losses >= 2:
            scores["fatigue"] = 50
        else:
            scores["fatigue"] = 80

        if session_hours > 8:
            scores["fatigue"] = min(scores["fatigue"], 40)
            issues.append(f"Long session: {session_hours:.1f}h")
        elif session_hours > 5:
            scores["fatigue"] = min(scores["fatigue"], 60)

        # ════ CHECK 3: Network Health ════
        tick_age = self.state.get("last_tick_age", 0)
        net_failures = self.state.get("network_failures", 0)
        last_tick = self.state.get("last_tick_time", 0)
        time_since_tick = now - last_tick if last_tick else 999

        # Trust tick_age (live feed status) as primary signal
        # last_tick_time may be stale if diagnostics run between tick updates
        if tick_age > 10:
            scores["network"] = 15
            issues.append(f"NETWORK LAG: tick age={tick_age}s")
        elif tick_age > 5:
            scores["network"] = 40
            issues.append(f"Network slow: tick age={tick_age}s")
        elif net_failures > 3:
            scores["network"] = 40
            issues.append(f"Network unstable: {net_failures} failures")
        elif net_failures > 0:
            scores["network"] = 60
        elif tick_age == 0:
            # Ticks flowing — network is healthy regardless of last_tick_time
            scores["network"] = 90
        else:
            scores["network"] = 70

        # Also check heartbeat data if available
        if heartbeat_data:
            hb_net = heartbeat_data.get("net", {})
            if not hb_net.get("deriv", True):
                scores["network"] = min(scores.get("network", 50), 10)
                issues.append("Deriv disconnected")
            if not hb_net.get("ticks", True):
                scores["network"] = min(scores.get("network", 50), 15)
                issues.append("Tick feed down")

        # ════ CHECK 4: Execution Speed ════
        latencies = self.state.get("execution_latencies", [])
        if latencies:
            avg_lat = sum(latencies) / len(latencies)
            recent_lat = latencies[-5:] if len(latencies) >= 5 else latencies
            recent_avg = sum(recent_lat) / len(recent_lat)

            if avg_lat > 5000:
                scores["execution"] = 20
                issues.append(f"SLOW EXECUTION: avg {avg_lat:.0f}ms")
            elif recent_avg > 3000:
                scores["execution"] = 40
                issues.append(f"Execution slowing: {recent_avg:.0f}ms recent")
            elif avg_lat > 2000:
                scores["execution"] = 60
            else:
                scores["execution"] = 90
        else:
            scores["execution"] = 70  # no data, assume ok

        # ════ CHECK 5: Strategy Entry Quality ════
        strat_key = f"{market}:{self.state.get('_last_strategy', 'ALL')}"
        sr = self.state.get("strategy_results", {}).get(strat_key, {})
        strat_trades = sr.get("trades", 0)
        strat_wr = (sr.get("wins", 0) / strat_trades * 100) if strat_trades > 0 else 0

        # Check if ALL strategies at this market are failing (not just one)
        market_strats = {k: v for k, v in self.state.get("strategy_results", {}).items()
                        if k.startswith(market + ":") and v.get("trades", 0) >= 3}
        if market_strats:
            avg_market_wr = sum(
                (v.get("wins", 0) / v.get("trades", 1)) * 100
                for v in market_strats.values()
            ) / len(market_strats)

            if avg_market_wr < 30 and len(market_strats) >= 2:
                scores["strategy"] = 20
                issues.append(f"ALL strategies failing at {market}: WR={avg_market_wr:.0f}%")
            elif avg_market_wr < 40:
                scores["strategy"] = 45
            else:
                scores["strategy"] = 75
        else:
            scores["strategy"] = 60  # limited data

        # ════ CHECK 6: Timezone Session ════
        if session_stats:
            hs = session_stats.get(str(hour), {})
            h_trades = hs.get("trades", 0)
            h_pnl = hs.get("pnl", 0)
            h_wr = hs.get("wr", 0)
            if h_trades >= 5:
                if h_pnl > 1.0 and h_wr >= 55:
                    scores["timezone"] = 85
                elif h_pnl > -0.5:
                    scores["timezone"] = 60
                else:
                    scores["timezone"] = 30
                    issues.append(f"Bad hour h{hour}: {h_trades}T pnl=${h_pnl:+.2f} wr={h_wr:.0f}%")
            else:
                scores["timezone"] = 50  # not enough data for this hour
        else:
            scores["timezone"] = 50

        # ════ CHECK 7: Entry Timing Quality ════
        # Was the trade entered at a good moment? Check recent digit flow
        entry_quality = self._check_entry_timing(market, hour)
        scores["entry_timing"] = entry_quality["score"]
        if entry_quality["score"] < 40:
            issues.append(f"BAD ENTRY TIMING: {entry_quality['reason']}")

        # ════ CHECK 8: Market Manipulation Detection ════
        # Check for unusual digit distributions that suggest manipulation
        manipulation = self._check_manipulation(market)
        scores["manipulation"] = manipulation["score"]
        if manipulation["score"] < 50:
            issues.append(f"MANIPULATION ALERT: {manipulation['reason']}")

        # ════ CHECK 9: Entry Signal Quality ════
        # Was the signal strong enough to justify the trade?
        signal_quality = self._check_signal_quality(market, strategy if 'strategy' in dir() else "ALL")
        scores["signal_quality"] = signal_quality["score"]
        if signal_quality["score"] < 30:
            issues.append(f"WEAK SIGNAL: {signal_quality['reason']}")

        # ════ CHECK 10: Market Regime ════
        # Is this market in a tradeable regime?
        regime_score = self._check_market_regime(market)
        scores["regime"] = regime_score["score"]
        if regime_score["score"] < 40:
            issues.append(f"BAD REGIME: {regime_score['reason']}")

        # ════ CHECK 11: Market Decay vs System Issue ════
        # If system is unhealthy, the loss is likely system's fault, not market's
        system_health = sum(scores.get(k, 50) for k in ["overtrading", "fatigue", "network", "execution"]) / 4
        market_health = sum(scores.get(k, 50) for k in ["strategy", "timezone"]) / 2

        if system_health < 40 and market_health > 50:
            # System is the problem, not the market
            scores["root_cause"] = "system"
            issues.append(f"ROOT CAUSE: System issue (health={system_health:.0f}%) not market decay")
        elif market_health < 40 and system_health > 50:
            # Market is the problem
            scores["root_cause"] = "market"
            issues.append(f"ROOT CAUSE: Market decay (health={market_health:.0f}%)")
        elif system_health < 40 and market_health < 40:
            scores["root_cause"] = "both"
            issues.append("ROOT CAUSE: Both system and market degraded")
        else:
            scores["root_cause"] = "noise"
            # Neither is clearly broken — this is just normal trading variance

        # ════ COMPOSITE HEALTH SCORE ════
        weights = {
            "overtrading": 0.10, "fatigue": 0.10,
            "network": 0.15, "execution": 0.10,
            "strategy": 0.12, "timezone": 0.08,
            "entry_timing": 0.12, "manipulation": 0.10,
            "signal_quality": 0.08, "regime": 0.05,
        }
        health_score = 0
        for k, w in weights.items():
            health_score += scores.get(k, 50) * w
        health_score = round(max(0, min(100, health_score)))

        # ════ CAN WE TRADE? ════
        # Hard blockers: network down, extreme fatigue, severe overtrading
        hard_block = (
            scores.get("network", 50) < 20 or
            scores.get("fatigue", 50) < 15 or
            scores.get("overtrading", 50) < 15
        )
        can_trade = not hard_block

        # ════ STAKE MULTIPLIER ════
        if health_score >= 80:
            stake_mult = 1.0   # full confidence
            action = "full"
        elif health_score >= 60:
            stake_mult = 0.7   # slight reduction
            action = "reduced"
        elif health_score >= 40:
            stake_mult = 0.4   # significant reduction
            action = "cautious"
        elif health_score >= 20:
            stake_mult = 0.15  # minimal
            action = "minimal"
        else:
            stake_mult = 0.0   # stop
            action = "stop"

        result = {
            "health_score": health_score,
            "action": action,
            "stake_mult": stake_mult,
            "can_trade": can_trade,
            "issues": issues,
            "scores": scores,
            "root_cause": scores.get("root_cause", "unknown"),
            "timestamp": now,
            "market": market,
            "hour": hour,
        }

        # Store
        self.state["last_scan"] = now
        self.state.setdefault("diagnostic_history", []).append(result)
        if len(self.state["diagnostic_history"]) > 30:
            self.state["diagnostic_history"] = self.state["diagnostic_history"][-30:]
        self._save()

        return health_score, issues, can_trade, stake_mult, action

    def _check_entry_timing(self, market, hour):
        """Check if trade was entered at a good moment."""
        # Use timezone data to check if this hour historically performs well
        # Low hour = bad timing (e.g., 3am vs peak hours)
        score = 60  # default: ok
        reason = ""
        
        # Peak hours: 8-20 UTC generally better for digit markets
        if 2 <= hour <= 5:
            score = 30
            reason = f"Off-peak hour h{hour} — low liquidity window"
        elif 6 <= hour <= 7:
            score = 45
            reason = f"Early hour h{hour} — market warming up"
        elif 21 <= hour <= 23:
            score = 40
            reason = f"Late hour h{hour} — market winding down"
        
        # Check recent trade timestamps for rapid re-entry
        trade_times = self.state.get("trade_timestamps", [])
        if len(trade_times) >= 2:
            last_gap = trade_times[-1] - trade_times[-2]
            if last_gap < 5:  # less than 5 seconds between trades
                score = min(score, 25)
                reason = f"Re-entry too fast ({last_gap:.1f}s gap)"
            elif last_gap < 10:
                score = min(score, 40)
                reason = f"Rapid re-entry ({last_gap:.1f}s gap)"
        
        return {"score": score, "reason": reason}

    def _check_manipulation(self, market):
        """Detect unusual digit distributions suggesting manipulation."""
        try:
            import json
            from pathlib import Path
            mem_file = Path(__file__).parent.parent / "agent_memory.json"
            if not mem_file.exists():
                return {"score": 60, "reason": "no data"}
            
            mem = json.loads(mem_file.read_text())
            hist = mem.get("digit_history", {}).get(market, {})
            total = hist.get("_total", 0)
            
            if total < 100:
                return {"score": 60, "reason": "insufficient data"}
            
            # Calculate expected frequency (10% each)
            expected = total / 10.0
            
            # Check for extreme skew (any digit > 2x or < 0.3x expected)
            max_ratio = 0
            min_ratio = 10
            over_digit = None
            under_digit = None
            
            for d in range(10):
                count = hist.get(str(d), 0)
                if count == 0:
                    continue
                ratio = count / expected
                if ratio > max_ratio:
                    max_ratio = ratio
                    over_digit = d
                if ratio < min_ratio:
                    min_ratio = ratio
                    under_digit = d
            
            score = 80
            reason = ""
            
            if max_ratio > 1.5:
                score = 30
                reason = f"Digit {over_digit} overrepresented {max_ratio:.1f}x (possible bias)"
            elif max_ratio > 1.3:
                score = 50
                reason = f"Digit {over_digit} slightly over {max_ratio:.1f}x"
            
            # Volatility markets (R_75, R_10, etc.) naturally produce very few 0s
            vol_markets = {'R_75', 'R_10', 'R_25', 'R_50', 'R_100'}
            skip_zero = market in vol_markets
            if min_ratio < 0.3 and total > 500 and (under_digit != 0 or not skip_zero):
                score = min(score, 35)
                reason += f" | Digit {under_digit} underrepresented {min_ratio:.1f}x"
            
            # Check entropy — too low = artificial
            import math
            entropy = 0
            for d in range(10):
                count = hist.get(str(d), 0)
                if count > 0:
                    p = count / total
                    entropy -= p * math.log2(p)
            
            max_entropy = math.log2(10)
            if entropy < max_entropy * 0.85:
                score = min(score, 45)
                reason += f" | Low entropy {entropy:.2f}/{max_entropy:.2f}"
            
            return {"score": score, "reason": reason or "normal distribution"}
        except Exception:
            return {"score": 60, "reason": "analysis error"}

    def _check_signal_quality(self, market, strategy):
        """Check if the entry signal was strong enough."""
        # Check C++ engine prediction confidence
        try:
            import json
            from pathlib import Path
            state_file = Path(__file__).parent.parent / "trading_state.json"
            if not state_file.exists():
                return {"score": 60, "reason": "no state data"}
            
            state = json.loads(state_file.read_text())
            
            # Check confidence score from brain
            conf = state.get("confidence_score", 0)
            if conf < 3:
                return {"score": 30, "reason": f"Low confidence {conf}/10"}
            elif conf < 5:
                return {"score": 50, "reason": f"Moderate confidence {conf}/10"}
            
            # Check selected EV
            ev = state.get("selected_ev", 0)
            if ev < 0:
                return {"score": 25, "reason": f"Negative EV {ev:.3f}"}
            elif ev < 0.01:
                return {"score": 45, "reason": f"Low EV {ev:.3f}"}
            
            return {"score": 70, "reason": "adequate signal"}
        except Exception:
            return {"score": 60, "reason": "analysis error"}

    def _check_market_regime(self, market):
        """Check if market is in a tradeable regime."""
        try:
            import json
            from pathlib import Path
            
            # Check memory for market state
            mem_file = Path(__file__).parent.parent / "agent_memory.json"
            if not mem_file.exists():
                return {"score": 60, "reason": "no data"}
            
            mem = json.loads(mem_file.read_text())
            strats = mem.get("strategies", {})
            
            # Count active vs retired strategies for this market
            active = 0
            retired = 0
            total_pnl = 0
            
            for key, val in strats.items():
                if key.startswith(market + ":"):
                    if val.get("status") == "RETIRED":
                        retired += 1
                    else:
                        active += 1
                    total_pnl += val.get("total_profit", 0)
            
            if active + retired == 0:
                return {"score": 50, "reason": "no history for this market"}
            
            retire_rate = retired / (active + retired) if (active + retired) > 0 else 0
            
            if retire_rate > 0.7 and (active + retired) >= 5:
                return {"score": 25, "reason": f"{retire_rate:.0%} strategies retired at {market}"}
            elif retire_rate > 0.5:
                return {"score": 40, "reason": f"{retire_rate:.0%} strategies retired"}
            
            if total_pnl < -20:
                return {"score": 30, "reason": f"Market bleeding ${total_pnl:.2f}"}
            elif total_pnl < -5:
                return {"score": 45, "reason": f"Market losing ${total_pnl:.2f}"}
            
            return {"score": 70, "reason": "market healthy"}
        except Exception:
            return {"score": 60, "reason": "analysis error"}

    def get_status(self):
        """Dashboard status."""
        last = self.state.get("diagnostic_history", [])
        recent = last[-5:] if last else []
        health = recent[-1].get("health_score", 0) if recent else 0
        return {
            "health_score": health,
            "last_scan": self.state.get("last_scan", 0),
            "consecutive_losses": self.state.get("consecutive_losses", 0),
            "consecutive_wins": self.state.get("consecutive_wins", 0),
            "trade_rate_5min": sum(1 for t in self.state.get("trade_timestamps", []) if now_ts() - t < 300),
            "recent_issues": recent[-1].get("issues", []) if recent else [],
            "recent_root_cause": recent[-1].get("root_cause", "?") if recent else "?",
            "avg_latency": round(sum(self.state.get("execution_latencies", [])[-5:]) / max(1, len(self.state.get("execution_latencies", [])[-5:])), 0) if self.state.get("execution_latencies") else 0,
            "session_minutes": round((now_ts() - self.state.get("session_start", now_ts())) / 60, 0),
            "total_scans": len(last),
        }

    def get_diagnostic_for_dashboard(self):
        """Build dashboard rows for recent diagnostics."""
        scans = self.state.get("diagnostic_history", [])[-5:]
        if not scans:
            return '<div style="color:#64748b;font-size:10px;padding:4px 0">No diagnostics yet — runs after trades</div>'

        rows = ""
        for s in reversed(scans):
            hs = s.get("health_score", 0)
            action = s.get("action", "?")
            root = s.get("root_cause", "?")
            issues = s.get("issues", [])
            mkt = s.get("market", "?")
            h = s.get("hour", "?")
            ts = s.get("timestamp", 0)
            age = int(now_ts() - ts) if ts else 0

            hs_color = "#22c55e" if hs >= 70 else "#f59e0b" if hs >= 40 else "#ef4444"
            action_color = {"full": "#22c55e", "reduced": "#f59e0b", "cautious": "#f59e0b", "minimal": "#ef4444", "stop": "#ef4444"}.get(action, "#64748b")
            root_color = {"system": "#ef4444", "market": "#f59e0b", "both": "#ef4444", "noise": "#22c55e"}.get(root, "#64748b")

            rows += '<div style="padding:4px 0;border-bottom:1px solid #1a2332;font-size:10px">'
            rows += '<div style="display:flex;gap:6px;align-items:center">'
            rows += '<span style="color:%s;font-weight:700;min-width:35px">%d%%</span>' % (hs_color, hs)
            rows += '<span style="color:%s;font-weight:600;text-transform:uppercase;font-size:9px;min-width:55px">%s</span>' % (action_color, action)
            rows += '<span style="color:#3b82f6;font-weight:600;min-width:40px">%s h%s</span>' % (mkt, h)
            rows += '<span style="color:%s;font-size:9px;">Root: %s</span>' % (root_color, root)
            rows += '<span style="color:#3a4f6a;font-size:8px;margin-left:auto">%ds ago</span>' % age
            rows += '</div>'
            if issues:
                rows += '<div style="padding-left:22px;margin-top:1px">'
                for issue in issues[:3]:
                    rows += '<div style="font-size:8px;color:#f59e0b">⚠ %s</div>' % issue[:80]
                rows += '</div>'
            rows += '</div>'

        return rows
