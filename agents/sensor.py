"""
SENSOR — Lightweight tick buffer and signal generator.
No heavy indicators — brain_active.py does digit analysis directly.
"""
import time

class SensorAgent:
    def __init__(self, buffer_size=200):
        self.buffer_size = buffer_size
        self.data = {}  # symbol -> list of ticks
        self.deltas = {}
        self.digit_counts = {}
    
    def ingest_tick(self, tick_data, market_type="volatility"):
        """Ingest a tick."""
        symbol = tick_data.get('symbol', '')
        price = tick_data.get('quote', 0)
        epoch = tick_data.get('epoch', int(time.time()))
        
        if symbol not in self.data:
            self.data[symbol] = []
        
        self.data[symbol].append({'price': price, 'epoch': epoch, 'type': market_type})
        if len(self.data[symbol]) > self.buffer_size:
            self.data[symbol] = self.data[symbol][-self.buffer_size:]
    
    def ingest(self, price, epoch, symbol):
        """Legacy interface."""
        self.ingest_tick({'price': price, 'quote': price, 'epoch': epoch, 'symbol': symbol})
    
    def get_signal(self, symbol, market_type="volatility"):
        """Generate simple signal from tick data."""
        ticks = self.data.get(symbol, [])
        if len(ticks) < 10:
            return None
        
        prices = [t['price'] for t in ticks[-50:]]
        if not prices:
            return None
        
        # Simple statistics
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        std = variance ** 0.5
        
        recent_mean = sum(prices[-10:]) / 10
        z_score = (recent_mean - mean) / std if std > 0 else 0
        
        # Direction
        if len(prices) >= 5:
            direction = 'UP' if prices[-1] > prices[-5] else 'DOWN'
        else:
            direction = 'NEUTRAL'
        
        # Digit frequency
        digits = {}
        for p in prices:
            s = str(p).rstrip('0').lstrip('0')
            if s and s[-1].isdigit():
                d = int(s[-1])
                digits[d] = digits.get(d, 0) + 1
        
        return {
            'recent_mean': round(recent_mean, 4),
            'z_score': round(z_score, 3),
            'direction': direction,
            'volatility': round(std, 6),
            'tick_count': len(prices),
            'digit_freq': digits,
        }
    
    def get_data(self, symbol):
        return self.data.get(symbol, [])
    
    def get_status(self):
        return {sym: len(ticks) for sym, ticks in self.data.items()}
