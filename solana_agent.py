#!/usr/bin/env python3
"""
Autonomous Solana Moonshot Agent
---------------------------------
Loops every 30 minutes. Each cycle:
  1. Scan moonshot.money for new launches
  2. Scan Reddit for trending Solana tokens
  3. Scan DexScreener for volatile momentum plays
  4. Ask Claude (claude-opus-4-8) to decide: BUY / SELL / HOLD
  5. Execute trades autonomously — no human approval

Usage:
  python solana_agent.py               # Live trading
  python solana_agent.py --dry-run     # Simulate without real transactions
  python solana_agent.py --once        # Run one cycle and exit (useful for cron)

Setup:
  cp .env.example .env
  # Edit .env with SOLANA_PRIVATE_KEY and ANTHROPIC_API_KEY
  pip install -r requirements.txt
"""

import argparse
import logging
import sys
import time

import moonshot_trader.config as cfg
from moonshot_trader import market, wallet
from moonshot_trader.claude_brain import build_snapshot, decide
from moonshot_trader.jupiter import get_quote, get_swap_transaction, sol_to_lamports, token_units, lamports_to_sol
from moonshot_trader.moonshot_api import get_new_launches
from moonshot_trader.positions import Position, PositionBook
from moonshot_trader.reddit_scanner import scan_reddit_mentions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log"),
    ],
)
logger = logging.getLogger(__name__)

LOOP_INTERVAL_SECONDS = 30 * 60  # 30 minutes
SOL_MINT = cfg.SOL_MINT


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def _execute_buy(
    keypair, client, book: PositionBook, dry_run: bool,
    token_address: str, token_symbol: str, price_usd: float,
    pair_address: str, decimals: int = 6,
) -> bool:
    sol_bal = wallet.get_sol_balance(client, keypair.pubkey())
    size_sol = min(cfg.MAX_POSITION_SOL, max(0.0, sol_bal - cfg.MIN_SOL_RESERVE))
    if size_sol < 0.005:
        logger.info("Insufficient SOL for BUY (%.4f available)", size_sol)
        return False

    amount_lamports = sol_to_lamports(size_sol)
    quote = get_quote(
        input_mint=SOL_MINT,
        output_mint=token_address,
        amount_lamports=amount_lamports,
        slippage_bps=cfg.SLIPPAGE_BPS,
    )
    if not quote:
        logger.warning("No quote for %s", token_symbol)
        return False

    out_raw = int(quote.get("outAmount", 0))
    out_ui = out_raw / (10 ** decimals)
    logger.info("BUY %s: %.4f SOL → %.4f tokens @ $%.6f", token_symbol, size_sol, out_ui, price_usd)

    if dry_run:
        logger.info("[DRY RUN] Skipping transaction")
        return False

    swap_tx = get_swap_transaction(quote, str(keypair.pubkey()))
    if not swap_tx:
        return False
    sig = wallet.sign_and_send(client, swap_tx, keypair)
    if not sig:
        return False
    if not wallet.confirm_transaction(client, sig):
        logger.warning("BUY tx for %s did not confirm", token_symbol)
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
    book.add(pos)
    logger.info("Entered %s at $%.6f  tx=%s", token_symbol, price_usd, sig)
    return True


