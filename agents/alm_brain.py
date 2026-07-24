"""
ALM BRAIN — AI Reasoning Layer (Dual Model Architecture)
Uses Gemma 4 26B (free) via OpenRouter for AI reasoning.

Architecture:
  Gemma 4 26B (free, cloud) → quick decisions, strategy research, complex reasoning
  C++ Engine (fast computation) → tick processing, simulations
  Memory (storage) → persistent knowledge
"""
import json
import time
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CLOUD_MODEL = "google/gemma-4-26b-a4b-it:free"  # Free Gemma 4 26B via OpenRouter
LOCAL_MODEL = os.environ.get("ALM_MODEL", CLOUD_MODEL)


class ALMBrain:
    """
    Dual-model AI reasoning layer.

    LOCAL (Ollama — offline):
      - Fast decisions (< 1s)
      - Monitoring, classification, simple reasoning
      - Free, always available
      - Used in EXECUTE mode

    CLOUD (DeepSeek V4 Flash via OpenRouter):
      - Deep analysis (2-5s)
      - Strategy research, failure analysis, architecture improvement
      - Costs money, use wisely
      - Used in STUDY mode for complex queries

    Falls back to rule-based logic if neither available.
    """

    def __init__(self):
        # Local model (Ollama)
        self.connected = False
        self.model = LOCAL_MODEL
        self.url = OLLAMA_URL

        # Cloud model (DeepSeek via OpenRouter)
        self.cloud_available = bool(OPENROUTER_API_KEY)
        self.cloud_model = CLOUD_MODEL
        self.cloud_api_key = OPENROUTER_API_KEY
        self.cloud_total_queries = 0
        self.cloud_total_tokens = 0
        self.cloud_last_cost = 0.0
        self.cloud_budget = 5.0  # max $5 per session

        # Shared state
        self.total_queries = 0
        self.total_tokens = 0
        self.last_response = ""
        self.last_reasoning = ""
        self.last_model_used = CLOUD_MODEL  # default to cloud since Ollama is offline
        self.model_size = "—"
        self.enabled = True
        self.runtime_ok = False
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._local_future = None
        self._cloud_future = None
        self.notes = []
        self.log_callback = None
        self.model_usage_log = []
        self.next_decision = ""
        self.current_task = ""
        self.session_mode = "EXECUTE"
        self.session_start = time.time()
        self._mem = None  # set by brain_active after Memory init
        self.study_cycles = 0
        self.execute_cycles = 0
        self.study_interval = 50
        self.execute_interval = 20
        self.study_findings = []

        self._check_connection()

    def _check_connection(self):
        """Check if Ollama is running and model is available."""
        try:
            import urllib.request
            req = urllib.request.urlopen(f"{self.url}/api/tags", timeout=5)
            data = json.loads(req.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            if any(self.model in m for m in models):
                self.connected = True
                print(f"  [ALM-BRAIN] Local: {self.model}")
            elif models:
                self.model = models[0]
                self.connected = True
                print(f"  [ALM-BRAIN] Local: {self.model}")
            else:
                print(f"  [ALM-BRAIN] Ollama running but no models pulled")
        except Exception as e:
            self.model = CLOUD_MODEL  # Ollama offline, use cloud as primary
            print(f"  [ALM-BRAIN] Local model not available: {e}")

        if self.cloud_available:
            print(f"  [ALM-BRAIN] Cloud: {self.cloud_model} (OpenRouter)")
        else:
            print(f"  [ALM-BRAIN] Cloud: No OPENROUTER_API_KEY set")

    # ═══════════════════════════════════════════════════════
    # LOCAL MODEL (Ollama via local server)
    # ═══════════════════════════════════════════════════════

    def _query_local(self, prompt):
        """Query local model via Ollama."""
        if not self.connected:
            return None
        try:
            import urllib.request
            payload = json.dumps({
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 256,
                    "num_ctx": 2048,
                }
            })
            req = urllib.request.Request(
                f"{self.url}/api/generate",
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            if "error" not in result:
                self.runtime_ok = True
                self.last_model_used = self.model
                self.total_queries += 1
                gen_tokens = result.get("eval_count", 0)
                self.total_tokens += gen_tokens
                self._log_model_event("Local model responded: " + str(gen_tokens) + " tokens generated", gen_tokens)
                return result.get("response", "")
        except:
            pass
        return None

    # ═══════════════════════════════════════════════════════
    # CLOUD MODEL (DeepSeek V4 Flash via OpenRouter)
    # ═══════════════════════════════════════════════════════

    def _query_cloud(self, messages, temperature=0.3, max_tokens=512):
        """Query Gemma 4 26B (free) via OpenRouter API."""
        if not self.cloud_available:
            return None
        # Free model — no budget check needed

        try:
            import urllib.request
            payload = json.dumps({
                "model": self.cloud_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            })
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=payload.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.cloud_api_key}",
                    "HTTP-Referer": "https://ad-smta.local",
                    "X-Title": "AD-SMTA ALM Brain",
                },
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=60)
            data = json.loads(resp.read().decode())

            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            tokens = usage.get("total_tokens", 0)

            self.cloud_total_queries += 1
            self.cloud_total_tokens += tokens
            cost = self._estimate_cost(tokens)
            self.cloud_last_cost = cost
            self.last_model_used = self.cloud_model
            self._log_model_event("Cloud responded: " + str(tokens) + " tokens ($" + "{:.6f}".format(cost) + ")", tokens, cost)

            return content
        except Exception as e:
            self.write_note(f"Cloud query failed: {e}", "warn")
            return None

    def _estimate_cost(self, tokens):
        """Estimate cost — free model, always $0."""
        return 0.0

    def cloud_total_cost(self):
        """Total cloud cost this session — free model."""
        return 0.0

    # ═══════════════════════════════════════════════════════
    # SMART ROUTING
    # ═══════════════════════════════════════════════════════

    def query(self, prompt, context=None, depth="fast"):
        """
        Smart query routing:
          depth="fast"  → local model (free, instant)
          depth="deep"  → cloud DeepSeek (paid, thorough)
          depth="auto"  → auto-select based on complexity

        Returns: response text or fallback reasoning
        """
        if not self.enabled:
            return self._fallback_reasoning(prompt, context)

        full_prompt = prompt
        if context:
            full_prompt = f"""You are an autonomous quantitative trading AI analyzing Deriv synthetic markets.

Current State:
{json.dumps(context, indent=2, default=str)}

Question: {prompt}

Respond concisely with actionable analysis. Format as JSON if possible."""

        # Route based on depth
        if depth == "deep" and self.cloud_available:
            return self._query_cloud_deep(full_prompt)
        elif depth == "auto":
            if self.session_mode == "STUDY" and self.cloud_available:
                return self._query_cloud_deep(full_prompt)
            else:
                return self._query_local_fast(full_prompt)
        else:
            return self._query_local_fast(full_prompt)

    def _query_local_fast(self, prompt):
        """Fast local query with background polling."""
        try:
            if self._local_future and not self._local_future.done():
                return self.last_response or self._fallback_reasoning(prompt, None)
            self._local_future = self._executor.submit(self._query_local, prompt)
            return self.last_response or self._fallback_reasoning(prompt, None)
        except:
            return self._fallback_reasoning(prompt, None)

    def _query_cloud_deep(self, prompt):
        """Deep cloud query with background polling."""
        try:
            if self._cloud_future and not self._cloud_future.done():
                return self.last_response or self._fallback_reasoning(prompt, None)
            messages = [{"role": "system", "content": "You are a trading AI. Reply ONLY with valid JSON. No explanations, no markdown, no thinking. Just the JSON object."}, {"role": "user", "content": prompt}]
            self._cloud_future = self._executor.submit(
                self._query_cloud, messages, 0.3, 512
            )
            self.write_note("Cloud query sent to DeepSeek V4 Flash", "action")
            return self.last_response or self._fallback_reasoning(prompt, None)
        except:
            return self._fallback_reasoning(prompt, None)

    def poll_result(self):
        """Check if any background query finished."""
        # Check local
        if self._local_future and self._local_future.done():
            try:
                result = self._local_future.result(timeout=0)
                if result:
                    self.total_queries += 1
                    self.last_response = result.strip()
                    return self.last_response
            except:
                pass

        # Check cloud
        if self._cloud_future and self._cloud_future.done():
            try:
                result = self._cloud_future.result(timeout=0)
                if result:
                    self.total_queries += 1
                    self.last_response = result.strip()
                    self.write_note("Cloud response received", "win")
                    return self.last_response
            except:
                pass

        return None

    def _fallback_reasoning(self, prompt, context):
        """Rule-based fallback when no model available."""
        prompt_lower = prompt.lower()
        if "market" in prompt_lower or "select" in prompt_lower:
            return "Market selection: analyzing karma scores and regime conditions. Recommend rotating to highest-scoring market."
        if "strategy" in prompt_lower:
            return "Strategy analysis: checking edge, regime fit, and recent performance. Recommend the strategy with highest risk-adjusted score."
        if "fail" in prompt_lower or "loss" in prompt_lower:
            return "Failure analysis: review market regime, check for overfitting, consider rotating contract family."
        if "risk" in prompt_lower:
            return "Risk assessment: check drawdown, daily loss limit, consecutive losses. If near limits, reduce exposure."
        return "Analyzing... (local model offline, rule-based fallback)"

    # ═══════════════════════════════════════════════════════
    # HIGH-LEVEL ANALYSIS METHODS
    # ═══════════════════════════════════════════════════════

    def analyze_market(self, market_data, trade_history=None, current_regime="unknown"):
        """Analyze market situation — auto-routes to fast or deep."""
        context = {
            "market": market_data,
            "recent_trades": trade_history[-5:] if trade_history else [],
            "regime": current_regime,
            "total_pnl": sum(t.get("profit", 0) for t in (trade_history or [])),
        }
        prompt = "Analyze this market situation. Which strategy family should we use? What is the expected edge? Is the regime favorable?"
        depth = "deep" if self.session_mode == "STUDY" else "fast"
        return self.query(prompt, context, depth=depth)

    def explain_failure(self, trade, strategy, market):
        """AI explanation of why a strategy failed — uses deep model."""
        context = {"trade": trade, "strategy": strategy, "market": market}
        prompt = "Why did this trade fail? What pattern should we look for to avoid similar losses?"
        return self.query(prompt, context, depth="deep" if self.cloud_available else "fast")

    def recommend_next(self, current_state, available_markets, contract_families):
        """AI recommendation for next action — uses deep model."""
        context = {
            "current": current_state,
            "markets": available_markets,
            "families": contract_families,
        }
        prompt = "Based on our performance, which market and contract family should we trade next? Consider diversification and risk management."
        depth = "deep" if self.cloud_available else "fast"
        return self.query(prompt, context, depth=depth)

    def deep_research(self, topic, data=None):
        """Explicit deep research query — always uses cloud model."""
        prompt = f"Deep research: {topic}"
        if data:
            prompt += f"\n\nData:\n{json.dumps(data, indent=2, default=str)}"
        prompt += "\n\nProvide thorough analysis with specific recommendations. Consider multiple angles."
        return self.query(prompt, depth="deep")

    def compare_strategies(self, strategies):
        """Compare multiple strategies using deep analysis."""
        prompt = f"Compare these trading strategies and recommend the best one for current market conditions:\n\n{json.dumps(strategies, indent=2, default=str)}"
        return self.query(prompt, depth="deep" if self.cloud_available else "fast")

    # ═══════════════════════════════════════════════════════
    # SESSION MANAGEMENT
    # ═══════════════════════════════════════════════════════

    def update_session(self, cycle_count):
        if self.session_mode == "EXECUTE":
            self.execute_cycles += 1
            if self.execute_cycles >= self.study_interval:
                self.session_mode = "STUDY"
                self.execute_cycles = 0
                self.study_cycles = 0
                self.write_note("Switched to STUDY mode — deep analysis", "action")
                self.current_task = "Deep market research"
        elif self.session_mode == "STUDY":
            self.study_cycles += 1
            if self.study_cycles >= self.execute_interval:
                self.session_mode = "EXECUTE"
                self.study_cycles = 0
                self.execute_cycles = 0
                self.write_note("Switched to EXECUTE mode — ready to trade", "action")
                self.current_task = "Executing trades"

    def get_session_status(self):
        elapsed = int(time.time() - self.session_start)
        return {
            "mode": self.session_mode,
            "study_cycles": self.study_cycles,
            "execute_cycles": self.execute_cycles,
            "elapsed_seconds": elapsed,
            "study_interval": self.study_interval,
            "execute_interval": self.execute_interval,
            "study_findings": self.study_findings[-5:],
        }

    def study_market(self, scout_status, memory_status):
        findings = []
        markets = scout_status.get("markets", {})
        best_market = None
        best_karma = -1
        for sym, data in markets.items():
            if data.get("active") and data.get("karma", 0) > best_karma:
                best_karma = data["karma"]
                best_market = sym
        if best_market:
            findings.append(f"Best market: {best_market} (karma={best_karma:.2f})")
        best_strats = memory_status.get("best_strategies", [])
        if best_strats:
            top = best_strats[0]
            findings.append(f"Top strategy: {top.get('strategy','?')} (WR={top.get('win_rate',0):.0f}%)")
        patterns = memory_status.get("failure_patterns", [])
        for p in patterns[:2]:
            findings.append(f"Pattern: {p.get('type','?')} -> {p.get('action','?')}")
        if findings:
            self.study_findings = findings
            self.write_note(f"Study: {'; '.join(findings[:3])}", "plan")
        return findings

    def _log_model_event(self, event, tokens=0, cost=0):
        """Log model usage to console and callback."""
        model = self.last_model_used or "rule_based"
        tag = "[MODEL] " + model
        print("  " + tag + ": " + event + " (tokens=" + str(tokens) + ", cost=$" + "{:.6f}".format(cost) + ")", flush=True)
        entry = {"model": model, "event": event, "tokens": tokens, "cost": cost, "time": int(time.time() * 1000)}
        self.model_usage_log.append(entry)
        if len(self.model_usage_log) > 100:
            self.model_usage_log = self.model_usage_log[-100:]
        if self.log_callback:
            try:
                self.log_callback(model, event, tokens, cost)
            except:
                pass

    def write_note(self, note, level="info"):
        self.notes.append({"text": note, "level": level, "time": int(time.time() * 1000)})
        if len(self.notes) > 30:
            self.notes = self.notes[-30:]

    def set_next_decision(self, decision):
        self.next_decision = decision
        self.write_note("Next: " + decision, "plan")

    def set_current_task(self, task):
        self.current_task = task
        self.write_note("Running: " + task, "action")

    def restore_state(self, state_data):
        """Restore brain state from saved data."""
        try:
            if isinstance(state_data, dict):
                self.notes = state_data.get("notes", self.notes)
                self.next_decision = state_data.get("next_decision", self.next_decision)
                self.current_task = state_data.get("current_task", self.current_task)
        except: pass

    def save_state(self):
        """Return state dict for saving."""
        return {
            "notes": self.notes[-50:],
            "next_decision": self.next_decision,
            "current_task": self.current_task,
            "enabled": self.enabled,
        }

    def nexus_decide(self, market, strategy, regime, recent_trades, balance, signal=None,
                    strategy_health=None, risk_state=None, env_text=""):
        """Core AI decision pipeline. Returns dict with ok, reason, confidence, model."""
        prompt = "You are a trading AI. Should we take this trade? Market: " + str(market) + " | Strategy: " + str(strategy) + " | Regime: " + str(regime) + " | Balance: $" + "{:.2f}".format(balance) + " | Signal: " + str(signal)
        if recent_trades:
            prompt += " | Recent: " + json.dumps(recent_trades[-3:], default=str)[:300]
        prompt += ' Answer with JSON: {"ok": true/false, "confidence": 1-10, "reason": "brief explanation"}'

        if self.session_mode == "STUDY" and self.cloud_available:
            result_text = self.query(prompt, depth="deep")
        else:
            result_text = self.query(prompt, depth="fast")

        try:
            if result_text and "{" in result_text:
                start = result_text.index("{")
                end = result_text.index("}") + 1
                parsed = json.loads(result_text[start:end])
                ok = parsed.get("ok", True)
                conf = parsed.get("confidence", 5)
                reason = parsed.get("reason", "brain_decided")
                return {
                    "ok": ok, "reason": reason, "confidence": conf,
                    "model": self.last_model_used or "rule_based",
                    "decision": "TRADE" if ok else "WAIT"
                }
        except (json.JSONDecodeError, ValueError):
            pass

        self._log_model_event("NEXUS fallback: rule-based (no model response)")
        return {
            "ok": True, "reason": "rule_based_fallback", "confidence": 5,
            "model": "rule_based", "decision": "TRADE"
        }

    def build_environment(self, market, strategy, regime, recent_trades, balance,
                          signal=None, strategy_health=None, risk_state=None,
                          tick_data=None, cpp_predictions=None, all_markets=None,
                          price_action=None):
        """Build environment snapshot for NEXUS decision."""
        return {
            "market": market, "strategy": strategy, "regime": regime,
            "balance": balance, "signal": signal,
            "strategy_health": strategy_health, "risk_state": risk_state,
            "recent_trades": recent_trades[-5:] if recent_trades else [],
            "tick_data": tick_data, "cpp_predictions": cpp_predictions,
            "all_markets": all_markets, "price_action": price_action,
            "session_mode": self.session_mode,
            "cloud_cost": round(self.cloud_total_cost(), 4),
            "queries_today": self.total_queries,
        }

    def format_environment(self, env):
        """Format environment snapshot as text for model input."""
        lines = []
        lines.append("Market: " + str(env.get("market", "?")) + " | Strategy: " + str(env.get("strategy", "?")))
        lines.append("Regime: " + str(env.get("regime", "?")) + " | Balance: $" + "{:.2f}".format(env.get("balance", 0)))
        lines.append("Signal: " + str(env.get("signal", "none")) + " | Mode: " + str(env.get("session_mode", "?")))
        risk = env.get("risk_state")
        if risk:
            lines.append("Risk: mode=" + str(risk.get("trading_mode", "?")) + " streak=" + str(risk.get("consec_loss", 0)) + "L/" + str(risk.get("consec_win", 0)) + "W")
        trades = env.get("recent_trades", [])
        if trades:
            lines.append("Recent: " + json.dumps(trades[-3:], default=str)[:200])
        return "\n".join(lines)

    def quick_check(self, market, strategy, contract, regime, balance, consec_loss):
        """Quick brain check — uses model when available, falls back to rules."""
        # Hard rules first (no model needed)
        if consec_loss >= 5:
            self._log_model_event("Quick BLOCKED: " + str(consec_loss) + " consecutive losses")
            return {"ok": False, "reason": f"Too many consecutive losses ({consec_loss})"}
        if consec_loss >= 3 and balance < 9000:
            self._log_model_event("Quick BLOCKED: loss streak + low balance")
            return {"ok": False, "reason": f"Loss streak {consec_loss} with low balance"}

        # Ask model (shows [MODEL] log)
        prompt = "Quick trade check. Market: " + str(market) + " Strategy: " + str(strategy) + " Contract: " + str(contract) + " Regime: " + str(regime) + " Balance: $" + "{:.2f}".format(balance) + " ConsecLoss: " + str(consec_loss) + '. Reply JSON: {"ok":true/false,"reason":"brief"}'
        try:
            # Synchronous call — try local first, fallback to cloud
            result_text = None
            if self.connected:
                result_text = self._query_local(prompt)
            if not result_text and self.cloud_available:
                messages = [{"role": "system", "content": "You are a trading AI. Reply ONLY with valid JSON. No explanations, no markdown, no thinking. Just the JSON object."}, {"role": "user", "content": prompt}]
                result_text = self._query_cloud(messages, 0.3, 256)

            if result_text and "{" in result_text:
                start = result_text.index("{")
                end = result_text.index("}") + 1
                parsed = json.loads(result_text[start:end])
                ok = parsed.get("ok", True)
                reason = parsed.get("reason", "model_checked")
                model = self.last_model_used or "rule_based"
                self._log_model_event("Quick check: ok=" + str(ok) + " model=" + model + " reason=" + reason[:40])
                return {"ok": ok, "reason": reason}
        except Exception as e:
            self._log_model_event("Quick check model error: " + str(e))

        self._log_model_event("Quick check: rule fallback -> approved")
        return {"ok": True, "reason": "rule_based_approved"}

    def self_evolve(self, pnl_history, trade_log, context):
        """Self-evolve based on persistent knowledge. Returns {"evolved": True/False, "changes": [...]}"""
        if not pnl_history:
            return {"evolved": False, "changes": []}
        recent_pnl = sum(pnl_history[-10:]) if len(pnl_history) >= 10 else sum(pnl_history)
        changes = []

        # Use persistent memory for smarter evolution
        try:
            accuracy = self._mem.get_model_accuracy() if hasattr(self, "_mem") and self._mem else {}
            families = self._mem.get_family_insights() if hasattr(self, "_mem") and self._mem else []
            sessions = self._mem.get_session_insights() if hasattr(self, "_mem") and self._mem else {}
        except:
            accuracy = {}
            families = []
            sessions = {}

        # Stake adjustment
        if recent_pnl < -5 and context.get("stake", 0) > 1:
            changes.append({"type": "reduce_stake", "reason": f"Recent PnL ${recent_pnl:+.2f} negative"})
        if recent_pnl > 5:
            changes.append({"type": "boost_confidence", "reason": f"Recent PnL ${recent_pnl:+.2f} positive"})

        # AI accuracy-based evolution
        ai_acc = accuracy.get("accuracy_pct", 0)
        if ai_acc > 0 and ai_acc < 40:
            changes.append({"type": "increase_rule_weight", "reason": f"AI accuracy {ai_acc:.0f}% — rule-based decisions may be better"})
        elif ai_acc > 70:
            changes.append({"type": "increase_ai_weight", "reason": f"AI accuracy {ai_acc:.0f}% — trust AI decisions more"})

        # Family-based evolution
        if families:
            best = families[0] if families else {}
            worst = families[-1] if len(families) > 1 else {}
            if best.get("total_profit", 0) > 0:
                changes.append({"type": "prefer_family", "family": best.get("family", ""), "reason": f"Best family: {best.get('family', '?')} (${best.get('total_profit', 0):+.2f})"})
            if worst.get("total_profit", 0) < -5:
                changes.append({"type": "avoid_family", "family": worst.get("family", ""), "reason": f"Worst family: {worst.get('family', '?')} (${worst.get('total_profit', 0):+.2f})"})

        # Session-based evolution
        if sessions and sessions.get("avg_win_rate", 0) < 40:
            changes.append({"type": "reduce_frequency", "reason": f"Session avg WR {sessions.get('avg_win_rate', 0):.0f}% — trade less frequently"})

        self.write_note(f"Self-evolve: PnL={recent_pnl:+.2f} changes={len(changes)} ai_acc={ai_acc:.0f}%", "evolve")
        return {"evolved": len(changes) > 0, "changes": changes}
    def record_trade_result(self, profit):
        """Record trade result for ALM learning."""
        if not hasattr(self, "_trade_results"):
            self._trade_results = []
        self._trade_results.append({"profit": profit, "time": int(time.time() * 1000)})
        if len(self._trade_results) > 200:
            self._trade_results = self._trade_results[-200:]
        total = sum(r["profit"] for r in self._trade_results)
        wins = sum(1 for r in self._trade_results if r["profit"] > 0)
        self.write_note(f"Trade: ${profit:+.2f} | Running PnL: ${total:+.2f} WR: {wins}/{len(self._trade_results)}", "trade")

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.write_note(f"{'Enabled' if enabled else 'Disabled'}", "action")

    def _get_model_counts(self):
        """Count queries per model."""
        counts = {}
        for entry in self.model_usage_log:
            m = entry.get("model", "unknown")
            counts[m] = counts.get(m, 0) + 1
        return counts

    def get_status(self):
        return {
            "connected": self.connected,
            "model": self.model,
            "cloud_available": self.cloud_available,
            "cloud_model": self.cloud_model if self.cloud_available else None,
            "cloud_queries": self.cloud_total_queries,
            "cloud_tokens": self.cloud_total_tokens,
            "cloud_cost": round(self.cloud_total_cost(), 4),
            "cloud_budget": self.cloud_budget,
            "last_model_used": self.last_model_used,
            "total_queries": self.total_queries,
            "total_tokens": self.total_tokens,
            "last_response": self.last_response[:200] if self.last_response else "",
            "notes": self.notes[-10:],
            "model_usage_log": self.model_usage_log[-20:],
            "model_counts": self._get_model_counts(),
            "next_decision": self.next_decision,
            "current_task": self.current_task,
            "session_mode": self.session_mode,
            "session": self.get_session_status(),
        }
