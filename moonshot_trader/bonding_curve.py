"""
moonshot.money bonding curve integration.

Handles pre-graduation token buys/sells directly on moonshot's on-chain
bonding curve program — earlier than Jupiter can see them.

Falls back gracefully if moonshot-py isn't installed:
  pip install "moonshot-py @ git+https://github.com/ruiqic/moonshot-py.git"

Will break if moonshot updates their contracts. Fix: re-install moonshot-py.
"""

import asyncio
import logging
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# --- Known constants (from moonshot-py/src/moonshot/constants.py) ---
MOONSHOT_PROGRAM_ID_STR = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
TOKEN_PRECISION = 1_000_000_000  # 1 SOL in lamports / 1 token at 9 decimals

try:
    from moonshot.token_launchpad import TokenLaunchpad
    from moonshot.types import FixedSide
    from moonshot.constants import MOONSHOT_PROGRAM_ID, TOKEN_PRECISION as _TP
    from solana.rpc.async_api import AsyncClient as _AsyncClient
    from anchorpy import Wallet as _AnchorWallet

    MOONSHOT_AVAILABLE = True
    logger.debug("moonshot-py loaded — bonding curve trading enabled")
except ImportError:
    MOONSHOT_AVAILABLE = False
    logger.info(
        "moonshot-py not installed — bonding curve trades disabled. "
        "Install: pip install 'moonshot-py @ git+https://github.com/ruiqic/moonshot-py.git'"
    )


def _curve_pda(mint_address: str) -> "Pubkey":
    """Derive the PDA for a token's curve account."""
    mint = Pubkey.from_string(mint_address)
    program_id = Pubkey.from_string(MOONSHOT_PROGRAM_ID_STR)
    pda, _ = Pubkey.find_program_address([b"token", bytes(mint)], program_id)
    return pda


def is_on_bonding_curve(mint_address: str, rpc_url: str) -> bool:
    """
    Returns True if this token still has an active moonshot curve account
    (i.e. hasn't graduated to Raydium yet).
    """
    async def _check() -> bool:
        client = _AsyncClient(rpc_url)
        try:
            pda = _curve_pda(mint_address)
            resp = await client.get_account_info(pda)
            return resp.value is not None
        except Exception as e:
            logger.debug("Curve account check failed for %s: %s", mint_address[:8], e)
            return False
        finally:
            await client.close()

    if not MOONSHOT_AVAILABLE:
        return False
    try:
        return asyncio.run(_check())
    except Exception as e:
        logger.debug("is_on_bonding_curve error: %s", e)
        return False


def buy_on_curve(
    keypair: Keypair,
    rpc_url: str,
    mint_address: str,
    sol_amount: float,
    slippage_bps: int = 500,
) -> Optional[str]:
    """
    Buy a pre-graduation token on the moonshot bonding curve.
    sol_amount is in SOL (e.g. 0.05).
    Returns transaction signature string, or None on failure.
    """
    if not MOONSHOT_AVAILABLE:
        logger.warning("moonshot-py not installed — cannot buy on curve")
        return None

    async def _buy() -> Optional[str]:
        client = _AsyncClient(rpc_url)
        try:
            wallet = _AnchorWallet(keypair)
            mint = Pubkey.from_string(mint_address)
            launchpad = TokenLaunchpad(client, wallet, mint)

            lamports = int(sol_amount * TOKEN_PRECISION)
            ix = await launchpad.get_buy_ix(
                lamports,
                fixed_side=FixedSide.ExactIn(),
                slippage_bps=slippage_bps,
            )
            sig = await launchpad.send_ix(ix)
            return str(sig)
        except Exception as e:
            logger.error("Bonding curve BUY failed for %s: %s", mint_address[:8], e)
            return None
        finally:
            await client.close()

    try:
        return asyncio.run(_buy())
    except Exception as e:
        logger.error("buy_on_curve runtime error: %s", e)
        return None


def sell_on_curve(
    keypair: Keypair,
    rpc_url: str,
    mint_address: str,
    token_amount: float,
    decimals: int = 9,
    slippage_bps: int = 500,
) -> Optional[str]:
    """
    Sell tokens back on the moonshot bonding curve.
    token_amount is UI amount (e.g. 1000.0 tokens).
    Returns transaction signature string, or None on failure.
    """
    if not MOONSHOT_AVAILABLE:
        logger.warning("moonshot-py not installed — cannot sell on curve")
        return None

    async def _sell() -> Optional[str]:
        client = _AsyncClient(rpc_url)
        try:
            wallet = _AnchorWallet(keypair)
            mint = Pubkey.from_string(mint_address)
            launchpad = TokenLaunchpad(client, wallet, mint)

            amount_raw = int(token_amount * (10 ** decimals))
            ix = await launchpad.get_sell_ix(
                amount_raw,
                fixed_side=FixedSide.ExactIn(),
                slippage_bps=slippage_bps,
            )
            sig = await launchpad.send_ix(ix)
            return str(sig)
        except Exception as e:
            logger.error("Bonding curve SELL failed for %s: %s", mint_address[:8], e)
            return None
        finally:
            await client.close()

    try:
        return asyncio.run(_sell())
    except Exception as e:
        logger.error("sell_on_curve runtime error: %s", e)
        return None
