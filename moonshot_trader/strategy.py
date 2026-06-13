"""
Momentum pop-catching strategy.

Entry: 5-min price spike + volume acceleration + buyer dominance
Exit:  Take-profit, stop-loss, or time-based stale exit
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import moonshot_trader.config as cfg

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    token_address: str
    token_symbol: str
    action: str        # "BUY" or "SELL"
    price_usd: float
    reason: str


def check_entry(pair: dict) -> Optional[Signal]:
    """
    Returns a BUY signal if the pair shows a momentum pop pattern.
    Filters out tokens that don't meet liquidity/cap minimums.
    """
    base = pair.get("baseToken", {})
    addr = base.get("address", "")
    symbol = base.get("symbol", "?")

    price_usd = float(pair.get("priceUsd") or 0)
    if price_usd <= 0 or not addr:
        return None

    m5_pct = float(pair.get("priceChange", {}).get("m5") or 0)
    h1_pct = float(pair.get("priceChange", {}).get("h1") or 0)

    vol_m5 = float(pair.get("volume", {}).get("m5") or 0)
    vol_h1 = float(pair.get("volume", {}).get("h1") or 0)

    txns_m5 = pair.get("txns", {}).get("m5", {})
    buys_m5 = int(txns_m5.get("buys") or 0)
    sells_m5 = int(txns_m5.get("sells") or 0)

    market_cap = float(pair.get("marketCap") or 0)
    liquidity_usd = float(pair.get("liquidity", {}).get("usd") or 0)

    # Hard filters
    if market_cap < cfg.ENTRY_MIN_MARKET_CAP:
        return None
    if market_cap > cfg.ENTRY_MAX_MARKET_CAP:
        return None
    if liquidity_usd < cfg.ENTRY_MIN_LIQUIDITY:
        return None
    if vol_h1 < cfg.ENTRY_MIN_VOLUME_H1:
        return None

    # Volume acceleration: compare recent 5-min volume vs average 5-min rate over past hour
    avg_5min_vol = vol_h1 / 12 if vol_h1 > 0 else 0
    vol_accel = vol_m5 / avg_5min_vol if avg_5min_vol > 0 else 0

    buy_sell_ratio = buys_m5 / max(sells_m5, 1)

    if (m5_pct >= cfg.ENTRY_MOMENTUM_M5_PCT
            and h1_pct >= -5          # Not in a hard dump
            and vol_accel >= cfg.ENTRY_VOLUME_ACCEL
            and buy_sell_ratio >= cfg.ENTRY_BUY_SELL_RATIO):

        reason = (
            f"5m:+{m5_pct:.1f}%  1h:{h1_pct:+.1f}%  "
            f"vol_accel:{vol_accel:.1f}x  B/S:{buy_sell_ratio:.1f}  "
            f"mcap:${market_cap:,.0f}"
        )
        return Signal(addr, symbol, "BUY", price_usd, reason)

    return None


def check_exit(
    entry_price_usd: float,
    current_price_usd: float,
    entry_time: float,
) -> Optional[str]:
    """
    Returns a reason string if position should be closed, else None.
    """
    if entry_price_usd <= 0 or current_price_usd <= 0:
        return None

    pct = (current_price_usd - entry_price_usd) / entry_price_usd * 100
    age_minutes = (time.time() - entry_time) / 60

    if pct >= cfg.TAKE_PROFIT_PCT:
        return f"TAKE_PROFIT  +{pct:.1f}%"

    if pct <= cfg.STOP_LOSS_PCT:
        return f"STOP_LOSS  {pct:.1f}%"

    # Time-based exit: stale position that is moderately negative
    if age_minutes >= cfg.TIME_STOP_MINUTES and pct < -5:
        return f"TIME_STOP  {pct:.1f}% after {age_minutes:.0f}min"

    return None