def _execute_sell(
    keypair, client, book: PositionBook, dry_run: bool,
    token_address: str, token_symbol: str,
    token_amount: float, decimals: int, reason: str,
) -> bool:
    amount_raw = token_units(token_amount, decimals)
    if amount_raw <= 0:
        return False

    quote = get_quote(
        input_mint=token_address,
        output_mint=SOL_MINT,
        amount_lamports=amount_raw,
        slippage_bps=cfg.SLIPPAGE_BPS,
    )
    if not quote:
        logger.warning("No sell quote for %s — keeping position", token_symbol)
        return False

    out_sol = lamports_to_sol(int(quote.get("outAmount", 0)))
    logger.info("SELL %s: %.4f tokens → %.4f SOL  [%s]", token_symbol, token_amount, out_sol, reason)

    if dry_run:
        logger.info("[DRY RUN] Skipping transaction")
        return False

    swap_tx = get_swap_transaction(quote, str(keypair.pubkey()))
    if not swap_tx:
        return False
    sig = wallet.sign_and_send(client, swap_tx, keypair)
    if not sig:
        return False
    if not wallet.confirm_transaction(client, sig):
        logger.warning("SELL tx for %s did not confirm", token_symbol)
        return False

    book.remove(token_address)
    logger.info("Exited %s  [%s]  tx=%s", token_symbol, reason, sig)
    return True


# ---------------------------------------------------------------------------
# One trading cycle
# ---------------------------------------------------------------------------

