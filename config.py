import os

# Load .env file if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

# Deriv API
DERIV_APP_ID = 1089
DERIV_TOKEN = os.environ.get("DERIV_TOKEN", "")
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# ── Market Definitions ──────────────────────────────────
MARKET_TYPES = {
    "volatility":   ["R_10", "R_25", "R_50", "R_75", "R_100"],
    "jump":         ["JD10", "JD25", "JD50", "JD75", "JD100"],
    "crash_boom":   ["CRASH_1000", "CRASH_500", "BOOM_1000", "BOOM_500"],
    "digit":        ["R_10", "R_25", "R_50"],  # digit markets reuse volatility symbols
    "step":         ["STPUSD"],
}

ALL_MARKETS = []
for _type, _symbols in MARKET_TYPES.items():
    for _sym in _symbols:
        ALL_MARKETS.append({"symbol": _sym, "type": _type})

# ── Pipeline Parameters ─────────────────────────────────
TICK_BUFFER = 500             # ticks to keep per market
ANALYSIS_WINDOW = 100         # ticks for short-term analysis
SIGNAL_MIN_TICKS = 30         # minimum ticks before generating signals
CYCLE_INTERVAL = 2.0          # seconds between market scans

# ── Risk Management ─────────────────────────────────────
INITIAL_BALANCE = 100.0
MAX_DAILY_LOSS_PCT = 0.02     # 2% daily stop
KELLY_FRACTION = 0.10         # 10% Kelly (conservative)
MIN_EDGE = 0.01               # 1% minimum expected edge
MAX_STAKE_PCT = 0.10          # max 10% of balance per trade
MIN_STAKE = 0.35              # Deriv minimum
MAX_CONCURRENT_TRADES = 1

# ── Strategy Thresholds ─────────────────────────────────
# Volatility
VOL_ZSCORE_THRESHOLD = 1.8    # Z-score to trigger mean reversion
VOL_BB_PERIOD = 20            # Bollinger band period
VOL_MOMENTUM_WINDOW = 10      # momentum detection window

# Crash/Boom
SPIKE_LOOKBACK = 50           # ticks to analyze spike intervals
SPIKE_PROB_THRESHOLD = 0.6    # min probability to trade spike

# Digit
DIGIT_FREQ_THRESHOLD = 0.15   # min deviation from uniform (10%)
DIGIT_SAMPLE_SIZE = 100       # ticks for digit analysis

# ── Backtesting ─────────────────────────────────────────
BACKTEST_WINDOW = 50          # rolling trades for performance
PERFORMANCE_THRESHOLD = 0.40  # min win rate to keep trading
ADJUSTMENT_COOLDOWN = 10      # trades between parameter adjustments

# ── Dashboard ───────────────────────────────────────────
HTTP_PORT = 9100
WS_PORT = 9101
LOG_FILE = "trades.log"
STATE_FILE = "trading_state.json"
ABSOLUTE_MAX_STAKE = 5.0  # never risk more than $5 per trade

# ── AI Model Routing ──────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLOUD_MODEL = "deepseek/deepseek-chat-v3-0324"  # DeepSeek V4 Flash
CLOUD_BUDGET_PER_SESSION = 5.0  # max $5 cloud spend per session
