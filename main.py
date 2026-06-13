#!/usr/bin/env python3
"""
Moonshot Crypto Volatility Trader
----------------------------------
Automatically scans Solana for momentum pops and trades
using Jupiter aggregator. Monitors your existing wallet
tokens for exit signals too.

Usage:
  python main.py                  # Live trading
  python main.py --dry-run        # Simulate signals, no real transactions
  python main.py --status         # Print wallet + open positions and exit

Setup:
  cp .env.example .env
  # Edit .env with your SOLANA_PRIVATE_KEY
  pip install -r requirements.txt
"""

import argparse
import logging
import sys

import moonshot_trader.config as cfg
from moonshot_trader import market, wallet
from moonshot_trader.trader import MoonshotTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trader.log"),
    ],
)
logger = logging.getLogger(__name__)


def _check_config() -> None:
    if not cfg.PRIVATE_KEY:
        print("ERROR: SOLANA_PRIVATE_KEY not set in .env file")
        print("Copy .env.example to .env and fill in your wallet private key.")
        sys.exit(1)


def cmd_status(keypair, client) -> None:
    pubkey = keypair.pubkey()
    print(f"\nWallet: {pubkey}")

    sol = wallet.get_sol_balance(client, pubkey)
    print(f"SOL balance: {sol:.4f} SOL")

    token_bals = wallet.get_token_balances(client, pubkey)
    if token_bals:
        print(f"\nToken holdings ({len(token_bals)}):")
        for mint, info in token_bals.items():
            pair = market.get_pair_data(mint)
            if pair:
                price = float(pair.get("priceUsd") or 0)
                usd_value = price * info["ui_amount"]
                symbol = pair.get("baseToken", {}).get("symbol", mint[:8])
                print(f"  {symbol:12s}  {info['ui_amount']:.4f} tokens  ${usd_value:.2f}")
            else:
                print(f"  {mint[:16]}...  {info['ui_amount']:.4f} tokens  (price unavailable)")
    else:
        print("No SPL tokens in wallet.")

    from moonshot_trader.positions import PositionBook
    book = PositionBook()
    positions = book.all()
    if positions:
        print(f"\nBot-managed positions ({len(positions)}):")
        for p in positions:
            import time
            age = (time.time() - p.entry_time) / 60
            cur_price = market.get_price_usd(p.token_address) or p.entry_price_usd
            pct = (cur_price - p.entry_price_usd) / p.entry_price_usd * 100
            print(
                f"  {p.token_symbol:12s}  entry=${p.entry_price_usd:.6f}  "
                f"now=${cur_price:.6f}  {pct:+.1f}%  age:{age:.0f}min"
            )
    else:
        print("\nNo bot-managed positions.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Moonshot volatility trader")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate signals without sending real transactions")
    parser.add_argument("--status", action="store_true",
                        help="Print wallet status and exit")
    args = parser.parse_args()

    _check_config()

    try:
        keypair = wallet.load_keypair(cfg.PRIVATE_KEY)
    except Exception as e:
        print(f"ERROR: Could not load wallet from SOLANA_PRIVATE_KEY: {e}")
        sys.exit(1)

    client = wallet.make_client(cfg.RPC_ENDPOINT)

    if args.status:
        cmd_status(keypair, client)
        return

    trader = MoonshotTrader(keypair, client, dry_run=args.dry_run)
    try:
        trader.run()
    except KeyboardInterrupt:
        logger.info("Trader stopped by user.")


if __name__ == "__main__":
    main()
