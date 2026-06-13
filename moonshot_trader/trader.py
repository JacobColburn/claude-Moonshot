"""
Main trading loop.

Cycle:
  1. Check open positions → exit if take-profit / stop-loss / time-stop
  2. Scan for new pop signals → enter if capital available
  3. Also monitor existing wallet holdings (non-bot tokens) for exit signals
  4. Sleep, repeat
"""

import logging
import time
from typing import Optional

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey

import moonshot_trader.config as cfg
from moonshot_trader import jupiter, market, wallet
from moonshot_trader.positions import Position, PositionBook
from moonshot_trader.strategy import check_entry, check_exit

logger = logging.getLogger(__name__)

SOL_MINT = cfg.SOL_MINT


class MoonshotTrader:
    def __init__(self, keypair: Keypair, client: Client, dry_run: bool = False):
        self.keypair = keypair
        self.client = client
        self.pubkey: Pubkey = keypair.pubkey()
        self.book = PositionBook()
        self.dry_run = dry_run
        self._known_wallet_tokens: set[str] = set()  # tokens held before bot started

        if dry_run:
            logger.info("DRY RUN mode — no real transactions will be sent")

    # ------------------------------------------------------------------
    # Portfolio snapshot
    # ------------------------------------------------------------------

    def _sol_balance(self) -> float:
        return wallet.get_sol_balance(self.client, self.pubkey)

    def _available_sol(self) -> float:
        """SOL available for new trades (after reserve and open position allocation)."""
        bal = self._sol_balance()
        reserved = cfg.MIN_SOL_RESERVE
        return max(0.0, bal - reserved)

    def _position_size_sol(self) -> float:
        """How much SOL to spend on the next trade."""
        avail = self._available_sol()
        return min(cfg.MAX_POSITION_SOL, avail)

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def _buy(self, token_address: str, token_symbol: str, price_usd: float,
              pair_address: str, decimals: int = 6) -> bool:
        size_sol = self._position_size_sol()
        if size_sol < 0.005:
            logger.info("Insufficient SOL for new position (available: %.4f SOL)", size_sol)
            return False

        amount_lamports = jupiter.sol_to_lamports(size_sol)
        quote = jupiter.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_address,
            amount_lamports=amount_lamports,
            slippage_bps=cfg.SLIPPAGE_BPS,
        )
        if not quote:
            logger.warning("No quote available for %s", token_symbol)
            return False

        out_raw = int(quote.get("outAmount", 0))
        out_ui = out_raw / (10 ** decimals)

        logger.info(
            "BUY %s: %.4f SOL → %.4f tokens  (price: $%.6f)",
            token_symbol, size_sol, out_ui, price_usd,
        )

        if self.dry_run:
            logger.info("[DRY RUN] Skipping transaction")
            return False

        swap_tx = jupiter.get_swap_transaction(quote, str(self.pubkey))
        if not swap_tx:
            return False

        sig = wallet.sign_and_send(self.client, swap_tx, self.keypair)
        if not sig:
            return False

        confirmed = wallet.confirm_transaction(self.client, sig)
        if not confirmed:
            logger.warning("BUY transaction for %s did not confirm", token_symbol)
            return False

        pos = Position(
            token_address=token_address,
            token_symbol=token_symbol,
            entry_price_usd=price_usd,
            entry_sol_spent=size_sol,
            token_amount=out_ui,
            token_decimals=decimals,
            entry_time=time.time(),
            entry_tx=sig,
            pair_address=pair_address,
        )
        self.book.add(pos)
        return True

    def _sell(self, token_address: str, token_symbol: str,
              token_amount: float, decimals: int, reason: str) -> bool:
        amount_raw = jupiter.token_units(token_amount, decimals)
        if amount_raw <= 0:
            return False

        quote = jupiter.get_quote(
            input_mint=token_address,
            output_mint=SOL_MINT,
            amount_lamports=amount_raw,
            slippage_bps=cfg.SLIPPAGE_BPS,
        )
        if not quote:
            logger.warning("No sell quote for %s — keeping position", token_symbol)
            return False

        out_lamports = int(quote.get("outAmount", 0))
        out_sol = jupiter.lamports_to_sol(out_lamports)

        logger.info("SELL %s: %.4f tokens → %.4f SOL  [%s]",
                    token_symbol, token_amount, out_sol, reason)

        if self.dry_run:
            logger.info("[DRY RUN] Skipping transaction")
            return False

        swap_tx = jupiter.get_swap_transaction(quote, str(self.pubkey))
        if not swap_tx:
            return False

        sig = wallet.sign_and_send(self.client, swap_tx, self.keypair)
        if not sig:
            return False

        confirmed = wallet.confirm_transaction(self.client, sig)
        if not confirmed:
            logger.warning("SELL transaction for %s did not confirm", token_symbol)
            return False

        self.book.remove(token_address)
        return True

    # ------------------------------------------------------------------
    # Scanning and monitoring
    # ------------------------------------------------------------------

    def _monitor_positions(self) -> None:
        """Check each open position for exit signals."""
        if not self.book.all():
            return

        current_prices: dict[str, float] = {}
        for pos in self.book.all():
            price = market.get_price_usd(pos.token_address)
            if price:
                current_prices[pos.token_address] = price

        logger.info("Open positions:\n%s", self.book.pnl_summary(current_prices))

        for pos in list(self.book.all()):
            cur_price = current_prices.get(pos.token_address)
            if not cur_price:
                logger.debug("No price data for %s, skipping exit check", pos.token_symbol)
                continue

            reason = check_exit(pos.entry_price_usd, cur_price, pos.entry_time)
            if reason:
                self._sell(
                    pos.token_address,
                    pos.token_symbol,
                    pos.token_amount,
                    pos.token_decimals,
                    reason,
                )

    def _scan_for_entries(self) -> None:
        """Scan DexScreener for pop signals and enter if capacity allows."""
        if self.book.count() >= cfg.MAX_POSITIONS:
            logger.debug("Max positions reached (%d), skipping scan", cfg.MAX_POSITIONS)
            return

        if self._position_size_sol() < 0.005:
            logger.debug("Insufficient SOL for new trades, skipping scan")
            return

        candidates = market.scan_volatile_solana_tokens()
        signals_found = 0

        for pair in candidates:
            addr = pair.get("baseToken", {}).get("address", "")
            if not addr:
                continue
            if self.book.has(addr):
                continue  # Already holding
            if addr in self._known_wallet_tokens:
                continue  # Pre-existing holding — don't double-dip

            signal = check_entry(pair)
            if not signal:
                continue

            signals_found += 1
            logger.info(
                "SIGNAL %s: %s",
                signal.token_symbol,
                signal.reason,
            )

            pair_address = pair.get("pairAddress", "")
            # Try to get token decimals from the pair data
            decimals = 6  # default for most SPL tokens
            base_token_info = pair.get("baseToken", {})
            # Some pairs include decimals in their data
            if "decimals" in base_token_info:
                decimals = int(base_token_info["decimals"])

            bought = self._buy(
                token_address=addr,
                token_symbol=signal.token_symbol,
                price_usd=signal.price_usd,
                pair_address=pair_address,
                decimals=decimals,
            )

            if bought:
                logger.info("Entered %s at $%.6f", signal.token_symbol, signal.price_usd)
                if self.book.count() >= cfg.MAX_POSITIONS:
                    break

        if signals_found == 0:
            logger.debug("No entry signals found in this scan")

    def _load_existing_wallet_tokens(self) -> None:
        """
        Detect tokens already in the wallet that aren't bot positions.
        These get monitored for exit signals but won't trigger re-buys.
        """
        balances = wallet.get_token_balances(self.client, self.pubkey)
        for mint, info in balances.items():
            if mint == SOL_MINT:
                continue
            if not self.book.has(mint):
                self._known_wallet_tokens.add(mint)

        if self._known_wallet_tokens:
            logger.info(
                "Found %d pre-existing wallet tokens to monitor: %s",
                len(self._known_wallet_tokens),
                list(self._known_wallet_tokens)[:5],
            )

    def _monitor_wallet_tokens(self) -> None:
        """
        Apply exit logic to tokens already in the wallet when the bot started.
        Uses the current price vs. an estimated entry (we don't know real entry,
        so we baseline on first-seen price and monitor from there).
        """
        if not self._known_wallet_tokens:
            return

        balances = wallet.get_token_balances(self.client, self.pubkey)
        for mint in list(self._known_wallet_tokens):
            info = balances.get(mint)
            if not info or info["ui_amount"] <= 0:
                self._known_wallet_tokens.discard(mint)
                continue

            pair = market.get_pair_data(mint)
            if not pair:
                continue

            price = float(pair.get("priceUsd") or 0)
            symbol = pair.get("baseToken", {}).get("symbol", mint[:6])

            logger.info(market.format_pair_summary(pair))

            # For wallet tokens, we use a simplified momentum exit:
            # sell if -15% on 1h or +30% on 1h (catch pumps or cut losses)
            h1_pct = float(pair.get("priceChange", {}).get("h1") or 0)
            h24_pct = float(pair.get("priceChange", {}).get("h24") or 0)

            sell_reason: Optional[str] = None
            if h1_pct <= -15:
                sell_reason = f"WALLET_DUMP  1h:{h1_pct:.1f}%"
            elif h1_pct >= 30:
                sell_reason = f"WALLET_PUMP  1h:+{h1_pct:.1f}%"
            elif h24_pct <= -35:
                sell_reason = f"WALLET_BLEED  24h:{h24_pct:.1f}%"

            if sell_reason:
                self._sell(mint, symbol, info["ui_amount"], info["decimals"], sell_reason)
                self._known_wallet_tokens.discard(mint)

    # ------------------------------------------------------------------
    # Public run loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("Moonshot Volatility Trader starting up")
        logger.info("Wallet: %s", self.pubkey)

        sol_bal = self._sol_balance()
        logger.info("SOL balance: %.4f SOL", sol_bal)

        token_bals = wallet.get_token_balances(self.client, self.pubkey)
        if token_bals:
            logger.info("Wallet tokens: %s", list(token_bals.keys())[:10])

        self._load_existing_wallet_tokens()
        logger.info("=" * 60)

        last_scan = 0.0
        last_monitor = 0.0

        while True:
            now = time.time()

            # Monitor open positions
            if now - last_monitor >= cfg.MONITOR_INTERVAL_SECONDS:
                try:
                    self._monitor_positions()
                    self._monitor_wallet_tokens()
                except Exception as e:
                    logger.error("Monitor error: %s", e)
                last_monitor = now

            # Scan for new entries
            if now - last_scan >= cfg.SCAN_INTERVAL_SECONDS:
                try:
                    self._scan_for_entries()
                except Exception as e:
                    logger.error("Scan error: %s", e)
                last_scan = now

            time.sleep(5)
