"""
PRICE ACTION ANALYST — Reads what price is doing.
Detects: support/resistance, breakouts, rejections, momentum shifts, market structure.
"""
import time


class PriceAction:
    """Analyzes raw tick data for price action patterns."""

    def __init__(self):
        self.cache = {}  # symbol -> last analysis

    def analyze(self, ticks, symbol="unknown"):
        """
        Full price action analysis on a list of tick prices.
        Returns structured analysis dict.
        """
        if not ticks or len(ticks) < 10:
            return {"ready": False, "reason": "insufficient_ticks", "ticks": len(ticks or [])}

        prices = list(ticks)
        n = len(prices)

        result = {
            "ready": True,
            "symbol": symbol,
            "ticks": n,

            # Current price
            "current_price": prices[-1],
            "price_change": prices[-1] - prices[0],

            # Trend structure
            "trend": self._detect_trend(prices),

            # Support / Resistance
            "levels": self._find_support_resistance(prices),

            # Momentum
            "momentum": self._analyze_momentum(prices),

            # Breakout detection
            "breakout": self._detect_breakout(prices),

            # Rejection detection
            "rejection": self._detect_rejection(prices),

            # Market structure
            "structure": self._analyze_structure(prices),

            # Combined signal
            "signal": None,
            "confidence": 0,
            "reason": "",
        }

        # Combine all signals into one decision signal
        result["signal"], result["confidence"], result["reason"] = self._combine_signals(result)

        self.cache[symbol] = result
        return result

    def _detect_trend(self, prices):
        """Detect trend direction, strength, and structure."""
        n = len(prices)
        if n < 5:
            return {"direction": "NEUTRAL", "strength": 0, "hh_ll": "none"}

        # Simple trend: compare first half vs second half
        half = n // 2
        first_avg = sum(prices[:half]) / half
        second_avg = sum(prices[half:]) / (n - half)
        diff = second_avg - first_avg

        # Higher highs / lower lows
        highs = [max(prices[i:i+3]) for i in range(0, n-2, 3)]
        lows = [min(prices[i:i+3]) for i in range(0, n-2, 3)]

        if len(highs) >= 2:
            hh = all(highs[i] >= highs[i-1] for i in range(1, len(highs)))
            ll = all(lows[i] <= lows[i-1] for i in range(1, len(lows)))
        else:
            hh = False
            ll = False

        if diff > 0.01:
            direction = "UP"
            strength = min(1.0, abs(diff) / 0.05)
        elif diff < -0.01:
            direction = "DOWN"
            strength = min(1.0, abs(diff) / 0.05)
        else:
            direction = "NEUTRAL"
            strength = 0

        hh_ll = "HH_HL" if hh else "LH_LL" if ll else "mixed"

        return {
            "direction": direction,
            "strength": round(strength, 3),
            "hh_ll": hh_ll,
            "slope": round(diff, 6),
        }

    def _find_support_resistance(self, prices):
        """Find support and resistance levels from recent price action."""
        n = len(prices)
        if n < 10:
            return {"support": [], "resistance": []}

        # Find local highs and lows (peaks and valleys)
        peaks = []
        valleys = []
        for i in range(2, n - 2):
            if prices[i] > prices[i-1] and prices[i] > prices[i+1]:
                peaks.append(prices[i])
            elif prices[i] < prices[i-1] and prices[i] < prices[i+1]:
                valleys.append(prices[i])

        current = prices[-1]

        # Resistance: peaks near or above current price
        resistance = sorted([p for p in peaks if p >= current * 0.999])[:3]
        # Support: valleys near or below current price
        support = sorted([v for v in valleys if v <= current * 1.001], reverse=True)[:3]

        # Distance to nearest support/resistance
        near_resistance = min(resistance) if resistance else None
        near_support = max(support) if support else None

        dist_to_resistance = (near_resistance - current) / current * 100 if near_resistance else None
        dist_to_support = (current - near_support) / current * 100 if near_support else None

        return {
            "support": support,
            "resistance": resistance,
            "nearest_support": near_support,
            "nearest_resistance": near_resistance,
            "dist_to_support_pct": round(dist_to_support, 4) if dist_to_support is not None else None,
            "dist_to_resistance_pct": round(dist_to_resistance, 4) if dist_to_resistance is not None else None,
            "at_support": dist_to_support is not None and dist_to_support < 0.02,
            "at_resistance": dist_to_resistance is not None and dist_to_resistance < 0.02,
        }

    def _analyze_momentum(self, prices):
        """Analyze momentum using rate of change and acceleration."""
        n = len(prices)
        if n < 10:
            return {"roc": 0, "acceleration": 0, "overbought": False, "oversold": False}

        # Rate of change (last 5 vs previous 5)
        recent = prices[-5:]
        previous = prices[-10:-5]
        roc = (sum(recent) / 5) - (sum(previous) / 5)

        # Acceleration (change in rate of change)
        if n >= 15:
            prev_recent = prices[-10:-5]
            prev_previous = prev_previous = prices[-15:-10]
            prev_roc = (sum(prev_recent) / 5) - (sum(prev_previous) / 5)
            acceleration = roc - prev_roc
        else:
            acceleration = 0

        # Overbought/oversold using RSI-like logic
        gains = []
        losses = []
        for i in range(1, min(15, n)):
            change = prices[-i] - prices[-i-1]
            if change > 0:
                gains.append(change)
            else:
                losses.append(abs(change))

        avg_gain = sum(gains) / max(len(gains), 1)
        avg_loss = sum(losses) / max(len(losses), 1)
        rs = avg_gain / max(avg_loss, 0.0001)
        rsi = 100 - (100 / (1 + rs))

        return {
            "roc": round(roc, 6),
            "acceleration": round(acceleration, 6),
            "rsi": round(rsi, 1),
            "overbought": rsi > 70,
            "oversold": rsi < 30,
            "momentum_building": abs(acceleration) > abs(roc) * 0.5 if roc != 0 else False,
        }

    def _detect_breakout(self, prices):
        """Detect breakout from recent range."""
        n = len(prices)
        if n < 15:
            return {"detected": False}

        # Calculate recent range (middle 60% to avoid extremes)
        sorted_prices = sorted(prices[-20:])
        cut = max(1, len(sorted_prices) // 5)
        trimmed = sorted_prices[cut:-cut] if cut < len(sorted_prices) // 2 else sorted_prices
        range_high = max(trimmed)
        range_low = min(trimmed)
        range_size = range_high - range_low

        current = prices[-1]
        prev = prices[-2] if n >= 2 else current

        # Breakout: price moves outside range
        breakout_up = current > range_high and prev <= range_high
        breakout_down = current < range_low and prev >= range_low

        # False breakout: price breaks then immediately reverses
        if n >= 3:
            false_breakout_up = (prices[-2] > range_high and prices[-1] < range_high)
            false_breakout_down = (prices[-2] < range_low and prices[-1] > range_low)
        else:
            false_breakout_up = False
            false_breakout_down = False

        return {
            "detected": breakout_up or breakout_down,
            "direction": "UP" if breakout_up else "DOWN" if breakout_down else "NONE",
            "false_breakout": false_breakout_up or false_breakout_down,
            "range_high": round(range_high, 4),
            "range_low": round(range_low, 4),
            "range_size": round(range_size, 6),
            "position_in_range": round((current - range_low) / max(range_size, 0.0001), 2),
        }

    def _detect_rejection(self, prices):
        """Detect price rejection at key levels."""
        n = len(prices)
        if n < 5:
            return {"detected": False}

        current = prices[-1]
        recent_high = max(prices[-5:])
        recent_low = min(prices[-5:])

        # Rejection: price approaches extreme then reverses
        rejection_high = (prices[-2] >= recent_high * 0.999 and prices[-1] < prices[-2])
        rejection_low = (prices[-2] <= recent_low * 1.001 and prices[-1] > prices[-2])

        # Wick analysis (for candle-like patterns)
        body = abs(prices[-1] - prices[-2]) if n >= 2 else 0
        upper_wick = recent_high - max(prices[-1], prices[-2])
        lower_wick = min(prices[-1], prices[-2]) - recent_low
        has_upper_wick = upper_wick > body * 1.5 if body > 0 else upper_wick > 0
        has_lower_wick = lower_wick > body * 1.5 if body > 0 else lower_wick > 0

        return {
            "detected": rejection_high or rejection_low,
            "direction": "DOWN" if rejection_high else "UP" if rejection_low else "NONE",
            "rejection_high": rejection_high,
            "rejection_low": rejection_low,
            "has_upper_wick": has_upper_wick,
            "has_lower_wick": has_lower_wick,
        }

    def _analyze_structure(self, prices):
        """Analyze market structure: consolidation, expansion, transition."""
        n = len(prices)
        if n < 10:
            return {"phase": "unknown", "volatility_trend": "stable"}

        # Volatility expansion/contraction
        first_half_vol = self._volatility(prices[:n//2])
        second_half_vol = self._volatility(prices[n//2:])
        vol_ratio = second_half_vol / max(first_half_vol, 0.0001)

        if vol_ratio > 1.3:
            vol_trend = "expanding"
        elif vol_ratio < 0.7:
            vol_trend = "contracting"
        else:
            vol_trend = "stable"

        # Phase detection
        recent_range = max(prices[-10:]) - min(prices[-10:])
        overall_range = max(prices) - min(prices)
        range_ratio = recent_range / max(overall_range, 0.0001)

        if range_ratio < 0.3 and vol_trend == "contracting":
            phase = "consolidation"  # squeeze — breakout coming
        elif vol_trend == "expanding":
            phase = "expansion"  # trending
        elif range_ratio > 0.7:
            phase = "wide_range"  # choppy
        else:
            phase = "transition"

        return {
            "phase": phase,
            "volatility_trend": vol_trend,
            "vol_ratio": round(vol_ratio, 2),
            "range_ratio": round(range_ratio, 2),
            "squeeze_imminent": phase == "consolidation",
        }

    def _volatility(self, prices):
        """Calculate volatility (standard deviation)."""
        if len(prices) < 2:
            return 0
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance ** 0.5

    def _combine_signals(self, analysis):
        """Combine all price action signals into one trade signal."""
        score = 0
        reasons = []

        # Trend
        trend = analysis.get("trend", {})
        if trend.get("direction") == "UP":
            score += 2
            reasons.append(f"trend UP (strength={trend.get('strength', 0):.2f})")
        elif trend.get("direction") == "DOWN":
            score -= 2
            reasons.append(f"trend DOWN (strength={trend.get('strength', 0):.2f})")

        # Momentum
        mom = analysis.get("momentum", {})
        if mom.get("overbought"):
            score -= 1
            reasons.append("overbought (RSI>70)")
        elif mom.get("oversold"):
            score += 1
            reasons.append("oversold (RSI<30)")
        if mom.get("momentum_building"):
            reasons.append("momentum building")

        # Breakout
        bk = analysis.get("breakout", {})
        if bk.get("detected") and not bk.get("false_breakout"):
            if bk.get("direction") == "UP":
                score += 3
                reasons.append("breakout UP (confirmed)")
            elif bk.get("direction") == "DOWN":
                score -= 3
                reasons.append("breakout DOWN (confirmed)")
        if bk.get("false_breakout"):
            reasons.append("false breakout detected — caution")

        # Rejection
        rj = analysis.get("rejection", {})
        if rj.get("detected"):
            if rj.get("direction") == "UP":
                score += 2
                reasons.append("rejection at low — buyers stepping in")
            elif rj.get("direction") == "DOWN":
                score -= 2
                reasons.append("rejection at high — sellers stepping in")

        # Support/Resistance
        levels = analysis.get("levels", {})
        if levels.get("at_support"):
            score += 2
            reasons.append("at support level")
        if levels.get("at_resistance"):
            score -= 2
            reasons.append("at resistance level")

        # Structure
        structure = analysis.get("structure", {})
        if structure.get("squeeze_imminent"):
            reasons.append("squeeze imminent — breakout expected")
        if structure.get("phase") == "consolidation":
            reasons.append("consolidation phase — wait for breakout")

        # Convert score to signal
        if score > 2:
            signal = "BUY"
        elif score < -2:
            signal = "SELL"
        else:
            signal = "WAIT"

        confidence = min(10, max(0, abs(score) * 1.5))
        reason = "; ".join(reasons[:3]) if reasons else "no clear signal"

        return signal, round(confidence, 1), reason

    def get_status(self):
        return {sym: {
            "signal": a.get("signal"),
            "confidence": a.get("confidence"),
            "trend": a.get("trend", {}).get("direction"),
            "phase": a.get("structure", {}).get("phase"),
        } for sym, a in self.cache.items()}
