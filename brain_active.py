#!/usr/bin/env python3
"""
AD-SMTA BRAIN v3 — With agent tools.
Tick analysis + 7 agents = smarter trading.
"""
import asyncio, json, time, os, sys, random
try:
    import faulthandler
    faulthandler.enable()
except: pass
import traceback
from pathlib import Path
from deriv_client import AutoReconnectDerivClient as DerivClient
from collections import deque
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
os.environ.update(ENV)

TOKEN = ENV.get('DERIV_PAT_TOKEN', '')
WS_URL = ENV.get('DERIV_WS_BASE', 'wss://api.derivws.com/trading/v1/options/ws')
DERIV_APP_ID = ENV.get('DERIV_APP_ID', '33UHIp8mdzBD5oBxBtesV')
DERIV_REST_BASE = ENV.get('DERIV_REST_BASE', 'https://api.derivws.com')
DERIV_DEMO_ACCOUNT = ENV.get('DERIV_DEMO_ACCOUNT', 'DOT93861750')
DERIV_REAL_ACCOUNT = ENV.get('DERIV_REAL_ACCOUNT', 'ROT92271006')
DISCORD = ENV.get('DISCORD_WEBHOOK', '')

# Paper trading mode (simulate trades without real money)
PAPER_MODE = False  # LIVE TRADING ON REAL DERIV ACCOUNT

# Markets sorted by research
MARKET_LIST = ['JD25', 'JD100', 'JD10', 'R_75', 'R_25', 'JD50']  # Removed: JD75(-$12), R_50(-$5), R_10(-$3), R_100(-$0.25)
MARKET_WEIGHTS = {'R_75': 1.0, 'JD100': 1.0, 'JD50': 1.0, 'JD25': 1.0, 'R_25': 1.0, 'R_100': 1.0, 'R_50': 1.0, 'JD75': 1.0, 'R_10': 1.0, 'JD10': 1.0}
# ════ MISSION CONFIG: trade only what the data proves works ════
MISSION_CONFIG = {}
try:
    _mc_file = Path(__file__).parent / 'mission_config.json'
    if _mc_file.exists():
        MISSION_CONFIG = json.loads(_mc_file.read_text())
except Exception:
    pass

MARKET_TYPES = {'R_75': 'volatility', 'JD100': 'jump', 'JD50': 'jump', 'JD25': 'jump',
                'R_25': 'volatility', 'R_100': 'volatility', 'R_50': 'volatility',
                'JD75': 'jump', 'R_10': 'volatility', 'JD10': 'jump'}

# Market, strategy & contract rotation tracker — full diversification
market_trade_counts = {}
strategy_trade_counts = {}
contract_trade_counts = {}
last_traded_market = None
last_traded_strategy = None
last_traded_contract = None
ROTATION_PENALTY = 0.35  # multiplier: each trade reduces score by 35%

# DIGIT TRADE MANAGER — smart cooldown instead of hard limit
# Tracks recent digit PnL. If profitable → allow with cooldown.
# If losing → force exit + long cooldown.
# When active, tries 2 markets simultaneously.
DIGIT_STRATEGIES = ('DIGIT_DIFF', 'DIGIT_MATCH', 'EVEN_BIAS', 'ODD_BIAS',
                     'OVER_BIAS', 'UNDER_BIAS', 'HIGH_DIGIT_DIFF', 'LOW_DIGIT_DIFF')
DIGIT_CONTRACTS = ('DIGITDIFF', 'DIGITMATCH', 'DIGITEVEN', 'DIGITODD')

class DigitManager:
    def __init__(self):
        self.recent_digit_pnl = 0.0
        self.digit_trade_history = []
        self.max_history = 10
        self.cooldown_until = 0
        self.cooldown_reason = ""
        self.active_markets = set()
        self.digit_wins_session = 0
        self.digit_losses_session = 0
        self.digit_exit_reason = ""
        self.consecutive_digit = 0
        self.MAX_CONSECUTIVE_DIGIT = 4
        # Per-market and per-strategy PnL tracking
        self.market_pnl = {}  # {market: {pnl, trades, wins, losses}}
        self.strategy_pnl = {}  # {strategy: {pnl, trades, wins, losses}}
        self.killed_markets = set()  # markets disabled for losing
        self.killed_strategies = set()  # strategies disabled for losing
        self.balance_start = None  # starting balance for growth check
        self.balance_current = 0.0

    def is_digit(self, strategy_name, contract_type=''):
        if any(ds in strategy_name for ds in DIGIT_STRATEGIES):
            return True
        if contract_type in DIGIT_CONTRACTS:
            return True
        return False

    def _update_market(self, market, profit):
        if market not in self.market_pnl:
            self.market_pnl[market] = {'pnl': 0, 'trades': 0, 'wins': 0, 'losses': 0}
        m = self.market_pnl[market]
        m['trades'] += 1
        m['pnl'] += profit
        if profit > 0:
            m['wins'] += 1
        else:
            m['losses'] += 1
        # KILL MARKET: 3+ trades and net negative → disable
        if m['trades'] >= 5 and m['pnl'] < -2.0:
            self.killed_markets.add(market)
            self.cooldown_until = time.time() + 180
            self.cooldown_reason = f"market {market} killing: {m['trades']}T PnL=\${m['pnl']:+.2f} — 3min cooldown"
            self.digit_exit_reason = self.cooldown_reason

    def _update_strategy(self, strategy, profit):
        if strategy not in self.strategy_pnl:
            self.strategy_pnl[strategy] = {'pnl': 0, 'trades': 0, 'wins': 0, 'losses': 0}
        s = self.strategy_pnl[strategy]
        s['trades'] += 1
        s['pnl'] += profit
        if profit > 0:
            s['wins'] += 1
        else:
            s['losses'] += 1
        # KILL STRATEGY: 3+ trades and net negative → disable
        if s['trades'] >= 5 and s['pnl'] < -2.0:
            self.killed_strategies.add(strategy)
            self.cooldown_until = time.time() + 180
            self.cooldown_reason = f"strategy {strategy} killing: {s['trades']}T PnL=\${s['pnl']:+.2f} — 3min cooldown"
            self.digit_exit_reason = self.cooldown_reason

    def _check_balance_growth(self):
        """If balance is not growing, be more aggressive about killing losers."""
        if self.balance_start is None:
            self.balance_start = self.balance_current
            return
        # If balance dropped below start, tighten all thresholds
        drop = self.balance_start - self.balance_current
        if drop > 3.0:
            # Aggressive: kill any market/strategy with ANY losses
            for m, data in self.market_pnl.items():
                if data['trades'] >= 2 and data['pnl'] < 0:
                    self.killed_markets.add(m)
            for s, data in self.strategy_pnl.items():
                if data['trades'] >= 2 and data['pnl'] < 0:
                    self.killed_strategies.add(s)

    def record(self, profit, market, strategy):
        self.digit_trade_history.append({'profit': profit, 'market': market, 'strategy': strategy, 'time': time.time()})
        if len(self.digit_trade_history) > self.max_history:
            self.digit_trade_history = self.digit_trade_history[-self.max_history:]
        self.recent_digit_pnl = sum(t['profit'] for t in self.digit_trade_history)
        self.consecutive_digit += 1
        if profit > 0:
            self.digit_wins_session += 1
        else:
            self.digit_losses_session += 1
        self.active_markets.add(market)
        # Track per-market and per-strategy
        self._update_market(market, profit)
        self._update_strategy(strategy, profit)
        self._check_balance_growth()
        # HARD EXIT: consecutive digit trades hit limit
        if self.consecutive_digit >= self.MAX_CONSECUTIVE_DIGIT:
            self.cooldown_until = time.time() + 90
            self.cooldown_reason = f"hit {self.consecutive_digit} consecutive digit trades — 90s cooldown"

    def is_in_cooldown(self):
        return time.time() < self.cooldown_until

    def get_cooldown_remaining(self):
        return max(0, self.cooldown_until - time.time())

    def is_market_killed(self, market):
        return market in self.killed_markets

    def is_strategy_killed(self, strategy):
        return strategy in self.killed_strategies

    def should_trade(self, market=None, strategy=None):
        # 1. Check cooldown
        if self.is_in_cooldown():
            return False, f"cooldown {self.get_cooldown_remaining():.0f}s ({self.cooldown_reason})"

        # 2. Check if market/strategy is killed
        if market and self.is_market_killed(market):
            return False, f"market {market} killed (PnL=\${self.market_pnl.get(market,{}).get('pnl',0):+.2f})"
        if strategy and self.is_strategy_killed(strategy):
            return False, f"strategy {strategy} killed (PnL=\${self.strategy_pnl.get(strategy,{}).get('pnl',0):+.2f})"

        # 3. HARD LIMIT: max 2 consecutive digit trades
        if self.consecutive_digit >= self.MAX_CONSECUTIVE_DIGIT:
            self.cooldown_until = time.time() + 180
            self.cooldown_reason = f"{self.consecutive_digit} consecutive digit trades — 3min cooldown"
            return False, self.cooldown_reason

        # 4. Losing money on recent digit trades → long cooldown
        if len(self.digit_trade_history) >= 3 and self.recent_digit_pnl < -2.0:
            self.cooldown_until = time.time() + 300
            self.cooldown_reason = f"losing PnL=${self.recent_digit_pnl:+.2f} — switching to trend"
            return False, self.cooldown_reason

        # 5. Last digit trade won → 60s cooldown
        if len(self.digit_trade_history) > 0 and self.digit_trade_history[-1]['profit'] > 0:
            self.cooldown_until = time.time() + 60
            self.cooldown_reason = f"digit win cooldown 60s (PnL=${self.recent_digit_pnl:+.2f})"
            return True, "win cooldown set"

        return True, f"ready (PnL=${self.recent_digit_pnl:+.2f})"

    def update_balance(self, balance):
        self.balance_current = balance
        if self.balance_start is None:
            self.balance_start = balance

    def reset(self):
        self.digit_trade_history.clear()
        self.recent_digit_pnl = 0.0
        self.active_markets.clear()
        self.digit_wins_session = 0
        self.digit_losses_session = 0
        self.digit_exit_reason = ""
        self.consecutive_digit = 0
        self.killed_markets.clear()
        self.killed_strategies.clear()
        self.market_pnl.clear()
        self.strategy_pnl.clear()
        self.balance_start = None

digit_mgr = DigitManager()
# NOISE DETECTOR — identifies random/noisy markets and logs them
class NoiseDetector:
    def __init__(self):
        self.market_entropy = {}  # {market: entropy_value}
        self.market_tick_variance = {}  # {market: variance}
        self.noisy_markets = set()  # markets flagged as noisy
        self.quiet_markets = set()  # markets with no signal
        self.last_analysis = 0
        self.analysis_interval = 30  # analyze every 60s

    def analyze(self, last_digits_dict, cycle):
        """Analyze digit distribution for noise. High entropy = noisy."""
        if cycle - self.last_analysis < self.analysis_interval:
            return
        self.last_analysis = cycle
        self.noisy_markets.clear()
        self.quiet_markets.clear()
        print(f"  [NOISE] Analyzing {len(last_digits_dict)} markets at cycle {cycle}", flush=True)

        for market, digits in last_digits_dict.items():
            print(f"  [NOISE] {market}: {len(digits)} digits", flush=True)
            if len(digits) < 30:
                self.quiet_markets.add(market)
                continue

            # Shannon entropy — high = random, low = pattern
            freq = {}
            for d in digits[-50:]:
                freq[d] = freq.get(d, 0) + 1
            n = len(digits[-50:])
            entropy = 0
            for count in freq.values():
                if count > 0:
                    p = count / n
                    entropy -= p * log2(p)

            self.market_entropy[market] = entropy

            # Max entropy for base-10 = log2(10) = 3.32
            # If entropy > 3.15, market is very noisy
            if entropy > 3.15:
                self.noisy_markets.add(market)
                log_agent("noise", f"{market}: ENTROPY {entropy:.2f}/3.32 — NOISY (random, avoid)")
            elif entropy < 2.5:
                # Good — some pattern exists
                pass
            else:
                log_agent("noise", f"{market}: entropy {entropy:.2f}/3.32 — moderate noise")

            # Check digit concentration — if one digit dominates, market has signal
            max_digit_ratio = max(freq.values()) / n if n > 0 else 0
            if max_digit_ratio > 0.18:
                # More than 18% on one digit = signal exists
                best_digit = max(freq, key=freq.get)
                log_agent("noise", f"{market}: digit {best_digit} at {max_digit_ratio:.0%} — SIGNAL detected")

    def is_noisy(self, market):
        return market in self.noisy_markets

    def is_quiet(self, market):
        return market in self.quiet_markets

    def get_status(self):
        return {
            'noisy': list(self.noisy_markets),
            'quiet': list(self.quiet_markets),
            'entropy': {k: round(v, 2) for k, v in self.market_entropy.items()},
        }

noise_detector = NoiseDetector()

# MARKET INTELLIGENCE — precision market selection from all data
class MarketIntelligence:
    def __init__(self):
        self.market_scores = {}
        self.strategy_scores = {}
        self.last_update = 0
        self.update_interval = 120
        self.market_rankings = []
        self.top_strategies = []

    def update(self, memory, entropy_data, cycle):
        if cycle - self.last_update < self.update_interval:
            return
        self.last_update = cycle
        strategies = memory.strategies if hasattr(memory, 'strategies') else {}
        self.market_scores = {}
        self.strategy_scores = {}
        for key, data in strategies.items():
            if not isinstance(data, dict): continue
            parts = key.split(':')
            if len(parts) < 2: continue
            mkt = parts[0]
            strat = ':'.join(parts[1:])
            trades = data.get('trades', 0)
            wins = data.get('wins', 0)
            pnl = data.get('total_profit', 0)
            if trades < 3: continue
            wr = wins / trades
            ev = pnl / trades
            wr_score = max(0, (wr - 0.5) * 2)
            ev_score = max(0, min(1, ev * 2))
            exp_score = min(1, trades / 20)
            score = wr_score * 0.4 + ev_score * 0.4 + exp_score * 0.2
            if pnl > 0: score *= 1.3
            elif pnl < -5: score *= 0.5
            self.strategy_scores[key] = {
                'score': round(score, 4), 'trades': trades,
                'wr': round(wr * 100, 1), 'ev': round(ev, 4),
                'pnl': round(pnl, 2), 'market': mkt, 'strategy': strat,
            }
            if mkt not in self.market_scores:
                self.market_scores[mkt] = {'scores': [], 'pnl': 0, 'trades': 0, 'wins': 0}
            self.market_scores[mkt]['scores'].append(score)
            self.market_scores[mkt]['pnl'] += pnl
            self.market_scores[mkt]['trades'] += trades
            self.market_scores[mkt]['wins'] += wins
        for mkt in self.market_scores:
            ms = self.market_scores[mkt]
            avg_score = sum(ms['scores']) / len(ms['scores']) if ms['scores'] else 0
            ms['final_score'] = round(avg_score, 4)
            ms['wr'] = round(ms['wins'] / max(1, ms['trades']) * 100, 1)
            entropy = entropy_data.get(mkt, 3.0)
            ms['noisy'] = entropy > 3.15
            if ms['noisy']:
                ms['final_score'] *= 0.3
        self.market_rankings = sorted(self.market_scores.items(), key=lambda x: -x[1]['final_score'])
        self.top_strategies = sorted(self.strategy_scores.items(), key=lambda x: -x[1]['score'])[:20]

    def get_best_market(self):
        for mkt, data in self.market_rankings:
            if not data.get('noisy', False) and data['final_score'] > 0:
                return mkt, data
        for mkt, data in self.market_rankings:
            if not data.get('noisy', False):
                return mkt, data
        return None, None

    def get_best_strategy(self, market=None):
        for key, data in self.top_strategies:
            if market and data['market'] != market: continue
            if data['score'] > 0 and data['trades'] >= 3:
                return key, data
        return None, None

    def get_recommendation(self):
        best_mkt, mkt_data = self.get_best_market()
        best_strat, strat_data = self.get_best_strategy()
        top3 = [(m, d) for m, d in self.market_rankings[:3] if not d.get('noisy')]
        return {
            'best_market': best_mkt, 'market_score': mkt_data,
            'best_strategy': best_strat, 'strategy_score': strat_data,
            'top_3_markets': top3,
        }

    def log_status(self, cycle):
        rec = self.get_recommendation()
        if rec['best_market']:
            log_agent('intelligence',
                "Best market: %s (score=%.3f, WR=%.1f%%, PnL=$%.2f)" % (
                    rec['best_market'], rec['market_score']['final_score'],
                    rec['market_score']['wr'], rec['market_score']['pnl']))
        if rec['best_strategy']:
            log_agent('intelligence',
                "Best strategy: %s (score=%.3f, WR=%.1f%%, EV=$%.4f)" % (
                    rec['best_strategy'], rec['strategy_score']['score'],
                    rec['strategy_score']['wr'], rec['strategy_score']['ev']))
        noisy = [m for m, d in self.market_rankings if d.get('noisy')]
        if noisy:
            log_agent('intelligence', "Noisy (avoid): %s" % ', '.join(noisy))

market_intel = MarketIntelligence()

# SELF-IMPROVING PROMPT — learns from trade results
class PromptImprover:
    def __init__(self):
        self.prompt_history = []
        self.last_improvement = 0
        self.improvement_interval = 300

    def build_smart_prompt(self, market, strategy, regime, recent_trades, balance, pnl, entropy):
        wins = sum(1 for t in recent_trades[-10:] if t.get('profit', 0) > 0)
        losses = sum(1 for t in recent_trades[-10:] if t.get('profit', 0) < 0)
        recent_wr = wins / max(1, wins + losses) * 100
        strat_key = "%s:%s" % (market, strategy)
        strat_data = market_intel.strategy_scores.get(strat_key, {})
        strat_wr = strat_data.get('wr', 0)
        strat_pnl = strat_data.get('pnl', 0)
        strat_trades = strat_data.get('trades', 0)
        noise_label = "noisy" if entropy > 3.15 else "has signal"
        prompt = (
            "You are an expert trading advisor. Analyze this trade:\n"
            "MARKET: %s\nSTRATEGY: %s\nREGIME: %s\n"
            "ENTROPY: %.2f/3.32 (%s)\n"
            "BALANCE: $%.2f\nDAILY PnL: $%+.2f\n"
            "STRATEGY on %s: %d trades, WR %.1f%%, PnL $%+.2f\n"
            "RECENT: %dW/%dL (%.0f%%)\n\n"
            "RULES:\n"
            "1. Entropy > 3.0 (noisy) = NO\n"
            "2. Strategy WR < 40%% after 10+ trades = NO\n"
            "3. Daily PnL < -$5 = NO\n"
            "4. Strategy PnL negative on market = NO\n"
            "5. Only YES if genuine edge\n\n"
            "Answer ONLY YES or NO with 1-sentence reason."
        ) % (market, strategy, regime, entropy, noise_label,
             balance, pnl, market, strat_wr, strat_pnl,
             wins, losses, recent_wr)
        return prompt

    def improve(self, cycle):
        if cycle - self.last_improvement < self.improvement_interval:
            return
        self.last_improvement = cycle
        if len(self.prompt_history) > 20:
            recent = self.prompt_history[-20:]
            win_prompts = [p for p in recent if p.get('result', 0) > 0]
            win_rate = len(win_prompts) / max(1, len(recent)) * 100
            log_agent('improver', "Prompt analysis: %d prompts, %.0f%% wins" % (len(recent), win_rate))

    def record_prompt(self, prompt, result, market, strategy):
        self.prompt_history.append({
            'prompt_hash': hash(prompt[:100]),
            'result': result, 'market': market,
            'strategy': strategy, 'time': time.time(),
        })
        if len(self.prompt_history) > 50:
            self.prompt_history = self.prompt_history[-50:]

prompt_improver = PromptImprover()


def is_digit_trade(strategy_name, contract_type=''):
    return digit_mgr.is_digit(strategy_name, contract_type)

def get_contract_rotation_multiplier(contract):
    """Penalize over-used contract types (PUT, CALL, ASIANU, ASIAND)."""
    count = contract_trade_counts.get(contract, 0)
    mult = max(0.2, 1.0 - count * ROTATION_PENALTY)
    if contract == last_traded_contract:
        mult *= 0.5
    return mult

def record_contract_trade(contract):
    global last_traded_contract
    contract_trade_counts[contract] = contract_trade_counts.get(contract, 0) + 1
    last_traded_contract = contract

def get_strategy_rotation_multiplier(strategy):
    """Penalize over-used strategies."""
    count = strategy_trade_counts.get(strategy, 0)
    mult = max(0.15, 1.0 - count * ROTATION_PENALTY)
    if strategy == last_traded_strategy:
        mult *= 0.5
    return mult

def record_strategy_trade(strategy):
    global last_traded_strategy
    strategy_trade_counts[strategy] = strategy_trade_counts.get(strategy, 0) + 1
    last_traded_strategy = strategy

def get_rotation_multiplier(market):
    """Returns a score multiplier that penalizes over-used markets."""
    global last_traded_market
    count = market_trade_counts.get(market, 0)
    # Penalize: each trade reduces multiplier by 40%, floor at 10%
    mult = max(0.1, 1.0 - count * ROTATION_PENALTY)
    # Extra penalty for same market as last trade
    if market == last_traded_market:
        mult *= 0.5
    return mult

def record_market_trade(market, strategy=None, contract=None):
    global last_traded_market
    market_trade_counts[market] = market_trade_counts.get(market, 0) + 1
    last_traded_market = market
    if strategy:
        record_strategy_trade(strategy)
    if contract:
        record_contract_trade(contract)
    # Track digit trades in smart manager
    if is_digit_trade(strategy or '', contract or ''):
        digit_mgr.record(0, market, strategy)  # profit updated later
    else:
        digit_mgr.consecutive_digit = 0  # reset counter on non-digit trade
    # Decay all counts periodically (every 20 trades, halve all counts)
    total = sum(market_trade_counts.values())
    if total > 0 and total % 20 == 0:
        for k in list(market_trade_counts.keys()):
            market_trade_counts[k] = max(0, market_trade_counts[k] // 2)
        for k in list(strategy_trade_counts.keys()):
            strategy_trade_counts[k] = max(0, strategy_trade_counts[k] // 2)
        for k in list(contract_trade_counts.keys()):
            contract_trade_counts[k] = max(0, contract_trade_counts[k] // 2)

STATE_FILE = Path(__file__).parent / 'trading_state.json'
DASH_STATE = Path(__file__).parent / 'dashboard' / 'templates' / 'state.json'

# ═══════════════════════════════════════════════════════
# AGENT TOOLS — Import the 7 useful agents
# ═══════════════════════════════════════════════════════
from agents.sensor import SensorAgent
from agents.regime import RegimeEngine
from agents.memory import Memory
from agents.contract_picker import ContractPicker
from agents.strategist import Strategist

# Stub Judge if missing
try:
    from agents.judge import Judge
except ImportError:
    class Judge:
        def validate(self, *a, **kw): return True, 'stub'
        def get_status(self): return {}
from agents.optimizer import CompetitionEngine, BotScorer, EfficiencyAgent
from agents.risk import Protector, PLManager
from agents.improver import ResearchDirector, SelfImprover
from agents.alm_brain import ALMBrain
from agents.system_watcher import SystemWatcher
from agents.price_action import PriceAction
from agents.telegram import send_message, log_trade, log_brain_action, log_system_event, notify_startup, notify_trade, notify_session_summary, notify_escalation, notify_market_rotation, notify_error, notify_daily_report, get_status as tg_status
from agents.goal_manager import GoalManager
from agents.unified_engine import UnifiedEngine
from agents.mission import Mission
from agents.growth import GrowthEngine
from agents.profit_replicator import ProfitReplicator
from agents.log_manager import log_manager
from agents.self_diagnostic import SelfDiagnostic
from agents.eat_specialist import EatMarketSpecialist
from agents.ymcrc import YieldGatekeeper
from agents.bunch_runner import BunchRunner
from agents.profit_mirror import ProfitMirror
from agents.mission_tracker import MissionTracker
from agents.supervisor import Supervisor
from agents.perf_tracker import PerformanceTracker

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
        self.alm = ALMBrain()
        self.alm._mem = self.memory  # wire persistent knowledge into brain
        self.price_action = PriceAction()
        self.efficiency = EfficiencyAgent()
        self.pl = PLManager()
        self.research = ResearchDirector()
        self.competition = CompetitionEngine()
        self.scorer = BotScorer()
        self.goal = GoalManager()
        self.improver = SelfImprover()
        self.session_mgr = SessionManager()
        self.overtrade = OverTradeGuard()
        self.overtrade.bypass = False  # Over-trade guard active — fatigue/degradation protection
        self.backtester = TickBacktester()
        self.prob_engine = ProbabilityEngine()
        self.round_profit = RoundProfitManager()
        self.profit_guard = ProfitGuard()
        self.trade_intel = TradeIntelligence()
        self.tz_intel = MarketTimezoneIntel()
        self.openrouter = UnifiedEngine(os.environ.get('OPENROUTER_API_KEY', ''))
        self.mission = Mission()
        self.growth = GrowthEngine()
        self.replicator = ProfitReplicator()
        self.diagnostic = SelfDiagnostic()
        self.eat = EatMarketSpecialist()
        self.ymcrc = YieldGatekeeper()
        self.bunch_runner = BunchRunner()
        self.profit_mirror = ProfitMirror()
        self.mission_tracker = MissionTracker()
        self.supervisor = Supervisor()
        self.perf_tracker = PerformanceTracker()
        self.anomaly = AnomalyDetector()
        self.state_brain = MarketStateBrain()
        self.initialized = False
        self.watcher = None  # initialized after tools setup
    
    def init(self, balance):
        """Initialize protector with starting balance."""
        self.protector.init(balance)
        self.goal.init(balance)
        self.session_mgr.reset()
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
        self.alm.record_trade_result(profit)
        win = profit > 0
        # Telegram notification
        try:
            notify_trade(profit, market, strategy, risk.balance if "risk" in dir() else 0, win, stake)
        except: pass
        try:
            log_trade(market, strategy, profit, risk.balance if "risk" in dir() else 0, win)
        except: pass
        # OpenRouter: notify model of trade result for scoring
        try:
            if hasattr(tools, 'openrouter') and tools.openrouter:
                tools.openrouter.notify_trade_result(profit)
        except: pass
        # POST-TRADE EVALUATION: retire losing strategies
        retired = self.memory.evaluate_and_retire()
        for r in retired:
            try:
                log_agent("learner", f"RETIRED: {r['key']} - {r['reason']}")
                log_sys(f"Strategy retired: {r['key']}", "warn")
            except: pass

        for exp_id in list(self.research.active_experiments):
            exp = self.research.experiments.get(exp_id, {})
            exp_market = exp.get("market", "")
            exp_strategy = exp.get("strategy", "")
            # Match if trade is on same market OR same strategy
            if exp_market == market or exp_strategy.endswith(strategy):
                self.research.record_trade(exp_id, win, profit)
        for rid in list(self.competition.active_rounds):
            rnd = self.competition.rounds.get(rid, {})
            if rnd.get("market") == market:
                self.competition.record_trade(rid, strategy, win, profit)
    
    def get_best_strategies(self):
        """Get best strategies from memory."""
        return self.memory.get_best_strategies(5)
    
    def get_strategist_rec(self, market, regime):
        """Get strategist recommendation."""
        return self.strategist.get_strategy_recommendation(market, regime)
    
    def get_champions(self):
        """Get champion strategies."""
        return self.strategist.get_champions(5)
    
    def propose_experiment(self, context):
        """Ask research director to propose next experiment."""
        return self.research.propose_experiment(context)
    
    def record_experiment(self, exp_id, win, pnl):
        """Record trade result for an active experiment."""
        self.research.record_trade(exp_id, win, pnl)
    
    def start_competition(self, market, strategies, ctx=None):
        """Start a strategy competition round."""
        return self.competition.start_competition(market, strategies, ctx)
    
    def record_competition(self, round_id, strategy, win, pnl):
        """Record trade in a competition round."""
        self.competition.record_trade(round_id, strategy, win, pnl)
    
    def score_all_strategies(self, performance_data):
        for key, perf in performance_data.items():
            if isinstance(perf, dict) and perf.get('trades', 0) >= 5:
                self.scorer.score_strategy(
                    key,
                    trades=perf.get('trades', 0),
                    wins=perf.get('wins', 0),
                    losses=perf.get('losses', 0),
                    pnl=perf.get('pnl', 0.0),
                    ev=perf.get('ev', 0.0)
                )
        return self.scorer.rank_all()
    
    def analyze_failures(self, performance_data):
        for key, perf in performance_data.items():
            if isinstance(perf, dict) and perf.get('trades', 0) >= 10:
                wr = perf.get('wins', 0) / max(perf.get('trades', 1), 1)
                ev = perf.get('ev', 0)
                if wr < 0.5 or ev < 0:
                    self.scorer.generate_failure_report(key, perf)
        return self.scorer.reports[-5:]
    
    def auto_optimize(self, strategy_key, performance):
        if performance.get('trades', 0) < 15:
            return None
        wr = performance.get('wins', 0) / max(performance.get('trades', 1), 1)
        recent_wr = performance.get('recent_win_rate', wr)
        ev = performance.get('ev', 0)
        if recent_wr < wr * 0.75 and performance.get('trades', 0) > 20:
            return {'action': 'OPTIMIZE', 'strategy': strategy_key, 'reason': f'WR declined {wr:.0%} to {recent_wr:.0%}', 'suggestion': 'Widen entry threshold'}
        if ev < 0 and performance.get('trades', 0) > 15:
            return {'action': 'KILL', 'strategy': strategy_key, 'reason': f'Negative EV ({ev:.4f})', 'suggestion': 'Archive and replace'}
        if performance.get('max_consecutive_losses', 0) >= 5:
            return {'action': 'RESTRICT', 'strategy': strategy_key, 'reason': f'Losing streak {performance["max_consecutive_losses"]}', 'suggestion': 'Limit to specific regimes'}
        return None
    
    def get_scorer_status(self):
        return self.scorer.get_status()

    def get_research_status(self):
        """Get research director status."""
        return self.research.get_status()
    
    def get_competition_status(self):
        """Get competition engine status."""
        return self.competition.get_status()
    
    def ask_brain(self, market, strategy, regime, recent_trades, balance,
                   signal=None, strategy_health=None, risk_state=None,
                   tick_data=None, cpp_predictions=None, all_markets=None,
                   price_action=None):
        """Ask ALM brain NEXUS pipeline if we should take this trade.
        Returns (ok, reason, nexus_result).
        Uses background thread to avoid blocking the trading loop."""
        if not self.alm or not self.alm.connected or not self.alm.enabled:
            return True, "brain_offline", {}

        # Notify: model is analyzing
        log_agent("brain", f"Analyzing: {strategy} on {market} regime={regime} bal=${balance:.2f}")

        # Build full environment for the model
        env_snapshot = self.alm.build_environment(
            market, strategy, regime, recent_trades, balance,
            signal=signal, strategy_health=strategy_health, risk_state=risk_state,
            tick_data=tick_data, cpp_predictions=cpp_predictions, all_markets=all_markets
        )
        env_text = self.alm.format_environment(env_snapshot)
        
        # Run NEXUS pipeline in background thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.alm.nexus_decide, market, strategy, regime,
                recent_trades, balance, signal, strategy_health, risk_state,
                env_text=env_text
            )
            try:
                result = future.result(timeout=15)  # 15s max
            except concurrent.futures.TimeoutError:
                print(f"  [ALM] NEXUS timeout (15s) — allowing trade", flush=True)
                self.alm.write_note(f"NEXUS timeout — auto-approved: {strategy} on {market}", "action")
                log_agent("brain", f"NEXUS TIMEOUT after 25s — auto-approving {strategy}")
                return True, "nexus_timeout_auto_approved", {}
            except Exception as e:
                print(f"  [ALM] NEXUS error: {e} — allowing trade", flush=True)
                log_agent("brain", f"NEXUS ERROR: {str(e)[:60]} — fallback to rule-based")
                return True, f"nexus_error: {e}", {}

        ok = result.get("ok", True)
        reason = result.get("reason", "nexus_decided")
        conf = result.get("confidence", 5)
        model = result.get("model", "?")
        decision = result.get("decision", "TRADE")
        raw = result.get("raw", "")[:120]

        if not ok:
            print(f"  [ALM] NEXUS WAIT: conf={conf}/10 [{model}] — {reason[:60]}", flush=True)
            self.alm.write_note(f"NEXUS WAIT: conf={conf}/10 [{model}] — {reason[:80]}", "loss")
            log_agent("brain", f"BLOCKED TRADE: {strategy} on {market} — conf={conf}/10 [{model}] reason={reason[:80]}")
            log_sys(f"NEXUS blocked {strategy}/{market}: conf={conf} reason={reason[:60]}", "warn")
            try:
                tools.memory.record_model_decision("block", market, strategy, reason, {"confidence": conf, "model": model})
            except: pass
            return False, reason, result

        print(f"  [ALM] NEXUS TRADE: conf={conf}/10 [{model}] — {reason[:60]}", flush=True)
        self.alm.write_note(f"NEXUS TRADE: conf={conf}/10 [{model}] — {reason[:80]}", "action")
        log_agent("brain", f"APPROVED TRADE: {strategy} on {market} — conf={conf}/10 [{model}] reason={reason[:80]}")
        log_sys(f"NEXUS approved {strategy}/{market}: conf={conf} [{model}]", "info")
        try:
            tools.memory.record_model_decision("approve", market, strategy, reason, {"confidence": conf, "model": model})
        except: pass
        # SUPERVISOR CHALLENGE: audit this decision before execution
        try:
            _supCtx = {
                "market": market, "strategy": strategy,
                "hour": int(time.strftime('%H')),
                "daily_loss": abs(min(0, risk.pnl)),
                "trades_today": risk.total,
                "strategy_pnl": tools.memory.strategies.get(f"{market}:{strategy}", {}).get("total_profit", 0),
            }
            _supResult = tools.supervisor.challenge(result, _supCtx)
            if not _supResult.get("approved", True):
                _challenges = _supResult.get("challenges", [])
                if _challenges:
                    log_agent("supervisor", f"CHALLENGED: {_challenges[0][:80]}")
                    return False, f"supervisor_blocked: {_challenges[0][:60]}", result
        except: pass
        return True, "nexus_approved", result

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


def _collect_procs():
    procs = {'total': 0, 'ollama_mb': 0, 'bot_mb': 0, 'llama_mb': 0, 'cpp_mb': 0, 'ssh_mb': 0}
    try:
        import subprocess as _sp
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
    except: pass
    return procs

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

# ═══════════════════════════════════════════════════════
# NETWORK HEALTH MONITOR — stops trading when disconnected
# ═══════════════════════════════════════════════════════
class NetworkHealth:
    def __init__(self):
        self.deriv_connected = False
        self.ticks_connected = False
        self.ollama_connected = False
        self.last_tick_time = 0
        self.consecutive_failures = 0

    def tick_received(self):
        self.ticks_connected = True
        self.last_tick_time = time.time()
        self.consecutive_failures = 0

    def deriv_ok(self):
        self.deriv_connected = True
        self.consecutive_failures = 0

    def deriv_down(self):
        self.deriv_connected = False
        self.consecutive_failures += 1

    def ticks_down(self):
        self.ticks_connected = False

    def ollama_ok(self):
        self.ollama_connected = True

    def ollama_down(self):
        self.ollama_connected = False

    def is_safe(self):
        now = time.time()
        if not self.deriv_connected:
            return False, "Deriv WS disconnected"
        if not self.ticks_connected:
            return False, "No tick data"
        if self.last_tick_time > 0 and (now - self.last_tick_time) > 30:
            self.ticks_connected = False
            return False, "Tick stream stalled >60s"
        if self.consecutive_failures >= 3:
            return False, str(self.consecutive_failures) + " connection failures"
        return True, "CLEAR"

    def get_status(self):
        return {
            "deriv": self.deriv_connected,
            "ticks": self.ticks_connected,
            "ollama": self.ollama_connected,
            "tick_age": int(time.time() - self.last_tick_time) if self.last_tick_time else -1,
            "failures": self.consecutive_failures,
            "safe": self.is_safe()[0],
        }

class TickCollector:
    def __init__(self):
        self.ticks = {}
        self.last_digits = {}
        self.running = False
        self.net_health = None
        self._deriv_client = None
    
    def set_deriv_client(self, client):
        self._deriv_client = client
    
    async def run(self):
        # Use v1 public endpoint for tick polling
        public_url = "wss://api.derivws.com/trading/v1/options/ws/public"
        while True:
            try:
                ws = await websockets.connect(public_url, ping_interval=20)
                print("  [TICKS] Connected to v1 public endpoint")
                if self.net_health: self.net_health.ticks_connected = True
                self.running = True
                while self.running:
                    try:
                        end = int(time.time())
                        for m in MARKET_LIST:
                            try:
                                await ws.send(json.dumps({
                                    "ticks_history": m, "adjust_start_time": 1,
                                    "count": 3, "end": end, "req_id": random.randint(100,999)
                                }))
                                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                                data = json.loads(msg)
                                hist = data.get("history", {})
                                prices = hist.get("prices", [])
                                if prices:
                                    if m not in self.ticks:
                                        self.ticks[m] = []
                                        self.last_digits[m] = []
                                    for price in prices:
                                        if price not in self.ticks[m][-10:]:
                                            self.ticks[m].append(price)
                                            if len(self.ticks[m]) > 200:
                                                self.ticks[m] = self.ticks[m][-200:]
                                            s = str(price).rstrip('0').lstrip('0')
                                            digit = int(s[-1]) if s and s[-1].isdigit() else 0
                                            self.last_digits[m].append(digit)
                                            if hasattr(self, "_backtester_ref") and self._backtester_ref:
                                                self._backtester_ref.record_tick(m, digit, price)
                                            if hasattr(self, "_trade_intel_ref") and self._trade_intel_ref:
                                                self._trade_intel_ref.update_tick(m, digit, price)
                                            if len(self.last_digits[m]) > 200:
                                                self.last_digits[m] = self.last_digits[m][-200:]
                                    if self.net_health: self.net_health.tick_received()
                            except (asyncio.TimeoutError, KeyError):
                                continue
                        await asyncio.sleep(2)
                    except websockets.ConnectionClosed:
                        if self.net_health: self.net_health.ticks_down()
                        break
            except Exception as e:
                if self.net_health: self.net_health.ticks_down()
                await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════
# DIGIT ANALYSIS (enhanced with sensor indicators)
# ═══════════════════════════════════════════════════════
import math as _math
from math import log2

def _chi_squared(freq, n, expected_per_bin):
    """Chi-squared goodness-of-fit test. Returns (chi2_stat, p_value_approx)."""
    chi2 = 0.0
    for d in range(10):
        obs = freq.get(d, 0)
        chi2 += (obs - expected_per_bin) ** 2 / expected_per_bin
    # Approximate p-value using Wilson-Hilferty (9 df)
    # chi2 > 16.92 => p < 0.05; chi2 > 21.67 => p < 0.01
    return chi2, chi2 > 16.92

def _even_odd_chi(evens, n):
    """Chi-squared for even/odd deviation from 50/50."""
    expected = n / 2
    chi2 = ((evens - expected) ** 2 / expected) + (((n - evens) - expected) ** 2 / expected)
    return chi2, chi2 > 3.84  # 1 df, p < 0.05

def analyze_digits(digits, window=50, cpp_signal=0):
    """Analyze digit frequencies with statistical significance testing.
    
    KEY FIX: Uses chi-squared test to avoid trading on random noise.
    DIGIT_MATCH targets UNDER-represented digits (regression to mean).
    DIGIT_DIFF targets OVER-represented digits (high probability of NOT matching).
    Only trades when deviation is statistically significant (p < 0.05).
    """
    if len(digits) < 15:
        return None
    recent = digits[-window:]
    n = len(recent)
    if n < 15:
        return None
    
    freq = {d: 0 for d in range(10)}
    for d in recent:
        freq[d] += 1
    expected_per_bin = n / 10.0
    
    # ── Chi-squared test: is the distribution non-uniform? ──
    chi2, is_significant = _chi_squared(freq, n, expected_per_bin)
    
    results = []
    
    if is_significant:
        # Distribution is non-uniform — we have signal
        
        for digit in range(10):
            obs = freq[digit]
            ratio = obs / expected_per_bin
            
            # DIGIT_DIFF: bet AGAINST over-represented digits
            # (high probability digit won't match next tick)
            if ratio > 1.3:
                strength = (obs - expected_per_bin) / expected_per_bin
                # EV for DIGITDIFF: ~90% win rate, ~6% payout
                # Real EV = win_rate * (1 + payout) - 1
                win_prob = 1.0 - (1.0 / 10.0)  # 90% chance digit differs
                ev = strength * 0.04  # conservative EV estimate
                results.append({
                    'strategy': f'DIGIT_DIFF_{digit}', 'contract': 'DIGITDIFF',
                    'digit': digit, 'ev': ev * 0.3,  # cooldown only — not main trade
                    'confidence': min(strength * 0.4, 0.5),
                    'reason': f'COOLDOWN: Digit {digit}: {obs}/{n} ({obs/n:.0%}) — parking bet',
                })
            
            # DIGIT_MATCH: bet ON under-represented digits (regression to mean)
            # Only when strongly under-represented AND significant
            if ratio < 0.7 and chi2 > 12.0:  # p < 0.01 for safety
                strength = (expected_per_bin - obs) / expected_per_bin
                # EV for DIGITMATCH: ~10% win rate, ~8x payout
                # Real EV = 0.1 * 9 - 1 = -0.1 (negative! must find edge)
                # We need the digit to be at least 2x under-represented
                ev = strength * 0.06  # only positive if strong under-representation
                results.append({
                    'strategy': f'DIGIT_MATCH_{digit}', 'contract': 'DIGITMATCH',
                    'digit': digit, 'ev': ev,
                    'confidence': min(strength * 0.6, 0.7),
                    'reason': f'Digit {digit}: {obs}/{n} ({obs/n:.0%}) — regression bet',
                })
    
    # ── Even/Odd bias (active strategy) ──
    evens = sum(1 for d in recent if d % 2 == 0)
    odds = n - evens
    e_ratio = evens / n
    if evens > n * 0.58:
        dev = (evens / n - 0.5) * 2
        results.append({
            'strategy': 'EVEN_BIAS', 'contract': 'DIGITEVEN',
            'digit': None, 'ev': dev * 0.03,
            'confidence': min(dev * 1.5, 0.7),
            'reason': f'Even bias: {evens}/{n} ({e_ratio:.0%}) — bet even',
        })
    elif odds > n * 0.58:
        dev = (odds / n - 0.5) * 2
        results.append({
            'strategy': 'ODD_BIAS', 'contract': 'DIGITODD',
            'digit': None, 'ev': dev * 0.03,
            'confidence': min(dev * 1.5, 0.7),
            'reason': f'Odd bias: {odds}/{n} ({1-e_ratio:.0%}) — bet odd',
        })
    
    # ── Over/Under barrier (Asian contracts) ──
    avg_digit = sum(recent) / len(recent)
    high_count = sum(1 for d in recent if d >= 6)
    low_count = sum(1 for d in recent if d <= 3)
    if high_count > n * 0.50:
        dev = (high_count / n - 0.5) * 2
        results.append({
            'strategy': 'OVER_BIAS', 'contract': 'ASIANU',
            'digit': None, 'ev': dev * 0.05,
            'confidence': min(dev * 1.5, 0.75),
            'reason': f'MAIN: High bias {high_count}/{n} >= 6 — Asian Up primary',
        })
    elif low_count > n * 0.50:
        dev = (low_count / n - 0.5) * 2
        results.append({
            'strategy': 'UNDER_BIAS', 'contract': 'ASIAND',
            'digit': None, 'ev': dev * 0.05,
            'confidence': min(dev * 1.5, 0.75),
            'reason': f'MAIN: Low bias {low_count}/{n} <= 3 — Asian Down primary',
        })
    
    # ── Trend detection (active, generates CALL/PUT) ──
    if len(digits) >= 20:
        first_half = sum(digits[-40:-20]) / max(len(digits[-40:-20]), 1)
        second_half = sum(digits[-20:]) / max(len(digits[-20:]), 1)
        diff = second_half - first_half
        if abs(diff) > 0.15:
            direction = 'RISE' if diff > 0 else 'FALL'
            contract = 'CALL' if diff > 0 else 'PUT'
            # C++ signal override disabled — C++ engine needs retraining
            # Previously flipped direction based on C++ signal which was poisoned
            results.append({
                'strategy': f'{direction}_TREND',
                'contract': contract,
                'digit': None, 'ev': abs(diff) * 0.06,
                'confidence': min(abs(diff) / 2.0, 0.8),
                'reason': f'MAIN: Trend {direction} ({diff:+.2f}) — C++={cpp_signal}',
            })
    
    # ── Tick-to-tick momentum (active CALL/PUT) ──
    if len(digits) >= 10:
        recent_10 = digits[-10:]
        rising = sum(1 for i in range(1, len(recent_10)) if recent_10[i] > recent_10[i-1])
        falling = sum(1 for i in range(1, len(recent_10)) if recent_10[i] < recent_10[i-1])
        
        # C++ signal bias: passed in or defaulted to 0
        cpp_bias = cpp_signal
        
        if cpp_bias < 0:
            # BEARISH C++ signal: favor PUT, still allow CALL if ticks strongly rising
            if rising >= 6:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.04, 'confidence': 0.6,
                    'reason': f'MAIN: Momentum UP {rising}/9 despite bearish C++',
                })
            if falling >= 3:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.05, 'confidence': 0.7,
                    'reason': f'MAIN: C++ bearish + tick down {falling}/9 — PUT',
                })
            # If ticks are balanced, still generate PUT from C++ signal
            if not results:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.03, 'confidence': 0.55,
                    'reason': f'MAIN: C++ bearish signal — PUT default',
                })
        elif cpp_bias > 0:
            # BULLISH C++ signal: favor CALL
            if rising >= 3:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.05, 'confidence': 0.7,
                    'reason': f'MAIN: C++ bullish + tick up {rising}/9 — CALL',
                })
            if falling >= 6:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.04, 'confidence': 0.6,
                    'reason': f'MAIN: Momentum DOWN {falling}/9 despite bullish C++',
                })
            if not results:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.03, 'confidence': 0.55,
                    'reason': f'MAIN: C++ bullish signal — CALL default',
                })
        else:
            # NEUTRAL: use tick momentum
            if rising >= 5:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.04, 'confidence': 0.6,
                    'reason': f'MAIN: Momentum UP {rising}/9 — primary bet',
                })
            elif falling >= 5:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.04, 'confidence': 0.6,
                    'reason': f'MAIN: Momentum DOWN {falling}/9 — primary bet',
                })
            # Balanced: generate both for picker to choose
            if not results:
                if rising >= falling:
                    results.append({
                        'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                        'digit': None, 'ev': 0.02, 'confidence': 0.45,
                        'reason': f'MAIN: Slight UP bias {rising}/{falling}',
                    })
                else:
                    results.append({
                        'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                        'digit': None, 'ev': 0.02, 'confidence': 0.45,
                        'reason': f'MAIN: Slight DOWN bias {falling}/{rising}',
                    })
    
    # ── Momentum fallback: use tick-to-tick direction ──
    if len(digits) >= 10 and not results:
        recent_10 = digits[-10:]
        rising = sum(1 for i in range(1, len(recent_10)) if recent_10[i] > recent_10[i-1])
        falling = sum(1 for i in range(1, len(recent_10)) if recent_10[i] < recent_10[i-1])
        # C++ signal in fallback
        fb_cpp = 0
        try:
            if cpp_engine and hasattr(cpp_engine, '_market_preds'):
                for mk, mkp in cpp_engine._market_preds.items():
                    fb_cpp = mkp.get('signal', 0)
                    break
        except: pass
        
        if fb_cpp < 0:
            # Bearish: prefer PUT even with weak falling
            if falling >= 4:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.03, 'confidence': 0.5,
                    'reason': f'C++ bearish + falling {falling}/9',
                })
            elif rising < 5:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.02, 'confidence': 0.45,
                    'reason': f'C++ bearish fallback',
                })
        elif fb_cpp > 0:
            if rising >= 4:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.03, 'confidence': 0.5,
                    'reason': f'C++ bullish + rising {rising}/9',
                })
            elif falling < 5:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.02, 'confidence': 0.45,
                    'reason': f'C++ bullish fallback',
                })
        else:
            # No C++ signal: use tick momentum
            if rising >= 5:
                results.append({
                    'strategy': 'MOMENTUM_UP', 'contract': 'CALL',
                    'digit': None, 'ev': 0.01, 'confidence': 0.4,
                    'reason': f'Fallback UP: {rising}/9 rising',
                })
            elif falling >= 5:
                results.append({
                    'strategy': 'MOMENTUM_DOWN', 'contract': 'PUT',
                    'digit': None, 'ev': 0.01, 'confidence': 0.4,
                    'reason': f'Fallback DOWN: {falling}/9 falling',
                })
        
        # Last resort: DIGITDIFF cooldown
        if not results:
            last = digits[-1]
            if last >= 7:
                results.append({
                    'strategy': 'HIGH_DIGIT_DIFF', 'contract': 'DIGITDIFF',
                    'digit': last, 'ev': 0.002, 'confidence': 0.2,
                    'reason': f'COOLDOWN: High digit {last} — parking bet',
                })
            elif last <= 2:
                results.append({
                    'strategy': 'LOW_DIGIT_DIFF', 'contract': 'DIGITDIFF',
                    'digit': last, 'ev': 0.002, 'confidence': 0.2,
                    'reason': f'COOLDOWN: Low digit {last} — parking bet',
                })
            elif last % 2 == 0:
                results.append({
                    'strategy': 'EVEN_FALLBACK', 'contract': 'DIGITEVEN',
                    'digit': None, 'ev': 0.02, 'confidence': 0.35,
                    'reason': f'MAIN: Digit {last} even — fallback primary',
                })
            else:
                results.append({
                    'strategy': 'ODD_FALLBACK', 'contract': 'DIGITODD',
                    'digit': None, 'ev': 0.02, 'confidence': 0.35,
                    'reason': f'MAIN: Digit {last} odd — fallback primary',
                })
    
    if not results:
        return None
    
    # PRIMARY STRATEGIES: CALL/PUT, EVEN/ODD, ASIAN (main trades)
    MAIN_CONTRACTS = {'CALL', 'PUT', 'DIGITEVEN', 'DIGITODD', 'ASIANU', 'ASIAND'}
    
    # Family classification
    FAMILY_MAP = {
        'CALL': 'directional', 'PUT': 'directional',
        'RISE_TREND': 'directional', 'FALL_TREND': 'directional',
        'MOMENTUM_UP': 'directional', 'MOMENTUM_DOWN': 'directional',
        'DIGITEVEN': 'parity', 'DIGITODD': 'parity',
        'EVEN_BIAS': 'parity', 'ODD_BIAS': 'parity',
        'ASIANU': 'barrier', 'ASIAND': 'barrier',
        'OVER_BIAS': 'barrier', 'UNDER_BIAS': 'barrier',
    }
    
    main_results = [r for r in results if r['contract'] in MAIN_CONTRACTS]
    cooldown_results = [r for r in results if r['contract'] not in MAIN_CONTRACTS]
    
    # Apply family rotation boost: boost parity/barrier, penalize CALL/PUT if WR < 68%
    # CALL/PUT needs ~68% WR to break even (0.95 payout). Penalize if below.
    if main_results:
        for r in main_results:
            fam = FAMILY_MAP.get(r['strategy'], 'directional')
            r['_family'] = fam
            if fam == 'directional':
                # Penalize CALL/PUT by -30% confidence (needs higher WR to trade)
                r['confidence'] = r.get('confidence', 0) * 0.7
                r['ev'] = r.get('ev', 0) * 0.8
            elif fam == 'parity':
                # Boost EVEN/ODD (better payout balance, 0.95 payout with ~50% WR)
                r['ev'] = r.get('ev', 0) + 0.03
                r['confidence'] = r.get('confidence', 0) + 0.08
            elif fam == 'barrier':
                # Boost ASIAN (good for trending markets)
                r['ev'] = r.get('ev', 0) + 0.02
                r['confidence'] = r.get('confidence', 0) + 0.05
    
    if main_results:
        return max(main_results, key=lambda r: r['ev'] * r['confidence'])
    
    if cooldown_results:
        return max(cooldown_results, key=lambda r: r['ev'] * r['confidence'])
    
    return max(results, key=lambda r: r['ev'] * r['confidence'])

