"""
Market data feed.

Reuses the battle-tested DexScreener client from moonshot_trader.market for the
heavy lifting (rate-limited HTTP, trending/boosted scans) and adapts it into the
TokenSnapshot shape the engine works with. Sync HTTP is wrapped so the async
engine can await it without blocking the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from engine.signals import TokenSnapshot, from_pair
from moonshot_trader import market

logger = logging.getLogger("engine.datafeed")


async def _to_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def scan_candidates() -> list[TokenSnapshot]:
    """Pull trending/volatile Solana pairs and convert to snapshots."""
    pairs = await _to_thread(market.scan_volatile_solana_tokens)
    snaps: list[TokenSnapshot] = []
    seen: set[str] = set()
    for p in pairs or []:
        snap = from_pair(p)
        if snap and snap.mint not in seen:
            seen.add(snap.mint)
            snaps.append(snap)
    logger.debug("scan: %d candidates", len(snaps))
    return snaps


async def refresh(mint: str) -> Optional[TokenSnapshot]:
    """Fresh snapshot for a single token (for managing open positions)."""
    pair = await _to_thread(market.get_pair_data, mint)
    if not pair:
        return None
    return from_pair(pair)


async def price(mint: str) -> Optional[float]:
    return await _to_thread(market.get_price_usd, mint)
