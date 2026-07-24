"""
PROFIT MIRROR — Three techniques to replicate winning behavior:

1. MARKET ECHO: When one market wins, try correlated markets immediately
2. WIN PATTERN REPLAY: Record win conditions, replay when they reappear
3. HOURLY HEATMAP: Track P&L per hour, boost/reduce activity automatically

These mirror profitable algorithms by learning from what works.
"""
import json
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

MIRROR_FILE = Path(__file__).parent.parent / 'profit_mirror_state.json'

# Market correlation groups — these tend to echo each other
MARKET_ECHO_GROUPS = [
    ['R_75', 'R_25', 'R_50'],        # R-series echo
    ['JD75', 'JD50', 'JD25'],        # JD-series echo
    ['JD100', 'R_100'],               # Large index echo
    ['JD10', 'R_10'],                 # Small index echo
    ['JD25', 'R_25'],                 # Cross-series echo
    ['JD50', 'R_50'],                 # Cross-series echo
]

def get_echo_markets(winning_market):
    """Return correlated markets that echo the winner."""
    echoes = []
    for group in MARKET_ECHO_GROUPS:
        if winning_market in group:
            echoes.extend([m for m in group if m != winning_market])
    return echoes


class MarketEcho:
    """When a market wins, immediately try correlated markets."""

    def __init__(self):
        self.echoes = []  # pending echoes: [{market, strategy, contract, time, expiry}]
        self.max_pending = 3  # max simultaneous echoes
        self.echo_cooldown = 15  # seconds between echoes on same market

    def on_trade_result(self, market, strategy, contract, pnl, won):
        """After a win, queue echoes on correlated markets."""
        if not won:
            # Loss clears pending echoes (setup broke)
            self.echoes.clear()
            return

        echo_markets = get_echo_markets(market)
        if not echo_markets:
            return

        now = time.time()
        for em in echo_markets:
            # Don't duplicate pending echoes
            if any(e['market'] == em and e['strategy'] == strategy for e in self.echoes):
                continue
            # Don't echo to markets recently echoed
            recent = [e for e in self.echoes if e['market'] == em and (now - e['time']) < self.echo_cooldown]
            if recent:
                continue
            if len(self.echoes) >= self.max_pending:
                break
            self.echoes.append({
                'market': em,
                'strategy': strategy,
                'contract': contract,
                'time': now,
                'expiry': now + 60,  # echo expires in 60s
                'source_market': market,
                'source_pnl': pnl,
            })

    def get_next_echo(self):
        """Get next pending echo to execute, or None."""
        now = time.time()
        # Remove expired
        self.echoes = [e for e in self.echoes if e['expiry'] > now]
        if self.echoes:
            return self.echoes.pop(0)
        return None

    def clear(self):
        self.echoes.clear()

    def get_status(self):
        return {
            'pending': len(self.echoes),
            'echoes': [{'market': e['market'], 'strategy': e['strategy']} for e in self.echoes[:3]]
        }


class WinPatternReplay:
    """Record the exact conditions of every win, replay when they reappear."""

    def __init__(self):
        self.win_patterns = []  # [{conditions, time, market, strategy, pnl}]
        self.max_patterns = 500
        self.match_threshold = 3  # min matching conditions to replay

    def record_win(self, market, strategy, contract, pnl, conditions):
        """Record a winning trade with its full conditions."""
        self.win_patterns.append({
            'market': market,
            'strategy': strategy,
            'contract': contract,
            'pnl': pnl,
            'conditions': conditions,  # {entropy, regime, digit_bias, hour, session, balance_range}
            'time': time.time(),
        })
        # Keep last N patterns
        if len(self.win_patterns) > self.max_patterns:
            self.win_patterns = self.win_patterns[-self.max_patterns:]

    def find_replay(self, current_market, current_conditions):
        """Find a matching win pattern to replay."""
        matches = []
        for p in self.win_patterns:
            if p['market'] != current_market:
                continue
            # Score how many conditions match
            score = 0
            total = 0
            pc = p.get('conditions', {})
            cc = current_conditions
            for key in ['regime', 'hour_range', 'session']:
                if key in pc and key in cc:
                    total += 1
                    if pc[key] == cc[key]:
                        score += 1
            for key in ['entropy_range']:
                if key in pc and key in cc:
                    total += 1
                    if pc[key] == cc[key]:
                        score += 1

            if total > 0 and score >= min(self.match_threshold, total):
                age = time.time() - p['time']
                # Prefer recent patterns (within 24h)
                if age < 86400:
                    matches.append({
                        'pattern': p,
                        'score': score,
                        'total': total,
                        'age_h': age / 3600,
                    })

        if matches:
            # Return best match (highest score, most recent)
            matches.sort(key=lambda x: (x['score'], -x['age_h']), reverse=True)
            return matches[0]
        return None

    def get_conditions_from_trade(self, market, entropy, regime, digit_bias, hour, session_label, balance):
        """Build conditions dict from current state."""
        # Entropy range bucket
        if entropy < 2.8:
            entropy_range = 'low'
        elif entropy < 3.15:
            entropy_range = 'medium'
        else:
            entropy_range = 'high'

        # Hour range bucket
        if 0 <= hour < 6:
            hour_range = 'night'
        elif 6 <= hour < 12:
            hour_range = 'morning'
        elif 12 <= hour < 18:
            hour_range = 'afternoon'
        else:
            hour_range = 'evening'

        # Balance range
        if balance > 9900:
            balance_range = 'high'
        elif balance > 9800:
            balance_range = 'mid'
        else:
            balance_range = 'low'

        return {
            'regime': regime,
            'entropy_range': entropy_range,
            'hour_range': hour_range,
            'session': session_label,
            'balance_range': balance_range,
        }

    def get_status(self):
        return {
            'patterns_stored': len(self.win_patterns),
            'last_win': self.win_patterns[-1]['market'] + ' ' + self.win_patterns[-1]['strategy'] if self.win_patterns else None,
        }


