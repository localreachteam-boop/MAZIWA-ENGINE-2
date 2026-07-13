#!/usr/bin/env python3
"""
AD-SMTA — Autonomous Deriv Synthetic Market Trading Agent
Full pipeline: Scout → Sensor → Brain → Governor → Muscle
Auto-selects best market. Streams live to dashboard.
"""
import asyncio, json, os, time, threading
from pathlib import Path
import websockets

from agents.scout import ScoutAgent
from agents.sensor import SensorAgent
from agents.brain import BrainAgent
from agents.governor import GovernorAgent
from agents.muscle import MuscleAgent
from agents.protector import Protector
from agents.memory import Memory
from agents.simulator import Simulator
from agents.strategist import Strategist
from agents.judge import Judge
from agents.contract_picker import ContractPicker
from agents.decider import DeciderAgent
from agents.cpp_engine import CPPMarketEngine
from agents.alm_brain import ALMBrain
from agents.executor import ExecutionAgent
from agents.recovery import RecoveryAgent
from agents.adversarial import AdversarialAgent
from agents.portfolio import PortfolioAgent
from agents.resource_manager import ResourceManager
from agents.resource_awareness import ResourceAwareness
from agents.phone_resources import PhoneResources
from agents.crypto_agent import CryptoAgent
from agents.mt5_agent import MT5Agent
from agents.multi_market import MultiMarketCore
from agents.composio_agent import ComposioAgent
from agents.discord_reporter import DiscordReporter
from config import (DERIV_TOKEN, DERIV_WS_URL, ALL_MARKETS, MARKET_TYPES,
    TICK_BUFFER, INITIAL_BALANCE, MAX_DAILY_LOSS_PCT, KELLY_FRACTION,
    MIN_EDGE, MAX_STAKE_PCT, MIN_STAKE, HTTP_PORT, WS_PORT, LOG_FILE,
    MAX_CONCURRENT_TRADES, PERFORMANCE_THRESHOLD, ADJUSTMENT_COOLDOWN)

STATE_FILE = Path(__file__).parent / "trading_state.json"
EXEC_LOG = Path(__file__).parent / "executor_debug.log"
connected_clients = set()

