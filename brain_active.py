#!/usr/bin/env python3
"""
AD-SMTA BRAIN v3 — With agent tools.
Tick analysis + 7 agents = smarter trading.
"""
import asyncio, json, time, os, sys, random
from pathlib import Path
import websockets

# Add project root to path for agent imports
sys.path.insert(0, str(Path(__file__).parent))

# Load .env
ENV = {}
with open(Path(__file__).parent / '.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            ENV[k.strip()] = v.strip()

TOKEN = ENV.get('DERIV_TOKEN', '')
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
DISCORD = ENV.get('DISCORD_WEBHOOK', '')

# Markets sorted by research
MARKET_LIST = ['R_75', 'JD100', 'JD50', 'JD25', 'R_25', 'R_100', 'R_50', 'JD75', 'R_10', 'JD10']
MARKET_WEIGHTS = {'R_75': 2.0, 'JD100': 1.5, 'JD50': 1.2, 'JD25': 1.1, 'R_25': 0.6, 'R_100': 0.5, 'R_50': 0.4, 'JD75': 0.3, 'R_10': 0.3, 'JD10': 0.2}
MARKET_TYPES = {'R_75': 'volatility', 'JD100': 'jump', 'JD50': 'jump', 'JD25': 'jump',
                'R_25': 'volatility', 'R_100': 'volatility', 'R_50': 'volatility',
                'JD75': 'jump', 'R_10': 'volatility', 'JD10': 'jump'}

STATE_FILE = Path(__file__).parent / 'trading_state.json'
DASH_STATE = Path(__file__).parent / 'dashboard' / 'templates' / 'state.json'

# ═══════════════════════════════════════════════════════
# AGENT TOOLS — Import the 7 useful agents
# ═══════════════════════════════════════════════════════
from agents.sensor import SensorAgent
from agents.memory import Memory
from agents.contract_picker import ContractPicker
from agents.judge import Judge
from agents.protector import Protector
from agents.regime import RegimeEngine
from agents.strategist import Strategist

class AgentTools:
    """Wraps 7 agents as tools the brain can call."""
    
    def __init__(self):
        self.sensor = SensorAgent(buffer_size=200)
        self.memory = Memory()
        self.picker = ContractPicker()
        self.judge = Judge()
        self.protector = Protector()
        self.regime = RegimeEngine()
        self.strategist = Strategist(self.memory)
        self.initialized = False
    
    def init(self, balance):
        """Initialize protector with starting balance."""
        self.protector.init(balance)
        self.initialized = True
    
    def feed_tick(self, market, price, epoch=None):
        """Feed tick to sensor + memory digit tracking."""
        epoch = epoch or int(time.time())
        market_type = MARKET_TYPES.get(market, 'volatility')
        self.sensor.ingest_tick({'quote': price, 'epoch': epoch, 'symbol': market}, market_type)
        digit = int(str(price).rstrip('0').lstrip('0')[-1]) if price else 0
        self.memory.record_digit(market, digit)
    
    def get_signal(self, market):
        """Get sensor signal for a market."""
        market_type = MARKET_TYPES.get(market, 'volatility')
        return self.sensor.get_signal(market, market_type)
    
    def get_digit_bias(self, market):
        """Get memory's digit bias for a market."""
        return self.memory.get_digit_bias(market)
    
    def get_regime(self, market, signal):
        """Classify market regime."""
        return self.regime.classify(market, signal)
    
    def pick_contract(self, regime, strategy, signal, digit_freq):
        """Ask picker for best contract."""
        return self.picker.pick_best(regime, strategy, signal, digit_freq)
    
    def validate_trade(self, context):
        """Ask judge if we should trade."""
        return self.judge.evaluate(context)
    
    def check_safety(self):
        """Ask protector if we're safe to trade."""
        return self.protector.check()
    
    def record_result(self, contract_type, profit, market, strategy, stake):
        """Record trade result to all agents."""
        self.picker.record_result(contract_type, profit, market)
        self.memory.record_trade(market, strategy, contract_type, profit, stake)
        self.protector.record_trade_result(profit)
        self.strategist.record_trade(f"{market}:{strategy}", profit)
    
    def get_best_strategies(self):
        """Get best strategies from memory."""
        return self.memory.get_best_strategies(5)
    
    def get_strategist_rec(self, market, regime):
        """Get strategist recommendation."""
        return self.strategist.get_strategy_recommendation(market, regime)
    
    def get_champions(self):
        """Get champion strategies."""
        return self.strategist.get_champions(5)
    
    def get_picker_status(self):
        """Get contract picker status."""
        return self.picker.get_status() if hasattr(self.picker, 'get_status') else {}

# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════
def discord(title, fields, color=0x00e5ff):
    if not DISCORD: return
    try:
        import urllib.request
        payload = json.dumps({'embeds': [{'title': title, 'color': color,
            'fields': [{'name': k, 'value': str(v), 'inline': True} for k, v in fields],
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'footer': {'text': 'AD-SMTA Brain v3'}}]}).encode()
        req = urllib.request.Request(DISCORD, data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'AD-SMTA/1.0'}, method='POST')
        urllib.request.urlopen(req, timeout=10)
    except: pass

