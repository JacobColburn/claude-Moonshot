"""
Signal extraction — turn a raw DexScreener pair into normalized 0-100 sub-signals.

Each signal answers one question about a token. They are deliberately bounded to
[0, 100] so the composite score is interpretable and the dashboard can render
them as comparable bars.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lin(x: float, x0: float, x1: float) -> float:
    """Map x in [x0, x1] linearly to [0, 100], clamped."""
    if x1 == x0:
        return 0.0
    return _clamp((x - x0) / (x1 - x0) * 100.0)


def _band(x: float, lo: float, lo_full: float, hi_full: float, hi: float) -> float:
    """
    Trapezoid: 0 below `lo`, ramps to 100 at `lo_full`, holds 100 until
    `hi_full`, ramps back to 0 at `hi`. Good for "healthy range" signals.
    """
    if x <= lo or x >= hi:
        return 0.0
    if lo_full <= x <= hi_full:
        return 100.0
    if x < lo_full:
        return _lin(x, lo, lo_full)
    return 100.0 - _lin(x, hi_full, hi)


@dataclass
class TokenSnapshot:
    """Flattened view of a DexScreener pair, plus computed signals."""

    mint: str
    symbol: str
    name: str
    price_usd: float
    pair_address: str
    dex: str
    liquidity_usd: float
    market_cap: float
    age_minutes: float
    change_m5: float
    change_h1: float
    change_h6: float
    change_h24: float
    vol_m5: float
    vol_h1: float
    vol_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    signals: dict = field(default_factory=dict)
    score: float = 0.0
    source: str = "dexscreener"

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "name": self.name,
            "price_usd": self.price_usd,
            "pair_address": self.pair_address,
            "dex": self.dex,
            "liquidity_usd": self.liquidity_usd,
            "market_cap": self.market_cap,
            "age_minutes": round(self.age_minutes, 1),
            "change_m5": self.change_m5,
            "change_h1": self.change_h1,
            "change_h24": self.change_h24,
            "vol_h1": self.vol_h1,
            "signals": self.signals,
            "score": round(self.score, 1),
            "source": self.source,
        }


def from_pair(pair: dict, source: str = "dexscreener") -> Optional[TokenSnapshot]:
    """Build a TokenSnapshot from a DexScreener pair dict. Returns None if unusable."""
    base = pair.get("baseToken", {}) or {}
    mint = base.get("address") or pair.get("mint") or ""
    price = float(pair.get("priceUsd") or 0)
    if not mint or price <= 0:
        return None

    pc = pair.get("priceChange", {}) or {}
    vol = pair.get("volume", {}) or {}
    txns = pair.get("txns", {}) or {}
    m5 = txns.get("m5", {}) or {}
    h1 = txns.get("h1", {}) or {}
    liq = pair.get("liquidity", {}) or {}

    created_ms = pair.get("pairCreatedAt") or 0
    age_min = ((time.time() * 1000) - created_ms) / 60000.0 if created_ms else 1e6

    return TokenSnapshot(
        mint=mint,
        symbol=base.get("symbol", mint[:6]),
        name=base.get("name", ""),
        price_usd=price,
        pair_address=pair.get("pairAddress", ""),
        dex=pair.get("dexId", "?"),
        liquidity_usd=float(liq.get("usd") or 0),
        market_cap=float(pair.get("marketCap") or pair.get("fdv") or 0),
        age_minutes=age_min,
        change_m5=float(pc.get("m5") or 0),
        change_h1=float(pc.get("h1") or 0),
        change_h6=float(pc.get("h6") or 0),
        change_h24=float(pc.get("h24") or 0),
        vol_m5=float(vol.get("m5") or 0),
        vol_h1=float(vol.get("h1") or 0),
        vol_h24=float(vol.get("h24") or 0),
        buys_m5=int(m5.get("buys") or 0),
        sells_m5=int(m5.get("sells") or 0),
        buys_h1=int(h1.get("buys") or 0),
        sells_h1=int(h1.get("sells") or 0),
        source=source,
    )


# ── Individual signals ──────────────────────────────────────────────────────

def momentum_signal(s: TokenSnapshot) -> float:
    """
    Reward fresh upward momentum (5-min pop) confirmed by the 1-hour trend,
    but penalize parabolic over-extension that tends to dump.
    """
    m5 = _lin(s.change_m5, 0.0, 12.0)            # 0% → 0, +12% → 100
    h1 = _band(s.change_h1, -15, 2, 60, 180)     # healthy uptrend, not blow-off
    blended = 0.6 * m5 + 0.4 * h1
    # Over-extension haircut: if it's already up huge on the hour, fade it.
    if s.change_h1 > 220:
        blended *= 0.5
    return _clamp(blended)


def volume_signal(s: TokenSnapshot) -> float:
    """
    Volume acceleration: recent 5-min volume vs the average 5-min rate over
    the last hour. >1x means activity is heating up *right now*.
    """
    avg_5m = (s.vol_h1 / 12.0) if s.vol_h1 > 0 else 0.0
    if avg_5m <= 0:
        return 0.0
    accel = s.vol_m5 / avg_5m
    return _lin(accel, 0.8, 4.0)                 # 0.8x → 0, 4x → 100


def pressure_signal(s: TokenSnapshot) -> float:
    """Buy/sell pressure — buyer dominance over the 5-min and 1-hour windows."""
    tot_m5 = s.buys_m5 + s.sells_m5
    tot_h1 = s.buys_h1 + s.sells_h1
    p_m5 = (s.buys_m5 / tot_m5) if tot_m5 else 0.5
    p_h1 = (s.buys_h1 / tot_h1) if tot_h1 else 0.5
    blended = 0.65 * p_m5 + 0.35 * p_h1          # 0..1
    # Map 0.5 (neutral) → 30, 0.75 (strong buying) → 100.
    return _lin(blended, 0.40, 0.75)


def liquidity_signal(s: TokenSnapshot) -> float:
    """Healthy-liquidity band: enough to enter/exit, not so deep it can't move."""
    return _band(s.liquidity_usd, 8_000, 30_000, 600_000, 3_000_000)


def turnover_signal(s: TokenSnapshot) -> float:
    """Turnover = hourly volume relative to market cap. High churn = live token."""
    if s.market_cap <= 0:
        return 0.0
    turnover = s.vol_h1 / s.market_cap
    return _lin(turnover, 0.02, 0.8)             # 2% → 0, 80%+ → 100


def freshness_signal(s: TokenSnapshot) -> float:
    """
    Newer launches carry more moonshot upside — but the first few minutes are
    rug-prone, so we floor the very-young and decay the old.
    """
    a = s.age_minutes
    if a < 10:
        return 45.0                              # too new to trust fully
    if a <= 240:
        return 100.0 - _lin(a, 10, 240) * 0.35   # 10m→~96, 4h→~80
    if a <= 2880:                                # up to 2 days
        return _clamp(80 - _lin(a, 240, 2880) * 0.7)
    return 15.0


def compute_signals(s: TokenSnapshot) -> dict:
    """Populate s.signals with all sub-signals and return the dict."""
    s.signals = {
        "momentum": round(momentum_signal(s), 1),
        "volume": round(volume_signal(s), 1),
        "pressure": round(pressure_signal(s), 1),
        "liquidity": round(liquidity_signal(s), 1),
        "turnover": round(turnover_signal(s), 1),
        "freshness": round(freshness_signal(s), 1),
    }
    return s.signals
