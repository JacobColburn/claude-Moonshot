"""
Claude AI decision engine for the autonomous Solana trader.

Takes a market snapshot (wallet state + candidate tokens) and returns
structured buy/sell/hold decisions. Uses claude-opus-4-8 with adaptive thinking.
"""

import json
import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are an autonomous Solana meme-coin / moonshot trader.
Your goal is high risk / high reward: catch momentum pops on new launches,
trending Reddit coins, and volatile tokens. The user accepts full loss risk.

You will receive a JSON snapshot with:
  - wallet: {sol_balance, token_holdings, open_positions}
  - candidates: list of tokens to evaluate (from DexScreener, moonshot.money, Reddit)
  - market_context: any additional signals

Respond ONLY with a JSON object in this exact schema — no explanation, no markdown:
{
  "actions": [
    {
      "action": "BUY" | "SELL" | "HOLD",
      "token_address": "<mint address>",
      "token_symbol": "<symbol>",
      "reason": "<one sentence why>",
      "confidence": 0.0-1.0,
      "urgency": "HIGH" | "MEDIUM" | "LOW"
    }
  ],
  "summary": "<brief overall market read, 1-2 sentences>"
}

Trading rules you must respect:
- Only BUY tokens that appear in the candidates list with a valid token_address
- Only SELL tokens that appear in open_positions or token_holdings
- Max 4 concurrent positions — do not BUY if already at max
- Skip tokens with liquidity < $15,000 (rug risk) unless urgency is extreme
- Prefer new launches (< 2 hours old) from moonshot.money for highest upside
- Reddit mentions with score > 200 are meaningful social signal
- For SELL: use reason codes TAKE_PROFIT / STOP_LOSS / TIME_STOP / MOMENTUM_FADE
- Confidence below 0.5 should be HOLD, not BUY
- If nothing interesting: return empty actions array with a summary explaining why
"""


def decide(snapshot: dict) -> dict:
    """
    Pass a market snapshot to Claude and get structured trade decisions.

    Returns a dict with 'actions' list and 'summary' string.
    On error returns {'actions': [], 'summary': 'Claude unavailable: <err>'}.
    """
    client = _get_client()

    prompt = json.dumps(snapshot, indent=2, default=str)

    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2048,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Here is the current market snapshot. Make trading decisions:\n\n{prompt}",
                }
            ],
        )

        # Extract text content block (skip thinking blocks)
        text = ""
        for block in response.content:
            if block.type == "text":
                text = block.text
                break

        if not text:
            logger.warning("Claude returned no text content")
            return {"actions": [], "summary": "Claude returned empty response"}

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

        result = json.loads(text)
        actions = result.get("actions", [])
        summary = result.get("summary", "")

        logger.info("Claude brain: %d actions | %s", len(actions), summary)
        for a in actions:
            logger.info(
                "  [%s] %s %s (conf=%.2f, urgency=%s) — %s",
                a.get("action"), a.get("token_symbol"), a.get("token_address", "")[:8],
                a.get("confidence", 0), a.get("urgency", "?"), a.get("reason", "")
            )

        return result

    except json.JSONDecodeError as e:
        logger.error("Claude response was not valid JSON: %s", e)
        return {"actions": [], "summary": f"JSON parse error: {e}"}
    except Exception as e:
        logger.error("Claude brain error: %s", e)
        return {"actions": [], "summary": f"Claude unavailable: {e}"}


def build_snapshot(
    sol_balance: float,
    token_holdings: list[dict],
    open_positions: list[dict],
    candidates: list[dict],
) -> dict:
    """Assemble the JSON snapshot that Claude evaluates."""

    def _trim_candidate(c: dict) -> dict:
        """Keep only the fields Claude needs — avoid token limit blow-out."""
        return {
            "source": c.get("source", "unknown"),
            "token_address": c.get("mint") or c.get("baseToken", {}).get("address", ""),
            "symbol": (
                c.get("symbol")
                or c.get("baseToken", {}).get("symbol", "?")
            ),
            "price_usd": float(c.get("priceUsd") or 0),
            "price_change_5m": float((c.get("priceChange") or {}).get("m5") or 0),
            "price_change_1h": float((c.get("priceChange") or {}).get("h1") or 0),
            "price_change_24h": float((c.get("priceChange") or {}).get("h24") or 0),
            "volume_5m": float((c.get("volume") or {}).get("m5") or 0),
            "volume_1h": float((c.get("volume") or {}).get("h1") or 0),
            "liquidity_usd": float((c.get("liquidity") or {}).get("usd") or 0),
            "market_cap": float(c.get("marketCap") or c.get("fdv") or 0),
            "pair_address": c.get("pairAddress", ""),
            "pair_age_ms": c.get("pairCreatedAt", 0),
            "buys_5m": int((c.get("txns") or {}).get("m5", {}).get("buys") or 0),
            "sells_5m": int((c.get("txns") or {}).get("m5", {}).get("sells") or 0),
            # Reddit-specific
            "reddit_score": c.get("reddit_score", 0),
            "reddit_subreddit": c.get("reddit_subreddit", ""),
            "reddit_title": c.get("reddit_title", "")[:120],
        }

    return {
        "wallet": {
            "sol_balance": sol_balance,
            "token_holdings": token_holdings,
            "open_positions": open_positions,
        },
        "candidates": [_trim_candidate(c) for c in candidates if (
            c.get("mint") or c.get("baseToken", {}).get("address", "")
        )],
    }