def get_system_resources():
    """Collect real system resource data for dashboard — uses /proc directly, no subprocess."""
    import os
    res = {
        'battery': 0, 'is_charging': False,
        'temperature': 0.0,
        'ram_pct': 0, 'ram_available_mb': 0, 'ram_used': 0,
        'swap_pct': 0, 'swap_used_mb': 0, 'swap_total_mb': 0,
        'disk_pct': 0, 'disk_free': '0GB',
        'cpu_load': '0.0',
    }
    try:
        with open('/proc/meminfo') as f:
            mi = {}
            for line in f:
                p = line.split()
                if len(p) >= 2:
                    mi[p[0].rstrip(':')] = int(p[1])
        total = mi.get('MemTotal', 1)
        avail = mi.get('MemAvailable', 0)
        used = total - avail
        res['ram_pct'] = round(used / total * 100) if total else 0
        res['ram_available_mb'] = avail // 1024
        res['ram_used'] = used // 1024
        st = mi.get('SwapTotal', 0)
        sf = mi.get('SwapFree', 0)
        su = st - sf
        res['swap_total_mb'] = st // 1024
        res['swap_used_mb'] = su // 1024
        res['swap_pct'] = round(su / st * 100) if st else 0
    except: pass
    try:
        s = os.statvfs('/')
        t = s.f_blocks * s.f_frsize
        fr = s.f_bavail * s.f_frsize
        u = t - fr
        res['disk_pct'] = round(u / t * 100) if t else 0
        res['disk_free'] = f"{fr // (1024**3)}GB"
    except: pass
    try:
        with open('/proc/loadavg') as f:
            res['cpu_load'] = f.read().split()[0]
    except: pass
    try:
        with open('/sys/class/power_supply/battery/capacity') as f:
            res['battery'] = int(f.read().strip())
        with open('/sys/class/power_supply/battery/status') as f:
            res['is_charging'] = 'Charging' in f.read().strip()
    except: pass
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            res['temperature'] = round(int(f.read().strip()) / 1000, 1)
    except: pass
    return res


def save_state(s):
    for f in [STATE_FILE, DASH_STATE]:
        try:
            with open(f, 'w') as fh: json.dump(s, fh)
        except: pass

# ═══════════════════════════════════════════════════════
# TICK COLLECTOR (separate WS)
# ═══════════════════════════════════════════════════════
class TickCollector:
    def __init__(self):
        self.ticks = {}
        self.last_digits = {}
        self.running = False
    
    async def run(self):
        while True:
            try:
                ws = await websockets.connect(WS_URL, ping_interval=20)
                await ws.send(json.dumps({"authorize": TOKEN, "req_id": 1}))
                resp = json.loads(await ws.recv())
                if resp.get('error'):
                    await asyncio.sleep(5)
                    continue
                print("  [TICKS] Connected")
                for m in MARKET_LIST[:4]:
                    await ws.send(json.dumps({"ticks": m, "subscribe": 1, "req_id": random.randint(100,999)}))
                self.running = True
                while self.running:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(msg)
                        if data.get('tick'):
                            sym = data['tick']['symbol']
                            price = data['tick']['quote']
                            if sym not in self.ticks:
                                self.ticks[sym] = []
                                self.last_digits[sym] = []
                            self.ticks[sym].append(price)
                            if len(self.ticks[sym]) > 200:
                                self.ticks[sym] = self.ticks[sym][-200:]
                            s = str(price).rstrip('0').lstrip('0')
                            digit = int(s[-1]) if s and s[-1].isdigit() else 0
                            self.last_digits[sym].append(digit)
                            if len(self.last_digits[sym]) > 200:
                                self.last_digits[sym] = self.last_digits[sym][-200:]
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break
            except Exception as e:
                await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════
