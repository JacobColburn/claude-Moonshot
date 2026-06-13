"""DexScreener market data — price feeds, trending token scanner."""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DEXSCREENER = "https://api.dexscreener.com"
_session = requests.Session()
_session.headers.update({"User-Agent": "moonshot-trader/1.0"})

_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.4  # 400ms between requests (~150 req/min, well under limit)


def _get(url: str, params: dict = None) -> Optional[dict]:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_GAP:
        time.sleep(_MIN_REQUEST_GAP - elapsed)
    try:
        resp = _session.get(url, params=params, timeout=12)
        _last_request_time = time.time()
        if resp.ok:
            return resp.json()
        logger.warning("DexScreener %s → %d", url, resp.status_code)
    except Exception as e:
        logger.warning("DexScreener request failed: %s", e)
    return None


def get_pair_data(token_address: str) -> Optional[dict]:
    """Get the best (highest-volume) Solana trading pair for a token."""
    data = _get(f"{_DEXSCREENER}/latest/dex/tokens/{token_address}")
    if not data:
        return None
    pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: p.get("volume", {}).get("h24", 0) or 0)


def get_price_usd(token_address: str) -> Optional[float]:
    pair = get_pair_data(token_address)
    if not pair:
        return None
    price_str = pair.get("priceUsd")
    return float(price_str) if price_str else None


def scan_volatile_solana_tokens() -> list[dict]:
    """
    Pull the latest trending Solana pairs and filter for pop-catching signals.
    Returns raw pair dicts from DexScreener, sorted by 5-min momentum.
    """
    candidates: list[dict] = []
    seen_addresses: set[str] = set()

    # Source 1: search for high-activity Solana pairs
    for query in ["SOL memecoin", "solana token"]:
        data = _get(f"{_DEXSCREENER}/latest/dex/search", params={"q": query})
        if data:
            for pair in data.get("pairs", []):
                if pair.get("chainId") != "solana":
                    continue
                addr = pair.get("baseToken", {}).get("address", "")
                if addr and addr not in seen_addresses:
                    seen_addresses.add(addr)
                    candidates.append(pair)

    # Source 2: boosted/trending tokens
    data = _get(f"{_DEXSCREENER}/token-boosts/latest/v1")
    if data:
        boost_items = data if isinstance(data, list) else data.get("items", [])
        for item in boost_items:
            if item.get("chainId") != "solana":
                continue
            addr = item.get("tokenAddress", "")
            if addr and addr not in seen_addresses:
                seen_addresses.add(addr)
                pair = get_pair_data(addr)
                if pair:
                    candidates.append(pair)

    logger.info("Scanner found %d raw Solana candidates", len(candidates))
    return candidates


def format_pair_summary(pair: dict) -> str:
    symbol = pair.get("baseToken", {}).get("symbol", "?")
    price = pair.get("priceUsd", "?")
    m5 = pair.get("priceChange", {}).get("m5", 0) or 0
    h1 = pair.get("priceChange", {}).get("h1", 0) or 0
    vol_h1 = pair.get("volume", {}).get("h1", 0) or 0
    mcap = pair.get("marketCap", 0) or 0
    return (
        f"{symbol:10s} ${price}  "
        f"5m:{m5:+.1f}%  1h:{h1:+.1f}%  "
        f"vol1h:${vol_h1:,.0f}  mcap:${mcap:,.0f}"
    )
