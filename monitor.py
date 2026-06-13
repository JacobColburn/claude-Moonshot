#!/usr/bin/env python3
"""
Position Monitor — lightweight 24/7 daemon
-------------------------------------------
Runs independently with NO Claude API calls and NO extra cost.
Checks open positions every 60 seconds and fires stop-loss / take-profit
/ time-stop exits automatically.

Run this in the background whenever you have live positions:
  python monitor.py

This pairs with the MCP server:
  - monitor.py  → protects positions 24/7 (mechanical rules, instant)
  - mcp_server  → gives Claude tools to scan and enter new positions

Both share the same positions.json file.
"""

import logging
import sys
import time

import moonshot_trader.config as cfg
from moonshot_trader import market, wallet
from moonshot_trader.bonding_curve import is_on_bonding_curve, sell_on_curve
from moonshot_trader.jupiter import (
    get_quote,
    get_swap_transaction,
    lamports_to_sol,
    token_units,
)
from moonshot_trader.positions import PositionBook
from moonshot_trader.strategy import check_exit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  MONITOR  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log"),
    ],
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds between position checks
SOL_MINT = cfg.SOL_MINT


def _sell_position(keypair, client, book: PositionBook, pos, reason: str) -> bool:
    """Execute a sell — tries bonding curve first, falls back to Jupiter."""
    amount_raw = token_units(pos.token_amount, pos.token_decimals)
    if amount_raw <= 0:
        return False

    sig = None

    on_curve = is_on_bonding_curve(pos.token_address, cfg.RPC_ENDPOINT)
    if on_curve:
        sig = sell_on_curve(
            keypair, cfg.RPC_ENDPOINT,
            pos.token_address, pos.token_amount, pos.token_decimals,
            cfg.SLIPPAGE_BPS,
        )

    if not sig:
        quote = get_quote(
            input_mint=pos.token_address,
            output_mint=SOL_MINT,
            amount_lamports=amount_raw,
            slippage_bps=cfg.SLIPPAGE_BPS,
        )
        if not quote:
            logger.warning("No sell quote for %s — will retry next cycle", pos.token_symbol)
            return False

        out_sol = lamports_to_sol(int(quote.get("outAmount", 0)))
        swap_tx = get_swap_transaction(quote, str(keypair.pubkey()))
        if not swap_tx:
            return False

        sig = wallet.sign_and_send(client, swap_tx, keypair)
        if not sig:
            return False

        if not wallet.confirm_transaction(client, sig):
            logger.warning("SELL tx for %s did not confirm", pos.token_symbol)
            return False

        logger.info("SELL %s  [%s]  → %.4f SOL  tx=%s",
                    pos.token_symbol, reason, out_sol, sig)

    book.remove(pos.token_address)
    return True


def run_monitor(keypair, client) -> None:
    book = PositionBook()
    logger.info("Monitor started — checking positions every %ds", CHECK_INTERVAL)
    logger.info("Wallet: %s", keypair.pubkey())

    while True:
        positions = book.all()

        if positions:
            logger.info("Checking %d position(s)...", len(positions))
            for pos in list(positions):
                try:
                    cur_price = market.get_price_usd(pos.token_address)
                    if not cur_price:
                        logger.debug("No price for %s, skipping", pos.token_symbol)
                        continue

                    pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100
                    age_min = (time.time() - pos.entry_time) / 60
                    logger.info(
                        "  %s  entry=$%.6f  now=$%.6f  %+.1f%%  age:%.0fmin",
                        pos.token_symbol, pos.entry_price_usd, cur_price, pct, age_min,
                    )

                    reason = check_exit(pos.entry_price_usd, cur_price, pos.entry_time)
                    if reason:
                        logger.info("EXIT SIGNAL %s: %s", pos.token_symbol, reason)
                        _sell_position(keypair, client, book, pos, reason)

                except Exception as e:
                    logger.error("Error checking %s: %s", pos.token_symbol, e)
        else:
            logger.debug("No open positions")

        time.sleep(CHECK_INTERVAL)


def main() -> None:
    if not cfg.PRIVATE_KEY:
        print("ERROR: SOLANA_PRIVATE_KEY not set in .env")
        sys.exit(1)

    try:
        keypair = wallet.load_keypair(cfg.PRIVATE_KEY)
    except Exception as e:
        print(f"ERROR: Could not load wallet: {e}")
        sys.exit(1)

    client = wallet.make_client(cfg.RPC_ENDPOINT)

    try:
        run_monitor(keypair, client)
    except KeyboardInterrupt:
        logger.info("Monitor stopped.")


if __name__ == "__main__":
    main()
