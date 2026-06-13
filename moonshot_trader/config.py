import os
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY: str = os.getenv("SOLANA_PRIVATE_KEY", "")
RPC_ENDPOINT: str = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
BIRDEYE_API_KEY: str = os.getenv("BIRDEYE_API_KEY", "")

# --- Position sizing ---
# Max SOL to spend per trade (keeps risk bounded; ~$7-15 at typical prices)
MAX_POSITION_SOL: float = 0.08
# Always keep this much SOL reserved for gas fees
MIN_SOL_RESERVE: float = 0.02
# Max number of open positions at once
MAX_POSITIONS: int = 4

# --- Exit thresholds ---
TAKE_PROFIT_PCT: float = 40.0   # Sell if up 40%
STOP_LOSS_PCT: float = -18.0    # Sell if down 18%
TIME_STOP_MINUTES: int = 90     # Sell stale losing positions after 90 min

# --- Entry signal requirements ---
ENTRY_MOMENTUM_M5_PCT: float = 4.0      # Min 5-min price gain %
ENTRY_VOLUME_ACCEL: float = 2.5         # 5-min volume must be 2.5x the average 5-min baseline
ENTRY_BUY_SELL_RATIO: float = 1.4       # Buyers must outnumber sellers by 40%
ENTRY_MIN_MARKET_CAP: float = 75_000    # Avoid extreme micro-caps (rug bait)
ENTRY_MIN_LIQUIDITY: float = 15_000     # Minimum pool liquidity in USD
ENTRY_MIN_VOLUME_H1: float = 8_000      # Minimum hourly volume to confirm activity
ENTRY_MAX_MARKET_CAP: float = 5_000_000 # Skip already-pumped large caps

# --- Scanner ---
SCAN_INTERVAL_SECONDS: int = 45         # How often to scan for new opportunities
MONITOR_INTERVAL_SECONDS: int = 20      # How often to check open positions

# --- Slippage ---
SLIPPAGE_BPS: int = 300                 # 3% slippage tolerance (needed for volatile tokens)

# --- Persistent state ---
POSITIONS_FILE: str = "positions.json"

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