class HourlyHeatmap:
    """Track P&L per hour, auto-boost/reduce activity."""

    def __init__(self):
        self.hourly = defaultdict(lambda: {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0})
        self.today = ''
        self._load()

    def _load(self):
        try:
            if MIRROR_FILE.exists():
                data = json.loads(MIRROR_FILE.read_text())
                saved = data.get('hourly_heatmap', {})
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if saved.get('date') == today:
                    self.hourly = defaultdict(lambda: {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}, saved.get('hours', {}))
                    self.today = today
        except Exception:
            pass

    def record_trade(self, hour, pnl, won):
        h = str(hour)
        self.hourly[h]['trades'] += 1
        self.hourly[h]['pnl'] = round(self.hourly[h]['pnl'] + pnl, 4)
        if won:
            self.hourly[h]['wins'] += 1
        else:
            self.hourly[h]['losses'] += 1

    def get_hour_status(self, hour):
        """Get status for current hour."""
        h = str(hour)
        data = self.hourly.get(h, {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0})
        trades = data['trades']
        pnl = data['pnl']
        wr = (data['wins'] / trades * 100) if trades > 0 else 0
        return {'trades': trades, 'pnl': pnl, 'wr': wr, 'wins': data['wins'], 'losses': data['losses']}

    def get_stake_multiplier(self, hour):
        """Return stake multiplier based on hourly performance.
        Profitable hours: boost. Losing hours: reduce.
        """
        data = self.get_hour_status(hour)
        trades = data['trades']
        pnl = data['pnl']
        wr = data['wr']

        if trades < 3:
            return 1.0  # not enough data

        if pnl > 2.0 and wr >= 60:
            return 1.5  # hot hour — boost
        elif pnl > 0.5 and wr >= 55:
            return 1.25  # warm
        elif pnl > 0:
            return 1.0  # neutral
        elif pnl > -2.0:
            return 0.75  # cold — reduce
        else:
            return 0.5  # bleeding hour — minimal

    def is_hot_hour(self, hour):
        """Check if this hour is historically profitable."""
        data = self.get_hour_status(hour)
        return data['pnl'] > 1.0 and data['trades'] >= 3 and data['wr'] >= 55

    def get_heatmap_data(self):
        """Return full heatmap for dashboard."""
        result = []
        for h in range(24):
            data = self.get_hour_status(h)
            result.append({
                'hour': h,
                'trades': data['trades'],
                'pnl': data['pnl'],
                'wr': data['wr'],
                'status': 'hot' if data['pnl'] > 1.0 and data['trades'] >= 3 else 'warm' if data['pnl'] > 0 else 'cold' if data['pnl'] > -2.0 else 'bleeding',
            })
        return result


class ProfitMirror:
    """Master controller for all three mirror techniques."""

    def __init__(self):
        self.echo = MarketEcho()
        self.replay = WinPatternReplay()
        self.heatmap = HourlyHeatmap()
        self._load()

    def _load(self):
        try:
            if MIRROR_FILE.exists():
                data = json.loads(MIRROR_FILE.read_text())
                # Restore replay patterns
                for p in data.get('win_patterns', []):
                    self.replay.win_patterns.append(p)
                self.replay.win_patterns = self.replay.win_patterns[-500:]
        except Exception:
            pass

    def _save(self):
        try:
            data = {
                'hourly_heatmap': {
                    'date': self.heatmap.today or datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                    'hours': dict(self.heatmap.hourly),
                },
                'win_patterns': self.replay.win_patterns[-500:],
                'echoes_pending': self.echo.get_status()['pending'],
            }
            MIRROR_FILE.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass

    def on_trade_result(self, market, strategy, contract, pnl, won, conditions=None):
        """Call after every trade. Triggers echo, records patterns, updates heatmap."""
        hour = int(time.strftime('%H'))
        self.heatmap.record_trade(hour, pnl, won)

        # Market echo on wins
        if won:
            self.echo.on_trade_result(market, strategy, contract, pnl, won)
            # Record win pattern
            if conditions:
                self.replay.record_win(market, strategy, contract, pnl, conditions)

        self._save()

    def get_next_echo(self):
        """Get next echo trade to execute."""
        return self.echo.get_next_echo()

    def get_replay(self, market, conditions):
        """Check if a win pattern should be replayed."""
        return self.replay.find_replay(market, conditions)

    def get_stake_multiplier(self, hour=None):
        """Combined stake multiplier from heatmap."""
        if hour is None:
            hour = int(time.strftime('%H'))
        return self.heatmap.get_stake_multiplier(hour)

    def get_status(self):
        return {
            'echo': self.echo.get_status(),
            'replay': self.replay.get_status(),
            'heatmap_today': sum(1 for h in range(24) if self.heatmap.get_hour_status(h)['trades'] > 0),
            'hot_hours': sum(1 for h in range(24) if self.heatmap.is_hot_hour(h)),
            'stake_mult': self.get_stake_multiplier(),
            'heatmap': self.heatmap.get_heatmap_data(),
        }