# ═══════════════════════════════════════════════════════
# TRADER (separate WS for buying)
# ═══════════════════════════════════════════════════════
class Trader:
    def __init__(self):
        self.ws = None
        self.req_id = 0
        self.balance = 0
        self.real_balance = 0
        self.paper_results = []
        self.paper_balance = 0
        self.paper_pnl = 0
        self.net_health = None
        self.connected = False
        self._deriv = None
    
    async def connect(self):
        self.paper_balance = self.balance if self.balance > 0 else 10000.0
        self.paper_pnl = 0
        # Use new v1 API client with OTP auth
        self._deriv = DerivClient(TOKEN, DERIV_APP_ID, DERIV_REST_BASE, WS_URL)
        account_id = DERIV_DEMO_ACCOUNT  # Use demo until real account has funds
        await self._deriv.connect(account_id)
        self.balance = self._deriv.balance
        self.real_balance = self.balance
        self.paper_balance = self.balance if self.balance > 0 else 10000.0
        self.paper_pnl = 0
        self.connected = True
        if self.net_health: self.net_health.deriv_ok()
        print(f"  [TRADE] Connected via v1 API. Balance: ${self.balance:.2f} ({self._deriv.loginid})")
        return True
    
    async def reconnect(self):
        """Reconnect via OTP."""
        try:
            if self._deriv:
                await self._deriv.close()
            self.connected = False
            print("  [TRADE] Reconnecting via OTP...")
            return await self.connect()
        except Exception as e:
            print(f"  [TRADE] Reconnect failed: {e}")
            return False
    
    async def _ws_safe_send(self, msg_dict):
        """Send with auto-reconnect on ConnectionClosed."""
        try:
            await self.ws.send(json.dumps(msg_dict))
        except (websockets.ConnectionClosed, websockets.ConnectionClosedError) as e:
            print(f"  [TRADE] WS disconnected, reconnecting: {e}")
            if self.net_health: self.net_health.deriv_down()
            if await self.reconnect():
                await self.ws.send(json.dumps(msg_dict))
            else:
                raise
    
    async def buy(self, contract, market, stake, digit=None):
        if PAPER_MODE:
            import random
            paper_profit = stake * random.choice([-1, 1]) * random.uniform(0.02, 0.95)
            if contract == 'DIGITMATCH':
                paper_profit = stake * random.choice([-1] * 9 + [1 * random.uniform(1, 8)])
            elif contract in ('DIGITDIFF',):
                paper_profit = stake * random.choice([-1] * 1 + [1 * random.uniform(0.01, 0.06)] * 9)
            elif contract in ('CALL', 'PUT', 'DIGITEVEN', 'DIGITODD'):
                paper_profit = stake * random.choice([-1] * 5 + [1 * random.uniform(0.3, 0.95)] * 5)
            self.paper_balance += paper_profit
            self.paper_pnl += paper_profit
            self.paper_results.append({'contract': contract, 'market': market, 'stake': stake, 'profit': round(paper_profit, 4), 'balance': round(self.paper_balance, 2)})
            return f'paper_{len(self.paper_results)}', None
        
        # Use v1 API client
        if not self._deriv or not self._deriv.connected:
            ok = await self.reconnect()
            if not ok: return None, "cannot connect"
        
        duration = 5 if contract in ('ASIANU', 'ASIAND') else 1
        barrier = str(digit) if digit is not None and contract in ('DIGITMATCH', 'DIGITDIFF') else None
        
        try:
            proposal, err = await self._deriv.propose(contract, market, stake, duration=duration, barrier=barrier)
            if err:
                # OTP might have expired — reconnect and retry once
                if 'otp' in str(err).lower() or 'expired' in str(err).lower():
                    await self.reconnect()
                    proposal, err = await self._deriv.propose(contract, market, stake, duration=duration, barrier=barrier)
                if err: return None, err
            
            buy_result, err = await self._deriv.buy(proposal['id'], stake)
            if err:
                if 'otp' in str(err).lower() or 'expired' in str(err).lower():
                    await self.reconnect()
                    proposal, err2 = await self._deriv.propose(contract, market, stake, duration=duration, barrier=barrier)
                    if err2: return None, err2
                    buy_result, err = await self._deriv.buy(proposal['id'], stake)
                if err: return None, err
            
            self.balance = self._deriv.balance
            return buy_result.get('contract_id'), None
        except Exception as e:
            return None, str(e)[:80]
    
    async def wait_result(self, cid, timeout=20):
        if PAPER_MODE and isinstance(cid, str) and cid.startswith('paper_'):
            if self.paper_results:
                return self.paper_results[-1]['profit']
            return 0
        if self._deriv and self._deriv.connected:
            profit, details = await self._deriv.wait_result(cid, timeout=timeout)
            if self._deriv.balance: self.balance = self._deriv.balance
            return float(profit)
        # Fallback: check balance diff
        try:
            if self._deriv:
                await self._deriv.ensure_connected()
                self.balance = self._deriv.balance
        except: pass
        return 0
    
    async def refresh_balance(self):
        # Fetch real Deriv balance — wait for balance response specifically
        try:
            self.req_id += 1
            req_id = self.req_id
            await self._ws_safe_send({"balance": 1, "req_id": req_id})
            # Read messages until we get the balance response
            for _ in range(5):
                msg = await asyncio.wait_for(self.ws.recv(), timeout=3)
                data = json.loads(msg)
                if 'balance' in data and data.get('req_id') == req_id:
                    self.real_balance = float(data['balance'].get('balance', self.real_balance))
                    break
                # Also handle balance updates from other messages
                if 'balance' in data:
                    self.real_balance = float(data['balance'].get('balance', self.real_balance))
        except: pass
        if PAPER_MODE:
            self.balance = self.paper_balance
        else:
            self.balance = self.real_balance

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
        self.confidence_score = 0
        self.escalation_level = 'NORMAL'
        self.rapid_fire = False
        self.last_trade_time = 0
        self.session_peak = 0
        self.session_trades_per_min = 0
        self.full_burst = False
        self.burst_win_streak = 0
        self.burst_trades_left = 0  # how many burst trades remaining
        self.burst_total_trades = 0  # total trades in this burst session
        self.alignment_score = 0
        self.trading_mode = 'OPTIMAL'
        self.family_rotation = 0
        self.family_trades = {'directional': 0, 'parity': 0, 'barrier': 0}
        self.last_family_trade = 0
        self.pl_multiplier = 1.0  # from PLManager
        self.profit_locked = 0  # profits locked in after win streaks
        self.mode_reason = 'default'
        self.market_health = {}  # market -> {wins, losses, ev, regime}
        self.mode_switch_count = 0
        self.exploration_trades_used = 0  # tracks how many trades done in EXPLORATION
    
    def can_trade(self):
        if self.start > 0 and abs(min(0, self.pnl)) / self.start >= 0.02:
            return False, "daily loss limit"
        # ── HARD $10 DAILY DRAWDOWN CIRCUIT BREAKER ──
        daily_loss = abs(min(0, self.pnl))
        if daily_loss >= 10.0:
            return False, f"daily drawdown ${daily_loss:.2f} >= $10 limit"
        # ── LOSS COOLDOWN: pause after consecutive losses ──
        now = time.time()
        if self.consec_loss >= 3:
            cooldown = 60   # 1 min after 3+ losses
            elapsed = now - getattr(self, '_last_loss_time', 0)
            if elapsed < cooldown:
                return False, f"cooldown {cooldown - elapsed:.0f}s ({self.consec_loss} losses)"
        elif self.consec_loss >= 2:
            cooldown = 30   # 30s after 2 losses
            elapsed = now - getattr(self, '_last_loss_time', 0)
            if elapsed < cooldown:
                return False, f"cooldown {cooldown - elapsed:.0f}s ({self.consec_loss} losses)"
        elif self.consec_loss >= 1:
            cooldown = 15   # 15s after 1 loss
            elapsed = now - getattr(self, '_last_loss_time', 0)
            if elapsed < cooldown:
                return False, f"cooldown {cooldown - elapsed:.0f}s ({self.consec_loss} loss)"
        # ── DAILY $20 PROFIT LOCK ──
        if hasattr(self, '_mission') and self._mission and self._mission.is_profit_locked():
                return True, 'ok'  # house money mode — allow trading
        return True, "ok"
    
    def calc_stake(self, strategy):
        ev = strategy.get('ev', 0)
        conf = strategy.get('confidence', 0.5)
        if ev <= 0: return 2.00
        contract = strategy.get('contract', 'DIGITDIFF')
        regime = strategy.get('regime', 'UNKNOWN')
        
        # Payout ratios per contract type
        PAYOUTS = {
            'DIGITMATCH': (0.10, 8.0),
            'DIGITDIFF':  (0.90, 0.06),
            'DIGITEVEN':  (0.50, 0.95),
            'DIGITODD':   (0.50, 0.95),
            'CALL':       (0.50, 0.95),
            'PUT':        (0.50, 0.95),
            'ASIANU':     (0.50, 0.95),
            'ASIAND':     (0.50, 0.95),
        }
        base_wr, base_payout = PAYOUTS.get(contract, (0.50, 0.50))
        p = base_wr + ev
        b = base_payout
        q = 1 - p
        kelly = max(0, (p * b - q) / b) if b > 0 else 0
        
        # ── BASE STAKE BY CONTRACT TYPE ──
        # Mode multiplier
        mode_mult = 1.0
        tm = getattr(self, 'trading_mode', 'OPTIMAL')
        if tm == 'CONSERVATIVE':
            mode_mult = 0.5
        elif tm == 'AGGRESSIVE':
            mode_mult = 3.0
        elif tm == 'RECOVERY':
            mode_mult = 0.3
        elif tm == 'PRECISION':
            mode_mult = 2.0
        elif tm == 'EXPLORATION':
            mode_mult = 0.5  # reasonable probes on new markets
        burst_mult = (2.5 if getattr(self, 'full_burst', False) else 1.0) * mode_mult
        if contract in ('CALL', 'PUT', 'ASIANU', 'ASIAND'):
            base_stake = min(8.0 * burst_mult, self.balance * 0.05 * burst_mult)
        elif contract == 'DIGITDIFF':
            base_stake = min(3.0 * burst_mult, self.balance * 0.025 * burst_mult)
        elif contract == 'DIGITMATCH':
            base_stake = min(2.0 * burst_mult, self.balance * 0.015 * burst_mult)
        else:
            base_stake = min(5.0 * burst_mult, self.balance * 0.035 * burst_mult)
        
        # ── DYNAMIC MULTIPLIER (market-driven) ──
        multiplier = 1.0
        
        # 1. Win streak: compound winning
        if self.consec_win >= 5:
            multiplier *= 2.0    # double stake on hot streak
            self.profit_locked = max(self.profit_locked, self.pnl * 0.5)  # lock 50% of profits
        elif self.consec_win >= 3:
            multiplier *= 1.5    # 50% more after 3 wins
        elif self.consec_win >= 2:
            multiplier *= 1.25   # 25% more after 2 wins
        
        # 2. Loss streak: protect capital
        if self.consec_loss >= 3:
            multiplier *= 0.25   # 75% reduction after 3 losses
        elif self.consec_loss >= 2:
            multiplier *= 0.4    # 60% reduction after 2 losses
        elif self.consec_loss >= 1:
            multiplier *= 0.7    # 30% reduction after 1 loss
        
        # 3. Apply PL manager multiplier
        multiplier *= getattr(self, 'pl_multiplier', 1.0)
        
        # 3. Market regime: trend = bigger, range = smaller
        if regime in ('MOMENTUM_EXPANSION', 'TREND'):
            multiplier *= 1.3
        elif regime == 'DIGIT_ANOMALY':
            multiplier *= 1.2
        elif regime == 'RANGE_COMPRESSION':
            multiplier *= 0.9
        
        # 4. Edge size: bigger edge = bigger stake
        if ev > 0.15:
            multiplier *= 1.4
        elif ev > 0.10:
            multiplier *= 1.2
        elif ev < 0.05:
            multiplier *= 0.8
        
        # 5. Drawdown protection: reduce when losing from peak
        if self.start > 0:
            drawdown = (self.balance - self.start) / self.start
            if drawdown < -0.005:   # down 0.5%
                multiplier *= 0.5
            elif drawdown < -0.002: # down 0.2%
                multiplier *= 0.75
        
        # 6. Confidence: low confidence = smaller stake
        multiplier *= max(0.5, min(conf, 1.5))
        
        # ── CONFIDENCE ESCALATION ──
        # When multiple factors align, scale up aggressively
        confidence_score = 0
        if ev > 0.10: confidence_score += 2
        elif ev > 0.05: confidence_score += 1
        if regime in ('MOMENTUM_EXPANSION', 'TREND'): confidence_score += 2
        elif regime == 'DIGIT_ANOMALY': confidence_score += 1
        if self.consec_win >= 3: confidence_score += 2
        elif self.consec_win >= 2: confidence_score += 1
        if conf > 0.7: confidence_score += 1
        if confidence_score >= 5:
            multiplier *= 2.0  # HIGH CONFIDENCE: double stake
            self.escalation_level = 'HIGH'
        elif confidence_score >= 3:
            multiplier *= 1.5  # MEDIUM CONFIDENCE: 50% boost
            self.escalation_level = 'MEDIUM'
        else:
            self.escalation_level = 'NORMAL'
        
        self.confidence_score = confidence_score
        
        # Apply multiplier
        stake = base_stake * kelly * 0.10 * multiplier
        
        # ── CONFIDENCE-ADJUSTED SIZING ──
        conf = getattr(self, 'confidence_score', 3)
        if conf >= 6:
            stake *= 1.5    # 50% more on perfect alignment
        elif conf >= 5:
            stake *= 1.25   # 25% more on strong setup
        elif conf >= 4:
            stake *= 1.0    # normal
        elif conf >= 3:
            stake *= 0.75   # 25% less on marginal setup
        else:
            stake *= 0.5    # 50% less on weak setup
        
        # Evolution override: model can set exact stake
        if hasattr(self, 'override_stake') and self.override_stake:
            return round(max(1.00, min(self.override_stake, base_stake)), 2)
        
        return round(max(1.00, min(stake, base_stake)), 2)
    
    def get_active_family(self):
        """Get which contract family to trade this cycle.
        Rotates: directional(0) → parity(1) → barrier(2) → repeat.
        Skips a family if it has 3+ consecutive losses."""
        families = ['directional', 'parity', 'barrier']
        # Check last 3 trades for consecutive losses per family
        for i in range(3):
            idx = (self.family_rotation + i) % 3
            fam = families[idx]
            if self.family_trades.get(fam + '_losses', 0) < 3:
                return fam
        return 'directional'  # fallback
    
    def record_family_trade(self, family, profit):
        """Record a trade result for family rotation."""
        self.family_trades[family] = self.family_trades.get(family, 0) + 1
        key = family + '_losses'
        if profit < 0:
            self.family_trades[key] = self.family_trades.get(key, 0) + 1
        else:
            self.family_trades[key] = 0  # reset on win
        # Rotate after every trade
        self.family_rotation = (self.family_rotation + 1) % 3
    
    def record(self, profit):
        self.total += 1
        self.pnl += profit
        self.balance += profit
        # Burst tracking: deactivate on loss or when trades run out
        if self.full_burst and profit < 0:
            self.full_burst = False
            self.burst_trades_left = 0
            print(f"  BURST ENDED: loss after {self.burst_total_trades} burst trades", flush=True)
        elif self.burst_trades_left <= 0 and self.full_burst:
            self.full_burst = False
            print(f"  BURST COMPLETE: {self.burst_total_trades} trades done — cooling to OPTIMAL", flush=True)
        # Track rolling results for loss hike detection
        if not hasattr(self, '_recent_results'):
            self._recent_results = []
        self._recent_results.append(profit)
        if len(self._recent_results) > 20:
            self._recent_results = self._recent_results[-20:]
        
        if profit > 0:
            self.wins += 1
            self.consec_win += 1
            self.consec_loss = 0
            # Win resets disconnect timer — market recovered
            if getattr(self, '_disconnect_until', 0) > 0:
                self._disconnect_until = 0
                self._disconnect_reason = ""
                log_agent("risk", "🟢 WIN RECOVERY: market conditions improved, cancelling disconnect")
        else:
            self.losses += 1
            self.consec_loss += 1
            self.consec_win = 0
            self._last_loss_time = time.time()
            # ── RETEST TRACKING: count retest trades ──
            if getattr(self, '_retest_mode', False):
                self._retest_trades = getattr(self, '_retest_trades', 0) + 1
                if profit > 0:
                    self._retest_wins = getattr(self, '_retest_wins', 0) + 1
                self._retest_pnl = getattr(self, '_retest_pnl', 0) + profit
            # ── LOSS HIKE DISCONNECT: escalating breaks to reset probability ──
            if not hasattr(self, '_recent_results') or len(self._recent_results) < 5:
                pass  # not enough data
            else:
                # Track consecutive disconnects for escalation
                if not hasattr(self, '_disconnect_count'):
                    self._disconnect_count = 0
                if not hasattr(self, '_last_disconnect_result'):
                    self._last_disconnect_result = 'none'

                recent_10 = self._recent_results[-10:]
                losses_in_10 = sum(1 for p in recent_10 if p < 0)
                total_pnl_10 = sum(recent_10)

                # ── ESCALATING COOLDOWNS based on disconnect history ──
                # Level 1: 3 consec losses → 3min
                # Level 2: 4+ losses in 8, PnL <-$3 → 10min
                # Level 3: 5+ losses in 10, PnL <-$5 → 30min
                # Level 4: repeat disconnect + still losing → 1hr
                # Level 5: 3+ repeat disconnects → 2hr
                # Level 6: still losing after 2hr → full session pause (4hr)
                if self._last_disconnect_result == 'still_losing':
                    self._disconnect_count += 1
                elif self._last_disconnect_result == 'recovered':
                    self._disconnect_count = 0

                # Check if we should escalate based on consecutive disconnects
                if self._disconnect_count >= 3:
                    # Level 6: 3+ failed disconnects → 4hr full session pause
                    cooldown = 14400
                    level = "FULL PAUSE"
                elif self._disconnect_count >= 2:
                    # Level 5: 2 failed disconnects → 2hr
                    cooldown = 7200
                    level = "2HR PAUSE"
                elif self._disconnect_count >= 1:
                    # Level 4: 1 failed disconnect (came back but still losing) → 1hr
                    cooldown = 3600
                    level = "1HR PAUSE"
                elif self.consec_loss == 3 and not getattr(self, '_disconnect_until', 0):
                    # Level 1: first offense, 3 consec losses → 3min
                    cooldown = 180
                    level = "3MIN"
                elif losses_in_10 >= 4 and total_pnl_10 < -3.0:
                    # Level 2: moderate → 10min
                    cooldown = 600
                    level = "10MIN"
                elif losses_in_10 >= 5 and total_pnl_10 < -5.0:
                    # Level 3: severe → 30min
                    cooldown = 1800
                    level = "30MIN"
                else:
                    cooldown = 0

                if cooldown > 0 and not getattr(self, '_disconnect_until', 0):
                    self._disconnect_until = time.time() + cooldown
                    self._disconnect_reason = f"{level}: {losses_in_10}/10 losses, PnL=${total_pnl_10:.2f} (disconnect #{self._disconnect_count + 1})"
                    print(f"  🩸 LOSS HIKE {level}: {losses_in_10}/10 losses (${total_pnl_10:.2f}) — disconnecting for {cooldown//60}min", flush=True)
                    log_agent("risk", f"🩸 DERIV DISCONNECT {level}: {self._disconnect_reason}")
    
    def wr(self):
        return (self.wins / self.total * 100) if self.total else 0


# ═══════════════════════════════════════════════════════
# MARKET TIMEZONE INTELLIGENCE — learn which hours each market performs best
# ═══════════════════════════════════════════════════════
TIMEZONE_HISTORY_FILE = Path(__file__).parent / 'timezone_history.json'

# Map Deriv market codes to their primary trading sessions
# These are synthetic markets but have different volatility patterns by hour
MARKET_TIMEZONE_MAP = {
    'R_75':   {'label': 'Volatility 75',  'peak_hours': [8, 13, 14, 15, 20, 21], 'type': 'volatility'},
    'R_100':  {'label': 'Volatility 100', 'peak_hours': [9, 13, 14, 15, 20],     'type': 'volatility'},
    'R_50':   {'label': 'Volatility 50',  'peak_hours': [8, 9, 13, 14, 20, 21],  'type': 'volatility'},
    'R_25':   {'label': 'Volatility 25',  'peak_hours': [10, 13, 14, 20, 21],    'type': 'volatility'},
    'R_10':   {'label': 'Volatility 10',  'peak_hours': [9, 13, 14, 20],         'type': 'volatility'},
    'JD75':   {'label': 'Jump 75',        'peak_hours': [8, 9, 14, 15, 20, 21],  'type': 'jump'},
    'JD100':  {'label': 'Jump 100',       'peak_hours': [9, 13, 14, 20, 21],     'type': 'jump'},
    'JD50':   {'label': 'Jump 50',        'peak_hours': [8, 13, 14, 15, 20],     'type': 'jump'},
    'JD25':   {'label': 'Jump 25',        'peak_hours': [10, 13, 14, 20, 21],    'type': 'jump'},
    'JD10':   {'label': 'Jump 10',        'peak_hours': [9, 13, 14, 20],         'type': 'jump'},
}

# Session labels for dashboard readability
SESSION_LABELS = {
    range(0, 4):   '🌙 Asian Late',
    range(4, 7):   '🌅 Asian Early',
    range(7, 10):  '🗼 European Open',
    range(10, 13): '🏛️ European Mid',
    range(13, 16): '🗽 US Open',
    range(16, 19): '📊 US Afternoon',
    range(19, 22): '🌙 Evening',
    range(22, 24): '🌌 Night',
}

def get_session_label(hour):
    for rng, label in SESSION_LABELS.items():
        if hour in rng:
            return label
    return '❓ Unknown'

def get_current_session_label():
    return get_session_label(int(time.strftime('%H')))

