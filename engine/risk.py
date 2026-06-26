"""
Exit / risk management.

The old bot's fixed take-profit capped every winner at +40% and relied on a
single hard stop. This replaces that with layered, dynamic exits:

  HARD_STOP        — cut losers fast.
  TRAILING_STOP    — once armed (position up TRAILING_ACTIVATION_PCT), ride the
                     position and exit only on a pullback from the peak. This is
                     what lets a moonshot run to +200% instead of stopping at +40.
  LIQUIDITY_DRAIN  — bail if pool liquidity collapses vs entry (rug in progress).
  MOMENTUM_FADE    — bail on a sharp 1-hour reversal even before the trail trips.
  TIME_STOP        — recycle capital out of stale, going-nowhere bags.
"""

from __future__ import annotations

from typing import Optional

from engine import config as cfg
from engine.portfolio import Position
from engine.signals import TokenSnapshot


def evaluate_exit(pos: Position, snap: Optional[TokenSnapshot]) -> Optional[str]:
    """
    Return a human-readable exit reason if `pos` should be closed now, else None.
    `snap` is the freshest market snapshot for the token (may be None if the feed
    failed — in which case we only have the last marked price to work with).
    """
    price = snap.price_usd if snap else pos.last_price_usd
    if price <= 0 or pos.entry_price_usd <= 0:
        return None

    pct = (price - pos.entry_price_usd) / pos.entry_price_usd * 100.0
    peak = max(pos.peak_price_usd, pos.entry_price_usd)
    peak_gain = (peak - pos.entry_price_usd) / pos.entry_price_usd * 100.0

    # 1. Hard stop — non-negotiable downside cap.
    if pct <= cfg.HARD_STOP_PCT:
        return f"HARD_STOP {pct:+.1f}%"

    # 2. Liquidity collapse — strongest rug tell; get out at any PnL.
    if snap and pos.entry_liquidity_usd > 0:
        liq_drop = (pos.entry_liquidity_usd - snap.liquidity_usd) / pos.entry_liquidity_usd * 100
        if liq_drop >= cfg.LIQUIDITY_COLLAPSE_PCT:
            return f"LIQUIDITY_DRAIN -{liq_drop:.0f}% liq"

    # 3. Trailing stop (armed) — let winners run, exit on reversal from peak.
    if peak_gain >= cfg.TRAILING_ACTIVATION_PCT:
        drawdown = (peak - price) / peak * 100.0
        if drawdown >= cfg.TRAILING_STOP_PCT:
            return f"TRAILING_STOP {pct:+.1f}% (peak +{peak_gain:.0f}%, -{drawdown:.0f}% off top)"
        return None  # armed and holding — do not apply the lower exits below

    # 4. Momentum fade — sharp 1h reversal while not yet a runner.
    if snap and snap.change_h1 <= cfg.MOMENTUM_FADE_H1_PCT and pct < 10:
        return f"MOMENTUM_FADE 1h {snap.change_h1:.0f}%"

    # 5. Time stop — stale and going nowhere.
    if pos.age_minutes() >= cfg.TIME_STOP_MINUTES and pct < cfg.TIME_STOP_MAX_PCT:
        return f"TIME_STOP {pct:+.1f}% @ {pos.age_minutes():.0f}min"

    return None
