"""
AMTO Orchestrator
Coordinates 4 agents in a pipeline: Sensor → Brain → Governor → Muscle
Connects to Deriv WebSocket API for real-time tick data and contract execution.
"""
import asyncio
import json
import time
import signal
import sys
from datetime import datetime, timezone

import websockets

from agents.sensor import SensorAgent
from agents.brain import BrainAgent
from agents.governor import GovernorAgent
from agents.muscle import MuscleAgent

# ─── Config ──────────────────────────────────────────────
from config import (
    DERIV_TOKEN, DERIV_WS_URL, MARKET, TICK_COUNT,
    EDGE_THRESHOLD, MAX_DAILY_LOSS_PCT, INITIAL_BALANCE,
    KELLY_FRACTION, CONTRACT_DURATION, PIPELINE_INTERVAL,
    STAKE_MULTIPLIER, LOG_FILE,
)


class AMTO:
    """Autonomous Multi-Agent Trading Orchestrator."""

    def __init__(self):
        # Instantiate agents
        self.sensor = SensorAgent(tick_buffer_size=TICK_COUNT)
        self.brain = BrainAgent(tick_buffer_size=TICK_COUNT)
        self.governor = GovernorAgent(
            balance=INITIAL_BALANCE,
            max_daily_loss_pct=MAX_DAILY_LOSS_PCT,
            edge_threshold=EDGE_THRESHOLD,
            kelly_fraction=KELLY_FRACTION,
        )
        self.muscle = MuscleAgent()

        self.ws = None
        self.running = False
        self.cycle_count = 0
        self.trade_count = 0
        self.proposal_sub_id = None
        self.buy_sub_id = None

        # Signal handling
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        print("\n[AMTO] Shutdown signal received. Stopping...")
        self.running = False

    # ─── WebSocket Handlers ─────────────────────────────
    async def connect(self):
        """Connect to Deriv WebSocket API."""
        print(f"[AMTO] Connecting to {DERIV_WS_URL[:50]}...")
        self.ws = await websockets.connect(DERIV_WS_URL)
        print("[AMTO] Connected to Deriv WebSocket")

        # Authorize
        await self._send({"authorize": DERIV_TOKEN})
        auth_resp = await self._recv()

        if "error" in auth_resp:
            print(f"[AMTO] AUTH FAILED: {auth_resp['error']}")
            return False

        loginid = auth_resp.get("authorize", {}).get("loginid", "unknown")
        balance = auth_resp.get("authorize", {}).get("balance", 0)
        print(f"[AMTO] Authorized: {loginid} | Balance: ${balance}")
        self.governor.balance = balance
        self.governor.initial_balance = balance
        self.governor.daily_start_balance = balance
        self.running = True
        return True

    async def subscribe_ticks(self):
        """Subscribe to live tick stream for the target market."""
        print(f"[AMTO] Subscribing to ticks: {MARKET}")
        await self._send({
            "ticks": MARKET,
            "subscribe": 1,
        })

    async def _send(self, data: dict):
        if self.ws:
            msg = json.dumps(data)
            await self.ws.send(msg)

    async def _recv(self, timeout=5):
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            return json.loads(raw)
        except asyncio.TimeoutError:
            return {"error": "TIMEOUT"}

    # ─── Pipeline ───────────────────────────────────────
    async def pipeline_cycle(self, tick_data: dict):
        """
        Run one full cycle of the 4-agent pipeline.
        """
        self.cycle_count += 1

        # AGENT 1: Sensor — ingest tick, build signal
        signal_packet = self.sensor.ingest_tick(tick_data)
        if signal_packet is None:
            return  # not enough data yet

        ticks = signal_packet["raw_data_summary"]["ticks_collected"]
        if self.cycle_count % 50 == 0:
            print(f"\n[AMTO] Cycle {self.cycle_count} | "
                  f"Ticks buffered: {ticks} | "
                  f"Balance: ${self.governor.balance:.2f} | "
                  f"Trades: {self.trade_count}")
            print(f"  [Sensor] Price: {signal_packet['raw_data_summary']['current_price']:.5f} | "
                  f"Vol ratio: {signal_packet['raw_data_summary']['vol_ratio']:.2f} | "
                  f"Trend: {signal_packet['raw_data_summary']['trend_slope']:.8f}")

        # AGENT 2: Brain — compute fair odds and edge
        analytics_packet = self.brain.analyze(signal_packet)
        if analytics_packet is None:
            return

        edge = analytics_packet["edge_percentage"]
        direction = analytics_packet["direction"]
        prob = analytics_packet["probability"]

        if direction != "NONE":
            print(f"  [Brain]  Direction: {direction} | "
                  f"Prob: {prob:.4f} | Edge: {edge:.2f}%")

        # AGENT 3: Governor — validate risk and size position
        approved_packet = self.governor.validate(analytics_packet)

        if approved_packet["execution_status"] == "REJECTED":
            if self.cycle_count % 100 == 0:
                print(f"  [Governor] REJECTED: {approved_packet.get('reason', '')}")
            return

        # AGENT 4: Muscle — execute the trade
        result = self.muscle.execute(approved_packet, self.ws.send)
        print(f"  [Governor] APPROVED | Stake: ${result.get('trade_info', {}).get('stake', 0):.2f}")

        if result["action"] == "PROPOSE":
            await self._execute_proposal(result["proposal"], result["trade_info"])

    async def _execute_proposal(self, proposal: dict, trade_info: dict):
        """Send proposal to Deriv, get contract, wait for result."""
        try:
            # Get proposal
            await self._send(proposal)
            proposal_resp = await self._recv(timeout=3)

            if "error" in proposal_resp or "proposal" not in proposal_resp:
                err = proposal_resp.get("error", {}).get("message", "Proposal failed")
                print(f"  [Muscle] Proposal failed: {err}")
                self.governor.record_outcome(0, trade_info.get("stake", 0))
                return

            p = proposal_resp["proposal"]
            contract_id = p.get("id", 0)
            buy_price = p.get("price", 0)
            payout = p.get("payout", 0)

            print(f"  [Muscle] Proposal received: contract {contract_id} "
                  f"| Buy: ${buy_price:.2f} | Payout: ${payout:.2f}")

            # Buy the contract — use our original stake as the price
            await self._send({"buy": contract_id, "price": self.muscle.last_stake, "subscribe": 1})
            buy_resp = await self._recv(timeout=5)

            buy_data = buy_resp.get("buy", {})
            if not buy_data or buy_data.get("error"):
                print(f"  [Muscle] Buy failed")
                self.governor.record_outcome(0, trade_info.get("stake", 0))
                return

            buy_data = buy_resp["buy"]
            self.trade_count += 1

            print(f"  [Muscle] Contract OPENED | ID: {buy_data.get('contract_id')}")

            # Wait for contract to resolve
            result = await self._wait_for_result(buy_data.get("contract_id", 0))
            if result:
                outcome = result.get("status", "UNKNOWN")
                profit = result.get("profit", 0)
                print(f"  [Muscle] Contract {outcome} | Profit: ${profit:+.2f}")

                # Feed back to Governor
                stake = trade_info.get("stake", 0)
                self.governor.record_outcome(profit, stake)

        except Exception as e:
            print(f"  [Muscle] Execution error: {e}")

    async def _wait_for_result(self, contract_id, timeout=60):
        """Listen for contract expiry."""
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = await self._recv(timeout=5)
                if "proposal_open_contract" in msg:
                    poc = msg["proposal_open_contract"]
                    if poc.get("is_sold"):
                        profit = poc.get("profit", 0)
                        return {
                            "status": "WIN" if profit > 0 else "LOSS",
                            "profit": profit,
                        }
                elif "tick" in msg:
                    pass  # keep listening
            print(f"  [Muscle] Contract {contract_id} timed out waiting for result")
            return None
        except Exception as e:
            print(f"  [Muscle] Wait error: {e}")
            return None

    # ─── Main Loop ──────────────────────────────────────
    async def run(self):
        """Main autonomous loop."""
        connected = await self.connect()
        if not connected:
            print("[AMTO] Failed to connect. Exiting.")
            return

        await self.subscribe_ticks()
        print(f"[AMTO] Pipeline active | Market: {MARKET} | "
              f"Edge threshold: {EDGE_THRESHOLD*100}% | "
              f"Kelly: {KELLY_FRACTION}")
        print("[AMTO] Waiting for tick data...\n")

        try:
            while self.running:
                raw = await self._recv(timeout=10)

                if raw is None:
                    continue

                if "error" in raw and raw.get("error") == "TIMEOUT":
                    continue

                if "error" in raw:
                    print(f"[AMTO] WS Error: {raw['error']}")
                    break

                # Tick data
                if "tick" in raw:
                    await self.pipeline_cycle(raw["tick"])

                # Contract results
                elif "proposal_open_contract" in raw:
                    poc = raw["proposal_open_contract"]
                    if poc.get("is_sold"):
                        profit = poc.get("profit", 0)
                        contract_id = poc.get("contract_id", 0)
                        outcome = "WIN" if profit > 0 else "LOSS"
                        print(f"\n[AMTO] Contract {contract_id} resolved: {outcome} "
                              f"(${profit:+.2f})")
                        self.governor.record_outcome(profit, 0)

                # Balance updates
                elif "balance" in raw:
                    new_bal = raw["balance"].get("balance", 0)
                    self.governor.balance = new_bal

        except websockets.ConnectionClosed:
            print("[AMTO] WebSocket connection closed")
        except Exception as e:
            print(f"[AMTO] Error: {e}")
        finally:
            self._print_summary()
            if self.ws:
                await self.ws.close()

    def _print_summary(self):
        status = self.governor.status()
        print("\n" + "=" * 50)
        print("  AMTO SESSION SUMMARY")
        print("=" * 50)
        print(f"  Balance:      ${status['balance']:.2f}")
        print(f"  Trades:       {status['trades_today']}")
        print(f"  Wins:         {status['wins']}")
        print(f"  Losses:       {status['losses']}")
        print(f"  Win Rate:     {status['win_rate']}%")
        print(f"  Daily Loss:   {status['daily_loss_pct']}%")
        print(f"  Pipeline Cycles: {self.cycle_count}")
        print("=" * 50)

        # Write trade log
        with open(LOG_FILE, "w") as f:
            json.dump(self.muscle.trade_log, f, indent=2)
        print(f"  Trade log saved to {LOG_FILE}")


async def main():
    orchestrator = AMTO()
    await orchestrator.run()


if __name__ == "__main__":
    asyncio.run(main())
