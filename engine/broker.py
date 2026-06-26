"""
Execution abstraction.

PaperBroker  — simulates fills against the live quoted price with configurable
               slippage + fees. No wallet, no chain, no risk. The default.
LiveBroker   — wraps the existing Jupiter + wallet primitives to place real swaps
               through your Solana wallet. Imported lazily so paper mode runs
               without the solana/solders dependencies installed.

Both expose the same surface:
    buy(snapshot, sol_amount)  -> Fill | None
    sell(position, price)      -> Fill | None
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from engine import config as cfg
from engine.portfolio import Position
from engine.signals import TokenSnapshot

logger = logging.getLogger("engine.broker")


@dataclass
class Fill:
    price_usd: float       # effective fill price after slippage
    amount: float          # token units in (buy) or out (sell)
    sol_delta: float       # SOL spent (buy, positive) or received (sell)
    tx: str = ""


class PaperBroker:
    """Simulated fills. Deterministic given price + config."""

    live = False

    def buy(self, snap: TokenSnapshot, sol_amount: float) -> Optional[Fill]:
        if sol_amount <= 0 or snap.price_usd <= 0:
            return None
        slip = cfg.PAPER_FILL_SLIPPAGE_PCT / 100.0
        fee = cfg.PAPER_FEE_PCT / 100.0
        fill_price = snap.price_usd * (1 + slip)
        sol_after_fee = sol_amount * (1 - fee)
        # Token amount is bookkeeping-only in paper mode (PnL is price-based),
        # but we compute it so positions display a realistic size.
        tokens = (sol_after_fee / fill_price) if fill_price else 0.0
        return Fill(price_usd=fill_price, amount=tokens, sol_delta=sol_amount, tx="paper")

    def sell(self, pos: Position, price: float) -> Optional[Fill]:
        if price <= 0:
            return None
        slip = cfg.PAPER_FILL_SLIPPAGE_PCT / 100.0
        fee = cfg.PAPER_FEE_PCT / 100.0
        fill_price = price * (1 - slip)
        # Mark-to-market SOL out, net of slippage + fee.
        gross = pos.sol_in * (fill_price / pos.entry_price_usd) if pos.entry_price_usd else 0.0
        sol_out = max(0.0, gross * (1 - fee))
        return Fill(price_usd=fill_price, amount=pos.amount, sol_delta=sol_out, tx="paper")


class LiveBroker:
    """Real swaps via Jupiter through your Solana wallet."""

    live = True

    def __init__(self):
        # Lazy imports — only needed when actually trading live.
        from moonshot_trader import jupiter, wallet  # noqa
        self._jupiter = jupiter
        self._wallet = wallet
        if not cfg.PRIVATE_KEY:
            raise RuntimeError("LIVE mode requires SOLANA_PRIVATE_KEY")
        self._keypair = wallet.load_keypair(cfg.PRIVATE_KEY)
        self._client = wallet.make_client(cfg.RPC_ENDPOINT)
        logger.info("LiveBroker ready — wallet %s", self._keypair.pubkey())

    def wallet_pubkey(self) -> str:
        return str(self._keypair.pubkey())

    def sol_balance(self) -> float:
        return self._wallet.get_sol_balance(self._client, self._keypair.pubkey())

    def buy(self, snap: TokenSnapshot, sol_amount: float) -> Optional[Fill]:
        j, w = self._jupiter, self._wallet
        lamports = j.sol_to_lamports(sol_amount)
        quote = j.get_quote(cfg.SOL_MINT, snap.mint, lamports, cfg.SLIPPAGE_BPS)
        if not quote:
            logger.warning("No buy quote for %s", snap.symbol)
            return None
        tx = j.get_swap_transaction(quote, self.wallet_pubkey())
        if not tx:
            return None
        sig = w.sign_and_send(self._client, tx, self._keypair)
        if not sig or not w.confirm_transaction(self._client, sig):
            logger.warning("Buy tx for %s failed to confirm", snap.symbol)
            return None
        decimals = 6
        out_ui = int(quote.get("outAmount", 0)) / (10 ** decimals)
        return Fill(price_usd=snap.price_usd, amount=out_ui, sol_delta=sol_amount, tx=sig)

    def sell(self, pos: Position, price: float) -> Optional[Fill]:
        j, w = self._jupiter, self._wallet
        raw = j.token_units(pos.amount, 6)
        if raw <= 0:
            return None
        quote = j.get_quote(pos.mint, cfg.SOL_MINT, raw, cfg.SLIPPAGE_BPS)
        if not quote:
            logger.warning("No sell quote for %s", pos.symbol)
            return None
        tx = j.get_swap_transaction(quote, self.wallet_pubkey())
        if not tx:
            return None
        sig = w.sign_and_send(self._client, tx, self._keypair)
        if not sig or not w.confirm_transaction(self._client, sig):
            logger.warning("Sell tx for %s failed to confirm", pos.symbol)
            return None
        sol_out = j.lamports_to_sol(int(quote.get("outAmount", 0)))
        return Fill(price_usd=price, amount=pos.amount, sol_delta=sol_out, tx=sig)


def make_broker():
    if cfg.LIVE:
        return LiveBroker()
    return PaperBroker()