def run_cycle(keypair, client, book: PositionBook, dry_run: bool) -> None:
    pubkey = keypair.pubkey()
    logger.info("─" * 60)
    logger.info("CYCLE START")

    # Wallet state
    sol_bal = wallet.get_sol_balance(client, pubkey)
    token_bals = wallet.get_token_balances(client, pubkey)
    logger.info("SOL: %.4f | Tokens: %d | Open positions: %d",
                sol_bal, len(token_bals), book.count())

    # Build open position summaries for Claude
    open_positions_summary = []
    for pos in book.all():
        cur_price = market.get_price_usd(pos.token_address) or pos.entry_price_usd
        pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100 if pos.entry_price_usd else 0
        age_min = (time.time() - pos.entry_time) / 60
        open_positions_summary.append({
            "token_address": pos.token_address,
            "symbol": pos.token_symbol,
            "entry_price_usd": pos.entry_price_usd,
            "current_price_usd": cur_price,
            "pct_change": round(pct, 2),
            "age_minutes": round(age_min, 1),
            "token_amount": pos.token_amount,
            "decimals": pos.token_decimals,
        })

    # Existing wallet holdings (not bot-managed)
    token_holdings_summary = []
    for mint, info in token_bals.items():
        if book.has(mint):
            continue
        pair = market.get_pair_data(mint)
        if pair:
            price = float(pair.get("priceUsd") or 0)
            h1 = float((pair.get("priceChange") or {}).get("h1") or 0)
            token_holdings_summary.append({
                "token_address": mint,
                "symbol": pair.get("baseToken", {}).get("symbol", mint[:8]),
                "amount": info["ui_amount"],
                "price_usd": price,
                "price_change_1h": h1,
            })

    # Data gathering
    logger.info("Scanning moonshot.money new launches...")
    moonshot_tokens = get_new_launches(limit=25)

    logger.info("Scanning Reddit for mentions...")
    reddit_tokens = scan_reddit_mentions(max_per_sub=20)

    logger.info("Scanning DexScreener for volatile tokens...")
    dex_tokens = market.scan_volatile_solana_tokens()

    # Combine all candidates (deduplicate by mint)
    all_candidates: dict[str, dict] = {}
    for tok in dex_tokens:
        mint = tok.get("baseToken", {}).get("address", "")
        if mint:
            all_candidates[mint] = {**tok, "mint": mint, "source": "dexscreener"}
    for tok in moonshot_tokens:
        mint = tok.get("mint", "")
        if mint:
            all_candidates[mint] = tok
    for tok in reddit_tokens:
        mint = tok.get("mint", "")
        if mint and mint not in all_candidates:
            all_candidates[mint] = tok
        elif mint:
            # Merge reddit signal into existing entry
            all_candidates[mint].update({
                "reddit_score": tok.get("reddit_score", 0),
                "reddit_subreddit": tok.get("reddit_subreddit", ""),
                "reddit_title": tok.get("reddit_title", ""),
                "source": "reddit+" + all_candidates[mint].get("source", ""),
            })

    candidates = list(all_candidates.values())
    logger.info("Total candidates for Claude: %d", len(candidates))

    # Ask Claude to decide
    snapshot = build_snapshot(
        sol_balance=sol_bal,
        token_holdings=token_holdings_summary,
        open_positions=open_positions_summary,
        candidates=candidates,
    )
    result = decide(snapshot)
    actions = result.get("actions", [])
    logger.info("Claude summary: %s", result.get("summary", ""))

    if not actions:
        logger.info("Claude chose to hold — no trades this cycle")
        return

    # Execute Claude's decisions
    executed = 0
    for action in actions:
        act = action.get("action", "HOLD").upper()
        addr = action.get("token_address", "")
        sym = action.get("token_symbol", addr[:8])
        reason = action.get("reason", act)
        conf = action.get("confidence", 0)

        if act == "BUY":
            if book.count() >= cfg.MAX_POSITIONS:
                logger.info("Skip BUY %s — max positions reached", sym)
                continue
            if book.has(addr):
                logger.info("Skip BUY %s — already in position", sym)
                continue
            # Find pair data for price / decimals
            cand = all_candidates.get(addr, {})
            price = float(cand.get("priceUsd") or cand.get("price_usd") or 0)
            pair_addr = cand.get("pairAddress") or cand.get("pair_address", "")
            decimals = 6
            base = cand.get("baseToken", {})
            if "decimals" in base:
                decimals = int(base["decimals"])
            if price > 0:
                ok = _execute_buy(keypair, client, book, dry_run,
                                  addr, sym, price, pair_addr, decimals)
                if ok:
                    executed += 1

        elif act == "SELL":
            pos = book.get(addr)
            if pos:
                ok = _execute_sell(keypair, client, book, dry_run,
                                   addr, sym, pos.token_amount, pos.token_decimals, reason)
                if ok:
                    executed += 1
            else:
                # Might be a pre-existing wallet token
                holding = next((h for h in token_holdings_summary if h["token_address"] == addr), None)
                if holding:
                    bal = token_bals.get(addr, {})
                    amount = bal.get("ui_amount", 0)
                    dec = bal.get("decimals", 6)
                    if amount > 0:
                        ok = _execute_sell(keypair, client, book, dry_run,
                                           addr, sym, amount, dec, reason)
                        if ok:
                            executed += 1

    logger.info("Cycle complete — executed %d / %d actions", executed, len(actions))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Solana Moonshot Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without sending real transactions")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit (useful for cron / testing)")
    args = parser.parse_args()

    # Config checks
    if not cfg.PRIVATE_KEY:
        print("ERROR: SOLANA_PRIVATE_KEY not set in .env")
        sys.exit(1)
    if not __import__("os").getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    try:
        keypair = wallet.load_keypair(cfg.PRIVATE_KEY)
    except Exception as e:
        print(f"ERROR: Could not load wallet: {e}")
        sys.exit(1)

    rpc_client = wallet.make_client(cfg.RPC_ENDPOINT)
    book = PositionBook()

    logger.info("=" * 60)
    logger.info("Autonomous Solana Moonshot Agent")
    logger.info("Wallet : %s", keypair.pubkey())
    logger.info("Mode   : %s", "DRY RUN" if args.dry_run else "LIVE")
    logger.info("Cycle  : every %d minutes", LOOP_INTERVAL_SECONDS // 60)
    logger.info("=" * 60)

    if args.once:
        run_cycle(keypair, rpc_client, book, args.dry_run)
        return

    while True:
        try:
            run_cycle(keypair, rpc_client, book, args.dry_run)
        except KeyboardInterrupt:
            logger.info("Agent stopped by user.")
            break
        except Exception as e:
            logger.error("Cycle error: %s", e, exc_info=True)

        logger.info("Sleeping %d minutes until next cycle...", LOOP_INTERVAL_SECONDS // 60)
        try:
            time.sleep(LOOP_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Agent stopped by user.")
            break


if __name__ == "__main__":
    main()
