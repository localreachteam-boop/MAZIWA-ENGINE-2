"""
REGIME CLASSIFICATION ENGINE
Classifies market state into 3 regimes:
  1. RANGE_COMPRESSION — low vol, digit strategies preferred
  2. MOMENTUM_EXPANSION — trending, rise/fall preferred
  3. DIGIT_ANOMALY — biased digits, digit match/diff preferred
"""
import math


class RegimeEngine:
    """
    Continuously classifies market state using tick statistics.
    Returns regime + confidence + recommended strategies.
    """

    # Regime constants
    RANGE = "RANGE_COMPRESSION"
    MOMENTUM = "MOMENTUM_EXPANSION"
    DIGIT_ANOMALY = "DIGIT_ANOMALY"
    UNKNOWN = "UNKNOWN"

    def __init__(self):
        self.regimes = {}  # symbol -> current regime

    def classify(self, symbol, signal):
        """
        Classify market regime from signal data.
        Returns (regime, confidence, recommended_strategies).
        """
        if signal is None:
            return self.UNKNOWN, 0, []

        stats = signal.get("statistics", {})
        digit_freq = signal.get("digit_frequency", {})
        z = abs(stats.get("z_score", 0))
        vol_ratio = stats.get("vol_ratio", 1.0)
        trend = abs(stats.get("trend_slope", 0))
        regime_label = stats.get("regime", "medium")
        bb_pos = stats.get("bb_position", 0.5)

        # ── Check for DIGIT_ANOMALY first ──────────────
        if digit_freq and len(digit_freq) >= 10:
            expected = 0.10
            max_dev = max(abs(float(v) - expected) for v in digit_freq.values())
            if max_dev > 0.03:
                confidence = min(1.0, max_dev / 0.05)
                self.regimes[symbol] = self.DIGIT_ANOMALY
                return self.DIGIT_ANOMALY, confidence, [
                    "DIGIT_MATCH", "DIGIT_DIFF", "EVEN_ODD", "OVER_UNDER"
                ]

        # ── Check for MOMENTUM_EXPANSION ───────────────
        if trend > 0.001 and z > 0.5:
            confidence = min(1.0, trend * 1000)
            self.regimes[symbol] = self.MOMENTUM
            return self.MOMENTUM, confidence, [
                "RISE_FALL", "TREND", "OVER_UNDER"
            ]

        if regime_label == "high" and z > 1.0:
            confidence = min(1.0, z / 2.0)
            self.regimes[symbol] = self.MOMENTUM
            return self.MOMENTUM, confidence, [
                "RISE_FALL", "TREND"
            ]

        # ── Check for RANGE_COMPRESSION ────────────────
        if vol_ratio < 0.8 and z < 0.8 and trend < 0.0005:
            confidence = min(1.0, (1 - vol_ratio) * 2)
            self.regimes[symbol] = self.RANGE
            return self.RANGE, confidence, [
                "DIGIT_MATCH", "DIGIT_DIFF", "EVEN_ODD", "ACCUMULATOR"
            ]

        if regime_label == "low":
            confidence = 0.7
            self.regimes[symbol] = self.RANGE
            return self.RANGE, confidence, [
                "DIGIT_MATCH", "EVEN_ODD"
            ]

        # ── Default: moderate conditions ───────────────
        self.regimes[symbol] = self.RANGE
        return self.RANGE, 0.5, ["RISE_FALL", "DIGIT_MATCH"]

    def get_regime(self, symbol):
        return self.regimes.get(symbol, self.UNKNOWN)

    def get_status(self):
        return dict(self.regimes)
