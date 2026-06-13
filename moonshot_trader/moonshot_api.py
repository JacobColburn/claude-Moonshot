"""
moonshot.money new token scanner.

Fetches recently launched tokens from the moonshot.money API and DexScreener
to surface high-risk/high-reward new launches for Claude to evaluate.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MOONSHOT_TOKENS_URL = "https://api.moonshot.money/tokens"
DEXSCREENER_PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

_last_request: float = 0.0
_MIN_INTERVAL = 1.5


def _throttled_get(url: str, params: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        _last_request = time.time()
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("moonshot_api request failed: %s", e)
    return None


def get_new_launches(limit: int = 30) -> list[dict]:
    """
    Return recently launched tokens from moonshot.money, enriched with
    DexScreener pair data when available.
    """
    data = _throttled_get(MOONSHOT_TOKENS_URL, params={"limit": limit, "sort": "createdAt"})
    tokens = []

    if isinstance(data, list):
        raw_tokens = data[:limit]
    elif isinstance(data, dict):
        raw_tokens = data.get("tokens", data.get("data", []))[:limit]
    else:
        raw_tokens = []

    for tok in raw_tokens:
        mint = tok.get("mint") or tok.get("address") or tok.get("token_address", "")
        if not mint:
            continue

        enriched = _enrich_from_dexscreener(mint, tok)
        if enriched:
            tokens.append(enriched)

    logger.info("moonshot.money: fetched %d new launches", len(tokens))
    return tokens


def _enrich_from_dexscreener(mint: str, base_info: dict) -> Optional[dict]:
    """Fetch DexScreener pair data for a mint address."""
    data = _throttled_get(DEXSCREENER_SEARCH_URL, params={"q": mint})
    if not data:
        return _minimal_token(mint, base_info)

    pairs = data.get("pairs") or []
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        return _minimal_token(mint, base_info)

    # Pick highest-liquidity Solana pair
    sol_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd") or 0), reverse=True)
    pair = sol_pairs[0]

    return {
        "source": "moonshot",
        "mint": mint,
        "symbol": pair.get("baseToken", {}).get("symbol", base_info.get("symbol", mint[:8])),
        "name": pair.get("baseToken", {}).get("name", base_info.get("name", "")),
        "pairAddress": pair.get("pairAddress", ""),
        "priceUsd": float(pair.get("priceUsd") or 0),
        "priceChange": pair.get("priceChange", {}),
        "volume": pair.get("volume", {}),
        "liquidity": pair.get("liquidity", {}),
        "fdv": float(pair.get("fdv") or 0),
        "marketCap": float(pair.get("marketCap") or pair.get("fdv") or 0),
        "txns": pair.get("txns", {}),
        "pairCreatedAt": pair.get("pairCreatedAt", base_info.get("createdAt", 0)),
        "dex": pair.get("dexId", "unknown"),
        "raw_moonshot": base_info,
    }


def _minimal_token(mint: str, base_info: dict) -> dict:
    """Return minimal token data when DexScreener enrichment fails."""
    return {
        "source": "moonshot",
        "mint": mint,
        "symbol": base_info.get("symbol", mint[:8]),
        "name": base_info.get("name", ""),
        "pairAddress": "",
        "priceUsd": float(base_info.get("price") or base_info.get("priceUsd") or 0),
        "priceChange": {},
        "volume": {},
        "liquidity": {},
        "fdv": 0.0,
        "marketCap": float(base_info.get("marketCap") or 0),
        "txns": {},
        "pairCreatedAt": base_info.get("createdAt", 0),
        "dex": "moonshot",
        "raw_moonshot": base_info,
    }
