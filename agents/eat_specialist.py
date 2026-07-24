
"""
EAT MARKET SPECIALIST — Kenya/East Africa timezone intelligence

Maps all trading windows to EAT (UTC+3) and identifies:
  - Optimal execution windows per session
  - ISP congestion spikes (5-9 PM EAT)
  - Platform rollover window (11:55 PM - 12:15 AM EAT)
  - Session overlaps for maximum liquidity
"""
import time
from datetime import datetime, timezone, timedelta

EAT = timezone(timedelta(hours=3))

# Session definitions in EAT
SESSIONS = {
    'asian_late': {
        'start': 0, 'end': 4,  # EAT
        'label': 'Asian Late',
        'quality': 'low',
        'reason': 'Low liquidity, wide spreads, broker processing resets',
    },
    'asian_early': {
        'start': 4, 'end': 7,
        'label': 'Asian Early',
        'quality': 'medium',
        'reason': 'Tokyo open, moderate liquidity',
    },
    'eu_open': {
        'start': 7, 'end': 10,
        'label': 'EU Open',
        'quality': 'high',
        'reason': 'London open, high volume, tight spreads',
    },
    'eu_mid': {
        'start': 10, 'end': 13,
        'label': 'EU Mid',
        'quality': 'high',
        'reason': 'Peak EU session, best liquidity',
    },
    'us_open': {
        'start': 13, 'end': 16,
        'label': 'US Open',
        'quality': 'high',
        'reason': 'NY open overlap with EU, maximum liquidity',
    },
    'us_afternoon': {
        'start': 16, 'end': 19,
        'label': 'US Afternoon',
        'quality': 'medium',
        'reason': 'US session active, EU winding down',
    },
    'evening': {
        'start': 19, 'end': 22,
        'label': 'Evening',
        'quality': 'low',
        'reason': 'ISP congestion in East Africa (5-9 PM EAT), micro-latency risk',
    },
    'night': {
        'start': 22, 'end': 24,
        'label': 'Night',
        'quality': 'very_low',
        'reason': 'Low volume, approaching rollover window',
    },
}

# ISP congestion window (East Africa)
ISP_CONGESTION_START = 17  # 5 PM EAT
ISP_CONGESTION_END = 21    # 9 PM EAT

# Platform rollover window (Deriv)
ROLLOVER_START = 23  # 11:55 PM EAT (23:55)
ROLLOVER_END = 0     # 12:15 AM EAT (00:15 next day)

# Optimal windows for each contract type in EAT
CONTRACT_OPTIMAL = {
    'DIGITDIFF': {
        'best_hours': [8, 9, 10, 11, 14, 15],  # EU/US peak
        'avoid_hours': [0, 1, 2, 3, 23],         # Night/rollover
        'reason': 'Digit strategies need tight spreads and fast execution',
    },
    'DIGITMATCH': {
        'best_hours': [9, 10, 14, 15],           # Peak liquidity
        'avoid_hours': [0, 1, 2, 3, 4, 23],
        'reason': 'Match needs fast fills, avoid low-liquidity hours',
    },
    'RISE': {
        'best_hours': [7, 8, 9, 10, 13, 14, 15],
        'avoid_hours': [0, 1, 2, 3, 22, 23],
        'reason': 'Trend strategies work best in active sessions',
    },
    'FALL': {
        'best_hours': [7, 8, 9, 10, 13, 14, 15],
        'avoid_hours': [0, 1, 2, 3, 22, 23],
        'reason': 'Trend strategies work best in active sessions',
    },
    'EVEN': {
        'best_hours': [8, 9, 10, 11, 14, 15, 16],
        'avoid_hours': [0, 1, 2, 3, 23],
        'reason': 'Parity strategies need balanced market conditions',
    },
    'ODD': {
        'best_hours': [8, 9, 10, 11, 14, 15, 16],
        'avoid_hours': [0, 1, 2, 3, 23],
        'reason': 'Parity strategies need balanced market conditions',
    },
    'OVER': {
        'best_hours': [9, 10, 14, 15],
        'avoid_hours': [0, 1, 2, 3, 22, 23],
        'reason': 'Barrier contracts need volatile, trending markets',
    },
    'UNDER': {
        'best_hours': [9, 10, 14, 15],
        'avoid_hours': [0, 1, 2, 3, 22, 23],
        'reason': 'Barrier contracts need volatile, trending markets',
    },
}


