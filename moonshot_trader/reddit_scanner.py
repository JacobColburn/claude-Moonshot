"""
Reddit scanner for Solana/crypto moonshot mentions.

Monitors high-signal subreddits for token tickers and Solana mint addresses,
then enriches hits with DexScreener data before handing to Claude.

Requires REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT in env.
Falls back to unauthenticated JSON scraping when credentials are absent.
"""

import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SUBREDDITS = [
    "CryptoMoonShots",
    "SolanaTrading",
    "memecoin",
    "solana",
    "CryptoCurrency",
]

# Matches base58 Solana public keys (32–44 chars, alphanumeric excluding 0OIl)
_SOLANA_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
# Matches $TICKER or token: TICKER patterns
_TICKER_RE = re.compile(r"\$([A-Z]{2,10})\b|(?:token|coin)[:\s]+([A-Z]{2,10})\b", re.IGNORECASE)

_DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
_last_dex_req: float = 0.0


def _dex_search(query: str) -> list[dict]:
    global _last_dex_req
    elapsed = time.time() - _last_dex_req
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    try:
        resp = requests.get(_DEXSCREENER_SEARCH, params={"q": query}, timeout=10)
        _last_dex_req = time.time()
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs") or []
            return [p for p in pairs if p.get("chainId") == "solana"]
    except Exception as e:
        logger.debug("DexScreener search failed for %s: %s", query, e)
    return []


def _fetch_subreddit_json(subreddit: str, limit: int = 25) -> list[dict]:
    """Fetch hot posts via public Reddit JSON API (no auth needed)."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json"
    try:
        resp = requests.get(
            url,
            params={"limit": limit},
            headers={"User-Agent": "TradeForge-Agent/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            posts = data.get("data", {}).get("children", [])
            return [p["data"] for p in posts if p.get("data")]
    except Exception as e:
        logger.debug("Reddit fetch failed for r/%s: %s", subreddit, e)
    return []


def _fetch_with_praw(subreddit: str, limit: int = 25) -> list[dict]:
    """Fetch using PRAW if credentials are configured."""
    client_id = os.getenv("REDDIT_CLIENT_ID", "")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    user_agent = os.getenv("REDDIT_USER_AGENT", "TradeForge-Agent/1.0")

    if not client_id or not client_secret:
        return []

    try:
        import praw
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        )
        posts = []
        for submission in reddit.subreddit(subreddit).hot(limit=limit):
            posts.append({
                "title": submission.title,
                "selftext": submission.selftext,
                "score": submission.score,
                "num_comments": submission.num_comments,
                "url": submission.url,
                "created_utc": submission.created_utc,
            })
        return posts
    except Exception as e:
        logger.debug("PRAW fetch failed for r/%s: %s", subreddit, e)
        return []


def _extract_candidates(post: dict) -> set[str]:
    """Extract Solana addresses and tickers from a Reddit post."""
    text = f"{post.get('title', '')} {post.get('selftext', '')}"
    candidates: set[str] = set()

    for addr in _SOLANA_ADDR_RE.findall(text):
        if len(addr) >= 32:  # Valid Solana pubkey length
            candidates.add(addr)

    for match in _TICKER_RE.finditer(text):
        ticker = match.group(1) or match.group(2)
        if ticker:
            candidates.add(ticker.upper())

    return candidates


def scan_reddit_mentions(max_per_sub: int = 20) -> list[dict]:
    """
    Scan configured subreddits for token mentions and return enriched token data.
    Returns list of DexScreener pair dicts tagged with reddit metadata.
    """
    seen_mints: set[str] = set()
    results: list[dict] = []

    for sub in SUBREDDITS:
        posts = _fetch_with_praw(sub, max_per_sub) or _fetch_subreddit_json(sub, max_per_sub)
        if not posts:
            logger.debug("No posts from r/%s", sub)
            continue

        logger.info("r/%s: scanning %d posts", sub, len(posts))

        # Collect all candidates from high-score posts first
        scored: list[tuple[int, str]] = []
        candidate_to_post: dict[str, dict] = {}
        for post in posts:
            score = post.get("score", 0)
            for cand in _extract_candidates(post):
                scored.append((score, cand))
                candidate_to_post[cand] = post

        # Sort by score desc, deduplicate
        scored.sort(reverse=True)
        seen_candidates: set[str] = set()
        for score, cand in scored:
            if cand in seen_candidates:
                continue
            seen_candidates.add(cand)

            pairs = _dex_search(cand)
            for pair in pairs:
                mint = pair.get("baseToken", {}).get("address", "")
                if not mint or mint in seen_mints:
                    continue
                seen_mints.add(mint)
                post = candidate_to_post.get(cand, {})
                enriched = {
                    **pair,
                    "source": "reddit",
                    "reddit_subreddit": sub,
                    "reddit_score": post.get("score", 0),
                    "reddit_comments": post.get("num_comments", 0),
                    "reddit_title": post.get("title", ""),
                    "mint": mint,
                }
                results.append(enriched)

    logger.info("Reddit scan: found %d unique Solana tokens", len(results))
    return results
