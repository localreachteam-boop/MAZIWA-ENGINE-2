#!/usr/bin/env python3
"""
AD-SMTA ACTIVE BRAIN — Runs as the mind of the trading system.
Connects to Deriv, analyzes markets, executes trades, learns, reports to Discord.
"""
import asyncio, json, time, os, random, hashlib
from pathlib import Path
import websockets

# Load .env
ENV = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            ENV[k.strip()] = v.strip()

DERIV_TOKEN = ENV.get('DERIV_TOKEN', '')
DERIV_WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"
DISCORD_WEBHOOK = ENV.get('DISCORD_WEBHOOK', '')
MEMORY_FILE = Path('agent_memory.json')
TRADE_LOG = Path('trades.log')
STATE_FILE = Path('trading_state.json')

# ── Market & Strategy Config ────────────────────────────
# Based on research: R_75 best, JD10 worst
# Prioritized markets (P&L weighted)
MARKETS = {
    'R_75':  {'weight': 2.0, 'type': 'volatility'},
    'JD100': {'weight': 1.5, 'type': 'jump'},
    'JD50':  {'weight': 1.2, 'type': 'jump'},
    'JD25':  {'weight': 1.1, 'type': 'jump'},
    'R_25':  {'weight': 0.6, 'type': 'volatility'},
    'R_100': {'weight': 0.5, 'type': 'volatility'},
    'R_50':  {'weight': 0.4, 'type': 'volatility'},
    'R_10':  {'weight': 0.3, 'type': 'volatility'},
    'JD75':  {'weight': 0.3, 'type': 'jump'},
    'JD10':  {'weight': 0.2, 'type': 'jump'},  # Worst market
}

# Contract types available on Deriv (valid ones only)
CONTRACTS = {
    'DIGITMATCH': {'payout': 8.0, 'break_even': 0.11, 'risk': 'high'},
    'DIGITDIFF':  {'payout': 0.06, 'break_even': 0.95, 'risk': 'low'},
    'DIGITEVEN':  {'payout': 0.95, 'break_even': 0.53, 'risk': 'medium'},
    'DIGITODD':   {'payout': 0.95, 'break_even': 0.53, 'risk': 'medium'},
    'CALL':       {'payout': 0.95, 'break_even': 0.53, 'risk': 'medium'},
    'PUT':        {'payout': 0.95, 'break_even': 0.53, 'risk': 'medium'},
}

# Blacklisted (from research)
BLACKLISTED = {'ONETOUCH': True}
BLACKLISTED_STRATEGIES = {'DIGIT_DIFF_0': True}

# Strategy weights (from research)
STRAT_WEIGHTS = {
    'DIGIT_DIFF_1': 2.0,
    'DIGIT_MATCH_2': 1.8,
    'DIGIT_DIFF_3': 1.3,
    'DIGIT_DIFF_1': 2.0,
}

# Risk params
INITIAL_BALANCE = 100.0
MAX_STAKE = 5.0
MIN_STAKE = 0.35
MAX_DAILY_LOSS_PCT = 0.02
KELLY_FRACTION = 0.10
COOLDOWN_AFTER_LOSS = 3
COOLDOWN_AFTER_WIN_STREAK = 5

# ── Memory ──────────────────────────────────────────────
def load_memory():
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except:
        return {'trades': [], 'market_profiles': {}, 'strategy_lifecycle': {}, 'digit_history': {}}

def save_memory(mem):
    with open(MEMORY_FILE, 'w') as f:
        json.dump(mem, f, indent=2)

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)
    # Also save for dashboard HTTP polling
    dash = Path('dashboard/templates/state.json')
    with open(dash, 'w') as f:
        json.dump(state, f)