# DIGIT ANALYSIS (enhanced with sensor indicators)
# ═══════════════════════════════════════════════════════
def analyze_digits(digits, window=50):
    """Analyze digit frequencies and find best trade."""
    if len(digits) < 15:
        return None
    recent = digits[-window:]
    n = len(recent)
    freq = {d: 0 for d in range(10)}
    for d in recent:
        freq[d] += 1
    expected = n / 10
    results = []

    # Digit Match: over-represented
    for digit in range(10):
        if freq[digit] > expected * 1.15:
            strength = (freq[digit] - expected) / expected
            ev = strength * 0.08
            results.append({'strategy': f'DIGIT_MATCH_{digit}', 'contract': 'DIGITMATCH',
                'digit': digit, 'ev': ev, 'confidence': min(strength, 1.0),
                'reason': f'Digit {digit}: {freq[digit]}/{n} ({freq[digit]/n:.0%})'})

    # Digit Diff: under-represented
    for digit in range(10):
        if freq[digit] < expected * 0.7:
            strength = (expected - freq[digit]) / expected
            ev = strength * 0.003
            results.append({'strategy': f'DIGIT_DIFF_{digit}', 'contract': 'DIGITDIFF',
                'digit': digit, 'ev': ev, 'confidence': min(strength, 1.0),
                'reason': f'Digit {digit}: {freq[digit]}/{n} ({freq[digit]/n:.0%}) under'})

    # Even/Odd
    evens = sum(1 for d in recent if d % 2 == 0)
    if evens > n * 0.58:
        results.append({'strategy': 'EVEN_BIAS', 'contract': 'DIGITEVEN',
            'ev': (evens/n - 0.5) * 0.1, 'confidence': (evens - n*0.5) / (n*0.5),
            'reason': f'Even: {evens}/{n}'})
    elif (n - evens) > n * 0.58:
        results.append({'strategy': 'ODD_BIAS', 'contract': 'DIGITODD',
            'ev': ((n-evens)/n - 0.5) * 0.1, 'confidence': ((n-evens) - n*0.5) / (n*0.5),
            'reason': f'Odd: {n-evens}/{n}'})

    # Trend
    if len(digits) >= 30:
        first_half = sum(digits[-30:-15]) / 15
        second_half = sum(digits[-15:]) / 15
        diff = second_half - first_half
        if diff > 0.5:
            results.append({'strategy': 'RISE_TREND', 'contract': 'CALL',
                'ev': abs(diff) * 0.01, 'confidence': min(abs(diff) / 2, 1.0),
                'reason': f'Rising ({diff:+.2f})'})
        elif diff < -0.5:
            results.append({'strategy': 'FALL_TREND', 'contract': 'PUT',
                'ev': abs(diff) * 0.01, 'confidence': min(abs(diff) / 2, 1.0),
                'reason': f'Falling ({diff:+.2f})'})

    if not results:
        return None
    return max(results, key=lambda r: r['ev'] * r['confidence'])

# ═══════════════════════════════════════════════════════
# TRADER (separate WS for buying)
# ═══════════════════════════════════════════════════════
class Trader:
    def __init__(self):
        self.ws = None
        self.req_id = 0
        self.balance = 0
    
    async def connect(self):
        self.ws = await websockets.connect(WS_URL, ping_interval=20)
        self.req_id += 1
        await self.ws.send(json.dumps({"authorize": TOKEN, "req_id": self.req_id}))
        resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
        if resp.get('error'): return False
        self.req_id += 1
        await self.ws.send(json.dumps({"balance": 1, "req_id": self.req_id}))
        bresp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
        self.balance = bresp.get('balance', {}).get('balance', 0)
        print(f"  [TRADE] Connected. Balance: ${self.balance:.2f}")
        return True
    
    async def buy(self, contract, market, stake, digit=None):
        params = {"contract_type": contract, "symbol": market, "amount": stake,
                  "currency": "USD", "duration": 1, "duration_unit": "t", "basis": "stake"}
        if digit is not None and contract in ('DIGITMATCH', 'DIGITDIFF'):
            params["barrier"] = str(digit)
        self.req_id += 1
        await self.ws.send(json.dumps({"proposal": 1, **params, "req_id": self.req_id}))
        for _ in range(20):
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
                data = json.loads(msg)
                if 'proposal' in data and data['proposal'].get('id'): break
                if data.get('error'): return None, data['error'].get('message', 'error')
            except asyncio.TimeoutError: return None, "proposal timeout"
        proposal_id = data['proposal']['id']
        self.req_id += 1
        await self.ws.send(json.dumps({"buy": proposal_id, "price": stake, "req_id": self.req_id}))
        for _ in range(20):
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
                data = json.loads(msg)
                if 'buy' in data and data['buy'].get('contract_id'): return data['buy']['contract_id'], None
                if data.get('error'): return None, data['error'].get('message', 'buy error')
            except asyncio.TimeoutError: return None, "buy timeout"
        return None, "no response"
    
    async def wait_result(self, cid, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=3)
                data = json.loads(msg)
                poc = data.get('proposal_open_contract', {})
                if poc.get('id') == cid and poc.get('is_sold'):
                    return poc.get('profit', 0)
            except: continue
        try:
            self.req_id += 1
            await self.ws.send(json.dumps({"balance": 1, "req_id": self.req_id}))
            msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
            data = json.loads(msg)
            new_bal = data.get('balance', {}).get('balance', self.balance)
            profit = new_bal - self.balance
            self.balance = new_bal
            return profit
        except: return 0
    
    async def refresh_balance(self):
        try:
            self.req_id += 1
            await self.ws.send(json.dumps({"balance": 1, "req_id": self.req_id}))
            msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
            data = json.loads(msg)
            self.balance = data.get('balance', {}).get('balance', self.balance)
        except: pass