def _elog(msg):
    """Append to executor debug log."""
    try:
        with open(EXEC_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except:
        pass
_last_state = {}
TRADE_LOG = []

# ── Dashboard Helpers ────────────────────────────────────
async def broadcast(msg):
    dead = []
    payload = json.dumps(msg)
    for c in list(connected_clients):
        try: await c.send(payload)
        except: dead.append(c)
    for d in dead: connected_clients.discard(d)

async def broadcast_log(msg, log_type="info"):
    await broadcast({"type": "log", "message": msg, "log_type": log_type})

async def dash_handler(websocket):
    connected_clients.add(websocket)
    try:
        await websocket.send(json.dumps(_last_state))
        async for _ in websocket: pass
    except: pass
    finally: connected_clients.discard(websocket)

# ── Symbol → Type lookup ────────────────────────────────
SYM_TO_TYPE = {}
for m in ALL_MARKETS:
    SYM_TO_TYPE[m["symbol"]] = m["type"]

# ── AD-SMTA Core ────────────────────────────────────────
class ADSMTA:
    def __init__(self):
        self.scout = ScoutAgent()
        self.sensor = SensorAgent(TICK_BUFFER)
        self.memory = Memory()
        self.brain = BrainAgent(MIN_EDGE, self.memory)
        self.strategist = Strategist(self.memory)
        self.governor = GovernorAgent(
            INITIAL_BALANCE, MAX_DAILY_LOSS_PCT, KELLY_FRACTION,
            MIN_EDGE, MAX_STAKE_PCT, MIN_STAKE)
        self.muscle = MuscleAgent()
        self.decider = DeciderAgent()
        self.cpp_engine = CPPMarketEngine()
        self.cpp_engine.enabled = True
        self.alm_brain = ALMBrain()
        self.alm_brain.enabled = True
        self.executor = ExecutionAgent()
        self.executor.enabled = True
        self.protector = Protector()
        self.simulator = Simulator()
        self.strategist = Strategist()
        self.judge = Judge()
        self.picker = ContractPicker()
        self.recovery = RecoveryAgent()
        self.adversarial = AdversarialAgent()
        self.portfolio = PortfolioAgent()
        self.resource_mgr = ResourceManager()
        self.resource_awareness = ResourceAwareness()
        self.phone_resources = PhoneResources()
        self.crypto_agent = CryptoAgent()
        self.mt5_agent = MT5Agent()
        self.multi_market = MultiMarketCore()
        self.composio = ComposioAgent()
        self.discord = DiscordReporter(os.environ.get('DISCORD_WEBHOOK', ''))

        self.ws = None
        self.running = False
        self.cycle_count = 0
        self.trades_count = 0
        self.trading = False
        self.cur_streak = 0
        self.best_streak = 0
        self.selected_market = None
        self.selected_type = None
        self.trades_on_market = 0
        self.TRADES_PER_MARKET = 3  # rotate market after 3 trades
        self.selected_strategy = None
        self.last_signal = {}
        self.last_recommendation = {}
        self.consecutive_no_edge = 0
        self.reselect_interval = 20  # re-evaluate market every 60 cycles
        self.balance = INITIAL_BALANCE

    async def connect(self):
        print("[AD-SMTA] Connecting to Deriv WebSocket...")
        self.ws = await websockets.connect(DERIV_WS_URL, open_timeout=15)
        await self.ws.send(json.dumps({"authorize": DERIV_TOKEN}))
        r = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
        if "error" in r:
            print(f"AUTH FAIL: {r['error']}")
            return False
        info = r["authorize"]
        self.balance = info["balance"]
        self.governor.balance = info["balance"]
        self.governor.initial_balance = info["balance"]
        self.governor.start_of_day_balance = info["balance"]
        print(f"Auth: OK | Bal: ${info['balance']}")
        await broadcast_log(f"Connected | Balance: ${info['balance']}", "info")
        self.decider.init_session(info["balance"])
        self.protector.init(info["balance"])
        self.running = True
        return True

    async def subscribe_all(self):
        """Subscribe to tick streams for all markets."""
        syms = self.scout.get_subscription_list()
        for sym in syms:
            try:
                await self.ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
            except:
                pass
        print(f"Subscribed to {len(syms)} markets")

    async def _send(self, data):
        """Send a message on the WebSocket."""
        if self.ws:
            await self.ws.send(json.dumps(data))

    async def recv_with_processing(self, timeout=10):
        """Receive a message, routing ticks through the pipeline."""
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(raw)
            return data
        except asyncio.TimeoutError:
            return None
        except websockets.ConnectionClosed:
            raise
        except Exception:
            return None

    async def process_tick(self, tick):
        """Route a tick through the full pipeline."""
        symbol = tick.get("symbol", "unknown")
        market_type = SYM_TO_TYPE.get(symbol, "volatility")

        # Stage 1: Sensor ingestion
        sym, signal = self.sensor.ingest_tick(tick, market_type)

        # Feed tick to C++ engine for ML predictions
        if self.cpp_engine.connected and getattr(self.cpp_engine, "enabled", True):
            try:
                cpp_result = self.cpp_engine.add_tick(
                    float(tick.get("quote", 0)),
                    float(tick.get("epoch", 0))
                )
                if cpp_result and signal:
                    signal["cpp_signal"] = cpp_result.get("signal", 0)
                    signal["cpp_confidence"] = cpp_result.get("confidence", 0)
                    signal["cpp_ev"] = cpp_result.get("ev", 0)
            except:
                pass

        # Record in Scout
        self.scout.record_tick(symbol, float(tick.get("quote", 0)), tick.get("epoch", 0))

        if signal is None:
            return

        # Scout evaluates market condition (ALL markets)
        analysis_preview = {
            "opportunity_score": 0,
            "regime": signal["statistics"]["regime"],
        }
        self.scout.update_market_analysis(symbol, None, [analysis_preview])

        # Record digit in memory for learning
        if signal:
            prices = list(self.sensor.market_data[symbol].prices) if symbol in self.sensor.market_data else []
            if prices:
                last_digit = int(str(prices[-1]).replace(".", "")[-1])
                self.memory.record_digit(symbol, last_digit)

        # Inject digit bias into signal for Brain
        if signal:
            freqs, best_digit, bias = self.memory.get_digit_bias(symbol)
            if freqs:
                signal["memory_digit_freq"] = {str(k): round(v, 4) for k, v in freqs.items()}
                signal["memory_best_digit"] = best_digit
                signal["memory_digit_bias"] = round(bias, 4)

        # Stage 2: Brain analysis (ALL markets get analyzed)
        recommendation, all_strategies = self.brain.analyze(signal)
        self.last_signal = signal

        # Always update Scout with all strategies found
        self.scout.update_market_analysis(symbol, recommendation, all_strategies)

        if recommendation is None:
            self.consecutive_no_edge += 1
            if self.consecutive_no_edge > self.reselect_interval:
                self.selected_market = None
                self.consecutive_no_edge = 0
            return

        self.last_recommendation = recommendation
        self.consecutive_no_edge = 0

        # ALM Brain: always study regardless of executor mode
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                self.alm_brain.update_session(self.cycle_count)
                # Study always runs — the executor handles trades independently
                self.alm_brain.study_market(
                    self.scout.get_status(),
                    self.memory.get_memory_summary()
                )
                self.alm_brain.set_current_task(f"Studying {symbol} patterns | Executor: {self.executor.status}")
            except:
                pass

        # Only TRADE on selected market (analysis already done above)
        if self.selected_market and symbol != self.selected_market:
            return

        # Stage 3: Decider — entry/exit decision
        decision = self.decider.decide(recommendation, self.governor.status())

        if decision["decision"] != "TRADE":
            return

        # Stage 4: Contract Picker — select best contract type
        pick = self.picker.pick_best(
            recommendation.get("regime", "UNKNOWN"),
            recommendation.get("strategy", ""),
            signal,
            signal.get("digit_frequency") if signal else None,
        )
        recommendation["contract_type"] = pick["contract_type"]
        # Agent note: contract pick
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                pick_name = pick.get("name", pick["contract_type"])
                self.alm_brain.write_note(f"Picked {pick_name} ({pick.get('reason','scored')})", "pick")
            except:
                pass
        recommendation["picker_score"] = pick.get("score", 0)
        recommendation["picker_reason"] = pick.get("reason", "")
        if pick.get("digit") is not None:
            recommendation.setdefault("params", {})["digit"] = pick["digit"]

        # Stage 5: Governor validation
        sim_result = self.simulator.simulate_strategy(signal, recommendation)
        adj = self.memory.get_adjustments()
        approved = self.governor.validate(recommendation, adj)

        if approved["status"] == "REJECTED":
            return

        # Stage 6: Protector — hard account safety
        self.protector.set_open_contracts(self.muscle.pending_count())
        allowed, reason = self.protector.check()
        if not allowed:
            return

        # Stage 7: Judge — 8-question final decision
        strat_key = f"{signal['market']}:{recommendation.get('strategy', recommendation.get('direction', '?'))}"
        health_score = self.strategist.strategies.get(strat_key, {}).get('health_score')
        judge_result = self.judge.evaluate({
            "market": signal['market'],
            "market_type": signal.get('market_type', 'unknown'),
            "strategy": recommendation,
            "regime": recommendation.get('regime', 'UNKNOWN'),
            "sim_result": sim_result,
            "strategy_health": health_score,
            "risk_clearance": allowed,
            "protector_status": self.protector.get_status(),
            "memory_stats": self.memory.get_memory_summary(),
            "signal": signal,
        })
        # Agent note: judge decision
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                self.alm_brain.write_note(f"Judge: {judge_result.get('decision','?')} - {judge_result.get('reason','')[:50]}",
                                          "win" if judge_result.get('decision') == 'TRADE' else "loss")
            except:
                pass

        if judge_result["decision"] != "TRADE":
            return

        # Stage 8: Don't stack trades
        if self.trading or self.muscle.pending_count() >= MAX_CONCURRENT_TRADES:
            return

        # ALM Brain: poll for completed queries
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                self.alm_brain.poll_result()
            except:
                pass

        # ALM Brain: Ask AI for strategic reasoning
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True) and self.cycle_count % 50 == 0:
            try:
                brain_input = {
                    "market": signal.get("market"),
                    "strategy": recommendation.get("strategy"),
                    "regime": recommendation.get("regime"),
                    "edge": recommendation.get("edge"),
                    "balance": self.governor.balance,
                    "trades_today": self.governor.trades_today,
                    "win_rate": self.governor.status()["win_rate"],
                }
                brain_reason = self.alm_brain.query(
                    "Should we trade this? What's the risk?", brain_input
                )
                self.alm_brain.last_reasoning = brain_reason[:100]
            except:
                pass

        # Agent note: executor queued
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                self.alm_brain.set_current_task(
                    f'Executor: {approved.get("contract_type","?")} on {approved["market"]} stake=${approved["stake"]:.2f}')
            except:
                pass

        # Executor cooldown/rules check — block if cooling down
        can_go, cd_reason = self.executor.can_trade(
            approved["market"], approved.get("contract_type", "CALL"), approved["stake"])
        if not can_go:
            print(f"  [BLOCKED] {cd_reason}")
            await broadcast_log(f"Blocked: {cd_reason}", "warning")
            return

        _elog(f"QUEUED {approved.get('contract_type','?')} on {approved['market']} stake=${approved.get('stake',0):.2f}")
        # Queue trade to Execution Agent (executed in main loop)
        proposal, trade_id = self.muscle.build_proposal(approved)
        self.executor.queue_trade(
            proposal, approved["market"], approved.get("contract_type", "CALL"),
            approved.get("strategy", "?"), approved["stake"],
            approved.get("params", {})
        )

    async def _execute_trade(self, approved):
        """Execute a trade through the Deriv API."""
        self.trading = True
        proposal, trade_id = self.muscle.build_proposal(approved)

        try:
            await self.ws.send(json.dumps(proposal))
            p_resp = await self._recv_until("proposal", 10)

            if not p_resp or "proposal" not in p_resp:
                print(f"  [X] Proposal failed for {approved['market']}")
                return

            p = p_resp["proposal"]
            pid = p.get("id", "")
            payout = p.get("payout", 0)
            ctype = approved.get("contract_type", "?")
            picker_score = approved.get("picker_score", 0)
            mkt = approved["market"]
            direction = approved["direction"]
            stake_val = approved["stake"]
            print(f"  [>] {mkt} {ctype} {direction} "
                  f"Stake=${stake_val:.2f} Payout=${payout:.2f} Score={picker_score:.0f}")
            await broadcast_log(f"Proposal: {ctype} {direction} on {mkt} Payout=${payout:.2f}", "trade")

            await self.ws.send(json.dumps({"buy": pid, "price": stake_val, "subscribe": 1}))
            b_resp = await self._recv_until("buy", 10)

            buy_ok = b_resp and "buy" in b_resp and not b_resp.get("buy", {}).get("error")
            self.protector.record_trade_attempt(buy_ok)

            if not buy_ok:
                print(f"  [X] Buy failed")
                return

            cid = b_resp["buy"].get("contract_id", 0)
            self.muscle.store_pending(trade_id, cid, proposal)
            self.trades_count += 1
            self.trades_on_market += 1
            self.scout.record_trade(approved["market"])
            print(f"  [+] OPENED #{cid} ({mkt})")
            await broadcast_log(f"OPENED {ctype} {direction} on {mkt} Stake=${stake_val:.2f}", "trade")

            result = await self._wait_for_result(cid, approved)

            if result:
                profit = result.get("profit", 0)
                stake = stake_val
                self.governor.record_outcome(profit, stake, approved["probability"])
                self.decider.record_trade(profit, stake)
                self.protector.record_trade_result(profit)
                strat_key = f"{mkt}:{approved.get('strategy', approved.get('direction', '?'))}"
                self.strategist.record_trade(strat_key, profit)
                self.picker.record_result(approved.get("contract_type", "?"), profit, approved["market"])

                # Force market rotation after consecutive losses or DIGITMATCH win
                if self.picker.should_rotate_market():
                    self.selected_market = None
                    self.trades_on_market = 0
                    print(f"  [ROTATE] Forced market switch after contract change")

                # Force market rotation after N trades on same market
                if self.trades_on_market >= self.TRADES_PER_MARKET:
                    self.selected_market = None
                    self.trades_on_market = 0
                    print(f"  [ROTATE] Market rotation after {self.TRADES_PER_MARKET} trades")

                self.memory.record_trade(
                    mkt, approved.get("strategy", approved.get("direction", "?")),
                    approved["contract_type"], profit, stake,
                    {"probability": approved.get("probability", 0), "edge": approved.get("edge", 0)}
                )
                # ALM Brain: Learn from result
                if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
                    try:
                        self.alm_brain.query(
                            f"Trade result: {'WIN' if profit > 0 else 'LOSS'} ${profit:.2f} on {mkt} with {approved.get('contract_type', '?')}",
                            {"profit": profit, "market": mkt, "contract": approved.get("contract_type"),
                             "balance": self.governor.balance}
                        )
                    except:
                        pass

                # Feed result to C++ engine for online learning
                if self.cpp_engine.connected and getattr(self.cpp_engine, "enabled", True):
                    try:
                        self.cpp_engine.learn(profit, stake)
                    except:
                        pass

                # ALM: Update market profile and strategy lifecycle
                self.memory.update_market_profile(
                    mkt, approved.get("regime", "UNKNOWN"),
                    approved.get("strategy", "?"), profit
                )
                strat_key_mem = f"{mkt}:{approved.get('strategy', '?')}"
                if profit > 0:
                    self.memory.record_strategy_lifecycle(strat_key_mem, "TRADE", f"Win ${profit:.2f}")
                elif self.strategist.should_retire(strat_key_mem):
                    self.memory.record_strategy_lifecycle(strat_key_mem, "RETIRE", "Failed strategy")

                if profit > 0:
                    self.cur_streak += 1
                    self.best_streak = max(self.best_streak, self.cur_streak)
                else:
                    self.cur_streak = 0

                log_type = "win" if profit > 0 else "loss"
                print(f"  [{"W" if profit > 0 else "L"}] P&L: ${profit:+.2f} "
                      f"Bal: ${self.governor.balance:.2f} "
                      f"W/L: {self.governor.wins}/{self.governor.losses}")
                await broadcast_log(f"{"WIN" if profit > 0 else "LOSS"} {ctype} on {mkt} P&L: ${profit:+.2f} Bal: ${self.governor.balance:.2f}", log_type)

                msg = {
                    "type": "result", "trade_id": trade_id,
                    "contract_id": cid, "market": mkt,
                    "direction": direction,
                    "contract_type": approved.get("contract_type", "?"),
                    "strategy": approved.get("strategy", "?"),
                    "stake": stake, "profit": profit,
                    "probability": approved["probability"],
                    "edge": approved.get("edge", 0),
                    "balance": self.governor.balance,
                    "trades": self.governor.trades_today,
                    "wins": self.governor.wins,
                    "losses": self.governor.losses,
                    "win_rate": self.governor.status()["win_rate"],
                    "time": int(time.time() * 1000),
                }
                TRADE_LOG.append(msg)
                await broadcast(msg)
                # Discord alert
                try:
                    self.discord.trade_result(mkt, ctype, stake, profit, self.governor.balance, self.governor.status()['win_rate'])
                except: pass

        except Exception as e:
            print(f"  [!] Execution error: {e}")
        finally:
            self.trading = False

    async def _execute_trade_exec(self, approved):
        """Execute trade — fire-and-forget. Main loop handles result via proposal_open_contract."""
        _elog(f"START {approved.get('contract_type','?')} on {approved['market']} stake=${approved.get('stake',0):.2f}")

        can_go, cd_reason = self.executor.can_trade(
            approved["market"], approved.get("contract_type", "CALL"), approved["stake"])
        if not can_go:
            _elog(f"BLOCKED {cd_reason}")
            self.executor.set_status(f"BLOCKED: {cd_reason[:40]}")
            return

        self.executor.executing = True
        self.executor.set_status("EXECUTING")
        trade_id = f"EX{int(time.time() * 1000)}"
        proposal, _ = self.muscle.build_proposal(approved)

        try:
            await self.ws.send(json.dumps(proposal))
            p_resp = await self._recv_until("proposal", 10)

            if not p_resp or "proposal" not in p_resp:
                _elog(f"PROPOSAL_FAILED {approved['market']} resp={str(p_resp)[:150]}")
                self.executor.set_status("IDLE")
                return

            p = p_resp["proposal"]
            pid = p.get("id", "")
            payout = p.get("payout", 0)
            ctype = approved.get("contract_type", "?")
            mkt = approved["market"]
            _elog(f"PROPOSAL_OK id={pid} payout=${payout:.2f}")

            await broadcast_log(f"Executor: {ctype} on {mkt} Payout=${payout:.2f}", "trade")
            await self._send({"buy": pid, "price": payout, "subscribe": 1})

            buy_resp = await self._recv_until("buy", 10)
            if not buy_resp or "buy" not in buy_resp:
                _elog(f"BUY_FAILED {mkt} resp={str(buy_resp)[:150]}")
                self.executor.set_status("IDLE")
                return

            cid = buy_resp["buy"].get("contract_id", 0)
            self.muscle.store_pending(trade_id, cid, proposal, approved)
            # Subscribe to contract updates for result
            await self._send({"proposal_open_contract": 1, "contract_id": cid, "subscribe": 1})
            self.executor.set_status(f"TRADING {ctype} on {mkt}")
            _elog(f"FIRED contract_id={cid}")

            # Store trade info for result handler
            self._pending_exec_trades = getattr(self, '_pending_exec_trades', {})
            self._pending_exec_trades[cid] = {
                "trade_id": trade_id, "market": mkt, "ctype": ctype,
                "strategy": approved.get("strategy", "?"), "stake": proposal.get("amount", 0),
                "edge": approved.get("edge", 0), "probability": approved.get("probability", 0),
                "regime": approved.get("regime", "UNKNOWN"),
                "fired_at": time.time(),
                "balance_at_fire": self.governor.balance,
            }
            
            # Launch background task to wait for result
            asyncio.ensure_future(self._wait_and_record(cid, trade_id, mkt, ctype, approved, proposal))

        except Exception as e:
            import traceback
            _elog(f"ERROR {e} | {traceback.format_exc()}")
            self.executor.set_status(f"ERROR: {str(e)[:40]}")
        finally:
            self.executor.executing = False
            # Always clear stuck status after buy cycle completes
            status = self.executor.status
            if status in ("EXECUTING",) or status.startswith("WAITING") or status.startswith("TRADING"):
                self.executor.set_status("IDLE")
                _elog(f"CLEAR status -> IDLE")

    async def _wait_and_record(self, cid, trade_id, mkt, ctype, approved, proposal):
        """Background task: wait for contract result via proposal_open_contract."""
        _elog(f"WAIT_RECORD started for cid={cid}")
        
        # Also try balance-change detection as fallback
        balance_before = self.governor.balance
        
        result = await self._wait_for_result(cid, approved, timeout=30)
        
        # Fallback: detect result via balance change if POC didn't fire
        if result is None:
            new_bal = self.governor.balance
            if abs(new_bal - balance_before) > 0.01:
                result = {"profit": new_bal - balance_before, "status": "WIN" if new_bal > balance_before else "LOSS"}
                _elog(f"BALANCE_FALLBACK detected result: profit={result['profit']:.2f}")
        if result is None:
            _elog(f"WAIT_RECORD timeout for cid={cid}")
            return
        
        profit = result["profit"]
        stake = proposal.get("amount", 0)
        self.governor.record_outcome(profit, stake)
        self.executor.record_result(mkt, ctype, profit, stake)
        self.executor.current_balance = self.governor.balance
        self.picker.record_result(ctype, profit, market=mkt)
        self.memory.record_trade(
            mkt, approved.get("strategy", "?"), ctype, profit, stake,
            {"probability": approved.get("probability", 0), "edge": approved.get("edge", 0)})
        self.memory.update_market_profile(
            mkt, approved.get("regime", "UNKNOWN"), approved.get("strategy", "?"), profit)
        
        rot = self.executor.get_rotation_signal()
        if rot["rotate_contract"]:
            try: self.picker.force_rotate()
            except: pass
            self.executor.on_contract_rotated()
        if rot["rotate_market"]:
            self.selected_market = None
            self.executor.on_market_rotated()
        
        if profit > 0:
            self.cur_streak += 1
            self.best_streak = max(self.best_streak, self.cur_streak)
        else:
            self.cur_streak = 0
        
        _elog(f"RESULT {ctype} on {mkt} P&L=${profit:+.2f} Bal=${self.governor.balance:.2f}")
        log_type = "win" if profit > 0 else "loss"
        # Force state broadcast after trade result
        try: await self.broadcast_state()
        except: pass
        await broadcast_log(
            f"Executor {ctype} on {mkt} {'WIN' if profit>0 else 'LOSS'} P&L: ${profit:+.2f} Bal: ${self.governor.balance:.2f}", log_type)
        
        msg = {
            "type": "result", "trade_id": trade_id,
            "contract_id": cid, "market": mkt,
            "contract_type": ctype, "strategy": approved.get("strategy", "?"),
            "stake": stake, "profit": profit,
            "probability": approved.get("probability", 0),
            "edge": approved.get("edge", 0),
            "balance": self.governor.balance,
            "trades": self.executor.trades_executed,
            "wins": self.executor.trades_won,
            "losses": self.executor.trades_lost,
            "win_rate": self.executor.win_rate,
            "agent": "executor",
            "time": int(time.time() * 1000),
        }
        TRADE_LOG.append(msg)
        await broadcast(msg)
        # Discord alert
        try:
            self.discord.trade_result(mkt, ctype, stake, profit, self.governor.balance, self.governor.status()['win_rate'])
        except: pass

        # Clean up pending
        pending = getattr(self, '_pending_exec_trades', {})
        pending.pop(cid, None)
        
        # ALM Brain learn
        if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
            try:
                self.alm_brain.write_note(
                    f"Executor: {'WIN' if profit>0 else 'LOSS'} ${profit:+.2f} {ctype} on {mkt}", log_type)
            except: pass
        
        # C++ engine learn
        if self.cpp_engine.connected and getattr(self.cpp_engine, "enabled", True):
            try: self.cpp_engine.learn(profit, stake)
            except: pass

    async def _recv_until(self, key, timeout=10):
        """Receive WS messages until we get one containing `key`, or timeout.
        Any proposal_open_contract messages are queued for later processing."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=2)
                data = json.loads(raw)
                if key in data:
                    return data
                if "tick" in data:
                    await self.process_tick(data["tick"])
                if "balance" in data:
                    self.balance = data["balance"].get("balance", 0)
                    self.governor.balance = self.balance
                # Queue proposal_open_contract for later processing
                if "proposal_open_contract" in data:
                    self._queued_poc = getattr(self, '_queued_poc', [])
                    self._queued_poc.append(data["proposal_open_contract"])
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                raise
            except Exception:
                continue
        return None
    async def _wait_for_result(self, contract_id, approved, timeout=120):
        """Wait for contract to resolve while processing other ticks."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
                data = json.loads(raw)

                if "proposal_open_contract" in data:
                    poc = data["proposal_open_contract"]
                    _elog(f"POC cid={poc.get("contract_id")} sold={poc.get("is_sold")} profit={poc.get("profit")} pending_keys={list(getattr(self, "_pending_exec_trades", {}).keys())}")
                    if poc.get("is_sold"):
                        self.muscle.resolve_pending(contract_id)
                        return {
                            "profit": poc.get("profit", 0),
                            "status": "WIN" if poc.get("profit", 0) > 0 else "LOSS",
                        }

                elif "tick" in data:
                    await self.process_tick(data["tick"])

                elif "balance" in data:
                    new_bal = data["balance"].get("balance", 0)
                    bal_diff = new_bal - self.balance if self.balance else 0
                    _elog(f"BALANCE old=${self.balance:.2f} new=${new_bal:.2f} diff=${bal_diff:.2f} pending={list(getattr(self, '_pending_exec_trades', {}).keys())}")
                    self.balance = new_bal
                    self.governor.balance = self.balance
                    self.decider.update_balance(self.balance)
                    self.protector.update_balance(self.balance)
                    
                    # Detect trade results via balance changes
                    pending_exec = getattr(self, '_pending_exec_trades', {})
                    for cid, et in list(pending_exec.items()):
                        bal_change = self.balance - et.get("balance_at_fire", self.balance)
                        if abs(bal_change) > 0.01 and time.time() - et.get("fired_at", 0) > 1:
                            profit = bal_change
                            stake = et.get("stake", 0)
                            mkt = et["market"]
                            ctype = et["ctype"]
                            self.governor.balance = self.balance
                            self.executor.record_result(mkt, ctype, profit, stake)
                            self.executor.current_balance = self.balance
                            self.muscle.resolve_pending(cid)
                            self.picker.record_result(ctype, profit, market=mkt)
                            self.memory.record_trade(
                                mkt, et.get("strategy", "?"), ctype, profit, stake,
                                {"probability": et.get("probability", 0), "edge": et.get("edge", 0)})
                            self.memory.update_market_profile(
                                mkt, et.get("regime", "UNKNOWN"), et.get("strategy", "?"), profit)
                            rot = self.executor.get_rotation_signal()
                            if rot["rotate_contract"]:
                                try: self.picker.force_rotate()
                                except: pass
                                self.executor.on_contract_rotated()
                            if rot["rotate_market"]:
                                self.selected_market = None
                                self.executor.on_market_rotated()
                            _elog(f"BAL_DETECT {ctype} on {mkt} P&L=${profit:+.2f}")
                            log_type = "win" if profit > 0 else "loss"
                            await broadcast_log(
                                f"Executor {ctype} on {mkt} {'WIN' if profit>0 else 'LOSS'} P&L: ${profit:+.2f} Bal: ${self.balance:.2f}", log_type)
                            msg = {
                                "type": "result", "trade_id": et.get("trade_id", ""),
                                "contract_id": cid, "market": mkt,
                                "contract_type": ctype, "strategy": et.get("strategy", "?"),
                                "stake": stake, "profit": profit,
                                "probability": et.get("probability", 0),
                                "edge": et.get("edge", 0),
                                "balance": self.balance,
                                "trades": self.executor.trades_executed,
                                "wins": self.executor.trades_won,
                                "losses": self.executor.trades_lost,
                                "win_rate": self.executor.win_rate,
                                "agent": "executor",
                                "time": int(time.time() * 1000),
                            }
                            TRADE_LOG.append(msg)
                            await broadcast(msg)
                            pending_exec.pop(cid)

            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                raise
            except:
                continue

        print(f"  [!] Timeout waiting for {contract_id}")
        return None

    async def auto_select_market(self, force_rotate=False):
        """Auto-select the best market+strategy combo (good karma)."""
        # Check executor cooldown — if session ended, don't reselect
        if self.executor.active and self.executor.cooldown.current_tier == "SESSION_END":
            if self.executor.cooldown.is_cooling_down():
                return  # don't select market while session is cooling
        # Check executor rotation signals
        if self.executor.active:
            rot = self.executor.get_rotation_signal()
            if rot["rotate_market"]:
                force_rotate = True
                self.executor.on_market_rotated()
        force = force_rotate or self.selected_market is None
        sym, mtype, strat = self.scout.select_best_market(force_rotate=force)
        if sym and strat:
            if self.selected_market != sym:
                sname = strat.get("strategy", "?")
                ev = strat.get("expected_value", 0)
                print(f"  [*] Market: {sym} ({mtype}) Strategy: {sname} EV={ev:.4f}")
                await broadcast_log(f"Market selected: {sym} ({mtype}) | {sname} EV={ev:.4f}", "info")
            self.selected_market = sym
            self.selected_type = mtype
            self.selected_strategy = strat
            # Agent note: market selection
            if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
                try:
                    self.alm_brain.set_next_decision(f"Trade {sym} ({mtype}) with {strat.get('strategy','?')}")
                    self.alm_brain.set_current_task(f"Analyzing {sym} for {strat.get('strategy','?')}")
                except:
                    pass
        else:
            self.selected_market = None
            self.selected_type = None
            self.selected_strategy = None

    async def broadcast_state(self):
        global _last_state
        gs = self.governor.status()
        ss = self.scout.get_status()
        ms = self.muscle.get_status()
        st = {
            "type": "state",
            "balance": gs["balance"],
            "startBalance": gs["start_balance"],
            "trades": gs["trades_today"],
            "wins": gs["wins"],
            "losses": gs["losses"],
            "win_rate": gs["win_rate"],
            "daily_loss": gs["daily_loss_pct"],
            "total_pnl": gs["total_pnl"],
            "accuracy": gs["accuracy"],
            "cycles": self.cycle_count,
            "total_trades": self.trades_count,
            "bestStreak": self.best_streak,
            "selected_market": self.selected_market,
            "selected_type": self.selected_type,
            "selected_strategy": self.selected_strategy.get("strategy") if self.selected_strategy else None,
            "selected_ev": self.selected_strategy.get("expected_value", 0) if self.selected_strategy else 0,
            "all_strategies": self.last_recommendation.get("strategy", "") if self.last_recommendation else "",
            "regime": self.last_recommendation.get("regime", "unknown") if self.last_recommendation else "unknown",
            "regime_confidence": self.last_recommendation.get("regime_confidence", 0) if self.last_recommendation else 0,
            "adjustments": self.memory.get_adjustments(),
            "strategy_health": self.strategist.get_status(),
            "simulation": self.simulator.get_status(),
            "judge": self.judge.get_status(),
            "picker": self.picker.get_status(),
            "trade_history": TRADE_LOG[-50:],
            "active_markets": ss["active_markets"],
            "total_markets": ss["total_markets"],
            "market_list": {
                sym: {"type": m["type"], "active": m["active"],
                       "score": m["score"], "ticks": m["ticks"]}
                for sym, m in ss["markets"].items()
                if m["active"]
            },
            "signal": self.last_signal.get("statistics", {}),
            "recommendation": self.last_recommendation,
            "pending_trades": ms["pending"],
            "session": self.decider.get_status(),
            "protection": self.protector.get_status(),
            "cpp_engine": self.cpp_engine.get_status(),
            "alm_brain": self.alm_brain.get_status() if self.alm_brain.connected else {"connected": False, "notes": [], "session": {"mode": "OFFLINE"}},
            "executor": self.executor.get_status(),
            "memory": self.memory.get_memory_summary(),
            "recovery": self.recovery.get_status(),
            "adversarial": self.adversarial.get_status(),
            "portfolio": self.portfolio.get_status(),
            "resource_mgr": self.resource_mgr.get_status(),
            "m_almis": self.resource_awareness.get_status(),
            "phone_resources": self.phone_resources.scan(),
            "crypto": self.crypto_agent.get_status(),
            "mt5": self.mt5_agent.get_status(),
            "multi_market": self.multi_market.get_status(),
            "composio": self.composio.get_status(),
            "discord": self.discord.get_status(),
            "evolution": self.memory.get_evolution_history(5),
        }
        _last_state = st
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(st, f)
            # Also save to dashboard directory for HTTP polling fallback
            dash_state = Path(__file__).parent / "dashboard" / "templates" / "state.json"
            with open(dash_state, "w") as f:
                json.dump(st, f)
            self.recovery.save_state(st)
        except: pass
        await broadcast(st)

    async def run(self):
        if not await self.connect():
            print("[AD-SMTA] Deriv offline — dashboard still active")
            await self.broadcast_state()
            # Keep alive for dashboard even without Deriv
            while True:
                await asyncio.sleep(5)
                try:
                    await self.broadcast_state()
                except: pass

        await self.subscribe_all()
        await self.auto_select_market()
        self.executor.start_session(self.balance)
        await self.broadcast_state()

        print(f"{'='*55}")
        print(f"  AD-SMTA LIVE — {len(ALL_MARKETS)} markets monitored")
        print(f"  Selected: {self.selected_market} ({self.selected_type})")
        print(f"{'='*55}\n")

        try:
            async for raw in self.ws:
                data = json.loads(raw)

                if "tick" in data:
                    self.cycle_count += 1
                    await self.process_tick(data["tick"])
                    # M-ALMIS: Check resources every cycle
                    try:
                        self.resource_awareness.check_resources()
                    except: pass
                    # Multi-market: fetch external market data
                    if self.cycle_count % 15 == 0:
                        try:
                            self.crypto_agent.fetch_data()
                            self.multi_market.ingest_crypto_data(self.crypto_agent.get_status())
                        except: pass
                    if self.cycle_count % 30 == 0:
                        try:
                            self.mt5_agent.fetch_data()
                            self.multi_market.ingest_mt5_data(self.mt5_agent.get_status())
                        except: pass
                    # Feed Deriv data to multi-market core
                    if self.cycle_count % 10 == 0 and self.scout:
                        try:
                            self.multi_market.ingest_deriv_data(self.scout.get_status().get("markets", {}))
                        except: pass

                    # Periodic state broadcast
                    if self.cycle_count % 3 == 0:
                        await self.broadcast_state()

                    # Periodic market re-selection + immediate reselection when no market
                    if self.cycle_count % self.reselect_interval == 0 or self.selected_market is None:
                        await self.auto_select_market()

                elif "proposal_open_contract" in data:
                    poc = data["proposal_open_contract"]
                    print("POC: cid=%s sold=%s profit=%s" % (poc.get("contract_id"), poc.get("is_sold"), poc.get("profit")))
                    if poc.get("is_sold"):
                        cid = poc.get("contract_id", 0)
                        profit = poc.get("profit", 0)
                        info = self.muscle.resolve_pending(cid)
                        if info:
                            stake = info["proposal"].get("amount", 0)
                            self.governor.record_outcome(profit, stake)

                            # Feed result to executor agent
                            pending_exec = getattr(self, '_pending_exec_trades', {})
                            if cid in pending_exec:
                                et = pending_exec.pop(cid)
                                mkt = et["market"]
                                ctype = et["ctype"]
                                self.executor.record_result(mkt, ctype, profit, stake)
                                self.executor.current_balance = self.governor.balance

                                # Record in memory and contract picker
                                self.picker.record_result(ctype, profit, market=mkt)
                                self.memory.record_trade(
                                    mkt, et.get("strategy", "?"), ctype, profit, stake,
                                    {"probability": et.get("probability", 0), "edge": et.get("edge", 0)}
                                )
                                self.memory.update_market_profile(
                                    mkt, et.get("regime", "UNKNOWN"), et.get("strategy", "?"), profit
                                )

                                # Handle rotation
                                rot = self.executor.get_rotation_signal()
                                if rot["rotate_contract"]:
                                    try: self.picker.force_rotate()
                                    except: pass
                                    self.executor.on_contract_rotated()
                                if rot["rotate_market"]:
                                    self.selected_market = None
                                    self.executor.on_market_rotated()
                                    _elog(f"ROTATE market: {rot['reason']}")

                                if profit > 0:
                                    self.cur_streak += 1
                                    self.best_streak = max(self.best_streak, self.cur_streak)
                                else:
                                    self.cur_streak = 0

                                log_type = "win" if profit > 0 else "loss"
                                _elog(f"RESULT {ctype} on {mkt} P&L=${profit:+.2f} Bal=${self.governor.balance:.2f}")
                                await broadcast_log(
                                    f"Executor {ctype} on {mkt} {'WIN' if profit>0 else 'LOSS'} P&L: ${profit:+.2f} Bal: ${self.governor.balance:.2f}", log_type)

                                msg = {
                                    "type": "result", "trade_id": et.get("trade_id", ""),
                                    "contract_id": cid, "market": mkt,
                                    "contract_type": ctype,
                                    "strategy": et.get("strategy", "?"),
                                    "stake": stake, "profit": profit,
                                    "probability": et.get("probability", 0),
                                    "edge": et.get("edge", 0),
                                    "balance": self.governor.balance,
                                    "trades": self.executor.trades_executed,
                                    "wins": self.executor.trades_won,
                                    "losses": self.executor.trades_lost,
                                    "win_rate": self.executor.win_rate,
                                    "agent": "executor",
                                    "time": int(time.time() * 1000),
                                }
                                TRADE_LOG.append(msg)
                                await broadcast(msg)

                                # ALM Brain learn
                                if self.alm_brain.connected and getattr(self.alm_brain, "enabled", True):
                                    try:
                                        self.alm_brain.write_note(
                                            f"Executor: {'WIN' if profit>0 else 'LOSS'} ${profit:+.2f} {ctype} on {mkt}", log_type)
                                    except: pass

                                # Feed to C++ engine
                                if self.cpp_engine.connected and getattr(self.cpp_engine, "enabled", True):
                                    try: self.cpp_engine.learn(profit, stake)
                                    except: pass
                            else:
                                # Old-style trade (not from executor)
                                if not self.executor.executing:
                                    _elog(f"RESULT_OLD cid={cid} profit=${profit:.2f}")

                elif "balance" in data:
                    self.balance = data["balance"].get("balance", 0)
                    self.governor.balance = self.balance
                    self.decider.update_balance(self.balance)
                    self.protector.update_balance(self.balance)

                # Execute queued trades between tick processing
                if self.executor.has_trades() and not self.executor.executing:
                    trade = self.executor.get_next_trade()
                    if trade:
                        # Reconstruct approved from queued trade
                        approved_exec = {
                            "market": trade["market"],
                            "contract_type": trade["contract_type"],
                            "stake": trade["stake"],
                            "strategy": trade["strategy"],
                            "params": trade.get("params", {}),
                            "direction": "RISE",
                            "probability": 0.5,
                            "edge": 0.02,
                        }
                        _elog(f"DISPATCHING queued trade {trade['contract_type']} on {trade['market']}")
                        await self._execute_trade_exec(approved_exec)
                        _elog(f"DISPATCH COMPLETE queue_now={len(self.executor.trade_queue)}")
                        
                        # Process any POC messages queued during _recv_until
                        for poc_data in getattr(self, '_queued_poc', []):
                            cid = poc_data.get("contract_id", 0)
                            if poc_data.get("is_sold"):
                                profit = poc_data.get("profit", 0)
                                info = self.muscle.resolve_pending(cid)
                                if info:
                                    stake = info["proposal"].get("amount", 0)
                                    self.governor.record_outcome(profit, stake)
                                    pending_exec = getattr(self, '_pending_exec_trades', {})
                                    if cid in pending_exec:
                                        et = pending_exec.pop(cid)
                                        mkt = et["market"]
                                        ctype = et["ctype"]
                                        self.executor.record_result(mkt, ctype, profit, stake)
                                        self.executor.current_balance = self.governor.balance
                                        self.picker.record_result(ctype, profit, market=mkt)
                                        self.memory.record_trade(
                                            mkt, et.get("strategy", "?"), ctype, profit, stake,
                                            {"probability": et.get("probability", 0), "edge": et.get("edge", 0)})
                                        self.memory.update_market_profile(
                                            mkt, et.get("regime", "UNKNOWN"), et.get("strategy", "?"), profit)
                                        rot = self.executor.get_rotation_signal()
                                        if rot["rotate_contract"]:
                                            try: self.picker.force_rotate()
                                            except: pass
                                            self.executor.on_contract_rotated()
                                        if rot["rotate_market"]:
                                            self.selected_market = None
                                            self.executor.on_market_rotated()
                                        _elog(f"QUEUED_POC_RESULT {ctype} on {mkt} P&L=${profit:+.2f}")
                                        log_type = "win" if profit > 0 else "loss"
                                        await broadcast_log(
                                            f"Executor {ctype} on {mkt} {'WIN' if profit>0 else 'LOSS'} P&L: ${profit:+.2f}", log_type)
                                        msg = {
                                            "type": "result", "trade_id": et.get("trade_id", ""),
                                            "contract_id": cid, "market": mkt,
                                            "contract_type": ctype, "strategy": et.get("strategy", "?"),
                                            "stake": stake, "profit": profit,
                                            "probability": et.get("probability", 0),
                                            "edge": et.get("edge", 0),
                                            "balance": self.governor.balance,
                                            "trades": self.executor.trades_executed,
                                            "wins": self.executor.trades_won,
                                            "losses": self.executor.trades_lost,
                                            "win_rate": self.executor.win_rate,
                                            "agent": "executor",
                                            "time": int(time.time() * 1000),
                                        }
                                        TRADE_LOG.append(msg)
                                        await broadcast(msg)
                        self._queued_poc = []

        except websockets.ConnectionClosed:
            print("\n[AD-SMTA] Disconnected")
        except Exception as e:
            print(f"\n[AD-SMTA] Error: {e}")
        finally:
            await self.broadcast_state()
            gs = self.governor.status()
            print(f"\n{'='*55}")
            print(f"  SESSION SUMMARY")
            print(f"{'='*55}")
            print(f"  Balance:   ${gs['balance']:.2f}")
            print(f"  Trades:    {gs['trades_today']}")
            print(f"  Wins:      {gs['wins']}  Losses: {gs['losses']}")
            print(f"  Win Rate:  {gs['win_rate']}%")
            print(f"  P&L:       ${gs['total_pnl']:.2f}")
            print(f"  Accuracy:  {gs['accuracy']}")
            print(f"  Cycles:    {self.cycle_count}")
            print(f"  Best Run:  {self.best_streak}")
            print(f"{'='*55}")
            # Shutdown C++ engine
            if self.cpp_engine.connected and getattr(self.cpp_engine, "enabled", True):
                self.cpp_engine.shutdown()
            # ALM Brain has no persistent process to close
            with open(LOG_FILE, "w") as f:
                json.dump(TRADE_LOG, f, indent=2)

# ── Main Entry ───────────────────────────────────────────
async def main():
    import socket as _sock

    # HTTP Dashboard Server
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    templates = str(Path(__file__).parent / "dashboard" / "templates")
    class H(SimpleHTTPRequestHandler):
        def __init__(s, *a, **k): super().__init__(*a, directory=templates, **k)
        def log_message(s, *a): pass

    HTTPServer.allow_reuse_address = True
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), H)
    httpd.socket.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # Verify HTTP is up
    time.sleep(0.5)
    try:
        import urllib.request
        r = urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/", timeout=3)
        print(f"  Dashboard: http://0.0.0.0:{HTTP_PORT} ({len(r.read())} bytes)")
    except Exception as e:
        print(f"  Dashboard: http://0.0.0.0:{HTTP_PORT} (verify failed: {e})")

    # WebSocket Server
    dws = await websockets.serve(dash_handler, "0.0.0.0", WS_PORT)
    print(f"  WebSocket: ws://0.0.0.0:{WS_PORT}")

    bot = ADSMTA()
    try:
        await bot.run()
    except Exception as e:
        print(f"  [FATAL] {e}")
    finally:
        try: dws.close()
        except: pass
        try: httpd.shutdown()
        except: pass

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
