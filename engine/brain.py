"""
Claude review layer (Opus 4.8).

The quant engine scores and ranks the whole field cheaply. Only the top-N
finalists — already past the sanity floors and the RugCheck safety gate — are
sent to Claude for a final qualitative gut-check: does this look like a genuine
momentum setup or a trap? Claude returns a conviction (0-1) per candidate, which
feeds position sizing. A low conviction vetoes the trade.

The brain is optional: if no API key is configured, every finalist passes with
conviction 1.0 and the engine runs as a pure quant system.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from engine import config as cfg

logger = logging.getLogger("engine.brain")

_client = None


def available() -> bool:
    return bool(cfg.BRAIN_ENABLED and cfg.ANTHROPIC_API_KEY)


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    return _client


SYSTEM = """You are the risk-and-conviction reviewer for an automated Solana \
memecoin momentum trader. A quantitative engine has already scored and \
safety-screened the candidates below; your job is the final human-style \
gut-check before real capital is deployed.

For each candidate decide a conviction from 0.0 to 1.0:
  - 0.0-0.4  trap / likely dump — veto (the engine will skip it)
  - 0.5-0.7  acceptable momentum setup — trade at reduced size
  - 0.8-1.0  high-quality setup — full size

Weigh: is the 5m/1h momentum genuine and supported by volume acceleration and \
buy pressure, or is it a single-candle spike about to reverse? Is liquidity and \
market cap in a tradable range? Does the safety report look clean? Penalize \
over-extended runners (already up huge on the hour) and thin/!suspicious books.

Respond with ONLY a JSON object, no prose, no markdown:
{"reviews":[{"mint":"<mint>","conviction":0.0-1.0,"note":"<=10 words"}],"read":"<one-sentence market read>"}"""


def review(finalists: list[dict]) -> dict:
    """
    finalists: list of compact dicts (symbol, mint, score, signals, safety, market...).
    Returns {mint: {conviction, note}} plus a 'read' summary under key '_read'.
    Fail-open: on any error every finalist gets conviction 1.0.
    """
    if not finalists:
        return {"_read": ""}
    if not available():
        return {**{f["mint"]: {"conviction": 1.0, "note": "quant-only"} for f in finalists},
                "_read": "Claude disabled — pure quant mode"}

    try:
        client = _get_client()
        payload = json.dumps({"candidates": finalists}, default=str)
        resp = client.messages.create(
            model=cfg.BRAIN_MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": f"Review these candidates:\n\n{payload}"}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        out: dict = {"_read": data.get("read", "")}
        for r in data.get("reviews", []):
            mint = r.get("mint")
            if mint:
                out[mint] = {
                    "conviction": max(0.0, min(1.0, float(r.get("conviction", 0)))),
                    "note": str(r.get("note", ""))[:60],
                }
        # Any finalist Claude didn't mention → neutral pass.
        for f in finalists:
            out.setdefault(f["mint"], {"conviction": 0.6, "note": "unrated"})
        logger.info("brain: reviewed %d — %s", len(finalists), out["_read"])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("brain review failed (%s) — passing through", e)
        return {**{f["mint"]: {"conviction": 1.0, "note": "brain error"} for f in finalists},
                "_read": f"Claude unavailable: {e}"}
