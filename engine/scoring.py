"""
Composite scoring + position sizing.

The composite score blends the normalized sub-signals using configurable weights,
then applies two contextual modifiers grounded in the research:

  • Session timing — Solana memecoin volume peaks during US market hours
    (~14:00-22:00 UTC); fresh momentum in that window is more likely to follow
    through, so we apply a small multiplier.
  • Sanity floors — anything below the liquidity / volume / market-cap floors is
    forced to a near-zero score regardless of how it looks on momentum.

Sizing scales the position from MIN→MAX as conviction rises above the entry
threshold, so high-conviction setups get more capital than marginal ones.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from engine import config as cfg
from engine.signals import TokenSnapshot, compute_signals


def session_multiplier(now: float | None = None) -> float:
    """1.10 during US peak hours, 1.0 mid, 0.92 in the dead zone."""
    h = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).hour
    if 14 <= h < 22:        # US market hours — peak memecoin volume
        return 1.10
    if 22 <= h or h < 4:    # late US / Asia open — still active
        return 1.0
    return 0.92             # 04:00-14:00 UTC — thin, choppy


def passes_floors(s: TokenSnapshot) -> bool:
    return (
        s.liquidity_usd >= cfg.MIN_LIQUIDITY_USD
        and s.vol_h1 >= cfg.MIN_VOLUME_H1_USD
        and cfg.MIN_MARKET_CAP <= s.market_cap <= cfg.MAX_MARKET_CAP
    )


def composite_score(s: TokenSnapshot, now: float | None = None) -> float:
    """Compute and store the composite 0-100 score on the snapshot."""
    sig = compute_signals(s)

    weights = cfg.WEIGHTS
    total_w = sum(weights.values()) or 1.0
    base = sum(sig[k] * weights[k] for k in weights) / total_w

    score = base * session_multiplier(now)

    # Hard sanity gate — keep junk out of the leaderboard entirely.
    if not passes_floors(s):
        score *= 0.25

    s.score = max(0.0, min(100.0, score))
    return s.score


def rank(snapshots: list[TokenSnapshot], now: float | None = None) -> list[TokenSnapshot]:
    """Score every snapshot and return them sorted best-first."""
    for s in snapshots:
        composite_score(s, now)
    return sorted(snapshots, key=lambda x: x.score, reverse=True)


def position_size_sol(
    score: float,
    equity_sol: float,
    available_sol: float,
    conviction: float = 1.0,
) -> float:
    """
    Translate conviction into a SOL stake.

    conviction in [0,1] comes from Claude (1.0 if the brain is disabled). It
    scales the equity fraction so a marginal pass risks less than a strong one.
    """
    if score < cfg.ENTRY_SCORE_THRESHOLD:
        return 0.0

    # 0 at the threshold → 1 at a perfect score.
    span = max(1.0, 100.0 - cfg.ENTRY_SCORE_THRESHOLD)
    strength = (score - cfg.ENTRY_SCORE_THRESHOLD) / span      # 0..1
    strength = 0.4 + 0.6 * strength                            # floor at 0.4

    frac = cfg.MAX_POSITION_FRACTION * strength * max(0.0, min(1.0, conviction))
    size = equity_sol * frac

    size = min(size, cfg.MAX_POSITION_SOL, max(0.0, available_sol - cfg.MIN_SOL_RESERVE))
    if size < cfg.MIN_POSITION_SOL:
        return 0.0
    return round(size, 4)
