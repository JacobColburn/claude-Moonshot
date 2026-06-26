"""
Central configuration for the Moonshot Engine.

Everything is overridable via environment variables so you can tune the bot
without editing code. Defaults are conservative and tuned for paper trading.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ── Mode ───────────────────────────────────────────────────────────────────
# LIVE=false  → paper trading (virtual balance, simulated fills). The default.
# LIVE=true   → real trades through your Solana wallet via Jupiter.
LIVE: bool = _b("LIVE", False)

# ── Wallet / network (only used when LIVE) ─────────────────────────────────
PRIVATE_KEY: str = os.getenv("SOLANA_PRIVATE_KEY", "")
RPC_ENDPOINT: str = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOL_MINT: str = "So11111111111111111111111111111111111111112"

# ── Paper-trading starting state ───────────────────────────────────────────
PAPER_START_SOL: float = _f("PAPER_START_SOL", 10.0)

# ── Capital & risk ─────────────────────────────────────────────────────────
# Fraction of total equity to deploy on a *maximum-conviction* trade.
MAX_POSITION_FRACTION: float = _f("MAX_POSITION_FRACTION", 0.18)
# Hard ceiling per trade in SOL (belt-and-suspenders on top of the fraction).
MAX_POSITION_SOL: float = _f("MAX_POSITION_SOL", 1.5)
MIN_POSITION_SOL: float = _f("MIN_POSITION_SOL", 0.02)
# Always keep this much SOL unspent (gas + dry powder).
MIN_SOL_RESERVE: float = _f("MIN_SOL_RESERVE", 0.05)
MAX_POSITIONS: int = _i("MAX_POSITIONS", 6)

# ── Entry gate ─────────────────────────────────────────────────────────────
# Composite score (0-100) a candidate must clear to be considered for a buy.
ENTRY_SCORE_THRESHOLD: float = _f("ENTRY_SCORE_THRESHOLD", 62.0)
# Sanity floors — reject obvious rug bait regardless of score.
MIN_LIQUIDITY_USD: float = _f("MIN_LIQUIDITY_USD", 12_000)
MIN_VOLUME_H1_USD: float = _f("MIN_VOLUME_H1_USD", 6_000)
MIN_MARKET_CAP: float = _f("MIN_MARKET_CAP", 40_000)
MAX_MARKET_CAP: float = _f("MAX_MARKET_CAP", 8_000_000)

# ── Signal weights (need not sum to 1; they are normalized) ────────────────
WEIGHTS = {
    "momentum": _f("W_MOMENTUM", 0.26),
    "volume": _f("W_VOLUME", 0.22),
    "pressure": _f("W_PRESSURE", 0.18),
    "liquidity": _f("W_LIQUIDITY", 0.14),
    "turnover": _f("W_TURNOVER", 0.12),
    "freshness": _f("W_FRESHNESS", 0.08),
}

# ── Exit / risk management ─────────────────────────────────────────────────
HARD_STOP_PCT: float = _f("HARD_STOP_PCT", -16.0)        # cut losers at -16%
TRAILING_ACTIVATION_PCT: float = _f("TRAIL_ACTIVATE_PCT", 22.0)  # arm trail at +22%
TRAILING_STOP_PCT: float = _f("TRAIL_STOP_PCT", 14.0)    # exit 14% off the peak
TIME_STOP_MINUTES: int = _i("TIME_STOP_MIN", 75)         # ditch stale flat/red bags
TIME_STOP_MAX_PCT: float = _f("TIME_STOP_MAX_PCT", 6.0)  # ...only if under +6%
LIQUIDITY_COLLAPSE_PCT: float = _f("LIQ_COLLAPSE_PCT", 45.0)  # bail if liq −45% from entry
MOMENTUM_FADE_H1_PCT: float = _f("MOM_FADE_H1_PCT", -22.0)    # bail on sharp 1h reversal

# ── Execution assumptions ──────────────────────────────────────────────────
SLIPPAGE_BPS: int = _i("SLIPPAGE_BPS", 300)              # 3% — volatile tokens
PAPER_FILL_SLIPPAGE_PCT: float = _f("PAPER_FILL_SLIPPAGE_PCT", 1.5)  # simulated
PAPER_FEE_PCT: float = _f("PAPER_FEE_PCT", 0.3)          # round-trip-ish fee model

# ── Loop cadence ───────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS: int = _i("SCAN_INTERVAL", 20)    # full scan + score cadence
POSITION_REFRESH_SECONDS: int = _i("POS_REFRESH", 8)    # price refresh for open bags
EQUITY_SAMPLE_SECONDS: int = _i("EQUITY_SAMPLE", 30)    # equity-curve sampling

# ── Claude brain ───────────────────────────────────────────────────────────
BRAIN_ENABLED: bool = _b("BRAIN_ENABLED", True)
BRAIN_MODEL: str = os.getenv("BRAIN_MODEL", "claude-opus-4-8")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
# Only the top-N scoring candidates get sent to Claude for a final gut-check.
BRAIN_REVIEW_TOP_N: int = _i("BRAIN_REVIEW_TOP_N", 5)
BRAIN_MIN_CONVICTION: float = _f("BRAIN_MIN_CONVICTION", 0.55)

# ── Persistence ────────────────────────────────────────────────────────────
STATE_DIR: str = os.getenv("STATE_DIR", "state")
PAPER_STATE_FILE: str = os.path.join(STATE_DIR, "paper_portfolio.json")
LIVE_STATE_FILE: str = os.path.join(STATE_DIR, "live_portfolio.json")
TRADES_FILE: str = os.path.join(STATE_DIR, "trades.json")

# ── Dashboard server ───────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = _i("PORT", 8000)


def state_file() -> str:
    return LIVE_STATE_FILE if LIVE else PAPER_STATE_FILE


def summary() -> dict:
    """Compact config snapshot for the dashboard header."""
    return {
        "mode": "LIVE" if LIVE else "PAPER",
        "brain_enabled": BRAIN_ENABLED and bool(ANTHROPIC_API_KEY),
        "brain_model": BRAIN_MODEL,
        "max_positions": MAX_POSITIONS,
        "entry_threshold": ENTRY_SCORE_THRESHOLD,
        "scan_interval": SCAN_INTERVAL_SECONDS,
        "hard_stop_pct": HARD_STOP_PCT,
        "trail_activate_pct": TRAILING_ACTIVATION_PCT,
        "trail_stop_pct": TRAILING_STOP_PCT,
    }