# ═══════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════
class Risk:
    def __init__(self, balance):
        self.balance = balance
        self.start = balance
        self.wins = 0
        self.losses = 0
        self.pnl = 0
        self.consec_loss = 0
        self.consec_win = 0
        self.total = 0
        self.market_count = {}
    
    def can_trade(self):
        if self.start > 0 and abs(min(0, self.pnl)) / self.start >= 0.02:
            return False, "daily loss limit"
        return True, "ok"
    
    def calc_stake(self, strategy):
        ev = strategy.get('ev', 0)
        conf = strategy.get('confidence', 0.5)
        if ev <= 0: return 0.35
        contract = strategy.get('contract', 'DIGITDIFF')
        if contract == 'DIGITMATCH':
            kelly = max(0, (8 * (0.1 + ev) - (0.9 - ev)) / 8)
        elif contract == 'DIGITDIFF':
            kelly = max(0, (0.06 * (0.9 + ev) - (0.1 - ev)) / 0.06)
        else:
            kelly = max(0, (0.95 * (0.5 + ev) - (0.5 - ev)) / 0.95)
        stake = self.balance * kelly * 0.1 * conf
        if contract == 'DIGITMATCH':
            return round(max(0.35, min(stake, 1.0, self.balance * 0.01)), 2)
        return round(max(0.35, min(stake, 3.0, self.balance * 0.02)), 2)
    
    def record(self, profit):
        self.total += 1
        self.pnl += profit
        self.balance += profit
        if profit > 0:
            self.wins += 1
            self.consec_win += 1
            self.consec_loss = 0
        else:
            self.losses += 1
            self.consec_loss += 1
            self.consec_win = 0
    
    def wr(self):
        return (self.wins / self.total * 100) if self.total else 0

# ═══════════════════════════════════════════════════════
# MAIN BRAIN
# ═══════════════════════════════════════════════════════
# ── Trade Log & Agent Notes (persisted in state) ──────
TRADE_LOG = []       # last 50 trades for dashboard
AGENT_NOTES = []     # agent decisions/notes
SYS_LOG = []         # system log entries

def log_sys(msg, log_type="info"):
    """Add entry to system log for dashboard."""
    entry = {'message': msg, 'log_type': log_type, 'time': int(time.time() * 1000)}
    SYS_LOG.append(entry)
    if len(SYS_LOG) > 100:
        SYS_LOG.pop(0)

def log_agent(agent, note):
    """Add agent note for dashboard."""
    entry = {'agent': agent, 'note': note, 'time': int(time.time() * 1000)}
    AGENT_NOTES.append(entry)
    if len(AGENT_NOTES) > 50:
        AGENT_NOTES.pop(0)

def log_trade(trade):
    """Add trade to history for dashboard."""
    TRADE_LOG.append(trade)
    if len(TRADE_LOG) > 50:
        TRADE_LOG.pop(0)