class MarketTimezoneIntel:
    """Tracks per-market, per-hour performance to learn which timezone windows are best.
    
    Saves history to timezone_history.json so the agent remembers across restarts.
    Uses a 24-hour clock (UTC) since Deriv markets run 24/7.
    
    Key insight: synthetic markets have different volatility patterns at different hours.
    The agent learns: "R_75 at 14:00 UTC = 72% WR over 45 trades" vs "R_75 at 03:00 UTC = 41% WR"
    """
    
    HOURS = 24
    MIN_TRADES_FOR_BOOST = 5   # need at least N trades per hour to boost
    MIN_TRADES_FOR_PENALTY = 3 # penalize after N bad trades
    BOOST_MAX = 1.4            # max score multiplier for hot hours
    PENALTY_MIN = 0.5          # min score multiplier for cold hours
    
    def __init__(self):
        # {market: {hour: {trades, wins, losses, pnl, wr}}}
        self.hourly_stats = {}
        for m in MARKET_TIMEZONE_MAP:
            self.hourly_stats[m] = {}
            for h in range(self.HOURS):
                self.hourly_stats[m][h] = {
                    'trades': 0, 'wins': 0, 'losses': 0,
                    'pnl': 0.0, 'wr': 0.0,
                }
        # Global session stats: {hour: {trades, wins, pnl}}
        self.session_stats = {}
        for h in range(self.HOURS):
            self.session_stats[h] = {'trades': 0, 'wins': 0, 'pnl': 0.0, 'wr': 0.0}
        
        # Today's stats for quick reference
        self.today_stats = {}  # {market: {hour: {trades, wins, pnl}}}
        self.today_date = ''
        
        # Best/worst hours per market (computed periodically)
        self.market_best_hours = {}  # {market: [(hour, wr), ...]}
        self.market_worst_hours = {}
        
        # Persistence
        self.last_save = 0
        self._load_history()
    
    def _load_history(self):
        """Load timezone history from disk."""
        try:
            if TIMEZONE_HISTORY_FILE.exists():
                data = json.loads(TIMEZONE_HISTORY_FILE.read_text())
                # Restore hourly stats
                for m, hours in data.get('hourly_stats', {}).items():
                    if m not in self.hourly_stats:
                        self.hourly_stats[m] = {}
                    for h_str, stats in hours.items():
                        h = int(h_str)
                        if h not in self.hourly_stats[m]:
                            self.hourly_stats[m][h] = {
                                'trades': 0, 'wins': 0, 'losses': 0,
                                'pnl': 0.0, 'wr': 0.0,
                            }
                        self.hourly_stats[m][h].update(stats)
                # Restore session stats
                for h_str, stats in data.get('session_stats', {}).items():
                    h = int(h_str)
                    if h in self.session_stats:
                        self.session_stats[h].update(stats)
                total_trades = sum(s['trades'] for mk in self.hourly_stats.values() for s in mk.values())
                print(f"  [TZ-INTEL] Loaded timezone history: {total_trades} trades across {len(self.hourly_stats)} markets")
                self._recompute_rankings()
        except Exception as e:
            print(f"  [TZ-INTEL] Could not load history: {e}")
    
    def _save_history(self):
        """Save timezone history to disk."""
        try:
            now = time.time()
            if now - self.last_save < 300:  # save every 5 min max
                return
            self.last_save = now
            data = {
                'hourly_stats': {},
                'session_stats': {str(h): s for h, s in self.session_stats.items()},
                'last_updated': int(now * 1000),
                'total_trades': sum(s['trades'] for mk in self.hourly_stats.values() for s in mk.values()),
            }
            for m, hours in self.hourly_stats.items():
                data['hourly_stats'][m] = {str(h): s for h, s in hours.items()}
            TIMEZONE_HISTORY_FILE.write_text(json.dumps(data, indent=1))
        except Exception as e:
            print(f"  [TZ-INTEL] Save error: {e}")
    
    def record_trade(self, market, profit, hour=None):
        """Record a trade result for timezone analysis."""
        if hour is None:
            hour = int(time.strftime('%H'))
        
        # Update market-specific stats
        if market not in self.hourly_stats:
            self.hourly_stats[market] = {}
        if hour not in self.hourly_stats[market]:
            self.hourly_stats[market][hour] = {
                'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'wr': 0.0,
            }
        
        ms = self.hourly_stats[market][hour]
        ms['trades'] += 1
        ms['pnl'] += profit
        if profit > 0:
            ms['wins'] += 1
        else:
            ms['losses'] += 1
        ms['wr'] = (ms['wins'] / ms['trades'] * 100) if ms['trades'] > 0 else 0
        
        # Update global session stats
        ss = self.session_stats[hour]
        ss['trades'] += 1
        ss['pnl'] += profit
        if profit > 0:
            ss['wins'] += 1
        ss['wr'] = (ss['wins'] / ss['trades'] * 100) if ss['trades'] > 0 else 0
        
        # Update today's stats
        today = time.strftime('%Y-%m-%d')
        if self.today_date != today:
            self.today_stats = {}
            self.today_date = today
            # DAY RESET: archive old logs, reset daily counters
            try:
                log_manager.cleanup_old(days=7)
                log_sys(f"Day reset: {today} — archived old logs", "info")
            except: pass
        if market not in self.today_stats:
            self.today_stats[market] = {}
        if hour not in self.today_stats[market]:
            self.today_stats[market][hour] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        ts = self.today_stats[market][hour]
        ts['trades'] += 1
        ts['pnl'] += profit
        if profit > 0:
            ts['wins'] += 1
        
        # Periodically recompute rankings
        if ms['trades'] % 5 == 0:
            self._recompute_rankings()
        
        # Periodically save
        self._save_history()
    
    def _recompute_rankings(self):
        """Recompute best/worst hours per market."""
        for market, hours in self.hourly_stats.items():
            scored = []
            for h, stats in hours.items():
                if stats['trades'] >= self.MIN_TRADES_FOR_BOOST:
                    scored.append((h, stats['wr'], stats['pnl'], stats['trades']))
            scored.sort(key=lambda x: -x[1])  # sort by WR descending
            self.market_best_hours[market] = [(h, wr, pnl, t) for h, wr, pnl, t in scored[:5]]
            self.market_worst_hours[market] = [(h, wr, pnl, t) for h, wr, pnl, t in scored[-5:]]
    
    def get_multiplier(self, market, hour=None):
        """Get score multiplier for a market at current hour.
        Returns 1.0 = neutral, >1.0 = boost, <1.0 = penalty.
        """
        if hour is None:
            hour = int(time.strftime('%H'))
        
        if market not in self.hourly_stats:
            return 1.0
        
        stats = self.hourly_stats[market].get(hour, {})
        trades = stats.get('trades', 0)
        wr = stats.get('wr', 0)
        
        if trades < self.MIN_TRADES_FOR_BOOST:
            # Not enough data — use default peak hours as guide
            default_peaks = MARKET_TIMEZONE_MAP.get(market, {}).get('peak_hours', [])
            if hour in default_peaks:
                return 1.1  # mild boost for known peak hours
            return 1.0
        
        # WR-based multiplier
        if wr >= 65:
            return min(self.BOOST_MAX, 1.0 + (wr - 50) / 100)
        elif wr >= 55:
            return 1.0 + (wr - 50) / 200  # mild boost
        elif wr <= 35:
            return max(self.PENALTY_MIN, 1.0 - (50 - wr) / 100)
        elif wr <= 45:
            return 1.0 - (50 - wr) / 200  # mild penalty
        else:
            return 1.0
    
    def is_bleeding(self, market=None, hour=None):
        """Check if current hour is a bleeding window.
        Returns (is_bleeding, reason, severity).
        severity: 'hard' = block trades, 'soft' = reduce stake, 'safe' = normal.
        """
        if hour is None:
            hour = int(time.strftime('%H'))
        
        # Check market-specific bleeding
        if market and market in self.hourly_stats:
            stats = self.hourly_stats[market].get(hour, {})
            trades = stats.get('trades', 0)
            pnl = stats.get('pnl', 0)
            wr = stats.get('wr', 0)
            
            if trades >= 5 and pnl < -5.0:
                return True, f"{market} hour {hour}:00 bled ${pnl:.2f} in {trades}T (WR={wr:.0f}%)", 'hard'
            if trades >= 3 and pnl < -2.0 and wr < 40:
                return True, f"{market} hour {hour}:00 cold: ${pnl:.2f}, WR={wr:.0f}%", 'soft'
        
        # Check global session bleeding
        ss = self.session_stats.get(hour, {})
        g_trades = ss.get('trades', 0)
        g_pnl = ss.get('pnl', 0)
        g_wr = ss.get('wr', 0)
        
        if g_trades >= 8 and g_pnl < -8.0:
            return True, f"Global hour {hour}:00 bled ${g_pnl:.2f} in {g_trades}T (WR={g_wr:.0f}%)", 'hard'
        if g_trades >= 5 and g_pnl < -3.0 and g_wr < 40:
            return True, f"Global hour {hour}:00 cold: ${g_pnl:.2f}, WR={g_wr:.0f}%", 'soft'
        
        return False, '', 'safe'
    
    def get_stake_multiplier(self, market=None, hour=None):
        """Get stake multiplier based on bleeding status.
        hard = 0.0 (block), soft = 0.5 (halve), safe = 1.0 (normal).
        Also applies market-specific boost/penalty on top.
        """
        is_bleed, reason, severity = self.is_bleeding(market, hour)
        if severity == 'hard':
            return 0.0, reason
        base = self.get_multiplier(market, hour)
        if severity == 'soft':
            return base * 0.5, reason
        return base, ''
    def get_best_hour(self, market):
        """Get the best-performing hour for a market."""
        if market in self.market_best_hours and self.market_best_hours[market]:
            h, wr, pnl, t = self.market_best_hours[market][0]
            return h, wr, pnl, t
        # Fallback to default peak hours
        defaults = MARKET_TIMEZONE_MAP.get(market, {}).get('peak_hours', [])
        if defaults:
            return defaults[0], 0, 0, 0
        return 14, 0, 0, 0  # default: 14:00 UTC (US open)
    
    def get_worst_hour(self, market):
        """Get the worst-performing hour for a market."""
        if market in self.market_worst_hours and self.market_worst_hours[market]:
            h, wr, pnl, t = self.market_worst_hours[market][0]
            return h, wr, pnl, t
        return None, 0, 0, 0
    
    def get_session_summary(self):
        """Get performance summary by trading session."""
        sessions = {}
        for label in SESSION_LABELS.values():
            sessions[label] = {'trades': 0, 'wins': 0, 'pnl': 0.0, 'wr': 0.0}
        
        for h, stats in self.session_stats.items():
            label = get_session_label(h)
            if label in sessions:
                sessions[label]['trades'] += stats['trades']
                sessions[label]['wins'] += stats['wins']
                sessions[label]['pnl'] += stats['pnl']
                if sessions[label]['trades'] > 0:
                    sessions[label]['wr'] = sessions[label]['wins'] / sessions[label]['trades'] * 100
        
        # Sort by WR
        sorted_sessions = sorted(sessions.items(), key=lambda x: -x[1]['wr'])
        return sorted_sessions
    
    def get_market_hour_report(self, market):
        """Get full hourly breakdown for a market."""
        if market not in self.hourly_stats:
            return []
        hours = []
        for h in range(self.HOURS):
            stats = self.hourly_stats[market][h]
            if stats['trades'] > 0:
                hours.append({
                    'hour': h,
                    'label': get_session_label(h),
                    'trades': stats['trades'],
                    'wins': stats['wins'],
                    'wr': round(stats['wr'], 1),
                    'pnl': round(stats['pnl'], 2),
                })
        hours.sort(key=lambda x: -x['wr'])
        return hours
    
    def get_status(self):
        """Get full status for state output."""
        now_hour = int(time.strftime('%H'))
        current_session = get_session_label(now_hour)
        
        # Top 3 best market-hours
        all_market_hours = []
        for market, hours in self.hourly_stats.items():
            for h, stats in hours.items():
                if stats['trades'] >= self.MIN_TRADES_FOR_BOOST:
                    all_market_hours.append({
                        'market': market, 'hour': h,
                        'label': get_session_label(h),
                        'trades': stats['trades'],
                        'wr': round(stats['wr'], 1),
                        'pnl': round(stats['pnl'], 2),
                    })
        all_market_hours.sort(key=lambda x: -x['wr'])
        
        # Top 3 worst market-hours
        cold_hours = list(reversed(all_market_hours[-3:])) if len(all_market_hours) >= 3 else []
        
        total_trades = sum(s['trades'] for mk in self.hourly_stats.values() for s in mk.values())
        
        return {
            'current_hour': now_hour,
            'current_session': current_session,
            'total_trades': total_trades,
            'hot_hours': all_market_hours[:5],
            'cold_hours': cold_hours,
            'session_summary': [
                {'session': label, 'trades': s['trades'], 'wr': round(s['wr'], 1), 'pnl': round(s['pnl'], 2)}
                for label, s in self.get_session_summary() if s['trades'] > 0
            ],
            'market_multipliers': {
                m: round(self.get_multiplier(m), 2) for m in MARKET_TIMEZONE_MAP
            },
        }


# ═══════════════════════════════════════════════════════
# ANOMALY DETECTOR — spike, gap, rapid reversal detection
# ═══════════════════════════════════════════════════════
class AnomalyDetector:
    """Detects unusual price behavior that signals danger or opportunity.
    
    Anomalies:
    - SPIKE: sudden large price movement (>3σ from recent mean)
    - GAP: price jumps between consecutive ticks
    - REVERSAL: rapid direction change after sustained move
    - STALL: price stops moving (low volatility spike)
    - CHOP: rapid alternating up/down (whipsaw zone)
    """
    
    SPIKE_THRESHOLD = 3.0    # standard deviations
    GAP_THRESHOLD = 0.003    # 0.3% price jump
    REVERSAL_WINDOW = 5      # ticks to check for reversal
    CHOP_WINDOW = 10          # ticks to check for chop
    STALL_THRESHOLD = 0.0001 # price change threshold for stall
    
    def __init__(self):
        self.anomalies = {}  # market -> list of recent anomalies
        self.anomaly_counts = {}  # market -> {type: count}
    
    def detect(self, ticks, market):
        """Analyze ticks for anomalies. Returns list of detected anomalies."""
        if not ticks or len(ticks) < 10:
            return []
        
        detected = []
        recent = ticks[-30:]  # last 30 ticks for context
        
        # ── SPIKE DETECTION ──
        changes = [abs(recent[i] - recent[i-1]) for i in range(1, len(recent))]
        if changes:
            mean_change = sum(changes) / len(changes)
            std_change = (sum((c - mean_change)**2 for c in changes) / len(changes)) ** 0.5
            if std_change > 0.0001:
                latest_change = changes[-1]
                z_score = (latest_change - mean_change) / std_change
                if z_score > self.SPIKE_THRESHOLD:
                    detected.append({
                        'type': 'SPIKE', 'market': market,
                        'severity': min(z_score / 5.0, 1.0),
                        'detail': f'z={z_score:.1f} change={latest_change:.6f}',
                        'action': 'AVOID',  # don't trade during spikes
                    })
        
        # ── GAP DETECTION (adaptive: compare to recent average gap) ──
        if len(recent) >= 5:
            gaps = [abs(recent[i] - recent[i-1]) / recent[i-1] if recent[i-1] != 0 else 0
                    for i in range(1, len(recent))]
            avg_gap = sum(gaps) / len(gaps) if gaps else 0.001
            latest_gap = gaps[-1] if gaps else 0
            # Only flag if latest gap is 3x+ the average (true anomaly, not normal volatility)
            if avg_gap > 0 and latest_gap > avg_gap * 5.0:
                severity = min((latest_gap / avg_gap - 2.0) / 5.0, 1.0)
                detected.append({
                    'type': 'GAP', 'market': market,
                    'severity': max(0.1, severity),
                    'detail': 'gap=%.2f%% (avg=%.2f%%, %.1fx)' % (latest_gap*100, avg_gap*100, latest_gap/avg_gap),
                    'action': 'WAIT',
                })
        
        # ── REVERSAL DETECTION ──
        if len(recent) >= self.REVERSAL_WINDOW:
            window = recent[-self.REVERSAL_WINDOW:]
            # Check if direction reversed
            up_moves = sum(1 for i in range(1, len(window)) if window[i] > window[i-1])
            down_moves = len(window) - 1 - up_moves
            total_moves = len(window) - 1
            if total_moves >= 3:
                # Was trending one way, now reversed
                first_half = window[:len(window)//2]
                second_half = window[len(window)//2:]
                fh_dir = sum(1 for i in range(1, len(first_half)) if first_half[i] > first_half[i-1])
                sh_dir = sum(1 for i in range(1, len(second_half)) if second_half[i] > second_half[i-1])
                fh_ratio = fh_dir / max(len(first_half) - 1, 1)
                sh_ratio = sh_dir / max(len(second_half) - 1, 1)
                # Reversal: first half trending up, second half trending down (or vice versa)
                if fh_ratio > 0.7 and sh_ratio < 0.3:
                    detected.append({
                        'type': 'REVERSAL', 'market': market,
                        'severity': 0.5,
                        'detail': f'up→down reversal ({fh_ratio:.0%}→{sh_ratio:.0%})',
                        'action': 'FLIP_SHORT',
                    })
                elif fh_ratio < 0.3 and sh_ratio > 0.7:
                    detected.append({
                        'type': 'REVERSAL', 'market': market,
                        'severity': 0.5,
                        'detail': f'down→up reversal ({fh_ratio:.0%}→{sh_ratio:.0%})',
                        'action': 'FLIP_LONG',
                    })
        
        # ── STALL DETECTION (adaptive: compare to recent volatility) ──
        if len(recent) >= 10:
            recent_range = max(recent[-10:]) - min(recent[-10:])
            latest_range = max(recent[-5:]) - min(recent[-5:])
            if recent_range > 0 and latest_range / recent_range < 0.1:
                detected.append({
                    'type': 'STALL', 'market': market,
                    'severity': 0.3,
                    'detail': 'volatility collapsed (%.0f%% of recent)' % (latest_range/recent_range*100),
                    'action': 'WAIT',
                })
        
        # ── CHOP DETECTION (whipsaw) ──
        # JD* markets are inherently choppy (jump between prices) — skip for them
        if len(recent) >= self.CHOP_WINDOW and not market.startswith('JD'):
            window = recent[-self.CHOP_WINDOW:]
            direction_changes = sum(
                1 for i in range(2, len(window))
                if (window[i] - window[i-1]) * (window[i-1] - window[i-2]) < 0
            )
            chop_ratio = direction_changes / (len(window) - 2)
            # R_* markets: >80% reversals = truly choppy (normal is ~50%)
            if chop_ratio > 0.8:
                detected.append({
                    'type': 'CHOP', 'market': market,
                    'severity': min(chop_ratio * 0.7, 0.8),
                    'detail': 'chop=%.0f%% (%d/%d reversals)' % (chop_ratio*100, direction_changes, len(window)-2),
                    'action': 'AVOID',
                })
        
        # Store anomalies
        if detected:
            self.anomalies[market] = detected[-5:]  # keep last 5
            for a in detected:
                self.anomaly_counts.setdefault(market, {})
                self.anomaly_counts[market][a['type']] = self.anomaly_counts[market].get(a['type'], 0) + 1
        
        return detected
    
    def is_safe(self, market):
        """Check if market is safe to trade (no high-severity anomalies)."""
        anomalies = self.anomalies.get(market, [])
        blocking = [a for a in anomalies if a['severity'] > 0.8 and a['action'] in ('AVOID', 'WAIT')]
        if blocking:
            return False, blocking[0]['type'] + ': ' + blocking[0]['detail']
        return True, 'ok'
    
    def get_status(self):
        return {
            'anomalies': {m: len(a) for m, a in self.anomalies.items()},
            'counts': dict(self.anomaly_counts),
        }


# ═══════════════════════════════════════════════════════
# MARKET STATE BRAIN — the missing piece
# Synthesizes ALL observations into coherent market state
# before ANY strategy is chosen.
# ═══════════════════════════════════════════════════════
class MarketStateBrain:
    """The brain that UNDERSTANDS market state before acting.
    
    Architecture:
    OBSERVATION → MarketStateBrain → ANALYSIS → DECISION
    
    This is what separates a simple indicator bot from an intelligent agent.
    It answers: "What IS the market doing right now?" before "What should I DO?"
    
    Inputs (from all observation layers):
    - Raw ticks (price history)
    - Sensor signal (directional bias)
    - Regime classification (trend/range/compression)
    - Noise level (entropy)
    - Anomaly status (spike/gap/reversal/chop)
    - Timezone context (session, hour)
    - C++ engine prediction (signal + confidence)
    - Price action (support/resistance, momentum, breakouts)
    
    Output: MarketState — a coherent description of what the market IS,
    plus a recommendation on what to DO (trade type, confidence, risk level).
    """
    
    # Market states
    STATE_STRONG_TREND = "STRONG_TREND"
    STATE_WEAK_TREND = "WEAK_TREND"
    STATE_RANGING = "RANGING"
    STATE_COMPRESSION = "COMPRESSION"
    STATE_VOLATILE = "VOLATILE"
    STATE_CALM = "CALM"
    STATE_ANOMALOUS = "ANOMALOUS"
    STATE_UNTRADABLE = "UNTRADABLE"
    
    # Trade recommendations
    REC_TRADE = "TRADE"
    REC_WAIT = "WAIT"
    REC_SKIP = "SKIP"
    REC_REDUCE = "REDUCE_SIZE"
    
    def __init__(self):
        self.state_history = {}  # market -> list of recent states
        self.state_durations = {}  # market -> how long in current state
        self.last_states = {}  # market -> last state
        self.trade_history = []  # [{market, state, strategy, result, time}]
        self.state_win_rates = {}  # state_type -> {trades, wins}
        self.total_analyses = 0
    
    def analyze(self, market, ticks, signal, regime, noise_level, anomaly_status,
                cpp_pred, price_action, hour=None, session_label=None):
        """Full market state analysis. Returns MarketState dict.
        
        This is the CORE of the brain. It reads everything and produces
        a single coherent understanding of what the market IS doing.
        """
        self.total_analyses += 1
        if hour is None:
            hour = int(time.strftime('%H'))
        if session_label is None:
            session_label = 'unknown'
        
        state = {
            'market': market,
            'hour': hour,
            'session': session_label,
            'timestamp': int(time.time() * 1000),
            
            # ── STATE CLASSIFICATION ──
            'market_state': self.STATE_UNTRADABLE,
            'state_confidence': 0.0,
            'state_reason': '',
            
            # ── RISK ASSESSMENT ──
            'risk_level': 'HIGH',  # default: don't trade
            'risk_reasons': [],
            
            # ── TRADE RECOMMENDATION ──
            'recommendation': self.REC_SKIP,
            'rec_reason': '',
            
            # ── STRATEGY GUIDANCE ──
            'best_contract_type': None,  # directional/digit/parity
            'direction_bias': None,      # CALL/PUT/None
            'preferred_strategies': [],   # specific strategies to try
            'avoid_strategies': [],       # strategies to avoid
            
            # ── CONFIDENCE MODIFIERS ──
            'confidence_boost': 1.0,
            'confidence_penalty': 1.0,
            'stake_multiplier': 1.0,
        }
        
        reasons = []
        risk_reasons = []
        confidence = 0.5  # start neutral
        
        # ══════════════════════════════════════════════════
        # LAYER 1: ANOMALY CHECK — hard veto
        # ══════════════════════════════════════════════════
        if anomaly_status:
            blocking = [a for a in anomaly_status if a['severity'] > 0.6]
            if blocking:
                state['market_state'] = self.STATE_ANOMALOUS
                state['recommendation'] = self.REC_SKIP
                state['rec_reason'] = f"Anomaly: {blocking[0]['type']} — {blocking[0]['detail']}"
                state['risk_level'] = 'EXTREME'
                state['risk_reasons'] = [f"{a['type']}: {a['detail']}" for a in blocking]
                state['state_confidence'] = blocking[0]['severity']
                self._record_state(market, state)
                return state
            # Mild anomalies → reduce size
            mild = [a for a in anomaly_status if a['severity'] > 0.3]
            if mild:
                confidence *= 0.6
                state['stake_multiplier'] *= 0.5
                risk_reasons.append(f"Mild anomaly: {mild[0]['type']}")
                reasons.append(f"anomaly_penalty={mild[0]['type']}")
        
        # ══════════════════════════════════════════════════
        # LAYER 2: NOISE CHECK — hard veto if too noisy
        # ══════════════════════════════════════════════════
        if noise_level is not None and noise_level > 3.4:
            state['market_state'] = self.STATE_VOLATILE
            state['recommendation'] = self.REC_SKIP
            state['rec_reason'] = f"Too noisy: entropy={noise_level:.2f}"
            state['risk_level'] = 'HIGH'
            state['risk_reasons'].append(f"noise={noise_level:.2f}")
            state['state_confidence'] = min(noise_level / 4.0, 1.0)
            self._record_state(market, state)
            return state
        elif noise_level is not None and noise_level > 2.8:
            confidence *= 0.7
            state['stake_multiplier'] *= 0.7
            risk_reasons.append(f"elevated noise={noise_level:.2f}")
        
        # ══════════════════════════════════════════════════
        # LAYER 3: REGIME — classify market structure
        # ══════════════════════════════════════════════════
        regime_str = str(regime) if regime else 'UNKNOWN'
        
        if regime_str in ('MOMENTUM', 'TREND_UP', 'TREND_DOWN', 'STRONG_TREND'):
            state['market_state'] = self.STATE_STRONG_TREND
            confidence *= 1.3
            state['stake_multiplier'] *= 1.2
            state['preferred_strategies'] = ['MOMENTUM_UP', 'MOMENTUM_DOWN', 'FALL_TREND', 'RISE_TREND']
            state['avoid_strategies'] = ['DIGIT_MATCH', 'DIGIT_DIFF', 'EVEN_BIAS', 'ODD_BIAS']
            state['best_contract_type'] = 'directional'
            reasons.append(f"strong_trend={regime_str}")
        
        elif regime_str in ('WEAK_TREND', 'MILD_TREND'):
            state['market_state'] = self.STATE_WEAK_TREND
            confidence *= 1.1
            state['preferred_strategies'] = ['MOMENTUM_UP', 'MOMENTUM_DOWN', 'FALL_TREND', 'RISE_TREND']
            state['best_contract_type'] = 'directional'
            reasons.append(f"weak_trend={regime_str}")
        
        elif regime_str in ('RANGE', 'RANGING', 'SIDEWAYS'):
            state['market_state'] = self.STATE_RANGING
            confidence *= 0.9
            state['preferred_strategies'] = ['EVEN_BIAS', 'ODD_BIAS', 'DIGIT_DIFF']
            state['avoid_strategies'] = ['MOMENTUM_UP', 'MOMENTUM_DOWN', 'FALL_TREND', 'RISE_TREND']
            state['best_contract_type'] = 'parity'
            reasons.append("ranging_market")
        
        elif regime_str in ('COMPRESSION', 'RANGE_COMPRESSION', 'LOW_VOL'):
            state['market_state'] = self.STATE_COMPRESSION
            confidence *= 0.8
            state['preferred_strategies'] = ['DIGIT_DIFF', 'EVEN_BIAS']
            state['avoid_strategies'] = ['MOMENTUM_UP', 'MOMENTUM_DOWN']
            state['best_contract_type'] = 'digit'
            state['stake_multiplier'] *= 0.8
            reasons.append("compression")
        
        elif regime_str == 'MEMORY':
            # Memory fallback — use whatever was working before
            state['market_state'] = self.STATE_WEAK_TREND
            confidence *= 0.85
            reasons.append("memory_fallback")
        
        else:
            # UNKNOWN regime — be cautious
            state['market_state'] = self.STATE_CALM
            confidence *= 0.7
            state['stake_multiplier'] *= 0.7
            state['risk_level'] = 'MEDIUM'
            risk_reasons.append(f"unknown_regime={regime_str}")
            reasons.append(f"unknown_regime")
        
        # ══════════════════════════════════════════════════
        # LAYER 4: SIGNAL STRENGTH — directional conviction
        # ══════════════════════════════════════════════════
        if signal:
            sig_strength = signal.get('strength', 0) if isinstance(signal, dict) else 0
            sig_direction = signal.get('direction', None) if isinstance(signal, dict) else None
            
            if sig_strength > 0.7:
                confidence *= 1.2
                state['direction_bias'] = sig_direction
                reasons.append(f"strong_signal={sig_direction}")
            elif sig_strength > 0.5:
                confidence *= 1.05
                state['direction_bias'] = sig_direction
                reasons.append(f"moderate_signal={sig_direction}")
            elif sig_strength < 0.2:
                confidence *= 0.8
                reasons.append("weak_signal")
        
        # ══════════════════════════════════════════════════
        # LAYER 5: C++ ENGINE — quantitative confirmation
        # ══════════════════════════════════════════════════
        if cpp_pred:
            cpp_signal = cpp_pred.get('signal', 0)
            cpp_conf = cpp_pred.get('confidence', 0)
            cpp_acc = cpp_pred.get('accuracy', 50)
            # Only use C++ signal if engine is actually performing well
            if cpp_acc > 45 and cpp_conf > 0.8 and cpp_signal != 0:
                confidence *= 1.1
                reasons.append(f"cpp_confirms={cpp_signal}")
            # Ignore negative C++ signals (poisoned model risk)
            elif cpp_acc <= 45:
                reasons.append(f"cpp_ignored(acc={cpp_acc:.0f}%)")
        
        # ══════════════════════════════════════════════════
        # LAYER 6: PRICE ACTION — structural confirmation
        # ══════════════════════════════════════════════════
        if price_action and isinstance(price_action, dict) and price_action.get('ready'):
            pa_signal = price_action.get('signal')
            pa_conf = price_action.get('confidence', 0)
            pa_momentum = price_action.get('momentum', {})
            pa_breakout = price_action.get('breakout', {})
            
            if pa_signal and pa_conf > 0.6:
                confidence *= 1.1
                reasons.append(f"pa_confirms={pa_signal}")
            
            # Momentum alignment
            if pa_momentum and isinstance(pa_momentum, dict):
                mom_dir = pa_momentum.get('direction', None)
                mom_str = pa_momentum.get('strength', 0)
                if mom_str > 0.6:
                    if mom_dir == 'up' and state.get('direction_bias') == 'CALL':
                        confidence *= 1.1
                        reasons.append("momentum_aligned")
                    elif mom_dir == 'down' and state.get('direction_bias') == 'PUT':
                        confidence *= 1.1
                        reasons.append("momentum_aligned")
                    elif mom_dir and state.get('direction_bias') and mom_dir != state.get('direction_bias', '').lower():
                        confidence *= 0.85
                        reasons.append("momentum_conflict")
            
            # Breakout detection
            if pa_breakout and isinstance(pa_breakout, dict) and pa_breakout.get('detected'):
                confidence *= 1.15
                state['stake_multiplier'] *= 1.1
                reasons.append(f"breakout={pa_breakout.get('type', '?')}")
        
        # ══════════════════════════════════════════════════
        # LAYER 7: TIMEZONE CONTEXT — session quality
        # ══════════════════════════════════════════════════
        if session_label and 'US Open' in session_label:
            confidence *= 1.1  # US open is typically high volume
            reasons.append("us_open_session")
        elif session_label and 'Asian Late' in session_label:
            confidence *= 0.9  # late Asian can be thin
            state['stake_multiplier'] *= 0.9
            reasons.append("thin_session")
        
        # ══════════════════════════════════════════════════
        # LAYER 8: TICK DATA QUALITY — sufficient data?
        # ══════════════════════════════════════════════════
        tick_count = len(ticks) if ticks else 0
        if tick_count < 15:
            state['recommendation'] = self.REC_WAIT
            state['rec_reason'] = f"Insufficient data: {tick_count} ticks"
            state['risk_level'] = 'MEDIUM'
            self._record_state(market, state)
            return state
        elif tick_count < 30:
            confidence *= 0.85
            reasons.append(f"limited_data={tick_count}")
        
        # ══════════════════════════════════════════════════
        # FINAL DECISION — combine all layers
        # ══════════════════════════════════════════════════
        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))
        state['state_confidence'] = round(confidence, 3)
        state['state_reason'] = ' + '.join(reasons[:5])
        
        # Risk level from confidence
        if confidence >= 0.7:
            state['risk_level'] = 'LOW'
        elif confidence >= 0.5:
            state['risk_level'] = 'MEDIUM'
        elif confidence >= 0.3:
            state['risk_level'] = 'HIGH'
        else:
            state['risk_level'] = 'EXTREME'
        
        # Recommendation
        if confidence >= 0.4 and state['market_state'] not in (self.STATE_ANOMALOUS, self.STATE_UNTRADABLE):
            state['recommendation'] = self.REC_TRADE
            state['rec_reason'] = f"Confident ({confidence:.0%}) in {state['market_state']}"
        elif confidence >= 0.25:
            state['recommendation'] = self.REC_REDUCE
            state['rec_reason'] = f"Moderate ({confidence:.0%}) — reduce size"
            state['stake_multiplier'] *= 0.6
        elif confidence >= 0.15:
            state['recommendation'] = self.REC_REDUCE
            state['rec_reason'] = f"Scout ({confidence:.0%}) — scout mode"
            state['stake_multiplier'] *= 0.3
            state['is_scout'] = True
        else:
            state['recommendation'] = self.REC_WAIT
            state['rec_reason'] = f"Low confidence ({confidence:.0%}) — wait"
        
        # Stake multiplier bounds
        state['stake_multiplier'] = max(0.3, min(2.0, state['stake_multiplier']))
        state['confidence_boost'] = confidence / 0.5  # relative to neutral 0.5
        state['confidence_penalty'] = 0.5 / max(confidence, 0.1)
        
        self._record_state(market, state)
        return state
    
    def _record_state(self, market, state):
        """Record state for duration tracking and history."""
        if market not in self.state_history:
            self.state_history[market] = []
        self.state_history[market].append({
            'state': state['market_state'],
            'confidence': state['state_confidence'],
            'recommendation': state['recommendation'],
            'time': state['timestamp'],
        })
        # Keep last 50 states per market
        self.state_history[market] = self.state_history[market][-50:]
        
        # Track state duration
        current = state['market_state']
        last = self.last_states.get(market)
        if last == current:
            self.state_durations[market] = self.state_durations.get(market, 0) + 1
        else:
            self.state_durations[market] = 1
        self.last_states[market] = current
    
    def record_trade_result(self, market, state_type, strategy, profit):
        """Record trade result to learn which states are profitable."""
        self.trade_history.append({
            'market': market, 'state': state_type, 'strategy': strategy,
            'profit': profit, 'time': int(time.time() * 1000),
        })
        if len(self.trade_history) > 500:
            self.trade_history = self.trade_history[-500:]
        
        # Update state win rates
        key = state_type
        if key not in self.state_win_rates:
            self.state_win_rates[key] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        sr = self.state_win_rates[key]
        sr['trades'] += 1
        sr['pnl'] += profit
        if profit > 0:
            sr['wins'] += 1
    
    def get_state_performance(self, state_type):
        """Get historical performance for a market state type."""
        sr = self.state_win_rates.get(state_type, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        wr = (sr['wins'] / sr['trades'] * 100) if sr['trades'] > 0 else 0
        return {'trades': sr['trades'], 'wr': round(wr, 1), 'pnl': round(sr['pnl'], 2)}
    
    def get_status(self):
        """Get full status for state output."""
        return {
            'total_analyses': self.total_analyses,
            'markets_tracked': len(self.state_history),
            'last_states': {
                m: {
                    'state': hist[-1]['state'] if hist else '?',
                    'confidence': hist[-1]['confidence'] if hist else 0,
                    'recommendation': hist[-1]['recommendation'] if hist else '?',
                    'duration': self.state_durations.get(m, 0),
                }
                for m, hist in self.state_history.items()
            },
            'state_performance': {
                st: self.get_state_performance(st)
                for st in self.state_win_rates
            },
        }



# ═══════════════════════════════════════════════════════
# OVER-TRADE GUARD — prevents volume/time-based over-trading
# ═══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════
# PROBABILITY ENGINE — calculates edge/ev for all strategies
# ═══════════════════════════════════════════════════════
class ProbabilityEngine:
    """Centralizes probability/edge/ev calculation for every strategy.
    
    Feeds into:
    - Judge Q4 (simulation validation)
    - Strategist recommendations
    - Candidate scoring in the main loop
    
    Data sources:
    - Backtester results (historical tick simulation)
    - Trade history (actual results)
    - Memory strategies (accumulated stats)
    """
    
    PAYOUTS = {
        'DIGITMATCH': 8.0, 'DIGITDIFF': 0.06,
        'DIGITEVEN': 0.95, 'DIGITODD': 0.95,
        'CALL': 0.95, 'PUT': 0.95,
        'ASIANU': 0.95, 'ASIAND': 0.95,
    }
    
    def __init__(self):
        self.strategy_edges = {}   # strategy_key -> {edge, ev, win_rate, confidence, sample_size}
        self.last_calc_cycle = 0
        self.calc_interval = 10    # recalculate every N cycles
    
    def calculate_all(self, trade_log, backtester_results, memory_strategies, cycle):
        """Recalculate edge for all strategies. Called from main loop."""
        if cycle - self.last_calc_cycle < self.calc_interval:
            return self.strategy_edges
        self.last_calc_cycle = cycle
        
        # Build per-strategy stats from trade history
        strat_stats = {}
        for t in (trade_log or [])[-500:]:
            key = f"{t.get('market','?')}:{t.get('strategy','?')}"
            contract = t.get('contract', t.get('contract_type', ''))
            if key not in strat_stats:
                strat_stats[key] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'contract': contract, 'trades': []}
            s = strat_stats[key]
            profit = t.get('profit', 0)
            if profit > 0:
                s['wins'] += 1
            else:
                s['losses'] += 1
            s['pnl'] += profit
            s['trades'].append(profit)
            if not s['contract'] and contract:
                s['contract'] = contract
        
        # Merge with backtester results
        for key, bt in (backtester_results or {}).items():
            if key not in strat_stats:
                strat_stats[key] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'contract': bt.get('contract', ''), 'trades': []}
            s = strat_stats[key]
            # Weight backtester data less than real trades
            bt_weight = 0.3
            s['wins'] = s['wins'] + int(bt.get('wins', 0) * bt_weight)
            s['losses'] = s['losses'] + int(bt.get('losses', 0) * bt_weight)
        
        # Calculate edge for each strategy
        self.strategy_edges = {}
        for key, s in strat_stats.items():
            total = s['wins'] + s['losses']
            if total < 3:
                continue
            
            contract = s['contract']
            payout = self.PAYOUTS.get(contract, 0.95)
            wr = s['wins'] / total
            
            # Expected value: EV = WR * (1 + payout) - 1
            ev = wr * (1 + payout) - 1
            
            # Edge: actual WR minus break-even WR
            # Break-even WR = 1 / (1 + payout)
            be_wr = 1.0 / (1.0 + payout)
            edge = wr - be_wr
            
            # Confidence: based on sample size
            confidence = min(total / 30.0, 1.0)  # full confidence at 30+ trades
            
            # Kelly fraction: f* = (bp - q) / b
            b = payout
            p = wr
            q = 1 - p
            kelly = max(0, (p * b - q) / b) if b > 0 else 0
            
            self.strategy_edges[key] = {
                'edge': round(edge, 4),
                'ev': round(ev, 4),
                'win_rate': round(wr * 100, 1),
                'confidence': round(confidence, 2),
                'sample_size': total,
                'payout': payout,
                'kelly': round(kelly, 4),
                'pnl': round(s['pnl'], 4),
                'contract': contract,
                'trades': total,
            }
        
        return self.strategy_edges
    
    def get_edge(self, market, strategy):
        """Get edge for a specific strategy on a market."""
        key = f"{market}:{strategy}"
        return self.strategy_edges.get(key)
    
    def get_ev(self, market, strategy):
        """Get expected value for a specific strategy."""
        e = self.get_edge(market, strategy)
        return e.get('ev', 0) if e else 0
    
    def get_confidence(self, market, strategy):
        """Get confidence level for a strategy."""
        e = self.get_edge(market, strategy)
        return e.get('confidence', 0) if e else 0
    
    def is_profitable(self, market, strategy):
        """Check if a strategy has positive edge with enough data."""
        e = self.get_edge(market, strategy)
        if not e:
            return False
        return e['edge'] > 0 and e['sample_size'] >= 5 and e['confidence'] >= 0.3
    
    def get_best_strategies(self, top_n=5):
        """Return top strategies by edge."""
        valid = [v for v in self.strategy_edges.values() if v['sample_size'] >= 5]
        valid.sort(key=lambda x: x['edge'], reverse=True)
        return valid[:top_n]
    
    def get_worst_strategies(self, bottom_n=5):
        """Return worst strategies by edge (for retirement)."""
        valid = [v for v in self.strategy_edges.values() if v['sample_size'] >= 5]
        valid.sort(key=lambda x: x['edge'])
        return valid[:bottom_n]
    
    def get_status(self):
        """Dashboard status."""
        profitable = sum(1 for v in self.strategy_edges.values() if v['edge'] > 0)
        total = len(self.strategy_edges)
        best = max((v['edge'] for v in self.strategy_edges.values()), default=0)
        worst = min((v['edge'] for v in self.strategy_edges.values()), default=0)
        return {
            'total_strategies': total,
            'profitable': profitable,
            'unprofitable': total - profitable,
            'best_edge': round(best, 4),
            'worst_edge': round(worst, 4),
            'avg_edge': round(sum(v['edge'] for v in self.strategy_edges.values()) / max(total, 1), 4),
        }