class EatMarketSpecialist:
    """Kenyan/EAT timezone intelligence for optimal trade timing."""

    def __init__(self):
        self.eat_offset = 3

    def now_eat(self):
        return datetime.now(EAT)

    def current_hour_eat(self):
        return self.now_eat().hour

    def get_session(self, hour_eat=None):
        h = hour_eat if hour_eat is not None else self.current_hour_eat()
        for key, sess in SESSIONS.items():
            if sess['start'] <= h < sess['end']:
                return key, sess
        return 'night', SESSIONS['night']

    def is_isp_congestion(self, hour_eat=None):
        h = hour_eat if hour_eat is not None else self.current_hour_eat()
        return ISP_CONGESTION_START <= h < ISP_CONGESTION_END

    def is_rollover_window(self, hour_eat=None, minute=None):
        now = self.now_eat()
        h = hour_eat if hour_eat is not None else now.hour
        m = minute if minute is not None else now.minute
        if h == 23 and m >= 55:
            return True
        if h == 0 and m <= 15:
            return True
        return False

    def get_time_quality(self, hour_eat=None):
        h = hour_eat if hour_eat is not None else self.current_hour_eat()
        if self.is_rollover_window(h):
            return 0, 'CRITICAL: Rollover window — DO NOT TRADE'
        if self.is_isp_congestion(h):
            return 10, 'ISP congestion (5-9 PM EAT) — micro-latency risk'
        _, sess = self.get_session(h)
        quality_map = {'high': 90, 'medium': 60, 'low': 30, 'very_low': 10}
        return quality_map.get(sess['quality'], 50), sess['reason']

    def get_contract_advice(self, contract_type, hour_eat=None):
        h = hour_eat if hour_eat is not None else self.current_hour_eat()
        ct = contract_type.upper()
        # Map contract types to the lookup key
        key = ct.replace('DIGITDIFF', 'DIGITDIFF').replace('DIGITMATCH', 'DIGITMATCH')
        if 'DIGIT' in ct and 'MATCH' in ct:
            key = 'DIGITMATCH'
        elif 'DIGIT' in ct:
            key = 'DIGITDIFF'
        elif 'CALL' in ct or 'RISE' in ct:
            key = 'RISE'
        elif 'PUT' in ct or 'FALL' in ct:
            key = 'FALL'
        elif 'EVEN' in ct:
            key = 'EVEN'
        elif 'ODD' in ct:
            key = 'ODD'
        elif 'OVER' in ct:
            key = 'OVER'
        elif 'UNDER' in ct:
            key = 'UNDER'
        else:
            return 50, 'Unknown contract type'

        info = CONTRACT_OPTIMAL.get(key, {})
        best = info.get('best_hours', [])
        avoid = info.get('avoid_hours', [])

        if h in avoid:
            return 15, f'AVOID {ct} at h{h}:00 EAT — {info.get("reason", "suboptimal window")}'
        if h in best:
            return 90, f'OPTIMAL {ct} at h{h}:00 EAT — peak liquidity'
        return 50, f'Neutral {ct} at h{h}:00 EAT'

    def get_market_quality_score(self, hour_eat=None):
        time_score, reason = self.get_time_quality(hour_eat)
        # time_quality already returns low score for congestion (10)
        # Only add rollover penalty (congestion already factored in)
        rollover_penalty = -50 if self.is_rollover_window(hour_eat) else 0
        return max(0, min(100, time_score + rollover_penalty))

    def get_status(self):
        h = self.current_hour_eat()
        session_key, session_info = self.get_session(h)
        quality, reason = self.get_time_quality(h)
        return {
            'current_hour_eat': h,
            'current_session': session_key,
            'session_label': session_info['label'],
            'session_quality': session_info['quality'],
            'time_quality': quality,
            'quality_reason': reason,
            'isp_congestion': self.is_isp_congestion(h),
            'rollover_window': self.is_rollover_window(h),
            'market_quality_score': self.get_market_quality_score(h),
        }