async def main():
    print("=" * 55)
    print("  AD-SMTA BRAIN v3 — AGENT-POWERED")
    print("=" * 55)
    log_sys("Brain v3 started", "info")
    log_agent("system", "All 7 agents initialized: sensor, memory, picker, judge, protector, regime, strategist")
    
    tc = TickCollector()
    trader = Trader()
    tools = AgentTools()
    
    if not await trader.connect():
        print("  FATAL: Cannot connect to Deriv")
        return
    
    risk = Risk(trader.balance)
    tools.init(trader.balance)
    
    tick_task = asyncio.create_task(tc.run())
    await asyncio.sleep(3)
    
    print(f"  Balance: ${risk.balance:.2f}")
    print(f"  Agents: sensor, memory, picker, judge, protector, regime, strategist")
    print(f"  Markets: {MARKET_LIST[:4]}")
    
    discord('🧠 Brain v3 Started', {
        'Balance': f'${risk.balance:.2f}',
        'Agents': '7 (sensor, memory, picker, judge, protector, regime, strategist)',
        'Markets': ', '.join(MARKET_LIST[:4]),
    })
    
    cycle = 0
    
    try:
        while True:
            cycle += 1
            
            # ── Feed ticks to agents ──
            for m in MARKET_LIST[:4]:
                for price in tc.ticks.get(m, [])[-3:]:
                    tools.feed_tick(m, price)
            
            # ── Find best market+strategy ──
            best_market = None
            best_data = None
            best_score = -1
            
            for m in MARKET_LIST[:4]:
                digits = tc.last_digits.get(m, [])
                if len(digits) < 20: continue
                
                # Basic digit analysis
                strategy = analyze_digits(digits)
                if not strategy: continue
                
                # Agent enrichment: get regime
                signal = tools.get_signal(m)
                regime_info = tools.get_regime(m, signal) if signal else None
                if isinstance(regime_info, tuple) and len(regime_info) >= 2:
                    regime = str(regime_info[0])
                elif isinstance(regime_info, dict):
                    regime = regime_info.get('regime', 'UNKNOWN')
                else:
                    regime = 'UNKNOWN'
                log_agent("regime", f"{m}: {regime}")
                
                # Agent enrichment: check memory for digit bias
                bias_result = tools.get_digit_bias(m)
                if isinstance(bias_result, tuple) and len(bias_result) >= 3:
                    freqs, best_digit, best_bias = bias_result
                    if best_digit is not None and strategy.get('digit') == best_digit and best_bias > 0.02:
                        strategy['confidence'] *= 1.2  # Boost if memory confirms bias
                
                # Agent enrichment: check strategist
                strat_key = f"{m}:{strategy['strategy']}"
                if not tools.strategist.is_approved(strat_key):
                    # New strategy, give it a chance but lower confidence
                    strategy['confidence'] *= 0.8
                
                score = strategy['ev'] * strategy['confidence'] * MARKET_WEIGHTS.get(m, 1)
                if regime in ('MOMENTUM', 'TREND'):
                    score *= 1.1  # Boost in trending markets
                
                if score > best_score:
                    best_score = score
                    best_market = m
                    best_data = strategy
                    best_data['regime'] = regime
            
            if not best_market:
                if cycle % 15 == 0:
                    print(f"  [{cycle}] Ticks: " + ", ".join(f"{m}:{len(tc.last_digits.get(m,[]))}" for m in MARKET_LIST[:4]))
                await asyncio.sleep(2)
                continue
            
            # ── Risk check ──
            can, reason = risk.can_trade()
            if not can:
                if cycle % 10 == 0: print(f"  [{cycle}] {reason}")
                await asyncio.sleep(3)
                continue
            
            # ── Protector check ──
            prot_allowed, prot_reason = tools.check_safety()
            if not prot_allowed:
                log_agent("protector", f"BLOCKED: {prot_reason}")
                if cycle % 10 == 0: print(f"  [{cycle}] Protector: {prot_reason}")
                await asyncio.sleep(3)
                continue
            
            # ── Calculate stake ──
            stake = risk.calc_stake(best_data)
            if stake < 0.35:
                await asyncio.sleep(2)
                continue
            
            # ── Judge validation (for DIGITMATCH only, skip for speed on others) ──
            if best_data['contract'] == 'DIGITMATCH':
                prot_status = {'frozen': False}
                judge_ctx = {
                    'market': best_market, 'market_type': MARKET_TYPES.get(best_market, 'volatility'),
                    'strategy': best_data, 'regime': best_data.get('regime', 'UNKNOWN'),
                    'sim_result': None, 'strategy_health': 50,
                    'risk_clearance': not prot_status.get('frozen', False),
                    'protector_status': prot_status,
                    'memory_stats': {}, 'signal': tools.get_signal(best_market) or {},
                }
                verdict = tools.validate_trade(judge_ctx)
                log_agent("judge", f"Evaluated {best_data['strategy']} on {best_market}: {verdict.get('decision', '?') if isinstance(verdict, dict) else '?'}")
                if verdict and isinstance(verdict, dict) and verdict.get('decision') == 'NO_TRADE':
                    if cycle % 20 == 0:
                        reason_str = verdict.get('reason', 'judge said no')
                        print(f"  [{cycle}] Judge blocked: {reason_str}")
                    await asyncio.sleep(2)
                    continue
            
            # ── Execute ──
            contract = best_data['contract']
            digit = best_data.get('digit')
            
            print(f"  [{cycle}] {best_market} | {best_data['strategy']} | {contract} | ${stake:.2f} | regime={best_data.get('regime','?')}")
            
            cid, err = await trader.buy(contract, best_market, stake, digit)
            if err:
                print(f"         Error: {err}")
                await asyncio.sleep(2)
                continue
            
            profit = await trader.wait_result(cid)
            risk.record(profit)
            await trader.refresh_balance()
            risk.balance = trader.balance
            
            # ── Record to all agents ──
            tools.record_result(contract, profit, best_market, best_data['strategy'], stake)
            
            # ── Log trade for dashboard ──
            emoji = '✅' if profit > 0 else '❌'
            log_trade({
                'market': best_market, 'strategy': best_data['strategy'],
                'contract': contract, 'digit': digit, 'stake': stake,
                'profit': profit, 'balance': risk.balance,
                'regime': best_data.get('regime', '?'),
                'reason': best_data.get('reason', ''),
                'time': int(time.time() * 1000),
            })
            log_agent("executor", f"{emoji} ${profit:+.2f} {contract} on {best_market} ({best_data['strategy']})")
            log_sys(f"Trade: {best_market} {contract} ${stake:.2f} → ${profit:+.2f}", "win" if profit > 0 else "loss")
            
            emoji = '✅' if profit > 0 else '❌'
            print(f"         {emoji} ${profit:+.2f} | Bal: ${risk.balance:.2f} | W/L: {risk.wins}/{risk.losses} ({risk.wr():.1f}%)")
            
            log_agent("picker", f"Contract shuffle: {best_data['strategy']} ({contract})")
            
            # ── Discord ──
            discord(f"{'✅' if profit > 0 else '❌'} ${profit:+.2f}", {
                'Market': best_market, 'Strategy': best_data['strategy'],
                'Contract': contract, 'Stake': f'${stake:.2f}',
                'P&L': f'${profit:+.2f}', 'Balance': f'${risk.balance:.2f}',
                'Win Rate': f'{risk.wr():.1f}%', 'Regime': best_data.get('regime', '?'),
            }, 0x22c55e if profit > 0 else 0xef4444)
            
            # ── Dashboard state (all fields the HTML expects) ──
            champions = tools.get_champions()
            best_strats = tools.get_best_strategies()
            prot_s = tools.protector.get_status()
            state = {
                # Core metrics
                'type': 'state', 'balance': risk.balance, 'startBalance': risk.start,
                'trades': risk.total, 'wins': risk.wins, 'losses': risk.losses,
                'win_rate': round(risk.wr(), 1),
                'daily_loss': round(abs(min(0, risk.pnl)) / risk.start * 100, 2) if risk.start else 0,
                'total_pnl': round(risk.pnl, 2), 'cycles': cycle, 'total_trades': risk.total,
                'bestStreak': risk.consec_win,
                'selected_market': best_market, 'selected_type': 'digit',
                'selected_strategy': best_data['strategy'],
                'selected_ev': round(best_data.get('ev', 0), 4),
                'all_strategies': best_data['strategy'],
                'regime': best_data.get('regime', 'UNKNOWN'),
                'regime_confidence': 0.8,
                'accuracy': round(risk.wr(), 1),
                'trade_history': TRADE_LOG[-20:],
                # Protection
                'protection': {
                    'daily_loss_pct': round(abs(min(0, risk.pnl)) / risk.start * 100, 2) if risk.start else 0,
                    'daily_loss_limit_pct': 2.0,
                    'balance_floor': round(risk.start * 0.98, 2),
                    'hour_trades': risk.total,
                    'hourly_cap': 50,
                    'peak_balance': round(max(risk.start, risk.balance), 2),
                    'drawdown_from_peak': round((1 - risk.balance / max(risk.start, risk.balance)) * 100, 2) if risk.balance > 0 else 0,
                    'frozen': not prot_s.get('allowed', True),
                    'freeze_reason': prot_s.get('reason', ''),
                },
                # Session
                'session': {
                    'mode': 'HUNT',
                    'session_pnl_pct': round(risk.pnl / risk.start * 100, 2) if risk.start else 0,
                    'daily_target_pct': 2.0,
                    'win_streak': risk.consec_win,
                    'pause_remaining': 0,
                },
                # Brain
                'brain_status': {'mode': 'ACTIVE', 'version': 'v3',
                    'consecutive_losses': risk.consec_loss, 'consecutive_wins': risk.consec_win},
                # Agents
                'agents': {
                    'sensor': True, 'memory': True, 'picker': True,
                    'judge': True, 'protector': True, 'regime': True, 'strategist': True,
                    'champions': [{'k': c.get('key',''), 's': c.get('score',0)} for c in champions] if champions else [],
                    'best_strategies': [{'k': s.get('market','')+':'+s.get('strategy',''), 'wr': s.get('win_rate',0)} for s in best_strats] if best_strats else [],
                },
                # Judge
                'judge': {
                    'last_decision': 'TRADE', 'last_reason': best_data.get('reason', ''),
                    'recent_trades': risk.total, 'recent_wains': risk.wins,
                    'total_decisions': risk.total,
                },
                # Picker
                'picker': {
                    'last_pick': {'name': best_data['strategy'], 'contract_type': best_data['contract'],
                        'score': round(best_data.get('ev', 0) * 100, 1), 'reason': best_data.get('reason', '')},
                    'catalog': [],
                },
                # Adjustments
                'adjustments': {'stake_reduction': 1.0, 'cooldown_boost': 0},
                # Strategy health
                'strategy_health': {
                    'total_strategies': len(champions) if champions else 0,
                    'active_strategies': len(champions) if champions else 0,
                    'champions': len(champions) if champions else 0,
                    'failed_archived': 0,
                    'best_health': 75,
                    'champion_list': [], 'failed_list': [],
                },
                # Simulation
                'simulation': {'paper_trades': 0, 'cached_sims': 0, 'strategies_tested': []},
                # ALM Brain
                'alm_brain': {'connected': True, 'notes': [], 'session': {'mode': 'LOCAL'},
                    'session_mode': 'LOCAL', 'connected': True, 'model': 'qwen2.5:3b',
                    'last_decision': best_data.get('reason', ''), 'current_task': best_data.get('strategy', ''),
                    'next_decision': f'{best_data.get("contract","?")} on {best_market}'},
                # Markets
                'active_markets': len(MARKET_LIST[:4]),
                'total_markets': len(MARKET_LIST),
                'market_list': {m: {'type': MARKET_TYPES.get(m, 'volatility'), 'active': True,
                    'score': round(MARKET_WEIGHTS.get(m, 1) * 50, 1),
                    'ticks': len(tc.last_digits.get(m, []))}
                    for m in MARKET_LIST[:4]},
                # Other panels (empty defaults)
                'adversarial': {'tested': 0, 'rejected': 0, 'risk_level': 'LOW'},
                'portfolio': {'total_strategies': 1, 'allocation': {'ACTIVE': 100}},
                'recovery': {'safe_mode': False, 'crash_count': 0},
                'cpp_engine': {'connected': False, 'enabled': False, 'status': 'Not built (Python-only mode)'},
                'resource_mgr': {'cpu': 0, 'ram': 0, 'mode': 'NORMAL'},
                'm_almis': {'mode': 'NORMAL', 'cpu': 0, 'ram': 0},
                'phone_resources': get_system_resources(),
                'crypto': {'connected': False, 'prices': {}},
                'mt5': {'connected': False, 'pairs': {}},
                'multi_market': {
                    'phase': 1, 'phase_name': 'OBSERVE',
                    'total_markets': len(MARKET_LIST),
                    'total_observations': sum(tc.last_digits.get(m, [None]).__len__() for m in MARKET_LIST[:4] if tc.last_digits.get(m)),
                    'rankings': [
                        {'name': m, 'source': 'deriv', 'type': MARKET_TYPES.get(m, '?'),
                         'market_score': round(MARKET_WEIGHTS.get(m, 1) * 50, 1),
                         'ticks': len(tc.last_digits.get(m, [])),
                         'traded': risk.market_count.get(m, 0),
                         'status': 'ACTIVE' if risk.market_count.get(m, 0) == 0 else ('TESTING' if risk.market_count.get(m, 0) < 5 else 'RETIRED'),
                         'trend': MARKET_TYPES.get(m, '?'),
                         'volatility': 0.01 * MARKET_WEIGHTS.get(m, 1),
                         'pnl': 0.0, 'wins': 0}
                        for m in MARKET_LIST[:6]
                    ],
                    'by_source': {
                        'deriv': len(MARKET_LIST[:6]),
                        'crypto': 0, 'mt5': 0,
                    },
                },
                'composio': {'connected': False, 'tools': []},
                'evolution': [],
                # Tick data
                'tick_data': {m: len(tc.last_digits.get(m, [])) for m in MARKET_LIST[:4]},
                # Executor status (old agent compatibility)
                'executor': {'trades_executed': risk.total, 'trades_won': risk.wins,
                    'trades_lost': risk.losses, 'win_rate': round(risk.wr(), 1),
                    'current_balance': risk.balance},
                # Memory status
                'memory': {'total_trades': risk.total, 'markets_traded': len(risk.market_count)},
                # Cooldown
                'cooling_down': False, 'remaining_sec': 0, 'tier': 'NORMAL',
                'edge': round(best_data.get('ev', 0), 4),
                # Agent notes & system log for dashboard
                'agent_notes': AGENT_NOTES[-20:],
                'sys_log': SYS_LOG[-30:],
                'discord': {'enabled': bool(DISCORD), 'total_sent': risk.total, 'errors': 0},
                'recommendation': {'direction': best_data.get('strategy', '').split('_')[-1] if 'TREND' in best_data.get('strategy','') else None,
                    'regime': best_data.get('regime', 'UNKNOWN'), 'strategy': best_data['strategy']},
                'signal': {'recent_mean': 0, 'z_score': 0, 'direction': 'NEUTRAL'},
                'time': int(time.time() * 1000),
            }
            # Update phone_resources with process info
            try:
                import subprocess as _sp
                procs = {'total': 0, 'ollama_mb': 0, 'bot_mb': 0, 'llama_mb': 0, 'cpp_mb': 0, 'ssh_mb': 0}
                for name, key in [('ollama', 'ollama_mb'), ('brain_active', 'bot_mb'), ('dashboard_server', 'bot_mb'), ('python3.*dashboard', 'cpp_mb'), ('sshd', 'ssh_mb')]:
                    try:
                        pids = _sp.check_output(['pgrep', '-f', name], stderr=_sp.DEVNULL).decode().strip().split('\n')
                        procs['total'] += len(pids)
                        for pid in pids:
                            try:
                                rss = int(open(f'/proc/{pid}/status').read().split('VmRSS:')[1].split()[0]) // 1024
                                procs[key] += rss
                            except: pass
                    except: pass
                state['phone_resources']['processes'] = procs
                state['phone_resources']['ollama_connected'] = True
            except: pass
            save_state(state)
            
            # ── Market rotation ──
            risk.market_count[best_market] = risk.market_count.get(best_market, 0) + 1
            if risk.market_count[best_market] >= 5:
                print(f"         [ROTATE] 5 trades on {best_market}")
                log_agent("picker", f"Market rotation: {best_market} → next in queue")
                log_sys(f"Market rotation: 5 trades on {best_market}", "info")
                risk.market_count[best_market] = 0
                MARKET_LIST.append(MARKET_LIST.pop(MARKET_LIST.index(best_market)))
            
            await asyncio.sleep(1)
    
    except KeyboardInterrupt: pass
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        import traceback; traceback.print_exc()
    finally:
        # Save memory
        tools.memory.save()
        print(f"\n{'='*55}")
        print(f"  SESSION: {risk.total} trades | W/L: {risk.wins}/{risk.losses} ({risk.wr():.1f}%)")
        print(f"  Balance: ${risk.balance:.2f} | P&L: ${risk.pnl:+.2f}")
        print(f"{'='*55}")
        discord('📊 Session End', {
            'Trades': str(risk.total), 'Win Rate': f'{risk.wr():.1f}%',
            'Balance': f'${risk.balance:.2f}', 'P&L': f'${risk.pnl:+.2f}',
        }, 0x22c55e if risk.pnl >= 0 else 0xef4444)
        tick_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