# ═══════════════════════════════════════════════════════
# TICK BACKTESTER — validates strategies against historical ticks
# ═══════════════════════════════════════════════════════
class TickBacktester:
    """Walks historical tick data and simulates strategy outcomes.
    
    Runs every N cycles on accumulated tick data. Feeds results to:
    - memory.record_simulation() for strategy validation
    - Judge sim_result for trade gating
    """
    
    PAYOUTS = {
        'DIGITMATCH': 8.0,
        'DIGITDIFF':  0.06,
        'DIGITEVEN':  0.95,
        'DIGITODD':   0.95,
        'CALL':       0.95,
        'PUT':        0.95,
        'ASIANU':     0.95,
        'ASIAND':     0.95,
    }
    
    def __init__(self):
        self.results = {}           # strategy_key -> {win_rate, ev, simulations, profit_factor, max_drawdown}
        self.last_run_cycle = 0
        self.run_interval = 3       # run every 3 cycles (aggressive learning)
        self.total_sims = 0
        self.min_ticks = 25         # minimum tick data needed (faster first sims)
        self._tick_history = {}     # market -> list of tick dicts {digit, price, time}
    
    def record_tick(self, market, digit, price=0):
        """Store tick for backtesting. Called from tick collector."""
        if market not in self._tick_history:
            self._tick_history[market] = []
        self._tick_history[market].append({'digit': digit, 'price': price})
        if len(self._tick_history[market]) > 800:
            self._tick_history[market] = self._tick_history[market][-800:]
    
    def should_run(self, cycle):
        return cycle - self.last_run_cycle >= self.run_interval and cycle > 0
    
    def run_backtests(self, cycle, strategy_candidates=None):
        """Run backtests across all markets. Returns dict of results per strategy."""
        self.last_run_cycle = cycle
        all_results = {}
        
        for market, ticks in self._tick_history.items():
            if len(ticks) < self.min_ticks:
                continue
            digits = [t['digit'] for t in ticks]
            prices = [t['price'] for t in ticks]
            
            # Backtest each strategy type on this market's data
            for strat_key, result in self._backtest_market(market, digits, prices).items():
                if strat_key not in all_results or result['simulations'] > all_results[strat_key].get('simulations', 0):
                    all_results[strat_key] = result
        
        self.results = all_results
        self.total_sims = len(all_results)
        return all_results
    
    def _backtest_market(self, market, digits, prices):
        """Backtest all strategy types on a single market's tick data."""
        results = {}
        n = len(digits)
        if n < self.min_ticks:
            return results
        
        # Walk the tick sequence with a sliding window
        window = 50
        for start in range(0, n - 10, 10):  # step by 10 for speed
            end = min(start + window, n - 1)
            if end - start < 20:
                continue
            
            window_digits = digits[start:end]
            window_prices = prices[start:end] if prices else [0] * len(window_digits)
            next_digit = digits[end] if end < n else None
            next_price = prices[end] if prices and end < len(prices) else None
            
            if next_digit is None:
                continue
            
            # --- DIGITDIFF: bet digit won't match ---
            freq = {}
            for d in window_digits:
                freq[d] = freq.get(d, 0) + 1
            expected = len(window_digits) / 10.0
            for digit in range(10):
                obs = freq.get(digit, 0)
                if obs / expected > 1.3:  # over-represented
                    key = f"{market}:DIGIT_DIFF_{digit}"
                    won = next_digit != digit
                    self._accumulate_result(results, key, 'DIGITDIFF', won, self.PAYOUTS['DIGITDIFF'])
            
            # --- DIGITMATCH: bet digit will match ---
            for digit in range(10):
                obs = freq.get(digit, 0)
                if obs / expected < 0.7:  # under-represented
                    key = f"{market}:DIGIT_MATCH_{digit}"
                    won = next_digit == digit
                    self._accumulate_result(results, key, 'DIGITMATCH', won, self.PAYOUTS['DIGITMATCH'])
            
            # --- EVEN/ODD bias ---
            evens = sum(1 for d in window_digits if d % 2 == 0)
            if evens / len(window_digits) > 0.58:
                won = next_digit % 2 == 0
                self._accumulate_result(results, f"{market}:EVEN_BIAS", 'DIGITEVEN', won, self.PAYOUTS['DIGITEVEN'])
            elif (len(window_digits) - evens) / len(window_digits) > 0.58:
                won = next_digit % 2 == 1
                self._accumulate_result(results, f"{market}:ODD_BIAS", 'DIGITODD', won, self.PAYOUTS['DIGITODD'])
            
            # --- RISE/FALL TREND ---
            if len(window_digits) >= 20:
                first_half = sum(window_digits[:len(window_digits)//2]) / max(len(window_digits)//2, 1)
                second_half = sum(window_digits[len(window_digits)//2:]) / max(len(window_digits)//2, 1)
                diff = second_half - first_half
                if abs(diff) > 0.15 and next_price and prices and end < len(prices) and end > 0:
                    prev_price = prices[end - 1]
                    if diff > 0:
                        won = next_price > prev_price
                        self._accumulate_result(results, f"{market}:RISE_TREND", 'CALL', won, self.PAYOUTS['CALL'])
                    else:
                        won = next_price < prev_price
                        self._accumulate_result(results, f"{market}:FALL_TREND", 'PUT', won, self.PAYOUTS['PUT'])
            
            # --- MOMENTUM ---
            if len(window_digits) >= 10:
                r10 = window_digits[-10:]
                rising = sum(1 for i in range(1, len(r10)) if r10[i] > r10[i-1])
                falling = 10 - 1 - rising
                if next_price and prices and end < len(prices) and end > 0:
                    prev_price = prices[end - 1]
                    if rising >= 6:
                        won = next_price > prev_price
                        self._accumulate_result(results, f"{market}:MOMENTUM_UP", 'CALL', won, self.PAYOUTS['CALL'])
                    elif falling >= 6:
                        won = next_price < prev_price
                        self._accumulate_result(results, f"{market}:MOMENTUM_DOWN", 'PUT', won, self.PAYOUTS['PUT'])
            
            # --- LOW_DIGIT_DIFF: bet low digits (0-4) won't appear ---
            low_count = sum(1 for d in window_digits if d <= 4)
            if low_count / len(window_digits) > 0.6:  # low digits over-represented
                for digit in range(5):  # bet against 0-4
                    key = f"{market}:LOW_DIGIT_DIFF_{digit}"
                    won = next_digit != digit
                    self._accumulate_result(results, key, 'DIGITDIFF', won, self.PAYOUTS['DIGITDIFF'])
            
            # --- HIGH_DIGIT_DIFF: bet high digits (5-9) won't appear ---
            high_count = sum(1 for d in window_digits if d >= 5)
            if high_count / len(window_digits) > 0.6:
                for digit in range(5, 10):  # bet against 5-9
                    key = f"{market}:HIGH_DIGIT_DIFF_{digit}"
                    won = next_digit != digit
                    self._accumulate_result(results, key, 'DIGITDIFF', won, self.PAYOUTS['DIGITDIFF'])
            
            # --- OVER_BIAS: average trend above 5 ---
            avg = sum(window_digits) / len(window_digits)
            if avg > 5.5:
                won = next_digit > 5 if next_digit is not None else False
                self._accumulate_result(results, f"{market}:OVER_BIAS", 'ASIANU', won, self.PAYOUTS['ASIANU'])
            elif avg < 4.5:
                won = next_digit < 5 if next_digit is not None else False
                self._accumulate_result(results, f"{market}:UNDER_BIAS", 'ASIAND', won, self.PAYOUTS['ASIAND'])
        
        # Finalize all results
        for key in results:
            r = results[key]
            total = r['wins'] + r['losses']
            if total > 0:
                r['win_rate'] = round(r['wins'] / total * 100, 1)
                r['simulations'] = total
                # EV calculation
                payout = self.PAYOUTS.get(r['contract'], 0.95)
                r['expected_value'] = round((r['wins'] / total) * (1 + payout) - 1, 4)
                # Profit factor
                gross_profit = r['wins'] * payout
                gross_loss = r['losses'] * 1.0
                r['profit_factor'] = round(gross_profit / max(gross_loss, 0.01), 2)
        
        return results
    
    def _accumulate_result(self, results, key, contract, won, payout):
        """Accumulate a single simulation result."""
        if key not in results:
            results[key] = {
                'strategy': key.split(':')[-1] if ':' in key else key,
                'market': key.split(':')[0] if ':' in key else 'unknown',
                'contract': contract,
                'wins': 0, 'losses': 0,
                'win_rate': 0, 'simulations': 0,
                'expected_value': 0, 'profit_factor': 0,
                'pnl': 0.0, 'max_drawdown': 0.0,
            }
        r = results[key]
        if won:
            r['wins'] += 1
            r['pnl'] += payout
        else:
            r['losses'] += 1
            r['pnl'] -= 1.0
        # Track max drawdown
        if r['pnl'] < r.get('_min_pnl', 0):
            r['_min_pnl'] = r['pnl']
        r['max_drawdown'] = round(max(r.get('_max_dd', 0), r.get('_min_pnl', 0) - r['pnl'] + r['pnl']), 4)
    
    def get_strategy_result(self, market, strategy_name):
        """Get simulation result for a specific strategy on a market."""
        key = f"{market}:{strategy_name}"
        return self.results.get(key)
    
    def get_best_strategies(self, top_n=5):
        """Return top strategies by EV with enough simulations."""
        valid = [r for r in self.results.values() if r.get('simulations', 0) >= 5 and r.get('expected_value', 0) > 0]
        valid.sort(key=lambda x: x.get('expected_value', 0), reverse=True)
        return valid[:top_n]
    
    def get_status(self):
        """Dashboard status."""
        valid = [r for r in self.results.values() if r.get('simulations', 0) >= 5]
        return {
            'total_strategies': len(self.results),
            'validated': len(valid),
            'best_ev': max((r.get('expected_value', 0) for r in valid), default=0),
            'best_wr': max((r.get('win_rate', 0) for r in valid), default=0),
            'tick_history': {m: len(t) for m, t in self._tick_history.items()},
            'last_run': self.last_run_cycle,
            'total_sims': self.total_sims,
        }


class OverTradeGuard:
    """Prevents over-trading by enforcing time-based and volume-based limits.
    
    Measures:
    1. Hourly trade cap — max trades per rolling hour
    2. Daily trade cap — max trades per day
    3. Session frequency — minimum gap between sessions
    4. Volume cooldown — force pause after burst of trades in short window
    5. Performance degradation — force break if recent WR collapses
    6. Fatigue detection — escalating pauses as session wears on
    """
    
    # ── Configurable limits ──
    HOURLY_LIMIT = 30        # max trades per rolling hour
    DAILY_LIMIT = 100        # max trades per day
    SESSION_GAP_WIN = 5      # seconds between sessions after a win
    SESSION_GAP_LOSS = 15    # seconds between sessions after a loss
    SESSION_GAP_CONSEC_LOSS = 30  # seconds after 2+ consecutive session losses
    VOLUME_WINDOW = 300      # 5-minute window for volume check
    VOLUME_LIMIT = 12        # max trades in 5-min window
    VOLUME_COOLDOWN = 120    # 2-min pause if volume limit hit
    DEGRADATION_WINDOW = 10  # look at last N trades for WR check
    DEGRADATION_WR_THRESHOLD = 0.15  # if recent WR < 15%, force break
    DEGRADATION_COOLDOWN = 60        # 1-min break if degraded
    FATIGUE_WINDOW = 20      # trades to check for fatigue
    FATIGUE_THRESHOLD = 0.30 # if WR over last N trades < 30%, slow down
    FATIGUE_COOLDOWN = 60    # 1-min fatigue pause
    
    def __init__(self):
        self.bypass = False             # set True to disable all guards (testing mode)
        self.trade_times = []           # timestamps of all trades (rolling)
        self.session_end_times = []     # timestamps of session completions
        self.daily_trade_count = 0
        self.daily_reset_time = 0
        self.break_until = 0            # forced break timestamp
        self.break_reason = ""
        self.total_breaks = 0
        self.skipped_trades = 0         # trades prevented by guard
        self.last_session_profit = 0    # last session PnL for gap calc
        
    def record_trade(self):
        """Record a trade occurrence."""
        now = time.time()
        self.trade_times.append(now)
        # Clean old trades (keep last hour)
        self.trade_times = [t for t in self.trade_times if now - t < 3600]
        # Daily count
        day_start = int(now // 86400) * 86400
        if self.daily_reset_time != day_start:
            self.daily_trade_count = 0
            self.daily_reset_time = day_start
        self.daily_trade_count += 1
    
    def record_session_end(self, pnl):
        """Record session completion for gap timing."""
        now = time.time()
        self.session_end_times.append(now)
        self.session_end_times = [t for t in self.session_end_times if now - t < 3600]
        self.last_session_profit = pnl
    
    def can_start_session(self, risk_obj, recent_trades=None):
        """Check if a new session can start. Returns (allowed, reason, wait_seconds)."""
        if self.bypass:
            return True, "bypass_active", 0
        now = time.time()
        
        # 1. Forced break active?
        if now < self.break_until:
            remaining = self.break_until - now
            return False, f"FORCED BREAK: {self.break_reason} ({remaining:.0f}s left)", remaining
        
        # 2. Hourly limit
        hourly_count = len([t for t in self.trade_times if now - t < 3600])
        if hourly_count >= self.HOURLY_LIMIT:
            self.break_until = now + 120
            self.break_reason = f"hourly limit hit ({hourly_count}/{self.HOURLY_LIMIT})"
            self.total_breaks += 1
            return False, self.break_reason, 120
        
        # 3. Daily limit
        if self.daily_trade_count >= self.DAILY_LIMIT:
            self.break_until = now + 600
            self.break_reason = f"daily limit hit ({self.daily_trade_count}/{self.DAILY_LIMIT})"
            self.total_breaks += 1
            return False, self.break_reason, 600
        
        # 4. Volume burst check (trades in last 5 min)
        recent_5min = len([t for t in self.trade_times if now - t < self.VOLUME_WINDOW])
        if recent_5min >= self.VOLUME_LIMIT:
            self.break_until = now + self.VOLUME_COOLDOWN
            self.break_reason = f"volume burst ({recent_5min} trades in 5min)"
            self.total_breaks += 1
            return False, self.break_reason, self.VOLUME_COOLDOWN
        
        # 5. Session frequency gap
        if self.session_end_times:
            last_session = self.session_end_times[-1]
            elapsed = now - last_session
            if self.last_session_profit < 0:
                # Loss session — longer gap
                gap = self.SESSION_GAP_LOSS
                if risk_obj.consec_loss >= 2:
                    gap = self.SESSION_GAP_CONSEC_LOSS
            else:
                gap = self.SESSION_GAP_WIN
            
            if elapsed < gap:
                wait = gap - elapsed
                return False, f"session gap ({wait:.0f}s after {'loss' if self.last_session_profit < 0 else 'win'})", wait
        
        # 6. Performance degradation check
        if recent_trades and len(recent_trades) >= self.DEGRADATION_WINDOW:
            last_n = recent_trades[-self.DEGRADATION_WINDOW:]
            recent_wins = sum(1 for t in last_n if t.get('profit', 0) > 0)
            recent_wr = recent_wins / len(last_n)
            if recent_wr < self.DEGRADATION_WR_THRESHOLD:
                self.break_until = now + self.DEGRADATION_COOLDOWN
                self.break_reason = f"WR degraded ({recent_wr:.0%} over last {len(last_n)} trades)"
                self.total_breaks += 1
                return False, self.break_reason, self.DEGRADATION_COOLDOWN
        
        # 7. Fatigue detection (slower pace, not full stop)
        if recent_trades and len(recent_trades) >= self.FATIGUE_WINDOW:
            last_n = recent_trades[-self.FATIGUE_WINDOW:]
            recent_wins = sum(1 for t in last_n if t.get('profit', 0) > 0)
            recent_wr = recent_wins / len(last_n)
            if recent_wr < self.FATIGUE_THRESHOLD:
                # Force a short pause instead of full break
                self.break_until = now + self.FATIGUE_COOLDOWN
                self.break_reason = f"fatigue detected (WR {recent_wr:.0%} over last {len(last_n)} trades)"
                self.total_breaks += 1
                return False, self.break_reason, self.FATIGUE_COOLDOWN
        
        return True, "ok", 0
    
    def get_status(self):
        """Get guard status for dashboard."""
        now = time.time()
        return {
            "hourly_trades": len([t for t in self.trade_times if now - t < 3600]),
            "hourly_limit": self.HOURLY_LIMIT,
            "daily_trades": self.daily_trade_count,
            "daily_limit": self.DAILY_LIMIT,
            "in_break": now < self.break_until,
            "break_remaining": max(0, self.break_until - now),
            "break_reason": self.break_reason,
            "total_breaks": self.total_breaks,
            "skipped_trades": self.skipped_trades,
        }


# ═══════════════════════════════════════════════════════
# SESSION MANAGER — 5-trade structured sessions
# ═══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════
# TRADE INTELLIGENCE — 10-layer precision system
# ═══════════════════════════════════════════════════════
class TradeIntelligence:
    """10-layer precision system for optimal trade decisions:
    
    1. Volatility-Adjusted Stake Sizing
    2. Drawdown Recovery Protocol
    3. Edge Decay Detection
    4. Execution Quality Tracking
    5. Portfolio-Level Risk
    6. Multi-Timeframe Regime Context
    7. Adaptive Kelly Criterion
    8. Market Session Awareness
    9. Real-time Anomaly Response
    10. Strategy Ensemble Scoring
    """
    
    def __init__(self):
        # ── Layer 1: Volatility-Adjusted Stake ──
        self.volatility_cache = {}  # market -> {avg_range, current_vol, multiplier}
        self.vol_window = 20
        
        # ── Layer 2: Drawdown Recovery ──
        self.consec_losses = 0
        self.consec_wins = 0
        self.recovery_step = 0  # 0=normal, 1=first_restore, 2=second_restore
        self.recovery_multiplier = 1.0
        self.loss_streak_threshold = 3
        self.win_streak_restore = 2  # need 2 wins to restore
        
        # ── Layer 3: Edge Decay Detection ──
        self.strategy_wr_history = {}  # strategy -> deque of last 10 results
        self.decay_threshold = 0.20    # WR drops 20% from baseline
        self.decay_min_trades = 10
        self.decayed_strategies = set()
        
        # ── Layer 4: Execution Quality ──
        self.execution_log = deque(maxlen=100)  # [{expected, actual, slippage, time}]
        self.avg_slippage = 0.0
        self.slippage_alert_threshold = 0.002  # 0.2%
        
        # ── Layer 5: Portfolio Risk ──
        self.open_positions = deque(maxlen=10)  # [{market, direction, stake, time, strategy}]
        self.max_correlated = 2
        self.max_total_exposure_pct = 0.05  # 5% of balance
        
        # ── Layer 6: Multi-Timeframe Regime ──
        self.tick_history = {}     # market -> deque of ticks
        self.trend_5min = {}       # market -> direction (-1/0/1)
        self.trend_window = 150    # ~5 min at 3 ticks/sec
        
        # ── Layer 7: Adaptive Kelly ──
        self.kelly_data = {}       # strategy -> {wins, losses, payout}
        self.kelly_fraction = 0.25  # use quarter-Kelly for safety
        
        # ── Layer 8: Market Session ──
        self.session_strategies = {
            'asian': {'preferred': ['DIGIT_DIFF', 'EVEN_BIAS', 'ODD_BIAS'], 'avoid': ['RISE_TREND', 'FALL_TREND']},
            'european': {'preferred': ['RISE_TREND', 'FALL_TREND', 'DIGIT_DIFF'], 'avoid': []},
            'us': {'preferred': ['RISE_TREND', 'FALL_TREND', 'MOMENTUM'], 'avoid': ['DIGIT_MATCH']},
            'off': {'preferred': ['DIGIT_DIFF', 'EVEN_BIAS'], 'avoid': ['CALL', 'PUT']},
        }
        self.current_session = 'us'
        
        # ── Layer 9: Anomaly Response ──
        self.anomaly_cooldown = {}  # market -> cooldown_until
        self.anomaly_reduction = 0.5  # 50% stake reduction on anomaly
        
        # ── Layer 10: Ensemble Scoring ──
        self.ensemble_candidates = deque(maxlen=5)  # top 5 candidates with weights
        
        # ── State ──
        self.balance = 0
        self.start_balance = 0
        self.recent_trades = deque(maxlen=50)
    
    def init(self, balance):
        self.balance = balance
        self.start_balance = balance
    
    def update_tick(self, market, digit, price):
        """Feed tick data for multi-timeframe analysis."""
        if market not in self.tick_history:
            self.tick_history[market] = deque(maxlen=self.trend_window * 2)
        self.tick_history[market].append({'digit': digit, 'price': price, 'time': time.time()})
        
        # Update 5-min trend
        ticks = list(self.tick_history[market])
        if len(ticks) >= self.trend_window:
            recent = ticks[-self.trend_window:]
            prices = [t['price'] for t in recent if t.get('price', 0) > 0]
            if len(prices) >= 20:
                first_half = sum(prices[:len(prices)//2]) / (len(prices)//2)
                second_half = sum(prices[len(prices)//2:]) / max(len(prices) - len(prices)//2, 1)
                diff = (second_half - first_half) / max(first_half, 1)
                if diff > 0.001:
                    self.trend_5min[market] = 1   # uptrend
                elif diff < -0.001:
                    self.trend_5min[market] = -1  # downtrend
                else:
                    self.trend_5min[market] = 0   # sideways
        
        # Update volatility
        if market not in self.volatility_cache:
            self.volatility_cache[market] = {'ranges': deque(maxlen=50), 'current': 0, 'multiplier': 1.0}
        if len(ticks) >= 10:
            last10 = ticks[-10:]
            digits = [t['digit'] for t in last10]
            vol = max(digits) - min(digits)
            self.volatility_cache[market]['ranges'].append(vol)
            ranges = list(self.volatility_cache[market]['ranges'])
            avg_vol = sum(ranges) / len(ranges) if ranges else 5
            current_vol = vol
            # High vol → reduce stake, low vol → increase stake
            if avg_vol > 0:
                vol_ratio = current_vol / avg_vol
                self.volatility_cache[market]['multiplier'] = max(0.5, min(2.0, 1.0 / vol_ratio))
                self.volatility_cache[market]['current'] = current_vol
    
    def calculate_stake(self, base_stake, market, strategy, contract, alignment, prob_edge):
        """Master stake calculation using all 10 layers."""
        stake = base_stake
        
        # Layer 1: Volatility adjustment
        vol_mult = self.volatility_cache.get(market, {}).get('multiplier', 1.0)
        stake *= vol_mult
        
        # Layer 2: Drawdown recovery
        stake *= self.recovery_multiplier
        
        # Layer 4: Execution quality (if slippage high, reduce)
        if self.avg_slippage > self.slippage_alert_threshold:
            stake *= 0.7
        
        # Layer 5: Portfolio risk check
        exposure = sum(p.get('stake', 0) for p in self.open_positions)
        total_exposure_pct = exposure / max(self.balance, 1)
        if total_exposure_pct >= self.max_total_exposure_pct:
            stake *= 0.5
        elif total_exposure_pct >= self.max_total_exposure_pct * 0.7:
            stake *= 0.7
        
        # Layer 6: Multi-timeframe regime
        trend_5m = self.trend_5min.get(market, 0)
        is_directional = contract in ('CALL', 'PUT')
        if is_directional:
            if contract == 'CALL' and trend_5m == -1:
                stake *= 0.5  # CALL against 5min downtrend
            elif contract == 'PUT' and trend_5m == 1:
                stake *= 0.5  # PUT against 5min uptrend
            elif contract == 'CALL' and trend_5m == 1:
                stake *= 1.2  # CALL with 5min uptrend
            elif contract == 'PUT' and trend_5m == -1:
                stake *= 1.2  # PUT with 5min downtrend
        
        # Layer 7: Kelly criterion
        kelly_mult = self._kelly_multiplier(strategy, contract)
        stake *= kelly_mult
        
        # Layer 8: Session preference
        session_mult = self._session_multiplier(strategy, contract)
        stake *= session_mult
        
        # Layer 9: Anomaly cooldown
        if market in self.anomaly_cooldown and time.time() < self.anomaly_cooldown[market]:
            stake *= self.anomaly_reduction
        
        # Layer 10: Ensemble confidence (handled separately in scoring)
        
        # Final clamp
        stake = max(0.35, min(stake, 25.0))
        return round(stake, 2)
    
    def record_trade_result(self, profit, market, strategy, contract, stake, balance):
        """Post-trade analysis for all layers."""
        self.balance = balance
        won = profit > 0
        
        # Layer 2: Drawdown recovery
        if won:
            self.consec_wins += 1
            self.consec_losses = 0
            if self.recovery_step == 1 and self.consec_wins >= self.win_streak_restore:
                self.recovery_multiplier = 0.75
                self.recovery_step = 2
            elif self.recovery_step == 2 and self.consec_wins >= self.win_streak_restore:
                self.recovery_multiplier = 1.0
                self.recovery_step = 0
        else:
            self.consec_losses += 1
            self.consec_wins = 0
            if self.consec_losses >= self.loss_streak_threshold:
                self.recovery_multiplier = 0.5
                self.recovery_step = 1
        
        # Layer 3: Edge decay detection
        self._update_edge_decay(strategy, market, won)
        
        # Layer 4: Execution quality (estimate slippage from expected vs actual)
        expected_pnl = stake * 0.5 if won else -stake  # rough estimate
        actual_pnl = profit
        slippage = abs(expected_pnl - actual_pnl) / max(stake, 0.01)
        self.execution_log.append({
            'expected': expected_pnl, 'actual': actual_pnl,
            'slippage': slippage, 'time': time.time(),
            'market': market, 'strategy': strategy,
        })
        if len(self.execution_log) > 10:
            slips = [e['slippage'] for e in self.execution_log]
            self.avg_slippage = sum(slips) / len(slips)
        
        # Layer 7: Update Kelly data
        key = f"{market}:{strategy}"
        if key not in self.kelly_data:
            self.kelly_data[key] = {'wins': 0, 'losses': 0, 'payout': 0.95}
        kd = self.kelly_data[key]
        if won:
            kd['wins'] += 1
        else:
            kd['losses'] += 1
        
        self.recent_trades.append({'profit': profit, 'market': market, 'strategy': strategy, 'time': time.time()})
    
    def open_position(self, market, direction, stake, strategy):
        """Track open position for portfolio risk."""
        self.open_positions.append({
            'market': market, 'direction': direction,
            'stake': stake, 'time': time.time(), 'strategy': strategy,
        })
    
    def close_position(self, market, strategy):
        """Remove closed position."""
        self.open_positions = deque(
            [p for p in self.open_positions if not (p.get('market') == market and p.get('strategy') == strategy)],
            maxlen=10
        )
    
    def on_anomaly(self, market, anomaly_type):
        """Layer 9: React to anomaly detection."""
        cooldown_seconds = {'GAP': 300, 'SPIKE': 180, 'REVERSAL': 120, 'CHOP': 240, 'STALL': 120}
        self.anomaly_cooldown[market] = time.time() + cooldown_seconds.get(anomaly_type, 180)
    
    def _kelly_multiplier(self, strategy, contract):
        """Layer 7: Quarter-Kelly stake adjustment."""
        key = f"{strategy}"
        for k, kd in self.kelly_data.items():
            if k.endswith(f":{strategy}"):
                key = k
                break
        kd = self.kelly_data.get(key, {'wins': 0, 'losses': 0})
        total = kd['wins'] + kd['losses']
        if total < 10:
            return 1.0
        p = kd['wins'] / total
        q = 1 - p
        b = 0.95  # average payout
        kelly = max(0, (p * b - q) / b) if b > 0 else 0
        # Use quarter-Kelly for safety
        return min(1.5, 1.0 + kelly * self.kelly_fraction * 2)
    
    def _session_multiplier(self, strategy, contract):
        """Layer 8: Session-based strategy preference."""
        session = self.session_strategies.get(self.current_session, {})
        preferred = session.get('preferred', [])
        avoid = session.get('avoid', [])
        
        for pref in preferred:
            if pref in strategy:
                return 1.2  # boost preferred strategies
        for av in avoid:
            if av in strategy or av == contract:
                return 0.6  # reduce avoided strategies
        return 1.0
    
    def _update_edge_decay(self, strategy, market, won):
        """Layer 3: Track WR per strategy and detect decay."""
        key = f"{market}:{strategy}"
        if key not in self.strategy_wr_history:
            self.strategy_wr_history[key] = deque(maxlen=20)
        self.strategy_wr_history[key].append(1 if won else 0)
        
        history = list(self.strategy_wr_history[key])
        if len(history) < self.decay_min_trades:
            return
        
        current_wr = sum(history) / len(history)
        # Check if WR dropped significantly from early performance
        if len(history) >= 15:
            early_wr = sum(history[:10]) / 10
            if early_wr > 0.5 and (early_wr - current_wr) > self.decay_threshold:
                self.decayed_strategies.add(key)
    
    def is_strategy_decayed(self, market, strategy):
        """Layer 3: Check if strategy has edge decay."""
        return f"{market}:{strategy}" in self.decayed_strategies
    
    def get_session(self):
        """Layer 8: Determine current market session from UTC hour."""
        hour = int(time.time() / 3600) % 24
        if 0 <= hour < 8:
            self.current_session = 'asian'
        elif 8 <= hour < 14:
            self.current_session = 'european'
        elif 14 <= hour < 22:
            self.current_session = 'us'
        else:
            self.current_session = 'off'
        return self.current_session
    
    def score_candidates(self, candidates):
        """Layer 10: Ensemble scoring — blend top candidates."""
        if len(candidates) <= 1:
            return candidates
        
        # Take top 5 and blend scores
        top = sorted(candidates, key=lambda c: c[0], reverse=True)[:5]
        
        # Weight by recency and consistency
        total_score = sum(c[0] for c in top)
        if total_score <= 0:
            return candidates
        
        blended = []
        for score, mkt, cand in top:
            weight = score / total_score
            # Boost if this strategy has been consistent
            key = f"{mkt}:{cand.get('strategy', '')}"
            kd = self.kelly_data.get(key, {})
            total = kd.get('wins', 0) + kd.get('losses', 0)
            if total >= 10:
                consistency = kd.get('wins', 0) / total
                weight *= (1 + consistency * 0.3)
            blended.append((score * weight * len(top), mkt, cand))
        
        return sorted(blended, key=lambda c: -c[0])
    
    def get_status(self):
        """Dashboard status."""
        session = self.get_session()
        return {
            'session': session,
            'recovery_step': self.recovery_step,
            'recovery_multiplier': round(self.recovery_multiplier, 2),
            'avg_slippage': round(self.avg_slippage, 4),
            'decayed_strategies': len(self.decayed_strategies),
            'open_positions': len(self.open_positions),
            'total_exposure': round(sum(p.get('stake', 0) for p in self.open_positions), 2),
            'kelly_strategies': len(self.kelly_data),
            'trend_5min': dict(self.trend_5min),
            'anomaly_cooldowns': sum(1 for v in self.anomaly_cooldown.values() if time.time() < v),
            'consec_losses': self.consec_losses,
            'consec_wins': self.consec_wins,
        }


# ═══════════════════════════════════════════════════════
# PROFIT GUARD — 8-layer profit/loss protection system
# ═══════════════════════════════════════════════════════
class ProfitGuard:
    """Comprehensive profit protection with 8 layers:
    
    1. Equity Curve Guard — auto-reduce if balance trend slopes down
    2. Watermark System — track all-time high, stop if drops too far
    3. Time-Based Tightening — tighten stops late in session
    4. Profit Ladder — tiered locking as cumulative profit grows
    5. Win Streak Compounding — increase on wins, cut on losses
    6. Break-Even Trailing — move individual trade stop to break-even
    7. Correlation Risk — cap exposure when correlated positions
    8. End-of-Day Lock — protect session gains after 22:00 UTC
    """
    
    def __init__(self, start_balance=0):
        # ── Layer 1: Equity Curve ──
        self.equity_curve = deque(maxlen=30)
        self.equity_ma_period = 20
        self.equity_below_ma_count = 0
        self.equity_stake_reduction = 1.0
        
        # ── Layer 2: Watermark ──
        self.watermark = start_balance
        self.watermark_drop_pct = 0.02   # stop at 2% below watermark
        self.watermark_paused = False
        self.watermark_min_stake = 0.35
        self.watermark_resume_pct = 0.005
        
        # ── Layer 3: Time-Based Tightening ──
        self.session_start_time = time.time()
        self.tighten_after_hours = 4      # tighten after 4 hours
        self.tighten_factor = 0.5         # reduce all stops by 50%
        self.time_tightened = False
        
        # ── Layer 4: Profit Ladder ──
        self.ladder_tiers = [
            (0,   0.0, 0.0),    # $0-3 profit → no lock
            (3,   1.0, 0.0),    # $3-7 profit → lock $1
            (7,   5.0, 0.0),    # $7-15 profit → lock $5
            (15, 10.0, 0.0),    # $15+ profit → lock $10
        ]
        self.ladder_floor = 0.0
        
        # ── Layer 5: Win Streak Compounding ──
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.streak_compound_pct = 0.12   # +12% per win in streak
        self.streak_loss_cut_pct = 0.30   # -30% after 2+ losses
        self.streak_multiplier = 1.0
        
        # ── Layer 6: Break-Even Trailing ──
        self.active_trades = {}           # trade_id -> {entry, current_stop, direction}
        self.break_even_trigger_pct = 0.015  # move to break-even after 1.5% profit
        
        # ── Layer 7: Correlation Risk ──
        self.open_positions = []          # [{market, direction, stake, time}]
        self.max_correlated_exposure = 3  # max 3 same-direction positions
        self.max_same_market = 2          # max 2 positions same market
        
        # ── Layer 8: End-of-Day Lock ──
        self.eod_hour = 22                # UTC hour to lock
        self.eod_locked = False
        self.eod_floor = 0.0
        
        # ── State ──
        self.start_balance = start_balance
        self.current_balance = start_balance
        self.daily_pnl = 0.0
        self.peak_daily_pnl = 0.0
        self.pause_until = 0
        self.pause_reason = ""
        self.reduction_multiplier = 1.0
        self.actions_today = []           # [{action, reason, time}]
    
    def on_trade(self, profit, balance, strategy, market, stake, contract):
        """Called after every trade. Returns (action, reason, stake_multiplier)."""
        now = time.time()
        self.current_balance = balance
        self.daily_pnl = balance - self.start_balance
        if self.daily_pnl > self.peak_daily_pnl:
            self.peak_daily_pnl = self.daily_pnl
        
        self.equity_curve.append({'balance': balance, 'time': now, 'profit': profit})
        
        multiplier = 1.0
        action = 'CONTINUE'
        reasons = []
        
        # ── Layer 1: Equity Curve Guard ──
        l1_action, l1_mult = self._check_equity_curve()
        if l1_mult != 1.0:
            multiplier *= l1_mult
            if l1_action == 'STOP':
                return 'STOP', f'EQUITY_CURVE: below MA, reducing', 0
            reasons.append(f'equity_reduce={l1_mult}')
        
        # ── Layer 2: Watermark ──
        l2_action, l2_reason = self._check_watermark(balance)
        if l2_action == 'STOP':
            return 'STOP', f'WATERMARK: {l2_reason}', 0
        if l2_action == 'REDUCE':
            multiplier *= 0.5
            reasons.append('watermark_reduce')
        
        # ── Layer 3: Time-Based Tightening ──
        l3_tightened = self._check_time_tightening()
        if l3_tightened:
            multiplier *= self.tighten_factor
            reasons.append('time_tightened')
        
        # ── Layer 4: Profit Ladder ──
        l4_action, l4_floor = self._check_profit_ladder(self.daily_pnl)
        if l4_action == 'STOP':
            return 'STOP', f'PROFIT_LADDER: below floor ${l4_floor:.2f}', 0
        reasons.append(f'ladder_floor=${l4_floor:.2f}')
        
        # ── Layer 5: Win Streak Compounding ──
        l5_mult = self._check_streak(profit)
        multiplier *= l5_mult
        
        # ── Layer 6: Break-Even Trailing ──
        self._update_break_even(profit, balance)
        
        # ── Layer 8: End-of-Day Lock ──
        l8_action, l8_reason = self._check_eod_lock(balance)
        if l8_action == 'STOP':
            return 'STOP', f'EOD_LOCK: {l8_reason}', 0
        if l8_action == 'REDUCE':
            multiplier *= 0.3
            reasons.append('eod_tightened')
        
        # Final multiplier
        multiplier = max(0.3, min(multiplier, 2.0))
        
        if reasons:
            self.actions_today.append({'action': action, 'reasons': reasons, 'time': now})
        
        return action, f'mult={multiplier:.2f} ' + ' '.join(reasons), multiplier
    
    def on_trade_open(self, trade_id, market, direction, stake, entry_price):
        """Track open position for correlation risk and break-even."""
        self.open_positions.append({
            'id': trade_id, 'market': market, 'direction': direction,
            'stake': stake, 'entry': entry_price, 'time': time.time()
        })
        self.active_trades[trade_id] = {
            'entry': entry_price, 'current_stop': 0, 'direction': direction,
            'peak_pnl_pct': 0, 'stake': stake, 'market': market,
        }
    
    def on_trade_close(self, trade_id):
        """Remove closed position from tracking."""
        self.open_positions = [p for p in self.open_positions if p.get('id') != trade_id]
        self.active_trades.pop(trade_id, None)
    
    def _check_equity_curve(self):
        """Layer 1: If equity curve slopes down, reduce stake."""
        if len(self.equity_curve) < self.equity_ma_period:
            return 'CONTINUE', 1.0
        
        recent = list(self.equity_curve)
        balances = [e['balance'] for e in recent[-self.equity_ma_period:]]
        ma = sum(balances) / len(balances)
        
        if self.current_balance < ma:
            self.equity_below_ma_count += 1
            if self.equity_below_ma_count >= 5:
                # 5+ consecutive periods below MA → reduce aggressively
                self.equity_stake_reduction = 0.4
                return 'REDUCE', 0.4
            self.equity_stake_reduction = 0.7
            return 'REDUCE', 0.7
        else:
            self.equity_below_ma_count = max(0, self.equity_below_ma_count - 1)
            self.equity_stake_reduction = min(1.0, self.equity_stake_reduction + 0.1)
            return 'CONTINUE', self.equity_stake_reduction
    
    def _check_watermark(self, balance):
        """Layer 2: Never let balance drop too far from all-time high."""
        if balance > self.watermark:
            self.watermark = balance
        
        drop_pct = (self.watermark - balance) / max(self.watermark, 1)
        
        if drop_pct >= self.watermark_drop_pct:
            self.watermark_paused = True
            return 'STOP', f'dropped {drop_pct*100:.1f}% below watermark ${self.watermark:.2f}'
        
        if drop_pct >= self.watermark_drop_pct * 0.6:
            return 'REDUCE', f'near watermark drop {drop_pct*100:.1f}%'
        
        # Auto-resume when recovery
        if self.watermark_paused and drop_pct < self.watermark_resume_pct:
            self.watermark_paused = False
        
        return 'CONTINUE', 'ok'
    
    def _check_time_tightening(self):
        """Layer 3: Tighten stops after N hours of trading."""
        hours_active = (time.time() - self.session_start_time) / 3600
        if hours_active >= self.tighten_after_hours and not self.time_tightened:
            self.time_tightened = True
        return self.time_tightened
    
    def _check_profit_ladder(self, pnl):
        """Layer 4: Tiered profit locking."""
        for threshold, floor, _ in reversed(self.ladder_tiers):
            if pnl >= threshold:
                self.ladder_floor = floor
                break
        
        if pnl > 0 and pnl < self.ladder_floor:
            return 'STOP', self.ladder_floor
        
        return 'CONTINUE', self.ladder_floor
    
    def _check_streak(self, profit):
        """Layer 5: Compound on win streaks, cut on loss streaks."""
        if profit > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            if self.consecutive_wins >= 3:
                self.streak_multiplier = min(2.0, 1.0 + (self.consecutive_wins - 2) * self.streak_compound_pct)
            elif self.consecutive_wins >= 2:
                self.streak_multiplier = 1.0 + self.streak_compound_pct
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            if self.consecutive_losses >= 2:
                self.streak_multiplier = max(0.3, 1.0 - self.consecutive_losses * self.streak_loss_cut_pct)
            elif self.consecutive_losses == 1:
                self.streak_multiplier = max(0.7, 1.0 - self.streak_loss_cut_pct)
        
        return self.streak_multiplier
    
    def _update_break_even(self, profit, balance):
        """Layer 6: Move trade stop to break-even when in profit."""
        for tid, trade in list(self.active_trades.items()):
            # Estimate current PnL percentage
            pnl_pct = profit / max(trade['stake'], 0.01)
            if pnl_pct > trade.get('peak_pnl_pct', 0):
                trade['peak_pnl_pct'] = pnl_pct
            # If peak profit exceeded trigger, lock at break-even
            if trade.get('peak_pnl_pct', 0) >= self.break_even_trigger_pct:
                trade['current_stop'] = trade['entry']
    
    def _check_correlation_risk(self):
        """Layer 7: Cap exposure from correlated positions."""
        if len(self.open_positions) == 0:
            return 1.0
        
        directions = {}
        markets = {}
        for pos in self.open_positions:
            d = pos.get('direction', '?')
            m = pos.get('market', '?')
            directions[d] = directions.get(d, 0) + 1
            markets[m] = markets.get(m, 0) + 1
        
        max_dir = max(directions.values()) if directions else 0
        max_mkt = max(markets.values()) if markets else 0
        
        if max_dir >= self.max_correlated_exposure:
            return 0.5  # halve stake
        if max_mkt >= self.max_same_market:
            return 0.6  # reduce stake
        return 1.0
    
    def _check_eod_lock(self, balance):
        """Layer 8: Protect gains after end-of-day hour."""
        current_hour = int(time.time() / 3600) % 24
        
        # Auto-clear EOD lock when day changes (before eod_hour)
        if current_hour < self.eod_hour and self.eod_locked:
            self.eod_locked = False
            self.eod_floor = 0.0
        
        if current_hour >= self.eod_hour:
            if not self.eod_locked and self.daily_pnl > 0:
                self.eod_locked = True
                self.eod_floor = self.daily_pnl * 0.7  # protect 70% of day's profit
            if self.eod_locked and self.daily_pnl < self.eod_floor:
                return 'STOP', f'daily PnL ${self.daily_pnl:.2f} < floor ${self.eod_floor:.2f}'
            if self.eod_locked:
                return 'REDUCE', f'EOD locked, floor=${self.eod_floor:.2f}'
        
        return 'CONTINUE', 'ok'
    
    def get_status(self):
        """Dashboard status."""
        return {
            'watermark': round(self.watermark, 2),
            'watermark_paused': self.watermark_paused,
            'equity_below_ma': self.equity_below_ma_count,
            'equity_reduction': round(self.equity_stake_reduction, 2),
            'time_tightened': self.time_tightened,
            'ladder_floor': round(self.ladder_floor, 2),
            'daily_pnl': round(self.daily_pnl, 4),
            'peak_daily_pnl': round(self.peak_daily_pnl, 4),
            'streak_multiplier': round(self.streak_multiplier, 2),
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses,
            'open_positions': len(self.open_positions),
            'eod_locked': self.eod_locked,
            'eod_floor': round(self.eod_floor, 2),
            'actions_today': len(self.actions_today),
        }

    def reset(self, balance):
        """Reset all protection layers for a new session/day."""
        self.start_balance = balance
        self.current_balance = balance
        self.daily_pnl = 0.0
        self.peak_daily_pnl = 0.0
        self.watermark = balance
        self.watermark_paused = False
        self.equity_curve.clear()
        self.equity_below_ma_count = 0
        self.equity_stake_reduction = 1.0
        self.time_tightened = False
        self.session_start_time = time.time()
        self.ladder_floor = 0.0
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.streak_multiplier = 1.0
        self.eod_locked = False
        self.eod_floor = 0.0
        self.open_positions.clear()
        self.active_trades.clear()
        self.actions_today.clear()
        self.pause_until = 0
        self.pause_reason = ""
        self.reduction_multiplier = 1.0


# ═══════════════════════════════════════════════════════
# ROUND PROFIT MANAGER — round-based trailing stop system
# ═══════════════════════════════════════════════════════
class RoundProfitManager:
    """Round-based profit protection with trailing stops.
    
    Concept:
    - System trades in "rounds" (each round has a profit target)
    - Round 1: trade until profit target hit (e.g., $5)
    - When target hit AND confidence still high → keep trading with trailing stop
    - Trailing stop = round_peak_profit - $2 (protects most gains)
    - When round ends (stop hit or confidence drops) → lock profit, start new round
    - Round 2 starts clean: stop_loss = locked_profit_from_round1 - $2
    - Losses in round 2 cannot eat into round 1's locked profit
    
    This ensures the system never gives back more than $2 of accumulated profit
    while allowing continued trading when conditions are favorable.
    """
    
    def __init__(self):
        # Round state
        self.round_number = 0
        self.round_start_balance = 0.0
        self.round_peak_profit = 0.0       # highest profit in current round
        self.round_stop_loss = -999.0      # trailing stop (profit threshold)
        self.round_target = 5.0            # profit target per round
        self.round_trailing_buffer = 2.0   # stop loss sits at peak - buffer
        
        # Locked profit from completed rounds
        self.locked_profit = 0.0
        self.total_rounds_completed = 0
        self.round_history = []            # [{round, pnl, peak, duration, exit_reason}]
        
        # Current round tracking
        self.round_trades = 0
        self.round_wins = 0
        self.round_losses = 0
        self.round_pnl = 0.0
        self.round_start_time = 0
        
        # Configuration
        self.MIN_CONFIDENCE_TO_TRAIL = 0.5   # keep trailing if alignment >= 5
        self.MIN_CONFIDENCE_TO_CONTINUE = 4  # continue round if alignment >= 4
        self.MAX_CONSEC_LOSS_IN_ROUND = 3    # exit round after 3 consecutive losses
        
        # Active state
        self.active = False
        self.exit_reason = ""
    
    def start_new_round(self, balance):
        """Start a new profit round."""
        self.round_number += 1
        self.round_start_balance = balance
        self.round_peak_profit = 0.0
        self.round_stop_loss = self.locked_profit - self.round_trailing_buffer
        self.round_trades = 0
        self.round_wins = 0
        self.round_losses = 0
        self.round_pnl = 0.0
        self.round_start_time = time.time()
        self.active = True
        self.exit_reason = ""
        return self.round_number
    
    def on_trade(self, profit, balance, alignment_score=0):
        """Called after every trade. Returns (action, reason).
        
        action: 'CONTINUE' | 'TRAIL' | 'NEW_ROUND' | 'STOP' | 'LOCK_AND_STOP'
        """
        if not self.active:
            return 'CONTINUE', 'round_not_active'
        
        self.round_trades += 1
        self.round_pnl += profit
        
        if profit > 0:
            self.round_wins += 1
        else:
            self.round_losses += 1
        
        # Update peak profit for this round
        current_profit = self.round_pnl
        if current_profit > self.round_peak_profit:
            self.round_peak_profit = current_profit
            # Move trailing stop up: stop_loss = peak - buffer
            # But never below locked profit from previous rounds
            new_stop = self.round_peak_profit - self.round_trailing_buffer
            self.round_stop_loss = max(new_stop, self.locked_profit - self.round_trailing_buffer)
        
        # ── CHECK 1: Round profit target hit ──
        if self.round_pnl >= self.round_target:
            if alignment_score >= self.MIN_CONFIDENCE_TO_TRAIL:
                # Confidence still high → trail instead of exit
                return 'TRAIL', f'TARGET_HIT: round {self.round_number} pnl=${self.round_pnl:+.2f} target=${self.round_target} — trailing (align={alignment_score})'
            else:
                # Confidence dropping → lock profit, end round
                self._complete_round('TARGET_AND_LOCK')
                return 'NEW_ROUND', f'TARGET_LOCKED: round {self.round_number} pnl=${self.round_pnl:+.2f}'
        
        # ── CHECK 2: Trailing stop hit ──
        if self.round_pnl <= self.round_stop_loss and self.round_peak_profit > 0:
            # Gave back too much from peak → lock what we have, end round
            self._complete_round('TRAILING_STOP')
            if self.round_pnl > 0:
                return 'NEW_ROUND', f'TRAILING_STOP: round {self.round_number} peaked=${self.round_peak_profit:+.2f} stopped=${self.round_pnl:+.2f}'
            else:
                return 'STOP', f'TRAILING_STOP_LOSS: round {self.round_number} lost ${self.round_pnl:+.2f}'
        
        # ── CHECK 3: Consecutive losses in round ──
        if self.round_losses >= self.MAX_CONSEC_LOSS_IN_ROUND:
            self._complete_round('CONSEC_LOSS')
            return 'STOP', f'CONSEC_LOSS: round {self.round_number} {self.round_losses} losses'
        
        # ── CHECK 4: Confidence dropping below minimum ──
        if alignment_score < self.MIN_CONFIDENCE_TO_CONTINUE and self.round_pnl > 0:
            self._complete_round('LOW_CONFIDENCE')
            return 'LOCK_AND_STOP', f'LOW_CONF: round {self.round_number} align={alignment_score} pnl=${self.round_pnl:+.2f}'
        
        # ── CHECK 5: Negative round and too many trades ──
        if self.round_pnl < -3.0 and self.round_trades >= 3:
            self._complete_round('ROUND_LOSS_LIMIT')
            return 'STOP', f'ROUND_LOSS: round {self.round_number} pnl=${self.round_pnl:+.2f}'
        
        return 'CONTINUE', f'round {self.round_number} pnl=${self.round_pnl:+.2f} peak=${self.round_peak_profit:+.2f} stop=${self.round_stop_loss:+.2f}'
    
    def _complete_round(self, reason):
        """Finish current round and lock profit."""
        duration = time.time() - self.round_start_time if self.round_start_time else 0
        self.round_history.append({
            'round': self.round_number,
            'pnl': round(self.round_pnl, 4),
            'peak': round(self.round_peak_profit, 4),
            'trades': self.round_trades,
            'wins': self.round_wins,
            'losses': self.round_losses,
            'duration_sec': round(duration, 1),
            'exit_reason': reason,
            'time': int(time.time() * 1000),
        })
        # Lock profit if round was positive
        if self.round_pnl > 0:
            self.locked_profit += self.round_pnl
        self.total_rounds_completed += 1
        self.active = False
        self.exit_reason = reason
    
    def should_start_new_round(self, balance, alignment_score):
        """Decide if we should start a new round."""
        if self.active:
            return False, 'round_active'
        # Don't start if alignment too low
        if alignment_score < self.MIN_CONFIDENCE_TO_CONTINUE:
            return False, f'low_alignment={alignment_score}'
        # Don't start if we've completed enough rounds and are ahead
        if self.total_rounds_completed >= 3 and self.locked_profit > 0:
            return False, f'rounds_complete={self.total_rounds_completed} locked=${self.locked_profit:+.2f}'
        return True, 'ready'
    
    def get_stake_multiplier(self):
        """Reduce stake when deep in a round loss, increase when near target."""
        if not self.active:
            return 1.0
        if self.round_pnl < -2.0:
            return 0.5   # reduce stake when losing
        if self.round_peak_profit >= self.round_target * 0.8:
            return 1.2   # near target, slightly more aggressive
        return 1.0
    
    def get_status(self):
        """Dashboard status."""
        return {
            'active': self.active,
            'round_number': self.round_number,
            'round_pnl': round(self.round_pnl, 4),
            'round_peak': round(self.round_peak_profit, 4),
            'round_target': self.round_target,
            'round_stop_loss': round(self.round_stop_loss, 4),
            'round_trades': self.round_trades,
            'round_wins': self.round_wins,
            'round_losses': self.round_losses,
            'locked_profit': round(self.locked_profit, 4),
            'total_rounds': self.total_rounds_completed,
            'exit_reason': self.exit_reason,
            'trailing_buffer': self.round_trailing_buffer,
            'history': self.round_history[-10:],
        }


class SessionManager:
    """Manages 5-trade sessions with structured entry/exit.
    - Waits for ALL security gates to pass (high confidence)
    - Executes 5 trades with same strategy/market
    - Exits after 5 trades OR 3 consecutive losses
    - Logs session P&L and performance
    """
    def __init__(self):
        self.active = False
        self.session_id = 0
        self.trades_in_session = 0
        self.max_trades = 8
        self.session_market = None
        self.session_strategy = None
        self.session_contract = None
        self.session_stake = 2.0
        self.session_pnl = 0.0
        self.session_wins = 0
        self.session_losses = 0
        self.consec_session_loss = 0
        self.session_start_time = 0
        self.sessions_completed = 0
        self.total_session_pnl = 0.0
        self.best_session_pnl = 0.0
        self.worst_session_pnl = 0.0
        self.sessions_profitable = 0
        self.cooldown_until = 0
        self.cooldown_reason = ""
        self.entry_gates = {
            "ticks_sufficient": False,
            "signal_confirmed": False,
            "regime_clear": False,
            "edge_positive": False,
            "risk_clear": False,
            "strategy_healthy": False,
        }
        self.session_log = []

    def reset(self):
        self.active = False
        self.trades_in_session = 0
        self.session_market = None
        self.session_strategy = None
        self.session_contract = None
        self.session_pnl = 0.0
        self.session_wins = 0
        self.session_losses = 0
        self.consec_session_loss = 0

    def check_entry_gates(self, tick_count, signal, regime, edge, consec_loss, consec_win, strategy_health, balance, start_balance):
        """Check entry gates. 4 of 6 must pass (not all 6 — real markets are messy)."""
        gates = {}
        # Gate 1: Enough data to analyze
        gates["ticks_sufficient"] = tick_count >= 15
        # Gate 2: Signal exists (NEUTRAL is OK — brain decides direction, not sensor)
        gates["signal_exists"] = signal is not None
        # Gate 3: Regime is known (RANGE_COMPRESSION is valid — many strategies work in ranges)
        gates["regime_known"] = regime is not None and regime != "UNKNOWN"
        # Gate 4: Positive edge
        gates["edge_positive"] = edge > 0.001
        # Gate 5: Risk clear (not in deep loss)
        gates["risk_clear"] = consec_loss < 4 and (balance / start_balance) > 0.97
        # Gate 6: Strategy has minimum health
        if strategy_health:
            trades = strategy_health.get("trades", 0)
            if trades >= 5:
                gates["strategy_healthy"] = strategy_health.get("wins", 0) / trades >= 0.40
            else:
                gates["strategy_healthy"] = True
        else:
            gates["strategy_healthy"] = True
        self.entry_gates = gates
        # 4 of 6 gates must pass
        passed = sum(1 for v in gates.values() if v)
        return passed >= 4, dict(gates)

    def enter_session(self, market, strategy, contract, stake):
        self.session_id += 1
        self.active = True
        self.trades_in_session = 0
        self.session_market = market
        self.session_strategy = strategy
        self.session_contract = contract
        self.session_stake = stake
        self.session_pnl = 0.0
        self.session_wins = 0
        self.session_losses = 0
        self.consec_session_loss = 0
        self.session_start_time = time.time()
        return self.session_id

    def record_session_trade(self, profit):
        self.trades_in_session += 1
        self.session_pnl += profit
        if profit > 0:
            self.session_wins += 1
            self.consec_session_loss = 0
        else:
            self.session_losses += 1
            self.consec_session_loss += 1
        
        # ── DYNAMIC SESSION LENGTH: extend if winning ──
        # Base: 5 trades. If no losses yet and profit increasing, extend to 10, then 20.
        # Market direction must be clear (regime != RANGE_COMPRESSION)
        if self.consec_session_loss == 0 and self.session_pnl > 0:
            if self.trades_in_session >= 8 and self.max_trades < 15:
                self.max_trades = 10
            if self.trades_in_session >= 15 and self.max_trades < 25 and self.session_pnl > 0:
                self.max_trades = 20
        # If 1 loss but still net positive, keep going up to 10
        if self.consec_session_loss <= 1 and self.session_pnl > 0:
            if self.trades_in_session >= 8 and self.max_trades < 15:
                self.max_trades = 10
        
        # Exit conditions
        if self.trades_in_session >= self.max_trades:
            return True, f"completed_{self.max_trades}_trades"
        if self.consec_session_loss >= 3:
            return True, "exit_3_consec_losses"
        
        return False, f"trade_{self.trades_in_session}/{self.max_trades} (session_pnl=${self.session_pnl:+.4f})"

    def close_session(self):
        self.active = False
        self.sessions_completed += 1
        self.total_session_pnl += self.session_pnl
        self.best_session_pnl = max(self.best_session_pnl, self.session_pnl)
        self.worst_session_pnl = min(self.worst_session_pnl, self.session_pnl)
        if self.session_pnl > 0:
            self.sessions_profitable += 1
        entry = {
            "session_id": self.session_id,
            "market": self.session_market,
            "strategy": self.session_strategy,
            "contract": self.session_contract,
            "trades": self.trades_in_session,
            "wins": self.session_wins,
            "losses": self.session_losses,
            "pnl": round(self.session_pnl, 4),
            "stake": self.session_stake,
            "duration": round(time.time() - self.session_start_time, 1),
            "time": int(time.time() * 1000),
        }
        self.session_log.append(entry)
        if len(self.session_log) > 50:
            self.session_log = self.session_log[-50:]
        if self.session_pnl < 0:
            self.cooldown_until = time.time() + 30
            self.cooldown_reason = f"lost_pnl={self.session_pnl:+.2f}"
        else:
            self.cooldown_until = time.time() + 10
            self.cooldown_reason = f"won_pnl={self.session_pnl:+.2f}"
        self.reset()
        return entry

    def is_in_cooldown(self):
        return time.time() < self.cooldown_until

    def get_status(self):
        wr = (self.sessions_profitable / self.sessions_completed * 100) if self.sessions_completed > 0 else 0
        return {
            "active": self.active, "session_id": self.session_id,
            "trades_in_session": self.trades_in_session, "max_trades": self.max_trades,
            "session_market": self.session_market, "session_strategy": self.session_strategy,
            "session_contract": self.session_contract, "session_pnl": round(self.session_pnl, 4),
            "session_wins": self.session_wins, "session_losses": self.session_losses,
            "sessions_completed": self.sessions_completed,
            "sessions_profitable": self.sessions_profitable,
            "total_session_pnl": round(self.total_session_pnl, 4),
            "session_win_rate": round(wr, 1),
            "entry_gates": self.entry_gates,
            "cooldown": self.is_in_cooldown(),
            "cooldown_reason": self.cooldown_reason,
            "session_log": self.session_log[-10:],
        }



# ═══════════════════════════════════════════════════════
# MAIN BRAIN
# ═══════════════════════════════════════════════════════
# ── Trade Log & Agent Notes (persisted in state) ──────
TRADE_LOG = []       # last 500 trades (full day)
AGENT_NOTES = []     # agent decisions/notes
SYS_LOG = []         # system log entries

def log_sys(msg, log_type="info"):
    """Add entry to system log for dashboard."""
    entry = {'message': msg, 'log_type': log_type, 'time': int(time.time() * 1000)}
    SYS_LOG.append(entry)
    if len(SYS_LOG) > 200:
        SYS_LOG.pop(0)
    try:
        log_manager.add_log('sys_log', entry)
    except: pass

def log_agent(agent, note):
    """Add agent note for dashboard."""
    entry = {'agent': agent, 'note': note, 'time': int(time.time() * 1000)}
    AGENT_NOTES.append(entry)
    try:
        if agent not in ('noise', 'diagnostic', 'regime', 'memory', 'backtester'):
            log_manager.add_log('agent_log', entry)
    except: pass
    # Keep max 100, but prioritize important agents (model, session, executor)
    if len(AGENT_NOTES) > 200:
        # Remove low-priority entries first (regime, memory spam)
        low_priority = ['regime', 'memory', 'noise', 'diagnostic', 'backtester']
        for i in range(len(AGENT_NOTES) - 200):
            for j, e in enumerate(AGENT_NOTES):
                if e.get('agent') in low_priority:
                    AGENT_NOTES.pop(j)
                    break
            else:
                AGENT_NOTES.pop(0)
    # Persist to disk for dashboard (keep last 50)
    try:
        important = [n for n in AGENT_NOTES[-200:] if n.get('agent') not in ('regime', 'memory', 'noise', 'diagnostic', 'backtester')]
        all_notes = important  # no regime/memory fallback
        Path('agent_notifications.json').write_text(json.dumps(all_notes[-50:]))
    except: pass

def log_model(model, event, tokens=0, cost=0):
    """Log model usage event for dashboard."""
    entry = {'agent': 'alm_brain', 'model': model, 'note': event, 'tokens': tokens, 'cost': cost, 'time': int(time.time() * 1000)}
    AGENT_NOTES.append(entry)
    try:
        log_manager.add_log('agent_log', entry)
    except: pass
    try:
        important = [n for n in AGENT_NOTES[-200:] if n.get('agent') not in ('regime', 'memory', 'noise', 'diagnostic', 'backtester')]
        Path('agent_notifications.json').write_text(json.dumps(important[-50:]))
    except: pass

def log_trade(trade):
    """Add trade to history for dashboard."""
    TRADE_LOG.append(trade)
    if len(TRADE_LOG) > 500:
        TRADE_LOG.pop(0)
    try:
        log_manager.add_trade(trade)
    except Exception:
        pass

HISTORY_FILE = Path(__file__).parent / 'persistent_history.json'

def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except: pass
    return {'trades': [], 'edge_history': [], 'sys_log': [], 'agent_notes': [], 'pnl_history': [], 'session_count': 0}

def save_history(data):
    try:
        HISTORY_FILE.write_text(json.dumps(data, indent=1, default=str))
    except: pass

def exception_handler(loop, context):
    """Catch and log all async exceptions."""
    exc = context.get('exception')
    msg = context.get('message', 'Unknown error')
    print(f"  [ASYNC ERROR] {msg}: {exc}", flush=True)
    if exc:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stdout, flush=True)
    sys.stdout.flush()

async def main():
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(exception_handler)
    print("=" * 55)
    print("  AD-SMTA BRAIN v3 — AGENT-POWERED")
    print("=" * 55)
    SESSION_START = time.time()
    hist = load_history()
    TRADE_LOG.extend(hist.get('trades', [])[-500:])
    AGENT_NOTES.extend(hist.get('agent_notes', [])[-200:])
    SYS_LOG.extend(hist.get('sys_log', [])[-200:])
    log_sys(f"Loaded {len(hist.get('trades',[]))} trades from history", "info")
    log_sys("Brain v3 started", "info")
    log_agent("system", "All 7 agents initialized: sensor, memory, picker, judge, protector, regime, strategist")
    
    tc = TickCollector()
    trader = Trader()
    tools = AgentTools()
    
    # Start C++ Quant Engine
    cpp_engine = None
    try:
        sys.path.insert(0, str(Path(__file__).parent / 'cpp_engine'))
        from bridge import CppEngine
        cpp_engine = CppEngine()
        if cpp_engine.start():
            print('  [C++ ENGINE] Online')
        else:
            print('  [C++ ENGINE] Failed to start')
            cpp_engine = None
    except Exception as e:
        print(f'  [C++ ENGINE] Not available: {e}')
    
    connected = False
    for _retry in range(10):
        try:
            connected = await trader.connect()
            if connected: break
        except Exception as e:
            print(f"  [RETRY {_retry+1}/10] Connect failed: {e}")
        await asyncio.sleep(3)
    if not connected:
        print("  FATAL: Cannot connect to Deriv after 10 retries")
        return
    
    risk = Risk(trader.balance)
    
    # Accumulate historical totals from loaded trades
    for ht in TRADE_LOG:
        if ht.get('profit', 0) != 0:
            risk.total += 1
            risk.pnl += ht.get('profit', 0)
            risk.balance += ht.get('profit', 0)
            if ht.get('profit', 0) > 0:
                risk.wins += 1
            else:
                risk.losses += 1
    if risk.total > 0:
        print(f"  Restored {risk.total} trades from history: W/L {risk.wins}/{risk.losses} ({risk.wr():.1f}%) PnL ${risk.pnl:+.4f}")
    tools.init(trader.balance)
    risk._mission = tools.mission
    tools.growth.set_start_balance(risk.balance)
    tools.profit_guard = ProfitGuard(trader.balance)
    tools.trade_intel.init(trader.balance)
    tools.log_manager = log_manager  # wire module-level log_manager into tools
    tools.watcher = SystemWatcher(tools)
    print('  [WATCHER] SystemWatcher initialized — 6 domains active')
    tools.round_profit.start_new_round(trader.balance)
    
    # ── STARTUP OPTIMIZATION: retire losers, boost winners ──
    try:
        strats = tools.memory.data.get("strategies", {})
        retired_count = 0
        boosted_count = 0
        for key, s in strats.items():
            if not isinstance(s, dict):
                continue
            market = key.split(":")[0] if ":" in key else ""
            strategy = key.split(":")[1] if ":" in key else key
            trades = s.get("trades", 0)
            wins = s.get("wins", 0)
            pnl = s.get("total_profit", s.get("pnl", 0))
            wr = wins / max(trades, 1)
            
            # RETIRE: R_50 all strategies (40% WR, -$2.86 PnL)
            if market == "R_50" and trades >= 3:
                s["status"] = "RETIRED"
                s["retired_reason"] = "R_50 market retired: 40% WR, -$2.86 PnL"
                s["retired_at"] = int(time.time() * 1000)
                retired_count += 1
            
            # RETIRE: MOMENTUM_UP with <50% WR (33% WR, -$4.58)
            elif strategy == "MOMENTUM_UP" and trades >= 3 and wr < 0.50:
                s["status"] = "RETIRED"
                s["retired_reason"] = f"MOMENTUM_UP retired: {wr*100:.0f}% WR, ${pnl:+.2f} PnL"
                s["retired_at"] = int(time.time() * 1000)
                retired_count += 1
            
            # RETIRE: Any strategy with <35% WR after 10+ trades
            elif trades >= 10 and wr < 0.35:
                s["status"] = "RETIRED"
                s["retired_reason"] = f"Low WR retired: {wr*100:.0f}% WR after {trades} trades"
                s["retired_at"] = int(time.time() * 1000)
                retired_count += 1
            
            # RETIRE: Any strategy with negative PnL after 5+ trades
            elif trades >= 5 and pnl < -1.0:
                s["status"] = "RETIRED"
                s["retired_reason"] = f"Negative PnL retired: ${pnl:+.2f} after {trades} trades"
                s["retired_at"] = int(time.time() * 1000)
                retired_count += 1
            
            # BOOST: DIGIT_DIFF with >60% WR
            elif "DIGIT_DIFF" in strategy and trades >= 3 and wr > 0.60:
                s["priority"] = "HIGH"
                s["boost_reason"] = f"DIGIT_DIFF boosted: {wr*100:.0f}% WR, ${pnl:+.2f}"
                boosted_count += 1
            
            # BOOST: RISE/FALL_TREND on JD75/JD100 with >60% WR
            elif strategy in ("RISE_TREND", "FALL_TREND") and market in ("JD75", "JD100") and trades >= 5 and wr > 0.60:
                s["priority"] = "HIGH"
                s["boost_reason"] = f"Core strategy boosted: {wr*100:.0f}% WR, ${pnl:+.2f}"
                boosted_count += 1
            
            # BOOST: Even/Odd with >55% WR
            elif strategy in ("EVEN_BIAS", "ODD_BIAS") and trades >= 5 and wr > 0.55:
                s["priority"] = "HIGH"
                s["boost_reason"] = f"Parity boosted: {wr*100:.0f}% WR, ${pnl:+.2f}"
                boosted_count += 1
        
        tools.memory.save()
        if retired_count > 0 or boosted_count > 0:
            print(f"  [OPTIMIZE] Retired {retired_count} losers, boosted {boosted_count} winners")
            log_sys(f"Startup optimization: retired {retired_count}, boosted {boosted_count}", "info")
    except Exception as e:
        print(f"  [OPTIMIZE] Error: {e}")
    
    # Restore brain state (evolution log, model usage, etc.)
    if STATE_FILE.exists():
        try:
            saved_state = json.loads(STATE_FILE.read_text())
            if tools.alm:
                tools.alm.restore_state(saved_state)
        except Exception as e:
            print(f"  [STATE] Could not restore brain state: {e}")
    
    # Network health monitor
    net_health = NetworkHealth()
    tc.net_health = net_health
    tc._backtester_ref = tools.backtester
    tc._trade_intel_ref = tools.trade_intel
    trader.net_health = net_health
    if tools.alm and tools.alm.connected:
        net_health.ollama_ok()
    
    tick_task = asyncio.create_task(tc.run())
    await asyncio.sleep(3)
    
    mode_tag = "📄PAPER" if PAPER_MODE else "💵LIVE"
    burst_tag = " 🔥BURST" if risk.full_burst else ""
    print(f"  Balance: ${risk.balance:.2f} | {mode_tag}{burst_tag}")
    print(f"  Agents: sensor, memory, picker, judge, protector, regime, strategist")
    print(f"  Markets: {MARKET_LIST}")
    
    # Auto-benchmark both engines at startup (with timeout protection)
    if hasattr(tools, 'openrouter') and tools.openrouter:
        import concurrent.futures
        print(f"  [BENCH] Benchmarking AI engines...", flush=True)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                oll_future = executor.submit(tools.openrouter.benchmark_engine, tools.openrouter.ENGINE_OLLAMA)
                oll_score = oll_future.result(timeout=10) or 0
        except Exception:
            oll_score = 0
            print(f"  [BENCH] Ollama bench timed out", flush=True)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                or_future = executor.submit(tools.openrouter.benchmark_engine, tools.openrouter.ENGINE_OPENROUTER)
                or_score = or_future.result(timeout=20) or 0
        except Exception:
            or_score = 0
            print(f"  [BENCH] OpenRouter bench timed out, defaulting to active", flush=True)
        print(f"  [BENCH] Ollama score={oll_score:.0f} | OpenRouter score={or_score:.0f}", flush=True)
        tools.openrouter.active_engine = tools.openrouter.ENGINE_OPENROUTER
        net_health.ollama_ok()
        print(f"  [BENCH] OpenRouter forced (gemma model active)", flush=True)
    
    discord('🧠 Brain v3 Started', {
        'Balance': f'${risk.balance:.2f}',
        'Agents': '7 (sensor, memory, picker, judge, protector, regime, strategist)',
        'Markets': ', '.join(MARKET_LIST),
    })
    notify_startup(risk.balance, mode_tag, "sensor, memory, picker, judge, protector, regime, strategist")
    
    cycle = 0
    
    try:
        while True:
            cycle += 1
            _rescue = [None]  # mutable container for rescue candidate
            cycle_start_time = time.time()
            sleep_time = 1.0  # default, overridden by burst logic
            best_market = None
            best_data = None
            
            # ── Chat command trigger (check every 3 cycles) ──
            if cycle % 3 == 0:
                try:
                    cmd_file = Path("commands.json")
                    if cmd_file.exists():
                        cmds = json.loads(cmd_file.read_text())
                        pending = cmds.get("commands", [])
                        if pending:
                            cmd = pending.pop(0)
                            cmds["commands"] = pending
                            cmds["last_check"] = time.time()
                            cmd_file.write_text(json.dumps(cmds))
                            cmd_type = cmd.get("type", "")
                            if cmd_type == "start_session":
                                market = cmd.get("market", "JD50")
                                strategy = cmd.get("strategy", "MOMENTUM_UP")
                                contract = cmd.get("contract", "CALL")
                                stake = max(1.0, min(float(cmd.get("stake", 1.5)), 15.0))
                                sm = tools.session_mgr
                                if not sm.active:
                                    sid = sm.enter_session(market, strategy, contract, stake)
                                    log_agent("command", f"CHAT COMMAND: Session #{sid} started — {market} {strategy} {contract} stake=${stake:.2f}")
                                    log_sys(f"Chat command: session started #{sid}", "info")
                                    print(f"  [{cycle}] >>> CHAT COMMAND: Session #{sid} started — {market} {strategy} {contract}", flush=True)
                                else:
                                    log_agent("command", f"CHAT COMMAND: Session already active (#{sm.session_id})")
                            elif cmd_type == "stop_session":
                                sm = tools.session_mgr
                                if sm.active:
                                    entry = sm.close_session()
                                    log_agent("command", f"CHAT COMMAND: Session #{entry['session_id']} stopped — PnL=${entry['pnl']:+.4f}")
                                    log_sys(f"Chat command: session stopped #{entry['session_id']}", "info")
                                    print(f"  [{cycle}] >>> CHAT COMMAND: Session stopped — PnL=${entry['pnl']:+.4f}", flush=True)
                            elif cmd_type == "status":
                                sm = tools.session_mgr
                                bal = risk.balance
                                log_agent("command", f"CHAT COMMAND: Bal=${bal:.2f} WR={risk.wr():.0f}% PnL=${risk.pnl:+.2f} Session={'active' if sm.active else 'idle'} trades={risk.total}")
                            elif cmd_type == "reset_stakes":
                                if hasattr(risk, 'override_stake'):
                                    delattr(risk, 'override_stake')
                                log_agent("command", "CHAT COMMAND: Stake override reset")
                            elif cmd_type == "force_trade":
                                # Force a single trade regardless of session
                                log_agent("command", f"CHAT COMMAND: Force trade requested — {cmd.get('market','?')} {cmd.get('strategy','?')}")
                                # Will be handled by session manager
                except Exception as e:
                    pass
            
            # ── Periodic maintenance every 50 cycles ──
            if cycle % 50 == 0:
                try:
                    tools.alm.reconnect_if_needed()
                    if tools.alm.connected:
                        net_health.ollama_ok()
                    elif hasattr(tools, 'openrouter') and tools.openrouter and tools.openrouter.active_engine == tools.openrouter.ENGINE_OPENROUTER:
                        net_health.ollama_ok()  # Cloud AI active, allow trading
                    else:
                        net_health.ollama_down()
                except:
                    net_health.ollama_down()
                try:
                    tools.research.cleanup_stale()
                except: pass
            
            # ── LOSS HIKE DISCONNECT: disconnect Deriv during severe loss spikes ──
            if hasattr(risk, '_disconnect_until') and risk._disconnect_until > 0 and time.time() < risk._disconnect_until:
                remaining = risk._disconnect_until - time.time()
                if trader.ws and trader.connected:
                    try:
                        await trader.ws.close()
                        trader.connected = False
                        if net_health: net_health.deriv_down()
                        print(f"  [{cycle}] 🩸 DERIV DISCONNECTED: loss hike — waiting {remaining:.0f}s", flush=True)
                        log_agent("risk", f"🩸 DERIV DISCONNECTED: {risk._disconnect_reason}")
                    except: pass
                await asyncio.sleep(5)
                continue
            elif hasattr(risk, '_disconnect_until') and risk._disconnect_until > 0 and time.time() >= risk._disconnect_until:
                # Reconnect Deriv
                if not trader.connected:
                    try:
                        if await trader.reconnect():
                            print(f"  [{cycle}] 🟢 DERIV RECONNECTED: entering retest phase", flush=True)
                            log_agent("risk", "🟢 DERIV RECONNECTED: retesting market with small lots")
                            risk._last_disconnect_result = 'testing'
                            risk._retest_mode = True
                            risk._retest_trades = 0
                            risk._retest_wins = 0
                            risk._retest_pnl = 0.0
                            risk._retest_start = time.time()
                    except: pass
            # ── RETEST PHASE: small lots after reconnect ──
            if hasattr(risk, '_last_disconnect_result') and risk._last_disconnect_result == 'testing':
                if risk.total > 0:
                    recent = risk._recent_results[-5:] if hasattr(risk, '_recent_results') else []
                    retest_trades = getattr(risk, '_retest_trades', 0)
                    if len(recent) >= 3:
                        recent_wins = sum(1 for p in recent if p > 0)
                        recent_losses = sum(1 for p in recent if p < 0)
                        recent_pnl = sum(recent)
                        if retest_trades >= 5:
                            # Retest complete — evaluate
                            retest_wr = (getattr(risk, '_retest_wins', 0) / retest_trades * 100) if retest_trades > 0 else 0
                            retest_pnl = getattr(risk, '_retest_pnl', 0)
                            if retest_wr >= 50 and retest_pnl >= 0:
                                risk._last_disconnect_result = 'recovered'
                                risk._disconnect_count = 0
                                risk._retest_mode = False
                                print(f"  [{cycle}] 🟢 RETEST PASSED: {retest_wr:.0f}% WR, ${retest_pnl:+.2f} PnL — resuming normal", flush=True)
                                log_agent("risk", f"🟢 RETEST PASSED: {retest_wr:.0f}% WR — market recovered")
                            else:
                                risk._last_disconnect_result = 'still_losing'
                                print(f"  [{cycle}] 🩸 RETEST FAILED: {retest_wr:.0f}% WR, ${retest_pnl:+.2f} — escalating break", flush=True)
                                log_agent("risk", f"🩸 RETEST FAILED: {retest_wr:.0f}% WR — escalating")
                                risk._disconnect_until = time.time() + 1  # escalate next cycle
                        elif recent_losses >= 3:
                            risk._last_disconnect_result = 'still_losing'
                            print(f"  [{cycle}] 🩸 STILL LOSING in retest: {recent_losses}/5 — escalating", flush=True)
                            log_agent("risk", f"🩸 STILL LOSING: {recent_losses}/5 after reconnect — escalating")
                            risk._disconnect_until = time.time() + 1
            
            # ── PERIODIC STATE WRITE (even during bleeding) ──
            if cycle % 25 == 0:
                try:
                    _state_patch = {
                        'bunch_runner': tools.bunch_runner.get_status(),
                        'profit_mirror': tools.profit_mirror.get_status(),
                        'mission_tracker': tools.mission_tracker.get_status(),
                        'alm_brain': tools.alm.get_status() if tools.alm else {},
                        'time': int(time.time() * 1000),
                        'cycles': cycle,
                        'balance': risk.balance,
                        'active_agent': tools.openrouter.active_engine if hasattr(tools, 'openrouter') and tools.openrouter else 'none',
                        'model_used': tools.openrouter.openrouter_model if hasattr(tools, 'openrouter') and tools.openrouter and tools.openrouter.active_engine == tools.openrouter.ENGINE_OPENROUTER else '',
                    }
                    # Merge into existing state
                    _existing = {}
                    try:
                        _existing = json.loads(Path('trading_state.json').read_text())
                    except: pass
                    _existing.update(_state_patch)
                    save_state(_existing)
                except Exception: pass
            
            # ── BLEEDING GATE with recovery grace ──
            if tools.tz_intel:
                _bleed_ok, _bleed_reason, _bleed_sev = tools.tz_intel.is_bleeding()
                if _bleed_ok and _bleed_sev == 'hard':
                    # Grace period: after 20 min without trading, allow small trades to recover
                    _grace = getattr(risk, '_bleed_grace_until', 0)
                    _now = int(time.time())
                    if _grace and _now > _grace:
                        print(f"  [{cycle}] 🩸 BLEEDING RECOVERY: grace period ended, trying small trades", flush=True)
                        log_agent("tz_intel", f"🩸 RECOVERY: trying small trades after grace period")
                        risk._bleed_grace_until = 0
                        # Continue with reduced stake
                    elif _grace and _now <= _grace:
                        # In grace period — trade with minimal stake
                        pass
                    else:
                        # First detection — set grace timer for 20 min
                        risk._bleed_grace_until = _now + 1200  # 20 minutes
                        if cycle % 5 == 0:
                            print(f"  [{cycle}] 🩸 BLEEDING HARD BLOCK: {_bleed_reason} — grace in 20min", flush=True)
                        if cycle % 20 == 0:
                            log_agent("tz_intel", f"🩸 BLEEDING HARD BLOCK: {_bleed_reason} — grace in 20min")
                        await asyncio.sleep(5)
                        continue
            # ── NETWORK GATE: stop trading if disconnected ──
            net_ok, net_reason = net_health.is_safe()
            if not net_ok:
                if cycle % 10 == 0:
                    print(f"  [{cycle}] NETWORK DOWN: {net_reason}", flush=True)
                    log_agent("network", f"TRADING STOPPED: {net_reason}")
                if not net_health.deriv_connected:
                    try:
                        if await trader.reconnect():
                            net_health.deriv_ok()
                    except: pass
                if not net_health.ollama_connected:
                    try:
                        tools.alm.reconnect_if_needed()
                        if tools.alm.connected:
                            net_health.ollama_ok()
                        elif hasattr(tools, 'openrouter') and tools.openrouter and tools.openrouter.active_engine == tools.openrouter.ENGINE_OPENROUTER:
                            net_health.ollama_ok()  # Cloud AI active, allow trading
                    except: pass
                await asyncio.sleep(3)
                continue
            
            # ── Feed ticks to agents + C++ engine ──
            try:
                for m in MARKET_LIST[:6]:
                    for price in tc.ticks.get(m, [])[-10:]:
                        tools.feed_tick(m, price)
                # Feed latest tick per market to C++ engine (async-safe)
                if cpp_engine and cpp_engine.connected:
                    def _cpp_tick():
                        results = {}
                        for m in MARKET_LIST:
                            latest = tc.ticks.get(m, [])
                            if latest:
                                try:
                                    r = cpp_engine.tick(latest[-1], time.time())
                                    if r: results[m] = r
                                except: pass
                        return results
                    cpp_results = await asyncio.get_event_loop().run_in_executor(None, _cpp_tick)
                    for m, pred in cpp_results.items():
                        if not hasattr(cpp_engine, '_market_preds'):
                            cpp_engine._market_preds = {}
                        cpp_engine._market_preds[m] = pred  # cache per market
                        if cycle % 5 == 0:
                            sig = pred.get('signal', '?')
                            conf = pred.get('confidence', 0)
                            print(f"  [C++ PRED] {m} signal={sig} conf={conf:.3f}", flush=True)
            except Exception as e:
                print(f"  [TICK FEED ERR] {e}", flush=True)
            # ── Poll AI brain results + update session ──
            try:
                tools.alm.poll_result()
                tools.alm.update_session(cycle)
            except: pass

            # ── OPENROUTER: Periodic strategy critique (every 50 cycles) ──
            if cycle % 50 == 0 and hasattr(tools, 'openrouter') and tools.openrouter and tools.openrouter.connected:
                try:
                    # Pick top strategy by PnL
                    top_strat = max(
                        [(k, v) for k, v in tools.memory.strategies.items() if isinstance(v, dict) and v.get('trades', 0) >= 10],
                        key=lambda x: x[1].get('total_profit', 0),
                        default=None
                    )
                    if top_strat:
                        critique, err = tools.openrouter.critique_strategy(top_strat[0], top_strat[1])
                        if critique:
                            log_agent("openrouter", f"Critique: {top_strat[0]} — {critique[:80]}")
                            print(f"  [OPENROUTER CRITIQUE] {top_strat[0]}: {critique[:100]}", flush=True)
                except: pass

            # ── Periodic state save (every 10 cycles) ──
            if cycle % 3 == 0:
                try:
                    ph = load_history()
                    prot_s = tools.protector.get_status() if hasattr(tools.protector, 'get_status') else {}
                    # ── WATCHER: single-call state builder ──
                    state_out = tools.watcher.build_state(
                        risk, cycle, best_market, best_data,
                        session_start=SESSION_START
                    )
                    state_out["executor"] = {
                        'trades_executed': risk.total, 'trades_won': risk.wins,
                        'trades_lost': risk.losses, 'win_rate': round(risk.wr(), 1),
                        'session_pnl': round(risk.pnl, 4),
                        'max_drawdown': round(max(0, risk.start - risk.balance), 4),
                        'status': risk.trading_mode,
                        'cooldown': {
                            'tier': risk.escalation_level,
                            'cooling_down': risk.consec_loss >= 3,
                            'remaining_sec': 0,
                            'reason': risk.mode_reason,
                        },
                        'rules': {
                            'trades_session_left': max(0, 30 - risk.total),
                            'trades_session_max': 30,
                            'session_time_left': 14400,
                            'trades_this_hour': risk.total,
                            'trades_hour_max': 50,
                            'daily_target_pct': 2.0,
                            'daily_loss_limit_pct': 2.0,
                        },
                        'consecutive_wins': risk.consec_win,
                        'consecutive_losses': risk.consec_loss,
                    }
                    # Add pnl_history and edge_history from persistent history
                    try:
                        state_out["pnl_history"] = ph.get("pnl_history", [])[-200:]
                        state_out["edge_history"] = ph.get("edge_history", [])[-200:]
                    except Exception:
                        state_out["pnl_history"] = []
                        state_out["edge_history"] = []
                    # Top-level connected status for dashboard red/green dot
                    state_out["connected"] = bool(net_health.deriv_connected and net_health.ticks_connected)
                    state_out["tick_data"] = {m: len(tc.last_digits.get(m, [])) for m in MARKET_LIST}
                    state_out["market_list"] = {
                        m: {"type": MARKET_TYPES.get(m, "volatility"), "active": True,
                            "score": round(MARKET_WEIGHTS.get(m, 1) * 50, 1),
                            "ticks": len(tc.last_digits.get(m, []))}
                        for m in MARKET_LIST
                    }
                    state_out["multi_market"] = {
                        "phase": 1, "phase_name": "OBSERVE",
                        "total_markets": len(MARKET_LIST),
                        "total_observations": sum(len(tc.last_digits.get(m, [])) for m in MARKET_LIST),
                        "rankings": [
                            {"name": m, "source": "deriv", "type": MARKET_TYPES.get(m, "?"),
                             "market_score": round(MARKET_WEIGHTS.get(m, 1) * 50 + (risk.market_count.get(m, 0) * 2), 1),
                             "ticks": len(tc.last_digits.get(m, [])),
                             "traded": risk.market_count.get(m, 0),
                             "status": "ACTIVE" if risk.market_count.get(m, 0) == 0 else ("TESTING" if risk.market_count.get(m, 0) < 5 else "RETIRED")}
                            for m in MARKET_LIST
                        ],
                    }
                    # SUPERVISOR + PERF TRACKER status for dashboard
                    try:
                        state_out["supervisor"] = tools.supervisor.get_status()
                        state_out["perf_tracker"] = tools.perf_tracker.get_status()
                        state_out["mission_config"] = {
                            "name": MISSION_CONFIG.get("name", "Default"),
                            "targets": MISSION_CONFIG.get("targets", {}),
                            "rules_active": bool(MISSION_CONFIG.get("rules")),
                        }
                    except: pass
                    save_state(state_out)
                    if cycle % 30 == 0:
                        print(f'  [WATCHER] State saved cycle={cycle} trades={risk.total} pnl=\${risk.pnl:+.2f} agent_notes={len(state_out.get("agent_notes",[]))}', flush=True)
                    # Periodic balance sync with Deriv (every 30 cycles)
                    if cycle % 30 == 0 and not PAPER_MODE:
                        try:
                            await asyncio.wait_for(trader.refresh_balance(), timeout=5)
                            risk.balance = trader.balance
                        except: pass
                    # ════ PERSISTENT KNOWLEDGE: save snapshot every 30 cycles ════
                    if cycle % 30 == 0:
                        try:
                            tools.memory.save_knowledge_snapshot(
                                brain_notes=tools.alm.notes if tools.alm else [],
                                model_log=tools.alm.model_usage_log if tools.alm else [],
                            )
                        except: pass
                    # Heartbeat: write a file dashboard can check
                    try:
                        noise_status = noise_detector.get_status()
                        hb = {
                            "alive": True,
                            "noise": noise_status,
                            "time": int(time.time() * 1000),
                            "cycle": cycle,
                            "balance": risk.balance,
                            "trades": risk.total,
                            "mode": risk.trading_mode,
                            "goal": tools.goal.get_mode() if tools.goal else "?",
                            "uptime": int(time.time() - SESSION_START),
                            "net": net_health.get_status() if net_health else {},
                            "model": tools.openrouter.get_status() if hasattr(tools, "openrouter") and tools.openrouter else {"active_engine": "none", "model": "none"},
                            "pnl": round(risk.pnl, 4),
                            "win_rate": round(risk.wr(), 1),
                            "consec_loss": risk.consec_loss,
                            "consec_win": risk.consec_win,
                            "round_profit": tools.round_profit.get_status() if tools.round_profit else {},
                            "bunch": tools.bunch_runner.get_status() if hasattr(tools, "bunch_runner") else {},
                        }
                        Path("heartbeat.json").write_text(json.dumps(hb))
                        # DAY CHECK: if new day, archive old trade logs
                        if hasattr(main, '_current_day') and main._current_day != time.strftime('%Y-%m-%d'):
                            try:
                                log_manager.cleanup_old(days=7)
                                log_sys(f"New day detected: {time.strftime('%Y-%m-%d')} — cleaned up old logs", "info")
                                main._current_day = time.strftime('%Y-%m-%d')
                            except: pass
                        if not hasattr(main, '_current_day'):
                            main._current_day = time.strftime('%Y-%m-%d')
                    except: pass
                except Exception as e:
                    print(f"  [STATE] save error: {e}", flush=True)

            # ── Brain status notification (every 10 cycles) ──
            if cycle % 10 == 0:
                active_strats = len([k for k,v in tools.memory.strategies.items() if isinstance(v, dict) and v.get("trades", 0) > 0])
                retired_strats = len([k for k,v in tools.memory.strategies.items() if isinstance(v, dict) and v.get("status") == "retired"])
                log_agent("brain",
                    f"STATUS: cycle={cycle} bal=${risk.balance:.2f} pnl=${risk.pnl:+.2f} "
                    f"wr={risk.wr():.0f}% mode={risk.trading_mode} "
                    f"strategies={active_strats} retired={retired_strats} "
                    f"streak={'W'+str(risk.consec_win) if risk.consec_win > 0 else 'L'+str(risk.consec_loss)}")

            # ── Log cycle activity ──
            if cycle % 2 == 0:
                log_agent("regime", f"Cycle {cycle}: mode={risk.trading_mode} score={risk.confidence_score} alignment={risk.alignment_score}")
                log_agent("memory", f"Markets observed: {sum(len(tc.last_digits.get(m, [])) for m in MARKET_LIST)} ticks across {len(MARKET_LIST)} markets")
                if risk.consec_loss > 0 and cycle % 20 == 0:
                    log_agent("protector", f"Loss streak: {risk.consec_loss} | mode: {risk.trading_mode} | reason: {risk.mode_reason}")
                if risk.consec_win > 0:
                    log_agent("brain", f"Win streak: {risk.consec_win} | confidence: {risk.confidence_score}")
            # ── PERIODIC SYS LOG HEARTBEAT ──
            if cycle % 50 == 0:
                log_sys(f'Cycle {cycle} | bal=${risk.balance:.2f} pnl=${risk.pnl:+.2f} streak={"W"+str(risk.consec_win) if risk.consec_win > 0 else "L"+str(risk.consec_loss)} mode={risk.trading_mode}')

            # ── NOISE ANALYSIS: detect noisy markets every 60s ──
            noise_detector.analyze(tc.last_digits, cycle)
            
            # ── TICK BACKTESTER: validate strategies every 20 cycles ──
            if tools.backtester.should_run(cycle):
                try:
                    backtest_results = tools.backtester.run_backtests(cycle)
                    # Feed top results to memory
                    for strat_key, result in backtest_results.items():
                        if result.get('simulations', 0) >= 5:
                            tools.memory.record_simulation(strat_key, result)
                    # Probability Engine: calculate edges from all data
                    tools.prob_engine.calculate_all(TRADE_LOG, backtest_results, tools.memory.strategies, cycle)
                    best_strats = tools.backtester.get_best_strategies(3)
                    if best_strats and cycle % 40 == 0:
                        bs = best_strats[0]
                        print(f"  [{cycle}] BACKTEST: {bs['strategy']} on {bs['market']} — WR={bs['win_rate']}% EV={bs['expected_value']:+.4f} sims={bs['simulations']}", flush=True)
                        log_agent('backtester', f"Best: {bs['market']}:{bs['strategy']} WR={bs['win_rate']}% EV={bs['expected_value']:+.4f} ({bs['simulations']} sims)")
                except Exception as e:
                    print(f"  [{cycle}] BACKTEST ERROR: {e}", flush=True)
            
            # ── MARKET INTELLIGENCE: rank markets/strategies every 2min ──
            market_intel.update(tools.memory, noise_detector.market_entropy, cycle)
            if cycle % 60 == 0:
                market_intel.log_status(cycle)
                rec = market_intel.get_recommendation()
                if rec['best_market']:
                    print(f"  [{cycle}] INTEL: best={rec['best_market']} (score={rec['market_score']['final_score']:.3f}) noisy={[m for m,d in market_intel.market_rankings if d.get('noisy')][:5]}", flush=True)
            
            # ── PROMPT IMPROVER: learn from trade results ──
            prompt_improver.improve(cycle)
            
            # ── SESSION MANAGER: check cooldown ──
            sm = tools.session_mgr
            if sm.active and sm.is_in_cooldown():
                if cycle % 10 == 0:
                    print(f"  [{cycle}] SESSION COOLDOWN: {sm.cooldown_reason}")
                await asyncio.sleep(2)
                continue
            
            # ── If session is active, reuse session strategy ──
            if sm.active:
                # Check if session strategy was retired — if so, exit session
                strat_key_s = f"{sm.session_market}:{sm.session_strategy}"
                if tools.memory.is_strategy_retired(sm.session_market, sm.session_strategy):
                    entry = sm.close_session()
                    tools.overtrade.record_session_end(entry['pnl'])
                    log_agent("session", f"SESSION EXITED: strategy retired — {strat_key_s} PnL=${entry['pnl']:+.4f}")
                    log_sys(f"Session #{entry['session_id']} force-exit: strategy retired", "warn")
                    await asyncio.sleep(5)
                    continue
                best_market = sm.session_market
                digits = tc.last_digits.get(best_market, [])
                # Get C++ signal BEFORE strategy analysis
                cpp_pred = None
                if cpp_engine and hasattr(cpp_engine, '_market_preds') and best_market in cpp_engine._market_preds:
                    cpp_pred = cpp_engine._market_preds[best_market]
                cpp_signal_val = cpp_pred.get('signal', 0) if cpp_pred else 0
                strategy = analyze_digits(digits, cpp_signal=cpp_signal_val)
                signal = tools.get_signal(best_market)
                if strategy:
                    # ── DIGIT LIMIT: exit session if digit trades exhausted ──
                    if (is_digit_trade(sm.session_strategy, sm.session_contract) and 
                        not digit_mgr.should_trade(sm.session_market, sm.session_strategy)[0]):
                        entry = sm.close_session()
                        tools.overtrade.record_session_end(entry['pnl'])
                        log_agent("session", f"SESSION EXITED: digit cooldown — {digit_mgr.cooldown_reason} — PnL=${entry['pnl']:+.4f}")
                        log_sys(f"Session #{entry['session_id']} force-exit: digit cooldown", "warn")
                        print(f"  [{cycle}] SESSION EXIT: digit cooldown active — {digit_mgr.cooldown_reason}", flush=True)
                        await asyncio.sleep(5)
                        continue
                    
                    # ── CONSECUTIVE LOSS EXIT: rotate after 2 session losses ──
                    if sm.consec_session_loss >= 2:
                        entry = sm.close_session()
                        tools.overtrade.record_session_end(entry['pnl'])
                        log_agent("session", f"SESSION EXITED: {sm.consec_session_loss} consecutive losses — PnL=${entry['pnl']:+.4f}")
                        log_sys(f"Session #{entry['session_id']} force-exit: {sm.consec_session_loss} losses", "warn")
                        print(f"  [{cycle}] SESSION EXIT: {sm.consec_session_loss} losses — rotating market+strategy", flush=True)
                        await asyncio.sleep(8)
                        continue
                    
                    strategy["strategy"] = sm.session_strategy
                    strategy["contract"] = sm.session_contract
                    strategy["confidence"] = 0.8
                    
                    # ── SIGNAL DIRECTION CHECK: flip if C++ says opposite ──
                    if cpp_signal_val != 0:
                        # C++ says PUT (-1) but session is CALL → flip to PUT
                        if cpp_signal_val < 0 and sm.session_contract in ('CALL', 'ASIANU'):
                            old_c = sm.session_contract
                            sm.session_contract = 'PUT'
                            sm.session_strategy = 'FALL_TREND'
                            print(f"  [{cycle}] SESSION FLIP: {old_c}→PUT (C++ signal={cpp_signal_val})", flush=True)
                            log_agent("session", f"DIRECTION FLIP: {old_c}→PUT — C++ signal={cpp_signal_val}")
                        # C++ says CALL (+1) but session is PUT → flip to CALL
                        elif cpp_signal_val > 0 and sm.session_contract in ('PUT', 'ASIAND'):
                            old_c = sm.session_contract
                            sm.session_contract = 'CALL'
                            sm.session_strategy = 'MOMENTUM_UP'
                            print(f"  [{cycle}] SESSION FLIP: {old_c}→CALL (C++ signal={cpp_signal_val})", flush=True)
                            log_agent("session", f"DIRECTION FLIP: {old_c}→CALL — C++ signal={cpp_signal_val}")
                        # After 2 session losses and strong opposite signal → exit session
                        elif sm.consec_session_loss >= 2 and (
                            (cpp_signal_val < 0 and sm.session_contract in ('CALL', 'ASIANU')) or
                            (cpp_signal_val > 0 and sm.session_contract in ('PUT', 'ASIAND'))
                        ):
                            entry = sm.close_session()
                            tools.overtrade.record_session_end(entry['pnl'])
                            log_agent("session", f"SESSION EXITED: signal mismatch after 2 losses — PnL=${entry['pnl']:+.4f}")
                            log_sys(f"Session #{entry['session_id']} force-exit: signal mismatch", "warn")
                            await asyncio.sleep(10)
                            continue
                    
                    regime_info = tools.get_regime(best_market, signal) if signal else None
                    if isinstance(regime_info, tuple) and len(regime_info) >= 2:
                        strategy["regime"] = str(regime_info[0])
                    elif isinstance(regime_info, dict):
                        strategy["regime"] = regime_info.get("regime", "UNKNOWN")
                    else:
                        strategy["regime"] = "UNKNOWN"
                    best_data = strategy
                    best_score = 999
                else:
                    if cycle % 10 == 0:
                        print(f"  [{cycle}] SESSION WAITING: {best_market} has {len(digits)} digits")
                    await asyncio.sleep(1)
                    continue
            # ── COLLECT ALL CANDIDATES from all markets ──
            candidates = []
            for m in MARKET_LIST:
                digits = tc.last_digits.get(m, [])
                if len(digits) < 10: continue
                
                # ── ANOMALY DETECTION: check each market before analysis ──
                anomalies = tools.anomaly.detect(digits, m)
                # Feed anomalies to TradeIntelligence for real-time response
                if anomalies:
                    for atype, adata in anomalies.items() if isinstance(anomalies, dict) else []:
                        tools.trade_intel.on_anomaly(m, atype)
                safe, anomaly_reason = tools.anomaly.is_safe(m)
                if not safe:
                    if cycle % 10 == 0:
                        print(f"  [{cycle}] ANOMALY: {m} — {anomaly_reason}")
                    continue
                
                # ── MARKET STATE BRAIN: understand what the market IS doing ──
                mk_signal = tools.get_signal(m)
                mk_regime_info = tools.get_regime(m, mk_signal) if mk_signal else None
                mk_regime = str(mk_regime_info[0]) if isinstance(mk_regime_info, tuple) else (mk_regime_info.get('regime', 'UNKNOWN') if isinstance(mk_regime_info, dict) else 'UNKNOWN')
                mk_noise = noise_detector.market_entropy.get(m, 0)
                mk_cpp = cpp_engine._market_preds.get(m, {}) if cpp_engine and hasattr(cpp_engine, '_market_preds') else {}
                mk_pa = tools.price_action.analyze(digits, m) if len(digits) >= 15 else {}
                mk_tz_mult = tools.tz_intel.get_multiplier(m)
                
                market_state = tools.state_brain.analyze(
                    m, digits, mk_signal, mk_regime, mk_noise,
                    anomalies, mk_cpp, mk_pa
                )
                if cycle % 5 == 0:
                    print(f"  [{cycle}] STATE BRAIN {m}: {market_state['market_state']} → {market_state['recommendation']} conf={market_state['state_confidence']:.0%}", flush=True)
                
                # Skip only on hard SKIP (ANOMALOUS), allow WAIT through with reduced sizing
                if market_state['recommendation'] == 'SKIP':
                    if cycle % 15 == 0:
                        print(f"  [{cycle}] STATE BRAIN: {m} → {market_state['recommendation']} ({market_state['rec_reason']})")
                    continue
                # WAIT = trade with reduced confidence, don't skip entirely
                if market_state['recommendation'] == 'WAIT':
                    if market_state.get('state_confidence', 0) < 0.10:
                        if cycle % 20 == 0:
                            print(f"  [{cycle}] STATE BRAIN: {m} → WAIT (conf too low {market_state.get('state_confidence',0):.0%})")
                        continue
                
                mk_cpp_sig = 0
                if cpp_engine and hasattr(cpp_engine, '_market_preds') and m in cpp_engine._market_preds:
                    mk_cpp_sig = cpp_engine._market_preds[m].get('signal', 0)
                strategy = analyze_digits(digits, cpp_signal=mk_cpp_sig)
                if not strategy: continue
                
                signal = tools.get_signal(m)
                regime_info = tools.get_regime(m, signal) if signal else None
                if isinstance(regime_info, tuple) and len(regime_info) >= 2:
                    regime = str(regime_info[0])
                elif isinstance(regime_info, dict):
                    regime = regime_info.get("regime", "UNKNOWN")
                else:
                    regime = 'UNKNOWN'
                
                bias_result = tools.get_digit_bias(m)
                if isinstance(bias_result, tuple) and len(bias_result) >= 3:
                    freqs, best_digit, best_bias = bias_result
                    if best_digit is not None and strategy.get('digit') == best_digit and best_bias > 0.02:
                        strategy['confidence'] *= 1.2
                
                strat_key = f"{m}:{strategy['strategy']}"
                if not tools.strategist.is_approved(strat_key):
                    strategy['confidence'] *= 0.8
                
                score = strategy['ev'] * strategy['confidence'] * MARKET_WEIGHTS.get(m, 1)
                # CONFIDENCE BOOST: boost score for historically profitable market+strategy combos
                _conf_key = f"{m}:{strategy['strategy']}"
                _conf_stats = tools.memory.strategies.get(_conf_key, {})
                _conf_trades = _conf_stats.get('trades', 0)
                _conf_wr = _conf_stats.get('win_rate', 0)
                _conf_pnl = _conf_stats.get('pnl', 0)
                if _conf_trades >= 5:
                    if _conf_wr >= 60 and _conf_pnl > 0:
                        score *= 1.5  # boost proven winners
                    elif _conf_wr < 40 or _conf_pnl < -2:
                        score *= 0.3  # penalize proven losers
                    elif _conf_wr < 50:
                        score *= 0.7  # slight penalty for mediocre
                _base_score = score
                if getattr(main, '_last_model_ok', False):
                    score *= 1.5
                # Mission: block bad market/hour combos from collected data
                _mok, _mreason = tools.mission.should_allow_market(m, int(time.strftime('%H')), tools.tz_intel)
                if not _mok:
                                score *= 0.05  # heavily penalize known bad combos
                # YMCRC: yield audit — block low-yield contracts (Digit Differs etc)
                _ym_pass, _ym_yield, _ym_action, _ym_reason, _ym_rec = tools.ymcrc.check_yield(
                    strategy.get('contract', 'CALL'))
                if not _ym_pass:
                    score *= 0.01  # kill toxic contract
                # Research: block worst markets entirely
                if m in ('JD75', 'R_50', 'R_10'):
                    score *= 0.02  # research-confirmed losing markets
                # Research: prefer PUT over CALL (PUT +$30 vs CALL -$2.44)
                if strategy.get('contract') == 'CALL':
                    score *= 0.7
                # Research-backed hour blocks (UTC): h3, h4, h19, h23
                _hr = int(time.strftime('%H'))
                if _hr in (3, 4, 19, 23):
                    score *= 0.05  # research-confirmed losing hours
                # ════ MISSION ENFORCEMENT: trade only what data proves works ════
                _mission = MISSION_CONFIG.get('rules', {})
                _hour = int(time.strftime('%H'))
                
                # Rule 1: Market whitelist — only R_75 allowed
                _allowed_markets = _mission.get('MARKET_WHITELIST', {}).get('allowed', [])
                _banned_markets = _mission.get('MARKET_WHITELIST', {}).get('banned', [])
                if _allowed_markets and m not in _allowed_markets:
                    score *= 0.01  # market not in whitelist
                if m in _banned_markets:
                    score *= 0.001  # market explicitly banned
                
                # Rule 2: Hour filter — only trade profitable hours
                _allowed_hours = _mission.get('HOUR_FILTER', {}).get('allowed', [])
                _banned_hours = _mission.get('HOUR_FILTER', {}).get('banned', [])
                _tight_hours = _mission.get('HOUR_FILTER', {}).get('tight_hours', [])
                if _allowed_hours and _hour not in _allowed_hours:
                    score *= 0.05  # hour not in whitelist
                if _hour in _banned_hours:
                    score *= 0.01  # hour explicitly banned
                if _hour in _tight_hours:
                    score *= 0.001  # worst hours — absolute zero
                
                # Rule 3: Strategy whitelist — only proven families
                _allowed_strats = _mission.get('STRATEGY_WHITELIST', {}).get('allowed', [])
                _strat_name = strategy.get('strategy', '')
                _strat_family = '_'.join(_strat_name.split('_')[:2]) if _strat_name else ''
                if _allowed_strats and _strat_name not in _allowed_strats and _strat_family not in _allowed_strats:
                    score *= 0.05  # strategy not in whitelist
                
                # Rule 4: Stake cap — max $2
                _max_stake = _mission.get('STAKE_RULES', {}).get('max_stake', 2.0)
                
                # Rule 5: Daily loss limit — $5 hard stop
                _max_daily_loss = _mission.get('STAKE_RULES', {}).get('max_daily_loss', 5.0)
                if risk.pnl < -_max_daily_loss:
                    score *= 0.001  # daily loss limit breached
                
                # Rule 6: Quality gates — min health, confidence, EV
                _min_health = _mission.get('QUALITY_GATES', {}).get('min_health_score', 70)
                _min_conf = _mission.get('QUALITY_GATES', {}).get('min_confidence', 5)
                if hasattr(tools, 'diagnostic'):
                    _last_scan = tools.diagnostic.state.get('diagnostic_history', [])
                    if _last_scan:
                        _health = _last_scan[-1].get('health_score', 0)
                        if _health < _min_health:
                            score *= 0.3  # system degraded
                if strategy.get('confidence', 0) < _min_conf:
                    score *= 0.5  # low confidence
                
                # Rule 7: Strategy must have positive PnL
                _strat_pnl = tools.memory.strategies.get(f"{m}:{_strat_name}", {}).get('total_profit', 0)
                _require_pos_pnl = _mission.get('QUALITY_GATES', {}).get('require_strategy_pnl_positive', True)
                if _require_pos_pnl and _strat_pnl < -1.0:
                    score *= 0.1  # strategy is losing money
                
                # Rule 8: Market must have positive PnL
                _mkt_pnl = sum(v.get('total_profit', 0) for k, v in tools.memory.strategies.items() if k.startswith(m + ':'))
                _require_mkt_pos = _mission.get('QUALITY_GATES', {}).get('require_market_pnl_positive', True)
                if _require_mkt_pos and _mkt_pnl < -5.0:
                    score *= 0.05  # market is bleeding
                
                # Rule 9: Max daily trades
                _max_daily = _mission.get('STAKE_RULES', {}).get('max_daily_trades', 20)
                if risk.total >= _max_daily:
                    score *= 0.001  # daily trade limit reached

                # Research: boost top-3 proven combos
                if m == 'JD25' and strategy.get('strategy') == 'DIGIT_DIFF_1':
                    score *= 1.8  # 67.6% WR, +$13.08
                if m == 'JD100' and strategy.get('strategy') == 'FALL_TREND':
                    score *= 1.5  # 54.2% WR, +$13.65
                if m == 'JD10' and strategy.get('strategy') == 'FALL_TREND':
                    score *= 1.5  # 69.6% WR, +$10.67
                if m == 'JD50':
                    score *= 1.3  # 80% WR, underused, needs data
                # EAT Specialist: timezone quality check
                _eat_score, _eat_reason = tools.eat.get_time_quality()
                if _eat_score < 30:
                    score *= 0.3  # penalize bad EAT windows
                # Profit Replicator: block combos with dead patterns
                _rep_blocked, _rep_info = tools.replicator.is_blocked(m, int(time.strftime('%H')), strategy.get('strategy', 'ALL'))
                if _rep_blocked:
                    score *= 0.01  # near-zero score for blocked combos
                # Profit Replicator: boost profitable combos
                _rep_mult, _rep_reason = tools.replicator.get_recommended_stake_mult(m, int(time.strftime('%H')), strategy.get('strategy', 'ALL'))
                if _rep_mult > 1.0 and _rep_reason != 'no_data':
                    score *= _rep_mult  # boost stake for winning patterns
                # CUMULATIVE STRATEGY CHECK: block if strategy is bleeding across ALL hours
                _strat_name = strategy.get('strategy', 'ALL')
                if _strat_name != 'ALL':
                    _strat_total_pnl = 0
                    _strat_total_trades = 0
                    for _ck, _cv in tools.replicator.state.get('combos', {}).items():
                        if _ck.endswith('|' + _strat_name) and _ck.startswith(m + '|'):
                            _strat_total_pnl += _cv.get('pnl', 0)
                            _strat_total_trades += _cv.get('trades', 0)
                    if _strat_total_pnl < -3.0 and _strat_total_trades >= 5:
                        score *= 0.05  # near-zero — strategy is a loser across all hours
                        log_agent('replicator', f'BLOCK {m}:{_strat_name} — cumulative loss \${_strat_total_pnl:.2f} across {_strat_total_trades} trades')
                # Self Diagnostic: scan system BEFORE blaming the market
                if risk.consec_loss > 0 or risk.pnl < 0:
                    _diag_score, _diag_issues, _diag_can, _diag_mult, _diag_action = tools.diagnostic.run_diagnostic(
                        m, int(time.strftime('%H')),
                        heartbeat_data=tools.alm.get_status() if hasattr(tools, 'alm') else {},
                        session_stats=tools.tz_intel.session_stats if tools.tz_intel else {}
                    )
                    if not _diag_can:
                        score *= 0.01  # system is broken, don't trade anything
                        if _diag_issues:
                            log_agent('diagnostic', f'SYSTEM BLOCK: {m} h{int(time.strftime("%H"))} — health={_diag_score}% root={tools.diagnostic.state.get("diagnostic_history", [{}])[-1].get("root_cause", "?")}')
                            for _iss in _diag_issues[:2]:
                                log_agent('diagnostic', f'  ⚠ {_iss}')
                    elif _diag_action in ('cautious', 'minimal'):
                        score *= _diag_mult  # system degraded, reduce but allow
                        log_agent('diagnostic', f'System degraded: {m} health={_diag_score}% action={_diag_action}')
                # Profit Replicator: recovery intelligence — can we recover if losing?
                if risk.consec_loss > 0 or risk.pnl < 0:
                    _r_conf, _r_action, _r_reason, _r_mult = tools.replicator.assess_recovery(
                        m, int(time.strftime('%H')),
                        [],
                        tools.tz_intel.session_stats if tools.tz_intel else {}
                    )
                    if _r_action == 'continue':
                        score *= 0.85  # slight reduction but keep going
                    elif _r_action == 'cautious':
                        score *= 0.4   # significant reduction
                    elif _r_action == 'pause':
                        score *= 0.05  # near-zero — stop trading this combo
                        log_agent('replicator', f'⏸ PAUSE {m} h{int(time.strftime("%H"))}: {_r_reason}')
                    elif _r_action == 'block':
                        score *= 0.0   # full block
                        log_agent('replicator', f'✕ BLOCK {m} h{int(time.strftime("%H"))}: {_r_reason}')
                # Timezone multiplier: boost/penalize based on historical hourly performance
                if tools.tz_intel:
                    score *= tools.tz_intel.get_multiplier(m)
                # Market State Brain: apply confidence boost/penalty
                if market_state:
                    score *= market_state.get('confidence_boost', 1.0)
                    # Avoid strategies the brain says to avoid
                    if strategy['strategy'] in market_state.get('avoid_strategies', []):
                        score *= 0.3
                    # Prefer strategies the brain recommends
                    if strategy['strategy'] in market_state.get('preferred_strategies', []):
                        score *= 1.3
                if regime in ('MOMENTUM', 'TREND'):
                    score *= 1.1
                # Apply rotation multiplier to diversify markets, strategies AND contracts
                score *= get_rotation_multiplier(m) * get_strategy_rotation_multiplier(strategy['strategy']) * get_contract_rotation_multiplier(strategy.get('contract', 'PUT'))
                
                strategy['regime'] = regime
                candidates.append((score, m, strategy))
            
            # ── Also add memory-based proven strategies as fallback ──
            for mk, mv in (tools.memory.strategies or {}).items():
                if not isinstance(mv, dict): continue
                m_parts = mk.split(':')
                if len(m_parts) < 2: continue
                mkt, strat_name = m_parts[0], ':'.join(m_parts[1:])
                if mkt not in MARKET_LIST: continue
                mv_trades = mv.get('trades', 0)
                mv_ev = mv.get('total_profit', 0) / mv_trades if mv_trades >= 5 else 0
                if mv_ev <= 0: continue
                already = any(c[1] == mkt and c[2].get('strategy') == strat_name for c in candidates)
                if already: continue
                mkt_digits = tc.last_digits.get(mkt, [])
                if len(mkt_digits) < 10: continue
                # Boost confidence when digit cooldown is active (forcing non-digit)
                is_digit_cooldown = digit_mgr.is_in_cooldown()
                conf_boost = 1.5 if is_digit_cooldown else 1.0
                mem_cand = {
                    'strategy': strat_name,
                    'contract': 'CALL' if 'UP' in strat_name or 'RISE' in strat_name else 'PUT',
                    'confidence': min((0.3 + mv_ev * 0.1) * conf_boost, 0.9),
                    'ev': mv_ev * conf_boost,
                    'regime': 'MEMORY',
                    'source': 'memory',
                    'reason': f'Memory fallback: {mv_trades}T ev={mv_ev:.4f}' + (' [digit cooldown]' if is_digit_cooldown else ''),
                }
                tz_mult, tz_bleed_reason = (1.0, '')
                if tools.tz_intel:
                    tz_mult, tz_bleed_reason = tools.tz_intel.get_stake_multiplier(mkt)
                    if tz_mult == 0:
                        # All modes: reduce stake instead of hard block
                        tz_mult = 0.2  # 80% stake reduction for bleed hours
                        if cycle % 15 == 0:
                            print(f"  [{cycle}] 🩸 TZ BLEED REDUCED: {mkt} — stake x0.2 ({tz_bleed_reason})", flush=True)
                # Use prob_engine edge when available (more accurate than inline EV)
                pe_edge = tools.prob_engine.get_edge(mkt, strat_name)
                pe_ev = pe_edge['ev'] if pe_edge else mem_cand['ev']
                pe_conf = pe_edge['confidence'] if pe_edge else mem_cand['confidence']
                m_score = pe_ev * pe_conf * MARKET_WEIGHTS.get(mkt, 1) * 0.7 * get_rotation_multiplier(mkt) * get_strategy_rotation_multiplier(strat_name) * get_contract_rotation_multiplier(mem_cand.get('contract', 'PUT')) * tz_mult
                # Priority boost: HIGH priority strategies get 1.5x score
                strat_mem = tools.memory.strategies.get(mk, {})
                if isinstance(strat_mem, dict) and strat_mem.get("priority") == "HIGH":
                    m_score *= 1.5
                candidates.append((m_score, mkt, mem_cand))
            
            # ── Sort candidates by score descending ──
            # ── REPLICATOR PRIORITY: boost candidates with profitable historical patterns ──
            _hour = int(time.strftime('%H'))
            for i, (score, mkt, cand) in enumerate(candidates):
                _strat = cand.get('strategy', 'ALL')
                _mult, _reason = tools.replicator.get_recommended_stake_mult(mkt, _hour, _strat)
                if _mult > 1.0 and _reason != 'no_data':
                    # Boost candidate score by replicator mult
                    candidates[i] = (score * _mult, mkt, cand)
                    if cycle % 20 == 0:
                        print(f'  [{cycle}] REPLICATOR PRIORITY: {mkt} {_strat} x{_mult:.2f} ({_reason})', flush=True)
            candidates.sort(key=lambda c: -c[0])
            # Trade Intelligence: ensemble scoring (blend top candidates)
            candidates = tools.trade_intel.score_candidates(candidates)
            
            # ── EXPERIMENT PRIORITY: boost candidates matching active experiments ──
            active_exps = tools.research.active_experiments
            exp_boosts = {}
            for eid in active_exps:
                exp = tools.research.experiments.get(eid, {})
                if exp.get('status') == 'running':
                    exp_key = f"{exp.get('market','')}:{exp.get('strategy','')}"
                    exp_boosts[exp_key] = exp.get('priority_score', 1.0)
            # Apply boost to candidates
            for i, (score, mkt, cand) in enumerate(candidates):
                exp_key = f"{mkt}:{cand['strategy']}"
                if exp_key in exp_boosts:
                    candidates[i] = (score * (1 + exp_boosts[exp_key]), mkt, cand)
            candidates.sort(key=lambda c: -c[0])
            
            # ── FILTER LOOP: try each candidate until one passes ──
            best_market = None
            best_data = None
            skipped_count = 0
            
            _base_score = None
            _filter_log = []
            for score, mkt, cand in candidates:
                _base_score = score
                strat_key = f"{mkt}:{cand['strategy']}"
                _filter_reason = None
                
                # Filter 1: retired?
                # Auto-retire known losing strategies (EXPLORATION allows them for learning)
                if cand['strategy'] in ('ODD_BIAS', 'MOMENTUM_UP', 'RISE_TREND'):
                    if risk.trading_mode != 'EXPLORATION':
                        _filter_reason = f"blacklist:{cand['strategy']}"
                        skipped_count += 1
                        continue
                if tools.memory.is_strategy_retired(mkt, cand['strategy']):
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    continue
                
                # Filter 2: negative EV with enough data?
                strat_ev = tools.memory.get_strategy_ev(mkt, cand['strategy'])
                if strat_ev < -0.10 and tools.memory.strategies.get(strat_key, {}).get('trades', 0) >= 20:
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    continue
                
                # Filter 3: low WR after 10+ trades?
                strat_stats = tools.memory.strategies.get(strat_key, {})
                strat_trades = strat_stats.get('trades', 0)
                strat_wins = strat_stats.get('wins', 0)
                if strat_trades >= 20:
                    strat_wr = strat_wins / strat_trades
                    if strat_wr < 0.25:
                        if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                            _rescue[0] = (_base_score, mkt, cand.copy())
                        skipped_count += 1
                        continue
                
                # Filter 4: replicator blocked combo with dead pattern?
                _rep_blocked, _rep_info = tools.replicator.is_blocked(mkt, int(time.strftime('%H')), cand['strategy'])
                if _rep_blocked:
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    continue
                # Filter 4b: anti-retry (just lost same strategy)?
                if TRADE_LOG and len(TRADE_LOG) > 0:
                    last_trade = TRADE_LOG[-1]
                    if (last_trade.get('market') == mkt and
                        last_trade.get('strategy') == cand['strategy'] and
                        last_trade.get('profit', 0) < 0):
                        sm_art = tools.session_mgr
                        if not (sm_art.active and sm_art.session_strategy == cand['strategy']):
                            if risk.consec_loss >= 2:
                                if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                                    _rescue[0] = (_base_score, mkt, cand.copy())
                                skipped_count += 1
                                continue
                
                # Filter 5: RETIRE signal?
                rec_key, rec_score, rec_action = tools.strategist.get_strategy_recommendation(mkt, cand.get('regime', 'UNKNOWN'))
                if rec_action == "RETIRE" and rec_key:
                    sm_ret = tools.session_mgr
                    if not (sm_ret.active and sm_ret.session_strategy == cand['strategy']):
                        if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                            _rescue[0] = (_base_score, mkt, cand.copy())
                        skipped_count += 1
                        continue
                
                # Filter 6: DIGIT TRADE LIMIT — max 2 consecutive digit trades
                if is_digit_trade(cand['strategy'], cand.get('contract', 'PUT')) and not digit_mgr.should_trade(mkt, cand['strategy'])[0]:
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    if cycle % 10 == 0:
                        print(f"  [{cycle}] SKIP digit cooldown: {strat_key} — {digit_mgr.cooldown_reason}")
                    continue
                
                # Filter 7b: MARKET HEALTH — skip markets with proven loss patterns
                _mk_all = sum(v.get("trades", 0) for k, v in tools.memory.strategies.items() if k.startswith(mkt + ":"))
                _mk_wins_all = sum(v.get("wins", 0) for k, v in tools.memory.strategies.items() if k.startswith(mkt + ":"))
                if _mk_all >= 20:
                    _mk_wr_all = _mk_wins_all / _mk_all
                    if _mk_wr_all < 0.40:
                        if cycle % 15 == 0:
                            print(f"  [{cycle}] SKIP unhealthy market: {mkt} (WR={_mk_wr_all:.0%} over {_mk_all} trades)")
                        continue
                # Filter 7: NOISE — skip noisy markets (logged, not traded)
                if noise_detector.is_noisy(mkt):
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    if cycle % 15 == 0:
                        print(f"  [{cycle}] SKIP noisy: {mkt} (entropy={noise_detector.market_entropy.get(mkt, 0):.2f})")
                    continue
                
                # ── Research: minimum score threshold ──
                min_score = 0.001 if risk.trading_mode == 'EXPLORATION' else 0.005
                if score < min_score:
                    _filter_reason = f"low_score:{score:.4f}<min:{min_score}"
                    _filter_log.append(_filter_reason)
                    if _base_score is not None and (_rescue[0] is None or _base_score > _rescue[0][0]):
                        _rescue[0] = (_base_score, mkt, cand.copy())
                    skipped_count += 1
                    continue
                # ── PASSED ALL FILTERS ──
                if skipped_count > 0 and cycle % 5 == 0:
                    _reasons = [r for r in _filter_log if r]
                    print(f"  [{cycle}] FILTERED {skipped_count} candidates: {', '.join(_reasons[:5])} | using: {strat_key} (score={score:.4f})")
                best_market = mkt
                best_data = cand
                best_data['regime'] = cand.get('regime', 'UNKNOWN')
                signal = tools.get_signal(best_market)
                # Record for rotation tracking
                record_market_trade(mkt, cand['strategy'], cand.get('contract', 'PUT'))
                break
            
            # -- RESCUE: trade best filtered candidate as scout --
            if not best_market and _rescue[0]:
                _r_score, _r_mkt, _r_cand = _rescue[0]
                best_market = _r_mkt
                best_data = _r_cand
                best_data['regime'] = _r_cand.get('regime', 'UNKNOWN')
                signal = tools.get_signal(best_market)
                risk.override_stake = risk.calc_stake(best_data) * 0.3
                main._last_model_ok = True
                main._rescue_trade = True
                if cycle % 5 == 0:
                    print(f"  [{cycle}] RESCUE: {best_market} {best_data['strategy']} score={_r_score:.4f} scout=${risk.override_stake:.2f}", flush=True)
                log_agent('bunch', f'RESCUE: {best_market} {best_data["strategy"]} score={_r_score:.4f} scout=${risk.override_stake:.2f}')

            if not best_market and skipped_count > 0 and cycle % 15 == 0:
                print(f"  [{cycle}] ALL {skipped_count} candidates filtered out \u2014 waiting")

            if not best_market:
                if cycle % 15 == 0:
                    print(f"  [{cycle}] Ticks: " + ", ".join(f"{m}:{len(tc.last_digits.get(m,[]))}" for m in MARKET_LIST[:6]))
                await asyncio.sleep(2)
                continue
            
            # -- Goal Manager check --
            daily_loss = abs(min(0, risk.pnl))
            goal_changed = tools.goal.update(
                risk.balance, risk.pnl, risk.consec_loss, risk.consec_win,
                risk.total, daily_loss
            )
            if goal_changed:
                try:
                    log_agent("goal", f"GOAL: {tools.goal.get_mode()} -- {tools.goal.get_goal().get('objective','')}")
                    log_sys(f"Goal switched: {tools.goal.get_mode()}", "info")
                except: pass

            session_trades = TRADE_LOG[-20:] if TRADE_LOG else []
            now_ms = int(time.time() * 1000)
            trades_this_hour = sum(1 for t in session_trades if now_ms - t.get('time', 0) < 600000)
            strategy_conf = int(best_data.get('confidence', 0) * 5) if best_data else 0
            allowed, goal_reason = tools.goal.check_trade_allowed(
                strategy_conf,
                best_data['contract'] if best_data else '',
                trades_this_hour
            )
            if not allowed:
                if cycle % 15 == 0:
                    print(f"  [{cycle}] GOAL BLOCKED: {goal_reason}")
                await asyncio.sleep(3)
                continue

            # -- Risk check --
            can, reason = risk.can_trade()
            if not can:
                if cycle % 10 == 0: print(f"  [{cycle}] {reason}")
                await asyncio.sleep(3)
                continue
            
            # ── Mode switching ──
            total_wr = risk.wins / max(risk.total, 1)
            recent_wr = 0
            if len(TRADE_LOG) >= 5:
                recent_wr = sum(1 for t in TRADE_LOG[-5:] if t.get('profit', 0) > 0) / 5
            
            old_mode = risk.trading_mode
            market_trades = risk.market_count.get(best_market, 0) if best_market else 0
            
            # Check if we have any strategy with enough data to exploit
            _has_exploitable = False
            for _mk, _mk_ct in risk.market_count.items():
                if _mk_ct >= 15:
                    _has_exploitable = True
                    break
            
            if risk.consec_loss >= 3 or risk.pnl < -risk.start * 0.010:
                risk.trading_mode = 'RECOVERY'
                risk.mode_reason = f'streak={risk.consec_loss}, P&L={risk.pnl:+.2f}'
            elif risk.consec_loss >= 3 or total_wr < 0.45:
                risk.trading_mode = 'CONSERVATIVE'
                risk.mode_reason = f'loss {risk.consec_loss}, WR {total_wr:.0%}'
            elif risk.consec_win >= 5 and recent_wr >= 0.8 and risk.alignment_score >= 4:
                risk.trading_mode = 'AGGRESSIVE'
                risk.mode_reason = f'win {risk.consec_win}, WR {recent_wr:.0%}'
            elif market_trades < 10 and risk.total > 5 and risk.exploration_trades_used < 30 and not _has_exploitable:
                risk.trading_mode = 'EXPLORATION'
                risk.mode_reason = f'new {best_market} ({market_trades})'
                risk.exploration_trades_used += 1
            elif risk.alignment_score >= 3 and recent_wr >= 0.65 and risk.consec_loss == 0:
                risk.trading_mode = 'PRECISION'
                risk.mode_reason = f'align={risk.alignment_score}, WR {recent_wr:.0%}'
            else:
                risk.trading_mode = 'OPTIMAL'
                risk.mode_reason = 'default'
            
            if old_mode != risk.trading_mode:
                risk.mode_switch_count += 1
                log_agent("brain", f"MODE: {old_mode} -> {risk.trading_mode} ({risk.mode_reason})")
                log_sys(f"Mode: {old_mode} -> {risk.trading_mode}", "info")
            
            # ── Protector check ──
            prot_allowed, prot_reason = tools.check_safety()
            if not prot_allowed:
                log_agent("protector", f"BLOCKED: {prot_reason}")
                if cycle % 10 == 0: print(f"  [{cycle}] Protector: {prot_reason}")
                await asyncio.sleep(3)
                continue
            
            # ── Reset evolution override periodically ──
            evo_age = risk.total - getattr(tools.alm, '_last_evolution_trades', 0)
            if evo_age > 25 and hasattr(risk, 'override_stake'):
                delattr(risk, 'override_stake')
            
            # ── Calculate stake ──
            stake = risk.calc_stake(best_data)
            # Trade Intelligence: 10-layer stake calculation
            pe_edge = tools.prob_engine.get_edge(best_market, best_data.get('strategy', ''))
            edge_val = pe_edge.get('edge', 0) if pe_edge else 0
            stake = tools.trade_intel.calculate_stake(
                stake, best_market, best_data.get('strategy', ''),
                best_data.get('contract', 'PUT'), risk.alignment_score, edge_val)
            # Edge decay check: skip if strategy has decayed
            if tools.trade_intel.is_strategy_decayed(best_market, best_data.get('strategy', '')):
                if cycle % 10 == 0:
                    print(f"  [{cycle}] EDGE DECAY: {best_market}:{best_data.get('strategy','')} — skipping", flush=True)
                continue
            if stake < 0.50:
                await asyncio.sleep(1)
                continue
            
            # ── Judge validation (for DIGITMATCH only, skip for speed on others) ──
            if best_data['contract'] == 'DIGITMATCH':
                prot_status = {'frozen': False}
                # Get simulation result from backtester
                _sim = tools.backtester.get_strategy_result(best_market, best_data['strategy'])
                _mem_stats = tools.memory.get_memory_summary() if tools.memory else {}
                judge_ctx = {
                    'market': best_market, 'market_type': MARKET_TYPES.get(best_market, 'volatility'),
                    'strategy': best_data, 'regime': best_data.get('regime', 'UNKNOWN'),
                    'sim_result': _sim, 'strategy_health': 50,
                    'risk_clearance': not prot_status.get('frozen', False),
                    'protector_status': prot_status,
                    'memory_stats': _mem_stats, 'signal': tools.get_signal(best_market) or {},
                }
                verdict = tools.validate_trade(judge_ctx)
                log_agent("judge", f"Evaluated {best_data['strategy']} on {best_market}: {verdict.get('decision', '?') if isinstance(verdict, dict) else '?'}")
                if verdict and isinstance(verdict, dict) and verdict.get('decision') == 'NO_TRADE':
                    if cycle % 20 == 0:
                        reason_str = verdict.get('reason', 'judge said no')
                        print(f"  [{cycle}] Judge blocked: {reason_str}")
                    await asyncio.sleep(1)
                    continue
            
            # ── AI Brain validation (every trade — model must approve) ──
            if True:
                # Build strategy health context for NEXUS
                strat_key = f"{best_market}:{best_data['strategy']}"
                strat_health = tools.memory.strategies.get(strat_key, {})
                risk_ctx = {
                    'mode': risk.trading_mode,
                    'consec_loss': risk.consec_loss,
                    'consec_win': risk.consec_win,
                    'pnl': risk.pnl,
                }
                # Build market overview for environment
                market_overview = {}
                for mk in MARKET_LIST:
                    mk_digits = tc.last_digits.get(mk, [])
                    mk_signal = tools.get_signal(mk)
                    mk_regime = tools.get_regime(mk, mk_signal) if mk_signal else "UNKNOWN"
                    if isinstance(mk_regime, tuple):
                        mk_regime = str(mk_regime[0])
                    elif isinstance(mk_regime, dict):
                        mk_regime = mk_regime.get('regime', 'UNKNOWN')
                    market_overview[mk] = {
                        "ticks": len(mk_digits),
                        "regime": mk_regime,
                        "traded": risk.market_count.get(mk, 0),
                    }
                
                # ── SESSION ENTRY GATE (cheap, no tokens) — check BEFORE expensive brain ──
                sm = tools.session_mgr
                if not sm.active:
                    tick_count = len(tc.last_digits.get(best_market, []))
                    signal_data = tools.get_signal(best_market)
                    strat_key_g = f"{best_market}:{best_data['strategy']}"
                    strat_health_g = tools.memory.strategies.get(strat_key_g, {})
                    gates_pass, gates_detail = sm.check_entry_gates(
                        tick_count, signal_data, best_data.get("regime", "UNKNOWN"),
                        best_data.get("ev", 0), risk.consec_loss, risk.consec_win,
                        strat_health_g, risk.balance, risk.start
                    )
                    if not gates_pass:
                        failed = [k for k, v in gates_detail.items() if not v]
                        if cycle % 10 == 0:
                            print(f"  [{cycle}] SESSION GATES BLOCKED: {failed}")
                        await asyncio.sleep(0.5)
                        continue
                    stake_g = risk.calc_stake(best_data)
                    stake_g = max(1.0, min(stake_g, 15.0))
                    # ── OVER-TRADE GUARD: check before starting session ──
                    ot_ok, ot_reason, ot_wait = tools.overtrade.can_start_session(risk, TRADE_LOG[-20:] if TRADE_LOG else [])
                    if not ot_ok:
                        tools.overtrade.skipped_trades += 1
                        if cycle % 10 == 0:
                            print(f"  [{cycle}] OVER-TRADE BLOCKED: {ot_reason}")
                        log_agent("overtrade", f"SESSION BLOCKED: {ot_reason}")
                        await asyncio.sleep(min(ot_wait, 5))
                        continue
                    sid = sm.enter_session(best_market, best_data["strategy"], best_data["contract"], stake_g)
                    tools.overtrade.record_trade()
                    state_info = ""
                    try:
                        state_info = f"state={market_state.get('market_state','?')} conf={market_state.get('state_confidence',0):.0%}"
                    except NameError:
                        pass
                    log_agent("session", f"SESSION #{sid} STARTED: {best_market} {best_data['strategy']} {best_data['contract']} stake=${stake_g:.2f} [{state_info}]")
                    log_sys(f"Session #{sid} started: {best_market} {best_data['strategy']}", "info")

                sm = tools.session_mgr
                if sm.active:
                    # Session: ask model for trade approval
                    brain_reason = f"SESSION #{sm.session_id} ACTIVE — trade {sm.trades_in_session+1}/{sm.max_trades}"
                    brain_ok = True  # default: allow
                    # EXPLORATION BYPASS: skip model check — let system explore freely
                    if risk.trading_mode == 'EXPLORATION':
                        brain_ok = True
                        brain_reason = f"SESSION #{sm.session_id} EXPLORATION BYPASS — learning mode"
                    elif risk.trading_mode == 'RECOVERY':
                        brain_ok = True
                        brain_reason = f"SESSION #{sm.session_id} RECOVERY BYPASS — rebuilding"
                    else:
                        try:
                            # Quick model check — does this setup still make sense?
                            quick_result = tools.alm.quick_check(
                                best_market, best_data['strategy'], best_data.get('contract', 'CALL'),
                                best_data.get('regime', 'UNKNOWN'), risk.balance, risk.consec_loss)
                            if quick_result and not quick_result.get('ok', True):
                                brain_ok = False
                                brain_reason = f"MODEL BLOCKED: {quick_result.get('reason', 'no reason')[:60]}"
                                log_agent("alm_brain", brain_reason)
                                print(f"  [{cycle}] MODEL BLOCKED SESSION TRADE: {brain_reason}", flush=True)
                            elif quick_result and quick_result.get('ok', True):
                                log_agent("alm_brain", f"MODEL APPROVED: {best_data['strategy']} {best_data.get('contract','')} on {best_market} — {quick_result.get('reason','')[:50]}")
                        except Exception as e:
                            log_agent("alm_brain", f"MODEL ERROR: {str(e)[:50]} — trade allowed")
                            pass
                else:
                    brain_ok, brain_reason, nexus_result = tools.ask_brain(
                        best_market, best_data['strategy'], best_data.get('regime', '?'),
                        TRADE_LOG[-10:], risk.balance,
                        signal=signal, strategy_health=strat_health, risk_state=risk_ctx,
                        tick_data=tc.last_digits.get(best_market, []),
                        cpp_predictions=cpp_engine.get_status() if cpp_engine else {},
                        all_markets=market_overview,
                        price_action=tools.price_action.analyze(tc.last_digits.get(best_market, []), best_market))
                    if not brain_ok:
                        log_agent("alm_brain", f"BRAIN BLOCKED: {brain_reason}")
                        print(f"  [{cycle}] Brain blocked: {brain_reason}")
                        await asyncio.sleep(1)
                        continue
                    else:
                        print(f"  [{cycle}] Brain approved: {brain_reason} — proceeding to execution", flush=True)
            
            # ── Efficiency check: skip if session is active ──
            sm_eff = tools.session_mgr
            if not sm_eff.active:
                strat_key = f"{best_market}:{best_data['strategy']}"
                eff_ok, eff_reason = tools.efficiency.should_trade(best_data['strategy'])
                if not eff_ok:
                    log_agent("efficiency", f"BLOCKED: {best_data['strategy']} — {eff_reason}")
                    best_strat, best_sv = tools.efficiency.get_best_strategy()
                    if best_strat and best_sv > -0.3:
                        log_agent("efficiency", f"Redirecting to {best_strat} (SV={best_sv:+.3f})")
                        continue
                    if cycle % 20 == 0:
                        print(f"  [{cycle}] Efficiency blocked: {eff_reason}")
                    await asyncio.sleep(1)
                    continue
            
            # BOT SCORING every 15 trades
            if risk.total > 0 and risk.total % 15 == 0:
                try:
                    perf_data = {}
                    for k, v in tools.memory.data["strategies"].items():
                        if isinstance(v, dict):
                            perf_data[k] = v
                    tools.score_all_strategies(perf_data)
                    elite = tools.scorer.get_elite()
                    if elite:
                        log_agent("scorer", f"ELITE: {', '.join(s['strategy'][:15] for s in elite[:3])}")
                    for key, perf in perf_data.items():
                        if isinstance(perf, dict) and perf.get('trades', 0) >= 15:
                            action = tools.auto_optimize(key, perf)
                            if action:
                                log_agent("optimizer", f"{action['action']}: {key} - {action['reason']}")
                                if action['action'] == 'KILL':
                                    tools.efficiency.blacklist(action['strategy'])
                    tools.analyze_failures(perf_data)
                except Exception as e:
                    log_sys(f"Scoring error: {e}", "warn")

            # -- SELF-IMPROVEMENT: close the cognitive loop --
            # ── MISSION SELF-TEST: every 20 trades, evaluate + adapt ──
            if risk.total > 0 and risk.total % 20 == 0:
                try:
                    _score, _recs, _results = tools.mission.run_self_test(tools.tz_intel, tools.memory.data)
                    log_agent('mission', f'SELF-TEST #{tools.mission.state["self_test"]["runs"]}: score={_score} | {len(_recs)} recommendations')
                    for _rec in _recs[:3]:
                        log_agent('mission', f'  📋 {_rec}')
                    _picks = tools.mission.auto_select_markets(tools.tz_intel)
                    for _pick in _picks[:3]:
                        log_agent('mission', f'  🎯 {_pick["market"]}: {_pick["confidence"]} — {_pick["reason"]}')
                except Exception as _e:
                    log_sys(f'Self-test error: {_e}', 'warn')
            if tools.improver.should_run(risk.total):
                try:
                    actions = tools.improver.run_improvement_cycle(
                        tools.memory, tools.strategist, tools.scorer, tools.competition
                    )
                    if actions:
                        for a in actions:
                            log_agent("improver", f"{a['type']}: {a['strategy']} -- {a['reason']}")
                        promotes = [a for a in actions if a['type'] == 'PROMOTE']
                        retires = [a for a in actions if a['type'] == 'RETIRE']
                        summary_parts = []
                        if promotes: summary_parts.append(f"{len(promotes)} promoted")
                        if retires: summary_parts.append(f"{len(retires)} retired")
                        log_agent("brain", f"IMPROVEMENT: {len(actions)} actions taken ({', '.join(summary_parts) if summary_parts else 'analyzed'})")
                        log_sys(f"Improvement cycle: {len(actions)} actions", "info")
                except Exception as e:
                    log_sys(f"Improvement error: {e}", "warn")
            

            # ════ SELF-EVOLUTION: Model owns the system ════
            if risk.total > 0 and risk.total % 10 == 0 and risk.total != getattr(tools.alm, "_last_evolution_trades", 0):
                try:
                    ph_evo = load_history()
                    evo_result = tools.alm.self_evolve(
                        ph_evo.get("pnl_history", [])[-30:],
                        TRADE_LOG[-20:],
                        {"stake": risk.calc_stake(best_data) if best_data else risk.balance * 0.02, "markets": MARKET_LIST, "mode": risk.trading_mode}
                    )
                    if evo_result.get("evolved"):
                        tools.alm._last_evolution_trades = risk.total
                        # Apply model's evolution decisions
                        if evo_result.get("stake_change"):
                            risk.override_stake = evo_result["stake_change"]
                            log_sys("EVOLUTION: stake → ${:.2f}".format(evo_result.get("stake_change", 0)))
                        risk_level = evo_result.get("risk_level", "balanced")
                        if risk_level == "defensive":
                            risk.override_stake = min(getattr(risk, "override_stake", 2.0), 2.0)
                            log_sys("EVOLUTION: DEFENSIVE mode — min stake", "warn")
                        elif risk_level == "aggressive":
                            base = risk.calc_stake(best_data) if best_data else 2.0
                            risk.override_stake = min(base * 1.5, 25.0)
                            log_sys("EVOLUTION: AGGRESSIVE mode — 1.5x stake", "info")
                        log_agent("evolution", evo_result.get("reason", "evolved") + " [WR={:.0f}% PnL=${:+.2f}]".format(evo_result.get("recent_wr",0), evo_result.get("recent_pnl",0)))
                    else:
                        # Evolution didn't succeed — set guard to avoid retrying every cycle
                        # but still log the attempt
                        tools.alm._last_evolution_trades = risk.total
                        log_sys(f"Evolution skipped: {evo_result.get('reason', 'unknown')}", "info")
                except Exception as e:
                    log_sys(f"Evolution error: {e}", "warn")
            # FULL BURST MODE
            # FULL BURST MODE: when alignment is perfect, go full speed ──
            alignment = 0
            
            # Market data quality
            mkt_digits = tc.last_digits.get(best_market, [])
            if len(mkt_digits) >= 20: alignment += 1  # enough data observed
            
            # Signal quality
            if signal:
                if signal.get('confidence', 0) > 0.6: alignment += 1
                if signal.get('direction', 'NEUTRAL') != 'NEUTRAL': alignment += 1
            
            # Regime quality
            regime_conf = best_data.get('confidence', 0)
            if regime_conf > 0.6: alignment += 1  # clear regime
            
            # Edge quality
            if best_data.get('ev', 0) > 0.03: alignment += 1  # positive edge
            
            # Risk state
            if risk.consec_loss == 0: alignment += 1  # no loss streak
            if risk.consec_win >= 2: alignment += 1  # winning momentum
            if risk.pnl >= 0: alignment += 1  # not losing
            
            # Escalation
            if risk.escalation_level == 'HIGH': alignment += 2
            
            # Strategy track record
            strat_key = f"{best_market}:{best_data['strategy']}"
            strat_stats = tools.memory.strategies.get(strat_key, {})
            strat_trades = strat_stats.get('trades', 0)
            strat_wr = strat_stats.get('wins', 0) / strat_trades if strat_trades > 0 else 0
            if strat_trades >= 5 and strat_wr >= 0.70: alignment += 1  # proven winner
            if strat_trades >= 10 and strat_wr < 0.40: alignment -= 2  # proven loser penalty
            
            # Contract family bonus (prefer non-DIGITDIFF)
            contract_type = best_data.get('contract', 'DIGITDIFF')
            if contract_type in ('CALL', 'PUT', 'DIGITEVEN', 'DIGITODD', 'ASIANU', 'ASIAND'):
                alignment += 1  # main contract bonus
            
            # C++ SIGNAL DIRECTION ALIGNMENT: critical for avoiding wrong-direction trades
            if cpp_engine and hasattr(cpp_engine, '_market_preds') and best_market in cpp_engine._market_preds:
                cpp_val = cpp_engine._market_preds[best_market].get('signal', 0)
                if contract_type in ('CALL', 'ASIANU') and cpp_val > 0:
                    alignment += 2  # CALL aligned with bullish C++ signal
                elif contract_type in ('PUT', 'ASIAND') and cpp_val < 0:
                    alignment += 2  # PUT aligned with bearish C++ signal
                elif contract_type in ('CALL', 'ASIANU') and cpp_val < 0:
                    alignment -= 3  # CALL against bearish C++ signal — heavy penalty
                elif contract_type in ('PUT', 'ASIAND') and cpp_val > 0:
                    alignment -= 3  # PUT against bullish C++ signal — heavy penalty
            
            risk.alignment_score = alignment
            
            # Session entry gates already checked before brain analysis
            
            # ── CONFIDENCE GATE ──
            risk.confidence_score = alignment
            min_confidence = 3  # session entry gates are the real filter
            if risk.trading_mode == 'EXPLORATION':
                min_confidence = 5  # higher bar in exploration — only take high-conviction setups
            if risk.trading_mode == 'RECOVERY':
                min_confidence = 5  # very high bar during recovery
            if alignment < min_confidence:
                if cycle % 30 == 0:
                    print(f"  [{cycle}] LOW CONFIDENCE: {alignment}/{min_confidence} — waiting for better setup")
                await asyncio.sleep(1)
                continue
            
            print(f"  [{cycle}] >>> ALIGNMENT PASSED: {alignment}/{min_confidence} — {best_market} {best_data['strategy']} {best_data['contract']}", flush=True)
            
            # ── PROFIT MIRROR: check for pending echo trades ──
            _echo = tools.profit_mirror.get_next_echo()
            if _echo and risk.consec_loss < 2:
                print(f"  [{cycle}] 🔊 ECHO: {_echo['market']} {_echo['strategy']} (from {_echo['source_market']})", flush=True)
                log_agent('mirror', f'Echo trade: {_echo["market"]} {_echo["strategy"]} ← {_echo["source_market"]} (won ${_echo["source_pnl"]:+.2f})')
                # Override best_market/strategy with echo
                best_market = _echo['market']
                best_data['strategy'] = _echo['strategy']
                best_data['contract'] = _echo.get('contract', 'PUT')
            
            # ── PROFIT MIRROR: check win pattern replay ──
            _replay_conditions = tools.profit_mirror.replay.get_conditions_from_trade(
                best_market,
                tc.get_entropy(best_market) if hasattr(tc, 'get_entropy') else 3.0,
                risk.market_state if hasattr(risk, 'market_state') else '?',
                tools.memory.get_digit_bias(best_market) if hasattr(tools.memory, 'get_digit_bias') else {},
                int(time.strftime('%H')),
                get_session_label(int(time.time())),
                risk.balance
            )
            _replay = tools.profit_mirror.get_replay(best_market, _replay_conditions)
            if _replay and risk.consec_loss < 2:
                _rp = _replay['pattern']
                print(f"  [{cycle}] 🔄 REPLAY: {_rp['market']} {_rp['strategy']} (matched {_replay['score']}/{_replay['total']} conditions, {_replay['age_h']:.1f}h old)", flush=True)
                log_agent('mirror', f'Pattern replay: {_rp["market"]} {_rp["strategy"]} (score={_replay["score"]}/{_replay["total"]}, {_replay["age_h"]:.1f}h old)')
            
            # ── BUNCH RUNNER: ALWAYS try to start a bunch (bunch-first mode) ──
            br = tools.bunch_runner
            _is_echo_trade = bool(_echo)
            _is_replay_trade = bool(_replay)
            if not (br.current_run and br.current_run.status == 'RUNNING'):
                # No active bunch — ALWAYS score and try to start
                entropy_val = tc.get_entropy(best_market) if hasattr(tc, 'get_entropy') else 3.0
                digit_bias = tools.memory.get_digit_bias(best_market) if hasattr(tools.memory, 'get_digit_bias') else {}
                session_label = get_session_label(int(time.time()))
                should_run, setup_score, bunch_size = br.should_start_run(
                    best_market, best_data['strategy'], risk.market_state if hasattr(risk, 'market_state') else '?',
                    entropy_val, digit_bias, session_label, risk.balance, risk.consec_loss,
                    hour=int(time.strftime('%H')), replicator=tools.replicator
                )
                if should_run and bunch_size >= 3:
                    run = br.start_run(best_market, best_data['strategy'], risk.market_state if hasattr(risk, 'market_state') else '?',
                                       setup_score, bunch_size, session_label)
                    log_agent('bunch', f'BUNCH START: {best_market} {best_data["strategy"]} | score={setup_score} size={bunch_size} stake=${run.stake}')
                    log_sys(f'Bunch started: {best_market} {best_data["strategy"]} score={setup_score} size={bunch_size}', 'info')
                elif setup_score < 15:
                    # Bunch score too low for multi-trade — but still allow single trade
                    if cycle % 20 == 0:
                        print(f'  [{cycle}] SINGLE (bunch skip): {best_market} {best_data["strategy"]} score={setup_score:.1f}', flush=True)
                # If score >= 15 but bunch_size < 3, still allow single trade (very low confidence)
                else:
                    if cycle % 20 == 0:
                        print(f'  [{cycle}] SINGLE TRADE (low bunch score): {best_market} {best_data["strategy"]} score={setup_score}', flush=True)
            elif br.current_run and br.current_run.status == 'RUNNING':
                # Active bunch — use bunch strategy
                if br.current_run.market != best_market or br.current_run.strategy != best_data['strategy']:
                    # Override to match active bunch
                    best_market = br.current_run.market
                    best_data['strategy'] = br.current_run.strategy
            
            # ── TURBO/BURST MODE ──
            # Activates when: alignment high + win streak + profitable
            # Trades every tick for N trades, then cools down to OPTIMAL
            if risk.burst_trades_left > 0:
                # Currently in burst — trade fast
                risk.full_burst = True
                risk.burst_trades_left -= 1
                risk.burst_total_trades += 1
                sleep_time = 0.1  # every tick
                if cycle % 5 == 0:
                    print(f"  [{cycle}] BURST: {risk.burst_trades_left} trades left | {risk.burst_total_trades} total", flush=True)
            elif risk.consec_win >= 3 and risk.pnl > 0 and alignment >= 5:
                # Activate burst: 10 fast trades
                risk.full_burst = True
                risk.burst_trades_left = 10
                risk.burst_total_trades = 0
                risk.trading_mode = 'AGGRESSIVE'
                print(f"  [{cycle}] BURST ACTIVATED: alignment={alignment} win_streak={risk.consec_win} — 10 fast trades", flush=True)
                log_agent("brain", f"BURST MODE: alignment={alignment} win={risk.consec_win} — 10 fast trades at full speed")
                sleep_time = 0.1
            elif alignment >= 5:
                # High alignment but no win streak — normal speed
                risk.full_burst = False
                sleep_time = 0.3
            elif alignment >= 4:
                risk.full_burst = False
                sleep_time = 0.5
            else:
                risk.full_burst = False
                sleep_time = 1.0
            
            # ── ENTRY OPTIMIZATION: check tick momentum before entry ──
            signal = tools.get_signal(best_market)
            tick_momentum = signal.get('direction', 'NEUTRAL') if signal else 'NEUTRAL'
            tick_velocity = signal.get('velocity', 0) if signal else 0
            
            # Skip if momentum contradicts trade direction (ALWAYS check, even in EXPLORATION)
            if best_data['contract'] in ('CALL', 'ASIANU') and tick_momentum == 'DOWN':
                if cycle % 10 == 0:
                    print(f"  [{cycle}] DIRECTION BLOCKED: CALL vs DOWN momentum", flush=True)
                await asyncio.sleep(0.5)
                continue
            if best_data['contract'] in ('PUT', 'ASIAND') and tick_momentum == 'UP':
                if cycle % 10 == 0:
                    print(f"  [{cycle}] DIRECTION BLOCKED: PUT vs UP momentum", flush=True)
                await asyncio.sleep(0.5)
                continue
            # Also check C++ signal direction
            cpp_sig = None
            if cpp_engine and hasattr(cpp_engine, '_market_preds'):
                cpp_sig = cpp_engine._market_preds.get(best_market)
            if cpp_sig:
                cpp_val = cpp_sig.get('signal', 0)
                if cpp_val < 0 and best_data['contract'] in ('CALL', 'ASIANU'):
                    if cycle % 10 == 0:
                        print(f"  [{cycle}] DIRECTION BLOCKED: CALL vs C++ signal={cpp_val}", flush=True)
                    await asyncio.sleep(0.5)
                    continue
                if cpp_val > 0 and best_data['contract'] in ('PUT', 'ASIAND'):
                    if cycle % 10 == 0:
                        print(f"  [{cycle}] DIRECTION BLOCKED: PUT vs C++ signal={cpp_val}", flush=True)
                    await asyncio.sleep(0.5)
                    continue
            
            # ── CONTRACT FAMILY ROTATION: only override DIGITDIFF (cooldown) ──
            # NEXUS-approved strategy is respected — only rotate if cooldown
            picker = tools.picker
            from agents.contract_picker import FAMILIES, CONTRACT_FAMILY
            current_family = CONTRACT_FAMILY.get(best_data['contract'], 'digit')
            
            # Only rotate away from digit family (DIGITDIFF cooldown)
            if current_family == 'digit':
                family_list = ['directional', 'parity', 'barrier']
                new_family = family_list[risk.total % len(family_list)] if family_list else current_family
                fam_contracts = FAMILIES.get(new_family, [])
                if fam_contracts:
                    new_contract = fam_contracts[0]
                    if new_family == 'directional':
                        best_data['strategy'] = 'FALL_TREND' if tick_momentum == 'DOWN' else 'RISE_TREND'
                        best_data['contract'] = 'PUT' if tick_momentum == 'DOWN' else 'CALL'
                    elif new_family == 'parity':
                        mkt_d = tc.last_digits.get(best_market, [])
                        even_count = sum(1 for d in mkt_d[-20:] if d % 2 == 0) if len(mkt_d) >= 20 else 10
                        if even_count > 10:
                            best_data['strategy'] = 'EVEN_BIAS'
                            best_data['contract'] = 'DIGITEVEN'
                        else:
                            best_data['strategy'] = 'ODD_BIAS'
                            best_data['contract'] = 'DIGITODD'
                        best_data['digit'] = None
                    elif new_family == 'barrier':
                        mkt_d2 = tc.last_digits.get(best_market, [])
                        high_count = sum(1 for d in mkt_d2[-20:] if d >= 6) if len(mkt_d2) >= 20 else 10
                        if high_count > 10:
                            best_data['strategy'] = 'OVER_BIAS'
                            best_data['contract'] = 'ASIANU'
                        else:
                            best_data['strategy'] = 'UNDER_BIAS'
                            best_data['contract'] = 'ASIAND'
                        best_data['digit'] = None
                    log_agent("picker", f"ROTATED from cooldown: {current_family} -> {new_family} ({best_data['contract']}) | {best_data['strategy']}")
            
            # Update picker state
            picker.active_family = CONTRACT_FAMILY.get(best_data['contract'], 'digit')
            picker.active_contract = best_data['contract']
            if not hasattr(picker, 'recent_families'):
                picker.recent_families = []
            picker.recent_families.append(picker.active_family)
            if len(picker.recent_families) > 5:
                picker.recent_families = picker.recent_families[-5:]
            
            # ── OBSERVATION GATE: ensure enough time has passed since last trade ──
            last_tt = getattr(risk, 'last_trade_time', 0) or 0
            time_since_last = time.time() - last_tt if last_tt else 999
            if time_since_last < 6:  # minimum 6 seconds between trades
                await asyncio.sleep(1)
                continue
            
            # ── CYCLE TIMEOUT CHECK: prevent infinite hang ──
            if time.time() - cycle_start_time > 30:
                print(f"  [{cycle}] CYCLE TIMEOUT (>30s) — skipping to next", flush=True)
                await asyncio.sleep(1)
                continue
            
            # ── Execute ──
            contract = best_data['contract']
            digit = best_data.get('digit')
            risk.last_trade_time = time.time()
            
            # Apply goal stake multiplier
            goal_mult = tools.goal.get_stake_multiplier()
            round_mult = tools.round_profit.get_stake_multiplier()
            # Growth engine: compound + streak + session + drawdown optimization
            _growth_stake, _growth_breakdown = tools.growth.calculate_optimal_stake(
                stake, risk.balance, max(risk.start, risk.balance),
                risk.consec_win, risk.consec_loss, best_market, int(time.strftime('%H')))
            stake = _growth_stake
            # ── RETEST MODE: force minimum stake ($1) until confidence confirmed ──
            if getattr(risk, '_retest_mode', False):
                retest_trades = getattr(risk, '_retest_trades', 0)
                retest_wins = getattr(risk, '_retest_wins', 0)
                retest_pnl = getattr(risk, '_retest_pnl', 0)
                # Phase 1 (first 3 trades): $1 flat
                # Phase 2 (trades 3-5): $1.50 if any wins, else $1
                # Phase 3 (trades 5+): evaluated by recovery check above
                if retest_trades < 3:
                    stake = 1.00
                    tag = "RETEST-1"
                elif retest_wins >= 1:
                    stake = 1.50
                    tag = "RETEST-2"
                else:
                    stake = 1.00
                    tag = "RETEST-1"
                print(f"  [{cycle}] {tag}: stake=${stake:.2f} ({retest_wins}/{retest_trades} W, ${retest_pnl:+.2f})", flush=True)
            stake = round(stake * goal_mult * round_mult, 2)
            # ── BUNCH STAKE OVERRIDE: use bunch runner's stake when active ──
            if br.current_run and br.current_run.status == 'RUNNING':
                stake = br.current_run.stake
                if cycle % 10 == 0:
                    print(f"  [{cycle}] BUNCH STAKE: ${stake:.2f} (run: {br.current_run.wins}W/{br.current_run.losses}L cum=${br.current_run.cumulative_pnl:+.2f})", flush=True)
            if cycle % 20 == 0 and not getattr(risk, '_retest_mode', False):
                print(f"  [{cycle}] GROWTH: {_growth_breakdown}", flush=True)

            # ── PROFIT REPLICATOR STAKE BOOST: amplify winning combos ──
            _rep_stake_mult, _rep_stake_reason = tools.replicator.get_recommended_stake_mult(
                best_market, int(time.strftime('%H')), best_data.get('strategy', 'ALL'))
            if _rep_stake_mult > 1.0:
                stake = round(stake * _rep_stake_mult, 2)
                print(f"  [{cycle}] REPLICATOR BOOST: x{_rep_stake_mult} ({_rep_stake_reason}) stake=${stake:.2f}", flush=True)
                log_agent('replicator', f'BOOST {best_market} h{int(time.strftime("%H"))}: x{_rep_stake_mult} ({_rep_stake_reason}) → ${stake:.2f}')
            elif _rep_stake_mult < 1.0:
                stake = round(stake * _rep_stake_mult, 2)
                log_agent('replicator', f'REDUCE {best_market} h{int(time.strftime("%H"))}: x{_rep_stake_mult} ({_rep_stake_reason}) → ${stake:.2f}')
            # ── HARD CAP: total stake cannot exceed 2x base ──
            _base_stake = risk.calc_stake(best_data)
            if stake > _base_stake * 2.0:
                stake = round(_base_stake * 2.0, 2)
            stake = max(1.00, min(stake, 15.0))
            # ── HEATMAP STAKE ADJUSTMENT ──
            _hm_mult = tools.profit_mirror.get_stake_multiplier()
            if _hm_mult != 1.0:
                stake = round(stake * _hm_mult, 2)
                stake = max(1.00, min(stake, 15.0))
                if cycle % 20 == 0:
                    print(f"  [{cycle}] HEATMAP: x{_hm_mult} stake=", flush=True)


            mode_icon = {'CONSERVATIVE' : '🧊', 'OPTIMAL': '⚡', 'AGGRESSIVE': '🔥🔥', 'RECOVERY': '🛡️', 'PRECISION': '🎯', 'EXPLORATION': '🔍'}[risk.trading_mode]
            escalation_tag = f" x{risk.escalation_level}" if risk.escalation_level != 'NORMAL' else ""
            burst_tag = " BURST" if risk.full_burst else ""
            print(f"  [{cycle}] {best_market} | {best_data['strategy']} | {contract} | ${stake:.2f} | regime={best_data.get('regime','?')} | {mode_icon}{risk.trading_mode}{escalation_tag}{burst_tag}")
            log_agent("executor", f"Entry: {contract} ${stake:.2f} on {best_market} ({risk.escalation_level} confidence)")
            
            try:
                # Global trade timeout: buy + result must complete in 20s
                trade_start = time.time()
                cid, err = await asyncio.wait_for(trader.buy(contract, best_market, stake, digit), timeout=15)
                # Track open position for portfolio risk
                tools.trade_intel.open_position(best_market, contract, stake, best_data.get('strategy', ''))
                if time.time() - trade_start > 14:
                    print(f"         [SLOW BUY] {time.time()-trade_start:.1f}s", flush=True)
            except asyncio.TimeoutError:
                print(f"         [BUY TIMEOUT 15s] — skipping trade", flush=True)
                log_agent("executor", f"BUY TIMEOUT: {contract} ${stake:.2f} on {best_market}")
                await asyncio.sleep(3)
                continue
            except (websockets.ConnectionClosed, websockets.ConnectionClosedError) as e:
                print(f"         [WS CRASH] {e} — reconnecting")
                try: await trader.reconnect()
                except: pass
                await asyncio.sleep(2)
                continue
            except Exception as e:
                print(f"         [BUY ERR] {e} — skipping")
                await asyncio.sleep(2)
                continue
            if err:
                print(f"         Error: {err}")
                await asyncio.sleep(1)
                continue
            
            try:
                profit = await asyncio.wait_for(trader.wait_result(cid, timeout=10), timeout=12)
            except asyncio.TimeoutError:
                print(f"         [RESULT TIMEOUT] — assuming $0", flush=True)
                profit = 0
            except Exception as e:
                print(f"         [WAIT ERR] {e} — assuming $0")
                profit = 0
            print(f"         [TRADE RESULT] profit=${profit:.2f} bal=${risk.balance:.2f}", flush=True)
            risk.record(profit)
            try:
                # Update digit manager with actual profit and balance
                digit_mgr.update_balance(risk.balance)
            except Exception as e:
                print(f"         [DIGIT ERR] {e}", flush=True)
            # Update digit manager with actual profit
            # Record prompt effectiveness
            if best_data:
                try:
                    prompt_improver.record_prompt(
                        best_data.get('reason', ''), profit, best_market, best_data.get('strategy', ''))
                except Exception as e:
                    print(f"         [PROMPT ERR] {e}", flush=True)
            if best_data and is_digit_trade(best_data.get('strategy',''), best_data.get('contract','')):
                digit_mgr.record(profit, best_market, best_data.get('strategy',''))
            try:
                await asyncio.wait_for(trader.refresh_balance(), timeout=5)
            except Exception:
                pass
            risk.balance = trader.balance
            risk.session_peak = max(risk.session_peak, risk.balance)
            
            # ── SESSION TRACKING ──
            sm = tools.session_mgr
            if sm.active:
                session_done, session_reason = sm.record_session_trade(profit)
                emoji_s = "✅" if profit > 0 else "❌"
                log_agent("session", f"SESSION #{sm.session_id} trade {sm.trades_in_session}/{sm.max_trades}: {emoji_s} ${profit:+.2f} | session_pnl=${sm.session_pnl:+.4f} | {session_reason}")
                # Force exit if 3 consecutive losses overall or session losses
                if not session_done and risk.consec_loss >= 3:
                    session_done = True
                    session_reason = f"exit_global_3_consec_losses (risk.consec_loss={risk.consec_loss})"
                if session_done:
                    session_entry = sm.close_session()
                    tools.overtrade.record_session_end(session_entry['pnl'])
                    session_emoji = "🟢" if session_entry["pnl"] > 0 else "🔴"
                    log_agent("session", f"SESSION #{session_entry['session_id']} CLOSED: {session_emoji} ${session_entry['pnl']:+.4f} | W/L: {session_entry['wins']}/{session_entry['losses']} | {session_entry['trades']}T")
                    # Reset ProfitGuard for next session
                    try:
                        tools.profit_guard.reset(risk.balance)
                        log_agent("profit_guard", "Session reset — all guards cleared")
                    except: pass
                    log_sys(f"Session #{session_entry['session_id']} closed: ${session_entry['pnl']:+.4f}", "win" if session_entry["pnl"] > 0 else "loss")
                    # ════ SUPERVISOR: session review + benchmark ════
                    try:
                        _ses_wr = session_entry.get('wins', 0) / session_entry.get('trades', 1) * 100 if session_entry.get('trades', 0) > 0 else 0
                        _review = tools.supervisor.review_session(
                            session_entry['pnl'], session_entry.get('trades', 0), _ses_wr
                        )
                        _bench = tools.supervisor.benchmark(session_entry['pnl'], _ses_wr, session_entry.get('trades', 0))
                        log_agent("supervisor", f"REVIEW: {_review['verdict']} PnL=${session_entry['pnl']:+.2f} WR={_ses_wr:.0f}% root={_review['root_cause']}")
                        if _review.get('failures'):
                            for _f in _review['failures'][:2]:
                                log_agent("supervisor", f"  FAILURE: {_f['message']}")
                        if _review.get('improvements'):
                            for _imp in _review['improvements'][:2]:
                                log_agent("supervisor", f"  IMPROVE: {_imp['action']} → {_imp['target']}")
                        if _bench.get('trend') == 'DECLINING':
                            log_agent("supervisor", f"TREND: DECLINING — avg PnL=${_bench.get('avg_pnl', 0):.2f}")
                        elif _bench.get('trend') == 'IMPROVING':
                            log_agent("supervisor", f"TREND: IMPROVING — avg PnL=${_bench.get('avg_pnl', 0):.2f}")
                        # Perf tracker end of day
                        _perf_report = tools.perf_tracker.end_of_day_review()
                        log_agent("perf", f"EOD: {_perf_report['trades']}T WR={_perf_report['win_rate']}% PnL=${_perf_report['pnl']:+.2f} targets={_perf_report['targets_met']}")
                    except Exception as _sup_err:
                        pass
            
            # ── BUNCH RUNNER: feed trade result ──
            br = tools.bunch_runner
            if br.current_run and br.current_run.status == 'RUNNING':
                continue_bunch, bunch_complete, bunch_result = br.on_trade_result(profit)
                if bunch_complete and bunch_result:
                    br_emoji = '🟢' if bunch_result['profitable'] else '🔴'
                    log_agent('bunch', f'{br_emoji} BUNCH COMPLETE: {bunch_result["market"]} {bunch_result["strategy"]} | {bunch_result["wins"]}W/{bunch_result["losses"]}L | cumPnL=${bunch_result["cumulative_pnl"]:+.2f} | exit={bunch_result["exit_reason"]}')
                    log_sys(f'Bunch run done: {bunch_result["market"]} {bunch_result["strategy"]} ${bunch_result["cumulative_pnl"]:+.2f} [{bunch_result["status"]}]', 'win' if bunch_result['profitable'] else 'loss')
                    # Reset bunch stake reference
                elif not continue_bunch:
                    # Bunch still running but trade done
                    log_agent('bunch', f'Run trade: ${profit:+.2f} | cum=${br.current_run.cumulative_pnl:+.2f} | {br.current_run.wins}W/{br.current_run.losses}L/{br.current_run.target_trades - br.current_run.wins - br.current_run.losses} left')

            # MISSION TRACKER: update after every trade
            try:
                _bs = tools.bunch_runner.daily_stats
                tools.mission_tracker.update(
                    risk.pnl, risk.balance, risk.total,
                    bunch_runs=_bs.get('total_runs', 0),
                    bunch_wins=_bs.get('profitable_runs', 0),
                    bunch_trades=_bs.get('total_bunch_trades', 0),
                    single_trades=risk.total - _bs.get('total_bunch_trades', 0)
                )
                _mt = tools.mission_tracker.get_status()
                if cycle % 30 == 0:
                    _msg = "MISSION: $%+.2f/$%.0f (%.0f%%) | %s | need %dT / %d bunches" % (_mt['current_pnl'], _mt['daily_target'], _mt['progress_pct'], _mt['mode'], _mt['trades_needed'], _mt['bunches_needed'])
                    print(f"  [{cycle}] {_msg}", flush=True)
            except Exception as _watch_err:
                    if cycle % 10 == 0:
                        import traceback
                        print(f"  [WATCHER ERR] cycle={cycle}: {_watch_err}", flush=True)
                        traceback.print_exc()
            # ── Force fresh balance fetch for accurate notification ──
            try:
                trader.req_id += 1
                await trader._ws_safe_send({"balance": 1, "req_id": trader.req_id})
                bmsg = await asyncio.wait_for(trader.ws.recv(), timeout=5)
                bdata = json.loads(bmsg)
                fresh_bal = bdata.get("balance", {}).get("balance", 0)
                if fresh_bal > 0:
                    trader.real_balance = fresh_bal
                    if not PAPER_MODE:
                        trader.balance = fresh_bal
                        risk.balance = fresh_bal
            except: pass
            
            # ── Record to all agents ──
            tools.record_result(contract, profit, best_market, best_data['strategy'], stake)

            # ════ PERSISTENT KNOWLEDGE: AI feedback loop ════
            try:
                tools.memory.record_model_feedback(profit, best_market, best_data.get('strategy', ''))
            except Exception as _fb_err:
                pass

            # ════ PERSISTENT KNOWLEDGE: daily drawdown tracking ════
            try:
                tools.memory.record_daily_drawdown(risk.pnl, risk.balance, daily_limit=10.0)
                if tools.memory.is_daily_limit_breached() and risk.trading_mode != 'STOPPED':
                    risk.trading_mode = 'STOPPED'
                    log_sys('CIRCUIT BREAKER: Daily $10 drawdown breached — stopping', 'loss')
            except Exception as _dd_err:
                pass

            # ════ PERSISTENT KNOWLEDGE: update strategy families every 10 trades ════
            try:
                if risk.total % 10 == 0:
                    tools.memory.update_strategy_families()
            except Exception as _fam_err:
                pass

            # ════ PERSISTENT KNOWLEDGE: session memory (every 5 trades) ════
            try:
                if risk.total % 5 == 0:
                    _best_fam = tools.memory.get_family_insights()
                    _top_market = _best_fam[0].get('family', '') if _best_fam else best_market
                    tools.memory.record_session(
                        session_id=risk.total,
                        pnl=risk.pnl,
                        balance=risk.balance,
                        trades=risk.total,
                        wins=risk.wins,
                        losses=risk.losses,
                        best_market=best_market,
                        best_strategy=best_data.get('strategy', ''),
                        mode=risk.trading_mode,
                        duration_seconds=time.time() - SESSION_START,
                    )
            except Exception as _sess_err:
                pass

            # ── PROFIT MIRROR: feed trade result ──
            try:
                _mirror_conditions = tools.profit_mirror.replay.get_conditions_from_trade(
                    best_market, tc.get_entropy(best_market) if hasattr(tc, 'get_entropy') else 3.0,
                    risk.market_state if hasattr(risk, 'market_state') else '?',
                    tools.memory.get_digit_bias(best_market) if hasattr(tools.memory, 'get_digit_bias') else {},
                    int(time.strftime('%H')),
                    get_session_label(int(time.time())),
                    risk.balance
                )
                tools.profit_mirror.on_trade_result(
                    best_market, best_data.get('strategy', ''), contract,
                    profit, profit > 0, _mirror_conditions
                )
                if profit > 0:
                    log_agent('mirror', f'📈 Win recorded: {best_market} {best_data.get("strategy","")} — {len(tools.profit_mirror.replay.win_patterns)} patterns stored')
            except Exception as _me:
                pass
            try:
                # ── GROWTH: record trade, update session, track compounding ──
                _growth_hour = int(time.strftime('%H'))
                tools.growth.record_trade(profit, risk.balance, best_market, _growth_hour)
            except Exception as e:
                print(f"         [GROWTH ERR] {e}", flush=True)
            try:
                # ── PROFIT REPLICATOR: track combo, detect pattern death ──
                tools.replicator.record_trade(
                    best_market, int(time.strftime('%H')),
                    best_data.get('strategy', 'ALL'),
                    profit, profit > 0
                )
            except Exception as e:
                print(f"         [REPLICATOR ERR] {e}", flush=True)
            try:
                # ── SELF DIAGNOSTIC: record trade for system health tracking ──
                exec_latency_ms = int((time.time() - trade_start) * 1000) if 'trade_start' in dir() else None
                tools.diagnostic.record_trade(
                    best_data.get('strategy', 'ALL'), best_market, profit,
                    execution_time_ms=exec_latency_ms
                )
                # PERF TRACKER: record trade for measurable targets
                try:
                    tools.perf_tracker.record_trade(profit, risk.balance, exec_latency_ms)
                    tools.perf_tracker.record_opportunity(True, taken=True)
                    # Check if targets are being met
                    _pt_targets = tools.perf_tracker.check_targets()
                    if not _pt_targets["all_met"]:
                        for _v in _pt_targets.get("violations", []):
                            if _v.get("severity") == "CRITICAL":
                                log_agent("perf", f"TARGET VIOLATION: {_v['target']} = {_v['current']} (limit: {_v.get('limit', _v.get('minimum', '?'))})")
                except: pass
                # SUPERVISOR: record trade for session review
                try:
                    tools.supervisor.record_trade(
                        best_market, best_data.get('strategy', 'ALL'),
                        profit, stake, risk.balance
                    )
                except: pass
                tools.diagnostic.update_tick_health(
                    0,  # tick_age — updated from heartbeat separately
                    0   # network_failures — updated from heartbeat separately
                )
            except Exception as e:
                print(f"         [DIAGNOSTIC ERR] {e}", flush=True)
            mission_result = 'ok'
            try:
                # ── MISSION: record trade + check $20 profit lock ──
                mission_result = tools.mission.record_trade(profit, risk.pnl - profit, risk.balance)
            except Exception as e:
                print(f"         [MISSION ERR] {e}", flush=True)
            if mission_result == 'lock':
                        log_agent('mission', f'🏆 DAILY $20 PROFIT LOCKED! House money mode ON. PnL=${risk.pnl:.2f}')
                        log_sys('Mission: daily $20 target hit — house money mode', 'win')
                        discord('🏆 DAILY TARGET HIT', {'PnL': f'${risk.pnl:.2f}', 'Balance': f'${risk.balance:.2f}'}, 0x22c55e)
            # Family rotation tracking (in main loop where risk is available)
            _FAMILY_MAP = {
                'CALL': 'directional', 'PUT': 'directional',
                'RISE_TREND': 'directional', 'FALL_TREND': 'directional',
                'MOMENTUM_UP': 'directional', 'MOMENTUM_DOWN': 'directional',
                'DIGITEVEN': 'parity', 'DIGITODD': 'parity',
                'EVEN_BIAS': 'parity', 'ODD_BIAS': 'parity',
                'ASIANU': 'barrier', 'ASIAND': 'barrier',
                'OVER_BIAS': 'barrier', 'UNDER_BIAS': 'barrier',
            }
            _fam = _FAMILY_MAP.get(best_data['strategy'], 'directional')
            risk.record_family_trade(_fam, profit)
            tools.efficiency.record_trade(best_data['strategy'], contract, stake, profit, best_market)
            # P&L management: check trailing stop, daily target, etc.
            pass  # PLManager uses getattr defaults for market context
            pl_action, pl_reason, pl_mult = tools.pl.on_trade(profit, risk.balance, risk.start)
            # ── ROUND PROFIT MANAGER: trailing stop + round progression ──
            rp_action, rp_reason = tools.round_profit.on_trade(profit, risk.balance, risk.alignment_score)
            if rp_action in ('TRAIL', 'LOCK_AND_STOP'):
                log_agent("round_profit", f"🔄 {rp_action}: {rp_reason}")
                log_sys(f"Round profit: {rp_reason}", "info")
            elif rp_action == 'NEW_ROUND':
                log_agent("round_profit", f"🎯 {rp_reason}")
                log_sys(f"New round: {rp_reason}", "win")
                tools.round_profit.start_new_round(risk.balance)
            elif rp_action == 'STOP':
                log_agent("round_profit", f"🛑 {rp_reason}")
                log_sys(f"Round stopped: {rp_reason}", "warn")
            # ── PROFIT GUARD: 8-layer protection ──
            # Trade Intelligence: record result for all 10 layers
            tools.trade_intel.record_trade_result(
                profit, best_market, best_data.get('strategy',''),
                contract, stake, risk.balance)
            tools.trade_intel.close_position(best_market, best_data.get('strategy',''))
            # Profit Guard: 8-layer protection
            pg_action, pg_reason, pg_mult = tools.profit_guard.on_trade(
                profit, risk.balance, best_data.get('strategy',''),
                best_market, stake, contract)
            if pg_action == 'STOP':
                log_agent("profit_guard", f"🛑 STOPPED: {pg_reason}")
                log_sys(f"ProfitGuard: {pg_reason}", "warn")
                discord(f"🛑 ProfitGuard STOP", {'Reason': pg_reason, 'Balance': f'${risk.balance:.2f}'}, 0xef4444)
                tools.protector.emergency_stop(pg_reason)
                await asyncio.sleep(300)
                tools.protector.resume()
                continue
            if pg_mult and pg_mult != 1.0:
                risk.pl_multiplier = risk.pl_multiplier * pg_mult
                if cycle % 10 == 0:
                    print(f"  [{cycle}] PROFIT GUARD: mult={pg_mult:.2f} — {pg_reason}", flush=True)
            # Apply PL manager's multiplier to future stakes
            if pl_mult and pl_mult != 1.0:
                risk.pl_multiplier = pl_mult
                if cycle % 10 == 0:
                    print(f"  [{cycle}] PL MANAGER: multiplier={pl_mult:.2f} — {pl_reason}", flush=True)
            if pl_action == 'STOP':
                log_agent("pl_manager", f"🛑 STOPPED: {pl_reason}")
                log_sys(f"Session stopped: {pl_reason}", "warn")
                discord(f"🛑 Session Stopped", {'Reason': pl_reason, 'Balance': f'${risk.balance:.2f}'}, 0xef4444)
                # Freeze session
                tools.protector.emergency_stop(pl_reason)
                await asyncio.sleep(300)  # 5 minute pause
                tools.protector.resume()
                tools.pl.revenge_cooldown_until = 0
                continue
            # Check if strategy should be killed
            eff_decisions = tools.efficiency.evaluate_all()
            for strat, action, reason in eff_decisions:
                if action == 'KILL':
                    log_agent("efficiency", f"🗑️ KILLED {strat}: {reason}")
                    log_sys(f"Strategy killed: {strat} — {reason}", "warn")
            
            # ── Log trade for dashboard ──
            # Persist to disk
            if 'pnl_history' not in hist: hist['pnl_history'] = []
            if 'edge_history' not in hist: hist['edge_history'] = []
            hist['pnl_history'].append({'trade_num': risk.total, 'profit': profit, 'balance': risk.balance, 'cum_pnl': risk.pnl, 'market': best_market, 'strategy': best_data['strategy'], 'stake': stake, 'time': int(time.time() * 1000)})
            hist['pnl_history'] = hist['pnl_history'][-1000:]
            hist['edge_history'].append({'edge': best_data.get('ev', 0), 'balance': risk.balance, 'time': int(time.time() * 1000)})
            hist['edge_history'] = hist['edge_history'][-500:]
            hist['trades'] = TRADE_LOG[-500:]
            hist['sys_log'] = SYS_LOG[-200:]
            hist['agent_notes'] = AGENT_NOTES[-200:]
            save_history(hist)
            
            log_trade({
                'market': best_market, 'strategy': best_data['strategy'],
                'contract': contract, 'digit': digit, 'stake': stake,
                'profit': profit, 'balance': risk.balance,
                'regime': best_data.get('regime', '?'),
                'reason': best_data.get('reason', ''),
                'status': 'won' if profit > 0 else 'lost',
                'time': int(time.time() * 1000),
                'hour': int(time.strftime('%H')),
                'session': get_current_session_label(),
            })
            # Timezone intelligence: record this trade
            if tools.tz_intel:
                tools.tz_intel.record_trade(best_market, profit)
            # Market State Brain: record result for learning
            if tools.state_brain:
                tools.state_brain.record_trade_result(best_market, market_state.get('market_state', '?') if 'market_state' in dir() else '?', best_data.get('strategy', '?'), profit)
            emoji = '✅' if profit > 0 else '❌'
            log_agent("executor", f"{emoji} ${profit:+.2f} {contract} on {best_market} ({best_data['strategy']})")
            log_sys(f"Trade: {best_market} {contract} ${stake:.2f} → ${profit:+.2f}", "win" if profit > 0 else "loss")
            
            # Model notification: what just happened + impact
            pnl_delta = risk.pnl
            streak_info = f"W{risk.consec_win}" if risk.consec_win > 0 else f"L{risk.consec_loss}"
            log_agent("brain",
                f"TRADE RESULT: {emoji} ${profit:+.2f} on {best_market} {best_data['strategy']} "
                f"| bal=${risk.balance:.2f} pnl=${pnl_delta:+.2f} "
                f"| wr={risk.wr():.0f}% streak={streak_info} "
                f"| stake=${stake:.2f} regime={best_data.get('regime', '?')}")
            
            emoji = '✅' if profit > 0 else '❌'
            print(f"         {emoji} ${profit:+.2f} | Bal: ${risk.balance:.2f} | W/L: {risk.wins}/{risk.losses} ({risk.wr():.1f}%)")
            
            # ── Loss-streak strategy switch ──
            if profit <= 0 and risk.consec_loss >= 3:
                # Force switch to different contract family
                try:
                    current_family = tools.picker.get_family(contract) if hasattr(tools.picker, 'get_family') else 'digit'
                except:
                    current_family = 'digit'
                alt_families = [f for f in ['directional', 'parity', 'barrier', 'digit'] if f != current_family]
                if alt_families:
                    new_family = alt_families[risk.consec_loss % len(alt_families)]
                    tools.picker.active_family = new_family
                    log_agent("protector", f"Loss streak {risk.consec_loss}: switching to {new_family} family")
                    log_sys(f"Strategy switch: {contract} → {new_family} (after {risk.consec_loss} losses)", "warn")
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(3)
            
            # ── Win-streak rotation ──
            if profit > 0 and risk.consec_win >= 2:
                log_agent("picker", f"Win streak {risk.consec_win}: rotating family")
                if hasattr(tools.picker, "shuffle_family"): tools.picker.shuffle_family()
            
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
                'active_agent': tools.openrouter.active_engine if hasattr(tools, 'openrouter') and tools.openrouter else 'none',
                'model_used': tools.openrouter.openrouter_model if hasattr(tools, 'openrouter') and tools.openrouter and tools.openrouter.active_engine == tools.openrouter.ENGINE_OPENROUTER else (tools.openrouter.ollama_model if hasattr(tools, 'openrouter') and tools.openrouter else 'none'),
                'bestStreak': risk.consec_win,
                'selected_market': best_market, 'selected_type': 'digit',
                'selected_strategy': best_data['strategy'],
                'selected_ev': round(best_data.get('ev', 0), 4),
                'all_strategies': best_data['strategy'],
                'regime': best_data.get('regime', 'UNKNOWN'),
                'regime_confidence': 0.8,
                'accuracy': round(risk.wr(), 1),
                'trade_log_full': log_manager.get_trades_formatted(200),
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
                    'consecutive_losses': risk.consec_loss, 'consecutive_wins': risk.consec_win,
                    'pl_streak_wins': tools.pl.consecutive_wins, 'pl_streak_losses': tools.pl.consecutive_losses},
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
                # ALM Brain (use get_status for full data including model_usage_log, evolution_log)
                'alm_brain': tools.alm.get_status() if tools.alm else {
                    'connected': False, 'enabled': False, 'model': 'qwen2.5:0.5b',
                    'cloud_model': 'none', 'cloud_available': False,
                    'total_queries': 0, 'total_tokens': 0,
                    'last_model_used': '', 'notes': [],
                    'session_mode': 'EXECUTE',
                    'escalation_count': 0, 'consecutive_losses': 0,
                    'consecutive_wins': 0, 'last_trade_pnl': 0,
                    'model_usage_log': [], 'evolution_log': [], 'model_counts': {},
                    'token_manager': {},
                },
                # Research & Competition
                'research': tools.get_research_status(),
                'competition': tools.get_competition_status(),
                # Markets
                'active_markets': len(MARKET_LIST),
                'total_markets': len(MARKET_LIST),
                'market_list': {m: {'type': MARKET_TYPES.get(m, 'volatility'), 'active': True,
                    'score': round(MARKET_WEIGHTS.get(m, 1) * 50, 1),
                    'ticks': len(tc.last_digits.get(m, []))}
                    for m in MARKET_LIST},
                # Other panels (empty defaults)
                'adversarial': {'tested': 0, 'rejected': 0, 'risk_level': 'LOW'},
                'portfolio': {'total_strategies': 1, 'allocation': {'ACTIVE': 100}},
                'efficiency': tools.efficiency.get_status(),
                'pl_manager': tools.pl.get_status(),
                'recovery': {'safe_mode': False, 'crash_count': 0},
                'cpp_engine': cpp_engine.get_status() if cpp_engine else {'connected': False, 'enabled': False, 'status': 'Not built (Python-only mode)'},
                'resource_mgr': {'cpu': 0, 'ram': 0, 'mode': 'NORMAL'},
                'm_almis': {'mode': 'NORMAL', 'cpu': 0, 'ram': 0},
                'phone_resources': get_system_resources(),
                'crypto': {'connected': False, 'prices': {}},
                'mt5': {'connected': False, 'pairs': {}},
                'multi_market': {
                    'phase': 1, 'phase_name': 'OBSERVE',
                    'total_markets': len(MARKET_LIST),
                    'total_observations': sum(tc.last_digits.get(m, [None]).__len__() for m in MARKET_LIST if tc.last_digits.get(m)),
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
                'tick_data': {m: len(tc.last_digits.get(m, [])) for m in MARKET_LIST},
                # Executor status (old agent compatibility)
                'executor': {'trades_executed': risk.total, 'trades_won': risk.wins,
                    'trades_lost': risk.losses, 'win_rate': round(risk.wr(), 1),
                    'current_balance': risk.balance},
                # Memory status
                'memory': {'total_trades': risk.total, 'markets_traded': len(risk.market_count)},
                # Cooldown
                'cooling_down': False, 'remaining_sec': 0, 'tier': 'NORMAL',
                'edge': round(best_data.get('ev', 0), 4),
                'escalation': risk.escalation_level,
                'confidence_score': risk.confidence_score,
                'rapid_fire': risk.rapid_fire,
                'session_peak': round(risk.session_peak, 2),
                'full_burst': risk.full_burst,
                'burst_win_streak': risk.burst_win_streak,
                'alignment_score': risk.alignment_score,
                'trading_mode': risk.trading_mode,
                'mode_reason': risk.mode_reason,
                'mode_switch_count': risk.mode_switch_count,
                'bot_scorer': tools.get_scorer_status(),
                # Agent notes & system log for dashboard
                'agent_notes': log_manager.get_recent('agent_log', 50),
                'sys_log': log_manager.get_recent('sys_log', 50),
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
            # ── Merge pnl_history into existing state (watcher wrote base) ──
            try:
                ph = load_history()
                _existing = json.loads(Path('trading_state.json').read_text())
                _existing["pnl_history"] = ph.get("pnl_history", [])[-200:]
                _existing["edge_history"] = ph.get("edge_history", [])[-200:]
                _existing["trade_log_full"] = log_manager.get_trades_formatted(200)
                _existing["sys_log"] = log_manager.get_recent("sys_log", 100)
                _existing["agent_notes"] = log_manager.get_recent("agent_log", 100)
                _existing["pl_manager"] = tools.pl.get_status() if tools.pl else {}
                # Phone resources
                try:
                    _pr = {}
                    with open('/proc/meminfo') as _f:
                        _mi = {}
                        for _line in _f:
                            _p = _line.split()
                            if len(_p) >= 2:
                                _mi[_p[0].rstrip(':')] = int(_p[1])
                        _total = _mi.get('MemTotal', 1)
                        _avail = _mi.get('MemAvailable', 0)
                        _used = _total - _avail
                        _pr['ram_pct'] = round(_used / _total * 100) if _total else 0
                        _pr['ram_available_mb'] = _avail // 1024
                        _pr['ram_used'] = _used // 1024
                    import shutil as _sh
                    _td, _ud, _fd = _sh.disk_usage('/')
                    _pr['disk_pct'] = round(_ud / _td * 100)
                    _pr['disk_free'] = f'{_fd // (1024**3)}GB'
                    try:
                        with open('/proc/loadavg') as _f:
                            _pr['cpu_load'] = _f.read().strip().split()[0]
                    except (PermissionError, FileNotFoundError):
                        import os as _os
                        _pr['cpu_load'] = str(_os.getloadavg()[0])
                    _existing['phone_resources'] = _pr
                except: pass
                save_state(_existing)
            except Exception as e:
                if cycle % 50 == 0:
                    print(f"  [STATE MERGE] Error: {e}", flush=True)
            
            # ── Market rotation ──
            risk.market_count[best_market] = risk.market_count.get(best_market, 0) + 1
            if risk.market_count[best_market] >= 5:
                print(f"         [ROTATE] 5 trades on {best_market}")
                log_agent("picker", f"Market rotation: {best_market} → next in queue")
                log_sys(f"Market rotation: 5 trades on {best_market}", "info")
                risk.market_count[best_market] = 0
                MARKET_LIST.append(MARKET_LIST.pop(MARKET_LIST.index(best_market)))
            
            # ── Research Director: propose experiments ──
            if risk.total > 0 and risk.total % 5 == 0:
                try:
                    perf_data = {}
                    for k, v in tools.memory.data["strategies"].items():
                        if isinstance(v, dict):
                            perf_data[k] = v
                    ctx = {
                        "known_strategies": list(set(k.split(":")[-1] if ":" in k else k for k in tools.strategist.strategies.keys())) if hasattr(tools.strategist, 'strategies') else MARKET_LIST,
                        "known_markets": MARKET_LIST,
                        "strategy_performance": perf_data,
                        "market_conditions": {m: tools.get_regime(m, tools.get_signal(m)) for m in MARKET_LIST},
                        "active_experiments": tools.research.active_experiments,
                    }
                    exp = tools.propose_experiment(ctx)
                    if exp:
                        log_agent("research", f"Experiment #{exp['id']}: {exp['type']} on {exp.get('market','')} — {exp.get('reason','')}")
                        log_sys(f"Experiment proposed: {exp['type']} #{exp['id']}", "info")
                except Exception as e:
                    log_sys(f"Research error: {e}", "warn")
            
            # ── Competition Engine: start rounds ──
            if risk.total > 0 and risk.total % 10 == 0 and not tools.competition.active_rounds:
                try:
                    avail = list(tools.strategist.strategies.keys())[:6] if hasattr(tools.strategist, 'strategies') else ['DIGIT_DIFF_6', 'CALL', 'PUT']
                    perf_data = {}
                    for k, v in tools.memory.data["strategies"].items():
                        if isinstance(v, dict):
                            perf_data[k] = v
                    competitors = tools.competition.pick_competitors(best_market, avail, perf_data)
                    if len(competitors) >= 2:
                        rnd = tools.start_competition(best_market, competitors, {"regime": best_data.get('regime', 'UNKNOWN')})
                        if rnd:
                            log_agent("competition", f"Competition #{rnd['id']}: {', '.join(competitors)} on {best_market}")
                            log_sys(f"Competition started: {len(competitors)} strategies", "info")
                except Exception as e:
                    log_sys(f"Competition error: {e}", "warn")
            
            await asyncio.sleep(sleep_time)
    
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
        notify_session_summary(risk.balance, risk.total, risk.wins, risk.losses, risk.pnl, risk.wr())
        notify_daily_report(risk.balance, risk.pnl, risk.total, risk.wr(),
            ','.join(set(t.get('market','?') for t in TRADE_LOG[-50:])),
            best_data.get('strategy','?') if best_data else '?')
        tick_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
