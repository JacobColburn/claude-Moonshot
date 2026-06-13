"""Jupiter aggregator v6 — quote and swap execution for Solana."""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP = "https://quote-api.jup.ag/v6/swap"

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 300,
) -> Optional[dict]:
    """Get a Jupiter swap quote. Amount is in smallest token units (lamports for SOL)."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    try:
        resp = _session.get(JUPITER_QUOTE, params=params, timeout=15)
        if resp.ok:
            return resp.json()
        logger.warning("Jupiter quote failed %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Jupiter quote error: %s", e)
    return None


def get_swap_transaction(quote: dict, user_public_key: str) -> Optional[str]:
    """
    Request the swap transaction from Jupiter.
    Returns base64-encoded versioned transaction string, or None on failure.
    """
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        resp = _session.post(JUPITER_SWAP, json=body, timeout=20)
        if resp.ok:
            data = resp.json()
            return data.get("swapTransaction")
        logger.warning("Jupiter swap tx failed %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Jupiter swap tx error: %s", e)
    return None


def sol_to_lamports(sol: float) -> int:
    return int(sol * 1_000_000_000)


def lamports_to_sol(lamports: int) -> float:
    return lamports / 1_000_000_000


def token_units(amount_ui: float, decimals: int) -> int:
    """Convert UI amount to raw token units."""
    return int(amount_ui * (10 ** decimals))


def quote_out_ui(quote: dict) -> float:
    """Parse the output amount from a quote response into a human-readable float."""
    out_amount = int(quote.get("outAmount", 0))
    # Jupiter returns output in smallest unit; we need decimals to convert
    # For SOL output: 9 decimals. We'll handle per-token in caller.
    return out_amount