# ── Discord ─────────────────────────────────────────────
def discord_send(title, fields, color=0x00e5ff):
    if not DISCORD_WEBHOOK:
        return
    try:
        import urllib.request, urllib.error
        payload = json.dumps({
            'embeds': [{
                'title': title,
                'color': color,
                'fields': [{'name': k, 'value': str(v), 'inline': True} for k, v in fields],
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'footer': {'text': 'AD-SMTA • Active Brain'}
            }]
        }).encode()
        req = urllib.request.Request(DISCORD_WEBHOOK, data=payload, headers={
            'Content-Type': 'application/json', 'User-Agent': 'AD-SMTA/1.0'
        }, method='POST')
        urllib.request.urlopen(req, timeout=10)
    except:
        pass

# ── Digit Analysis Engine ───────────────────────────────
class DigitAnalyzer:
    """Analyzes tick data for digit patterns."""
    
    def __init__(self):
        self.tick_history = {}  # market -> [last digits]
        self.max_history = 200
    
    def add_tick(self, market, tick_price):
        """Extract last digit from tick and store."""
        if market not in self.tick_history:
            self.tick_history[market] = []
        last_digit = int(str(tick_price).rstrip('0').lstrip('0')[-1]) if tick_price else 0
        self.tick_history[market].append(last_digit)
        if len(self.tick_history[market]) > self.max_history:
            self.tick_history[market] = self.tick_history[market][-self.max_history:]
        return last_digit
    
    def get_digit_frequencies(self, market, window=50):
        """Get frequency distribution of last digits."""
        history = self.tick_history.get(market, [])
        if len(history) < 10:
            return None
        recent = history[-window:]
        freq = {d: 0 for d in range(10)}
        for d in recent:
            freq[d] += 1
        total = len(recent)
        return {d: count/total for d, count in freq.items()}
    
    def find_imbalances(self, market, window=50):
        """Find digits that are over/under-represented."""
        freq = self.get_digit_frequencies(market, window)
        if not freq:
            return None
        
        expected = 0.1  # uniform distribution
        imbalances = {}
        for digit, pct in freq.items():
            deviation = pct - expected
            imbalances[digit] = {
                'frequency': pct,
                'deviation': deviation,
                'z_score': deviation / (expected * (1-expected) / window) ** 0.5 if window > 1 else 0
            }
        return imbalances
    
    def suggest_digit_match(self, market, window=50):
        """Suggest which digit to match (most over-represented)."""
        imbalances = self.find_imbalances(market, window)
        if not imbalances:
            return None, 0
        
        # Find digit with highest positive deviation
        best_digit = max(imbalances, key=lambda d: imbalances[d]['deviation'])
        best = imbalances[best_digit]
        
        if best['deviation'] > 0.02:  # At least 2% over-represented
            return best_digit, best['deviation']
        return None, 0
    
    def suggest_digit_diff(self, market, window=50):
        """Suggest which digit to differ from (most under-represented = safest)."""
        imbalances = self.find_imbalances(market, window)
        if not imbalances:
            return None, 0
        
        # Find digit with most negative deviation (safest to differ from)
        worst_digit = min(imbalances, key=lambda d: imbalances[d]['deviation'])
        worst = imbalances[worst_digit]
        
        if worst['deviation'] < -0.02:
            return worst_digit, abs(worst['deviation'])
        return None, 0
    
    def get_trend(self, market, window=20):
        """Detect if digits are trending up or down."""
        history = self.tick_history.get(market, [])
        if len(history) < window:
            return 'NEUTRAL', 0
        
        recent = history[-window:]
        first_half = sum(recent[:window//2]) / (window//2)
        second_half = sum(recent[window//2:]) / (window//2)
        diff = second_half - first_half
        
        if diff > 0.5:
            return 'RISING', diff
        elif diff < -0.5:
            return 'FALLING', diff
        return 'NEUTRAL', diff

# ── Strategy Engine ─────────────────────────────────────
class StrategyEngine:
    """Generates and evaluates trading strategies."""
    
    def __init__(self, digit_analyzer):
        self.da = digit_analyzer
        self.active_strategies = {}
    
    def evaluate_market(self, market):
        """Evaluate all strategies for a market and return best one."""
        scores = []
        
        # Strategy 1: Digit Match on imbalanced digit
        digit, strength = self.da.suggest_digit_match(market)
        if digit is not None and strength > 0.02:
            scores.append({
                'strategy': f'DIGIT_MATCH_{digit}',
                'contract': 'DIGITMATCH',
                'digit': digit,
                'expected_value': strength * 8.0 * 0.1,  # rough EV calc
                'confidence': min(strength * 10, 1.0),
                'reason': f'Digit {digit} over-represented by {strength:.1%}'
            })
        
        # Strategy 2: Digit Differs on under-represented digit
        digit, strength = self.da.suggest_digit_diff(market)
        if digit is not None and strength > 0.02:
            scores.append({
                'strategy': f'DIGIT_DIFF_{digit}',
                'contract': 'DIGITDIFF',
                'digit': digit,
                'expected_value': strength * 0.06 * 0.95,
                'confidence': min(strength * 10, 1.0),
                'reason': f'Digit {digit} under-represented, safe to differ'
            })
        
        # Strategy 3: Even/Odd imbalance
        history = self.da.tick_history.get(market, [])
        if len(history) >= 30:
            recent = history[-30:]
            evens = sum(1 for d in recent if d % 2 == 0)
            odds = 30 - evens
            if evens > 18:
                scores.append({
                    'strategy': 'EVEN_BIAS',
                    'contract': 'DIGITEVEN',
                    'expected_value': (evens/30 - 0.5) * 0.95,
                    'confidence': (evens - 15) / 15,
                    'reason': f'Even bias: {evens}/30 ticks'
                })
            elif odds > 18:
                scores.append({
                    'strategy': 'ODD_BIAS',
                    'contract': 'DIGITODD',
                    'expected_value': (odds/30 - 0.5) * 0.95,
                    'confidence': (odds - 15) / 15,
                    'reason': f'Odd bias: {odds}/30 ticks'
                })
        
        # Strategy 4: Rise/Fall based on trend
        trend, strength = self.da.get_trend(market)
        if trend != 'NEUTRAL' and abs(strength) > 0.3:
            contract = 'CALL' if trend == 'RISING' else 'PUT'
            scores.append({
                'strategy': f'{trend}_TREND',
                'contract': contract,
                'expected_value': abs(strength) * 0.05,
                'confidence': min(abs(strength) / 2, 1.0),
                'reason': f'{trend} trend detected (strength: {strength:.2f})'
            })
        
        if not scores:
            return None
        
        # Return best strategy by expected value * confidence
        best = max(scores, key=lambda s: s['expected_value'] * s['confidence'])
        return best

# ── Risk Manager ────────────────────────────────────────
class RiskManager:
    """Manages risk, cooldowns, and position sizing."""
    
    def __init__(self):
        self.balance = INITIAL_BALANCE
        self.start_balance = INITIAL_BALANCE
        self.trades_today = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.daily_pnl = 0
        self.cooldown_until = 0
        self.last_market = None
        self.market_trades = {}
        self.trade_history = []
    
    def can_trade(self):
        """Check if we're allowed to trade."""
        now = time.time()
        
        # Cooldown check
        if now < self.cooldown_until:
            return False, f"Cooldown until {time.strftime('%H:%M:%S', time.localtime(self.cooldown_until))}"
        
        # Daily loss limit
        if self.start_balance > 0:
            loss_pct = abs(min(0, self.daily_pnl)) / self.start_balance
            if loss_pct >= MAX_DAILY_LOSS_PCT:
                return False, f"Daily loss limit hit ({loss_pct:.1%})"
        
        # Too many consecutive losses
        if self.consecutive_losses >= COOLDOWN_AFTER_LOSS:
            self.cooldown_until = now + 300  # 5 min cooldown
            return False, f" {self.consecutive_losses} consecutive losses, cooling down"
        
        return True, "OK"
    
    def calculate_stake(self, strategy):
        """Calculate optimal stake using Kelly criterion."""
        ev = strategy.get('expected_value', 0)
        confidence = strategy.get('confidence', 0)
        
        if ev <= 0:
            return MIN_STAKE
        
        # Kelly: f = (bp - q) / b
        # For digit match: b = 8, p = 0.1 + ev, q = 1-p
        contract = strategy.get('contract', 'DIGITMATCH')
        if contract == 'DIGITMATCH':
            b = 8.0
            p = 0.1 + ev
        elif contract == 'DIGITDIFF':
            b = 0.06 / 0.94
            p = 0.9 + ev
        else:
            b = 0.95 / 0.05
            p = 0.5 + ev
        
        q = 1 - p
        kelly = max(0, (b * p - q) / b)
        
        # Apply Kelly fraction and confidence
        stake = self.balance * kelly * KELLY_FRACTION * confidence
        stake = max(MIN_STAKE, min(stake, MAX_STAKE, self.balance * 0.05))
        
        return round(stake, 2)
    
    def record_trade(self, result):
        """Record a trade result."""
        self.trades_today += 1
        self.daily_pnl += result['profit']
        self.balance += result['profit']
        
        if result['profit'] > 0:
            self.wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            # Short cooldown after loss
            self.cooldown_until = time.time() + 30
        
        self.trade_history.append(result)
        self.market_trades[result['market']] = self.market_trades.get(result['market'], 0) + 1
        
        # Long win streak cooldown (lock in profits)
        if self.consecutive_wins >= COOLDOWN_AFTER_WIN_STREAK:
            self.cooldown_until = time.time() + 600  # 10 min
            print(f"  [BRAIN] {self.consecutive_wins} win streak — locking profits for 10 min")
    
    def get_win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0
    
    def should_rotate_market(self, current_market):
        """Should we switch markets?"""
        trades_on_market = self.market_trades.get(current_market, 0)
        if trades_on_market >= 5:
            return True, f"5+ trades on {current_market}"
        if self.consecutive_losses >= 2:
            return True, f"Consecutive losses"
        return False, ""

# ── Deriv Connection ────────────────────────────────────
class DerivTrader:
    """Connects to Deriv and executes trades."""
    
    def __init__(self):
        self.ws = None
        self.connected = False
        self.balance = 0
        self.pending = {}
        self.tick_buffer = {}
        self.req_id = 0
    
    async def connect(self):
        """Connect to Deriv WebSocket."""
        try:
            self.ws = await websockets.connect(DERIV_WS, ping_interval=20)
            # Authorize
            self.req_id += 1
            await self.ws.send(json.dumps({
                "authorize": DERIV_TOKEN,
                "req_id": self.req_id
            }))
            resp = await asyncio.wait_for(self.ws.recv(), timeout=10)
            data = json.loads(resp)
            if data.get('error'):
                print(f"  [DERIV] Auth error: {data['error']}")
                return False
            self.connected = True
            auth = data.get('authorize', {})
            balance = 0
            # Try multiple paths for balance
            if isinstance(auth, dict):
                balance = auth.get('balance', 0)
            if not balance:
                balance = data.get('balance', {}).get('balance', 0)
            if not balance:
                # Request balance separately
                self.req_id += 1
                await self.ws.send(json.dumps({'balance': 1, 'req_id': self.req_id}))
                bresp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
                balance = bresp.get('balance', {}).get('balance', 0)
            self.balance = balance
            print(f"  [DERIV] Connected. Balance: ${balance:.2f}")
            return True
        except Exception as e:
            print(f"  [DERIV] Connection failed: {e}")
            return False
    
    async def subscribe_ticks(self, market):
        """Subscribe to tick stream for a market."""
        self.req_id += 1
        await self.ws.send(json.dumps({
            "ticks": market,
            "subscribe": 1,
            "req_id": self.req_id
        }))
    
    async def listen_ticks(self):
        """Background listener that buffers ticks from all subscribed markets."""
        try:
            while self.connected:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
                    data = json.loads(msg)
                    if data.get('tick'):
                        tick = data['tick']
                        sym = tick.get('symbol', '')
                        quote = tick.get('quote', 0)
                        if sym not in self.tick_buffer:
                            self.tick_buffer[sym] = []
                        self.tick_buffer[sym].append(quote)
                        if len(self.tick_buffer[sym]) > 200:
                            self.tick_buffer[sym] = self.tick_buffer[sym][-200:]
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    self.connected = False
                    break
                except:
                    continue
        except:
            self.connected = False
    
    async def buy_contract(self, contract_type, stake, market, digit=None):
        """Buy a contract on Deriv."""
        self.req_id += 1
        
        # Build proposal request
        params = {
            "contract_type": contract_type,
            "symbol": market,
            "amount": stake,
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",  # ticks
        }
        
        # Add digit parameter for digit contracts
        if digit is not None and contract_type in ('DIGITMATCH', 'DIGITDIFF'):
            params["barrier"] = str(digit)
        
        # Get proposal
        await self.ws.send(json.dumps({
            "proposal": 1,
            **params,
            "req_id": self.req_id
        }))
        
        resp = await asyncio.wait_for(self.ws.recv(), timeout=10)
        data = json.loads(resp)
        
        if data.get('error'):
            return None, data['error'].get('message', 'Unknown error')
        
        proposal = data.get('proposal', {})
        if not proposal.get('id'):
            return None, "No proposal ID"
        
        # Buy the contract
        self.req_id += 1
        await self.ws.send(json.dumps({
            "buy": proposal['id'],
            "price": stake,
            "req_id": self.req_id
        }))
        
        resp = await asyncio.wait_for(self.ws.recv(), timeout=10)
        data = json.loads(resp)
        
        if data.get('error'):
            return None, data['error'].get('message', 'Buy failed')
        
        buy = data.get('buy', {})
        return buy.get('contract_id'), None
    
    async def wait_for_result(self, contract_id, timeout=15):
        """Wait for contract result."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=2)
                data = json.loads(msg)
                if data.get('proposal_open_contract', {}).get('id') == contract_id:
                    poc = data['proposal_open_contract']
                    if poc.get('is_sold'):
                        profit = poc.get('profit', 0)
                        return {'profit': profit, 'status': 'WIN' if profit > 0 else 'LOSS'}
            except asyncio.TimeoutError:
                continue
            except:
                break
        
        # Fallback: check balance
        return None
    
    async def get_balance(self):
        """Get current balance."""
        self.req_id += 1
        await self.ws.send(json.dumps({
            "balance": 1,
            "req_id": self.req_id
        }))
        resp = await asyncio.wait_for(self.ws.recv(), timeout=5)
        data = json.loads(resp)
        return data.get('balance', {}).get('balance', 0)
    
    async def disconnect(self):
        if self.ws:
            await self.ws.close()

# ── Main Brain Loop ─────────────────────────────────────
async def run_brain():
    print("=" * 60)
    print("  AD-SMTA ACTIVE BRAIN — Starting")
    print("=" * 60)
    
    # Initialize components
    da = DigitAnalyzer()
    se = StrategyEngine(da)
    rm = RiskManager()
    deriv = DerivTrader()
    mem = load_memory()
    
    print(f"  [BRAIN] Connected to Deriv. Using real balance.")
    
    # Connect to Deriv
    print("  [BRAIN] Connecting to Deriv...")
    if not await deriv.connect():
        print("  [BRAIN] FATAL: Cannot connect to Deriv")
        return
    
    print(f"  [BRAIN] Starting balance: ${rm.balance:.2f}")
    print(f"  [BRAIN] Markets: {len(MARKETS)} | Contracts: {len(CONTRACTS)}")
    print(f"  [BRAIN] Blacklisted: ONETOUCH, DIGIT_DIFF_0")
    
    # Send startup to Discord
    discord_send('🧠 Active Brain Started', {
        'Balance': f'${rm.balance:.2f}',
        'Markets': str(len(MARKETS)),
        'Contracts': str(len(CONTRACTS)),
        'Mode': 'ACTIVE TRADING',
    })
    
    trade_count = 0
    cycle = 0
    
    # Start background tick listener
    tick_listener = asyncio.create_task(deriv.listen_ticks())
    print("  [BRAIN] Background tick listener started")
    
    # Wait a moment for ticks to start flowing
    await asyncio.sleep(3)
    
    try:
        while True:
            cycle += 1
            
            # ── Phase 1: Collect tick data from top markets ──
            top_markets = sorted(MARKETS.items(), key=lambda x: x[1]['weight'], reverse=True)[:4]
            
            # Ticks are collected in background by listen_ticks
            # Just feed them to the analyzer
            for market, mkt_info in top_markets:
                try:
                    ticks = deriv.tick_buffer.get(market, [])
                    for price in ticks[-20:]:
                        da.add_tick(market, price)
                except:
                    continue
            
            # ── Phase 2: Check if we can trade ──
            can_trade, reason = rm.can_trade()
            if not can_trade:
                if cycle % 10 == 0:
                    print(f"  [BRAIN] Cycle {cycle} — Waiting: {reason}")
                await asyncio.sleep(2)
                continue
            
            # ── Phase 3: Select best market ──
            best_market = None
            best_score = -1
            
            for market, mkt_info in top_markets:
                ticks = da.tick_history.get(market, [])
                if len(ticks) < 15:
                    continue
                
                # Score = weight * tick_data_quality
                data_quality = min(len(ticks) / 50, 1.0)
                score = mkt_info['weight'] * data_quality
                
                # Boost if we have good digit data
                imbalances = da.find_imbalances(market)
                if imbalances:
                    max_dev = max(abs(v['deviation']) for v in imbalances.values())
                    score += max_dev * 5
                
                if score > best_score:
                    best_score = score
                    best_market = market
            
            if not best_market:
                await asyncio.sleep(2)
                continue
            
            # ── Phase 4: Generate strategy ──
            strategy = se.evaluate_market(best_market)
            if not strategy:
                await asyncio.sleep(2)
                continue
            
            # Skip blacklisted strategies
            if strategy['strategy'] in BLACKLISTED_STRATEGIES:
                await asyncio.sleep(1)
                continue
            
            # ── Phase 5: Calculate stake ──
            stake = rm.calculate_stake(strategy)
            if stake < MIN_STAKE:
                await asyncio.sleep(2)
                continue
            
            # ── Phase 6: Execute trade ──
            contract_type = strategy['contract']
            digit = strategy.get('digit')
            
            print(f"\n  [BRAIN] Cycle {cycle}: {best_market} | {strategy['strategy']} | {contract_type} | ${stake:.2f}")
            print(f"         Reason: {strategy['reason']}")
            
            contract_id, error = await deriv.buy_contract(contract_type, stake, best_market, digit)
            
            if error:
                print(f"         Error: {error}")
                await asyncio.sleep(2)
                continue
            
            print(f"         Contract ID: {contract_id}")
            
            # ── Phase 7: Wait for result ──
            result = await deriv.wait_for_result(contract_id)
            
            if not result:
                # Fallback: assume based on balance
                try:
                    new_bal = await deriv.get_balance()
                    profit = new_bal - rm.balance
                    result = {'profit': profit, 'status': 'WIN' if profit > 0 else 'LOSS'}
                except:
                    result = {'profit': -stake, 'status': 'LOSS'}
            
            # ── Phase 8: Record and learn ──
            rm.record_trade(result)
            trade_count += 1
            
            trade_data = {
                'market': best_market,
                'strategy': strategy['strategy'],
                'contract_type': contract_type,
                'digit': digit,
                'stake': stake,
                'profit': result['profit'],
                'balance': rm.balance,
                'reason': strategy['reason'],
                'time': int(time.time() * 1000),
                'cycle': cycle,
            }
            
            # Save to memory
            mem['trades'].append(trade_data)
            save_memory(mem)
            
            # Update market profile
            mp = mem.setdefault('market_profiles', {}).setdefault(best_market, {'total_pnl': 0, 'trades': 0})
            mp['total_pnl'] = mp.get('total_pnl', 0) + result['profit']
            mp['trades'] = mp.get('trades', 0) + 1
            
            # Print result
            emoji = '✅ WIN' if result['profit'] > 0 else '❌ LOSS'
            print(f"         {emoji}: ${result['profit']:+.2f} | Balance: ${rm.balance:.2f} | W/L: {rm.wins}/{rm.losses} ({rm.get_win_rate():.1f}%)")
            
            # Discord alert for every trade
            discord_send(
                f"{'✅' if result['profit'] > 0 else '❌'} ${result['profit']:+.2f} — {strategy['strategy']}",
                {
                    'Market': best_market,
                    'Contract': contract_type,
                    'Stake': f'${stake:.2f}',
                    'P&L': f'${result["profit"]:+.2f}',
                    'Balance': f'${rm.balance:.2f}',
                    'Win Rate': f'{rm.get_win_rate():.1f}%',
                    'Reason': strategy['reason'][:50],
                },
                color=0x22c55e if result['profit'] > 0 else 0xef4444
            )
            
            # ── Phase 9: Update dashboard state ──
            state = {
                'type': 'state',
                'balance': rm.balance,
                'startBalance': rm.start_balance,
                'trades': rm.trades_today,
                'wins': rm.wins,
                'losses': rm.losses,
                'win_rate': round(rm.get_win_rate(), 1),
                'daily_loss': round(abs(min(0, rm.daily_pnl)) / rm.start_balance * 100, 2) if rm.start_balance else 0,
                'total_pnl': round(rm.daily_pnl, 2),
                'cycles': cycle,
                'total_trades': trade_count,
                'bestStreak': rm.consecutive_wins,
                'selected_market': best_market,
                'selected_strategy': strategy['strategy'],
                'regime': strategy.get('reason', 'ACTIVE'),
                'trade_history': mem['trades'][-50:],
                'market_profiles': {k: {'total_pnl': v.get('total_pnl',0), 'trades': v.get('trades',0)} for k, v in mem.get('market_profiles', {}).items()},
                'brain_status': {
                    'mode': 'ACTIVE',
                    'consecutive_losses': rm.consecutive_losses,
                    'consecutive_wins': rm.consecutive_wins,
                    'cooldown_until': rm.cooldown_until,
                    'markets_traded': rm.market_trades,
                },
                'discord': {'enabled': bool(DISCORD_WEBHOOK), 'total_sent': trade_count},
                'time': int(time.time() * 1000),
            }
            save_state(state)
            
            # ── Phase 10: Market rotation check ──
            should_rotate, rot_reason = rm.should_rotate_market(best_market)
            if should_rotate:
                print(f"         [ROTATE] {rot_reason}")
                rm.last_market = best_market
            
            # Brief pause between trades
            await asyncio.sleep(1)
    
    except KeyboardInterrupt:
        print("\n  [BRAIN] Shutting down...")
    except Exception as e:
        print(f"\n  [BRAIN] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Session summary
        print(f"\n{'='*60}")
        print(f"  SESSION SUMMARY")
        print(f"{'='*60}")
        print(f"  Balance:  ${rm.balance:.2f}")
        print(f"  Trades:   {rm.trades_today}")
        print(f"  Wins:     {rm.wins}  Losses: {rm.losses}")
        print(f"  Win Rate: {rm.get_win_rate():.1f}%")
        print(f"  P&L:      ${rm.daily_pnl:+.2f}")
        print(f"  Cycles:   {cycle}")
        print(f"{'='*60}")
        
        # Discord summary
        discord_send('📊 Session Summary', {
            'Balance': f'${rm.balance:.2f}',
            'Trades': str(rm.trades_today),
            'Win Rate': f'{rm.get_win_rate():.1f}%',
            'P&L': f'${rm.daily_pnl:+.2f}',
            'Cycles': str(cycle),
        }, color=0x22c55e if rm.daily_pnl >= 0 else 0xef4444)
        
        save_memory(mem)
        await deriv.disconnect()

if __name__ == "__main__":
    asyncio.run(run_brain())
