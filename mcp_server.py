#!/usr/bin/env python3
"""
Moonshot Solana Trader — MCP Server

Exposes Solana trading tools to Claude Code so Claude can scan, analyze,
buy, and sell tokens directly via Jupiter aggregator and DexScreener.

Configuration in ~/.claude/settings.json (or .claude/settings.json):
  {
    "mcpServers": {
      "solana-trader": {
        "command": "python",
        "args": ["mcp_server.py"],
        "cwd": "/path/to/tradeforge-v2",
        "env": {
          "SOLANA_PRIVATE_KEY": "your_key_here"
        }
      }
    }
  }
"""

import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

import moonshot_trader.config as cfg
from moonshot_trader import jupiter, market, wallet
from moonshot_trader.bonding_curve import buy_on_curve, is_on_bonding_curve, sell_on_curve
from moonshot_trader.moonshot_api import get_new_launches
from moonshot_trader.positions import Position, PositionBook
from moonshot_trader.reddit_scanner import scan_reddit_mentions as _scan_reddit
from moonshot_trader.strategy import check_entry, check_exit

mcp = FastMCP("Moonshot Solana Trader")

# Lazy-initialized globals (loaded on first tool call)
_keypair = None
_client = None
_book = None


def _init():
    global _keypair, _client, _book
    if _keypair is None:
        if not cfg.PRIVATE_KEY:
            raise RuntimeError("SOLANA_PRIVATE_KEY not set. Add it to .env or the MCP server env config.")
        _keypair = wallet.load_keypair(cfg.PRIVATE_KEY)
        _client = wallet.make_client(cfg.RPC_ENDPOINT)
        _book = PositionBook()


# ---------------------------------------------------------------------------
# Wallet / portfolio tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_wallet_status() -> dict:
    """
    Get the current Solana wallet balance and all token holdings with USD values.
    Call this to understand available capital before making trade decisions.
    """
    _init()
    pubkey = _keypair.pubkey()
    sol_balance = wallet.get_sol_balance(_client, pubkey)
    token_balances = wallet.get_token_balances(_client, pubkey)

    tokens = []
    for mint, info in token_balances.items():
        pair = market.get_pair_data(mint)
        price_usd = float(pair.get("priceUsd") or 0) if pair else 0
        symbol = pair.get("baseToken", {}).get("symbol", mint[:8]) if pair else mint[:8]
        usd_value = price_usd * info["ui_amount"]
        h1_pct = float(pair.get("priceChange", {}).get("h1") or 0) if pair else 0
        tokens.append({
            "mint": mint,
            "symbol": symbol,
            "amount": info["ui_amount"],
            "decimals": info["decimals"],
            "price_usd": price_usd,
            "value_usd": usd_value,
            "change_1h_pct": h1_pct,
        })

    tokens.sort(key=lambda t: t["value_usd"], reverse=True)

    return {
        "wallet": str(pubkey),
        "sol_balance": sol_balance,
        "sol_available_for_trades": max(0.0, sol_balance - cfg.MIN_SOL_RESERVE),
        "tokens": tokens,
        "total_token_value_usd": sum(t["value_usd"] for t in tokens),
    }


@mcp.tool()
def get_open_positions() -> dict:
    """
    Get all positions the bot is currently tracking (entered via buy_token).
    Shows entry price, current price, and unrealized P&L for each.
    """
    _init()
    positions = _book.all()
    result = []
    for p in positions:
        cur_price = market.get_price_usd(p.token_address) or p.entry_price_usd
        pct = (cur_price - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd else 0
        age_minutes = (time.time() - p.entry_time) / 60
        result.append({
            "symbol": p.token_symbol,
            "token_address": p.token_address,
            "entry_price_usd": p.entry_price_usd,
            "current_price_usd": cur_price,
            "pnl_pct": round(pct, 2),
            "sol_spent": p.entry_sol_spent,
            "token_amount": p.token_amount,
            "age_minutes": round(age_minutes, 1),
            "entry_tx": p.entry_tx,
        })
    return {"positions": result, "count": len(result)}


# ---------------------------------------------------------------------------
# Market data tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_token_info(token_address: str) -> dict:
    """
    Get detailed price, volume, liquidity, and market cap data for a specific
    Solana token from DexScreener. Use this to research a token before trading.
    """
    pair = market.get_pair_data(token_address)
    if not pair:
        return {"error": f"No trading pair found for {token_address}"}

    return {
        "symbol": pair.get("baseToken", {}).get("symbol"),
        "name": pair.get("baseToken", {}).get("name"),
        "address": token_address,
        "price_usd": pair.get("priceUsd"),
        "price_change": {
            "m5_pct": pair.get("priceChange", {}).get("m5"),
            "h1_pct": pair.get("priceChange", {}).get("h1"),
            "h6_pct": pair.get("priceChange", {}).get("h6"),
            "h24_pct": pair.get("priceChange", {}).get("h24"),
        },
        "volume": {
            "m5_usd": pair.get("volume", {}).get("m5"),
            "h1_usd": pair.get("volume", {}).get("h1"),
            "h24_usd": pair.get("volume", {}).get("h24"),
        },
        "transactions_5m": pair.get("txns", {}).get("m5"),
        "liquidity_usd": pair.get("liquidity", {}).get("usd"),
        "market_cap_usd": pair.get("marketCap"),
        "pair_address": pair.get("pairAddress"),
        "dex": pair.get("dexId"),
        "url": f"https://dexscreener.com/solana/{pair.get('pairAddress', token_address)}",
    }


@mcp.tool()
def scan_new_launches(limit: int = 20) -> dict:
    """
    Scan moonshot.money for the most recently launched Solana tokens.
    These are pre-graduation or newly graduated tokens — highest risk, highest reward.
    Returns token data enriched with DexScreener price/volume/liquidity info.
    Call this to find brand-new opportunities before they appear on DexScreener scans.
    """
    tokens = get_new_launches(limit=limit)
    results = []
    for t in tokens:
        age_hours = None
        created = t.get("pairCreatedAt", 0)
        if created:
            age_hours = round((time.time() * 1000 - created) / 3_600_000, 2)

        results.append({
            "mint": t.get("mint", ""),
            "symbol": t.get("symbol", "?"),
            "name": t.get("name", ""),
            "price_usd": t.get("priceUsd", 0),
            "price_change_1h_pct": float((t.get("priceChange") or {}).get("h1") or 0),
            "price_change_5m_pct": float((t.get("priceChange") or {}).get("m5") or 0),
            "volume_1h_usd": float((t.get("volume") or {}).get("h1") or 0),
            "liquidity_usd": float((t.get("liquidity") or {}).get("usd") or 0),
            "market_cap_usd": t.get("marketCap", 0),
            "age_hours": age_hours,
            "dex": t.get("dex", "moonshot"),
            "pair_address": t.get("pairAddress", ""),
        })

    return {
        "launches": results,
        "count": len(results),
        "note": "Sorted newest first. age_hours=None means launch time unknown.",
    }


@mcp.tool()
def scan_reddit_buzz(max_per_subreddit: int = 15) -> dict:
    """
    Scan Reddit (r/CryptoMoonShots, r/SolanaTrading, r/memecoin, r/solana) for
    trending Solana token mentions. Extracts mint addresses and $TICKER symbols,
    resolves them via DexScreener. High Reddit score = strong social momentum.
    Use this to catch community-driven pumps early.
    """
    tokens = _scan_reddit(max_per_sub=max_per_subreddit)
    results = []
    for t in tokens:
        results.append({
            "mint": t.get("mint", ""),
            "symbol": t.get("baseToken", {}).get("symbol") or t.get("symbol", "?"),
            "price_usd": float(t.get("priceUsd") or 0),
            "price_change_1h_pct": float((t.get("priceChange") or {}).get("h1") or 0),
            "price_change_5m_pct": float((t.get("priceChange") or {}).get("m5") or 0),
            "volume_1h_usd": float((t.get("volume") or {}).get("h1") or 0),
            "liquidity_usd": float((t.get("liquidity") or {}).get("usd") or 0),
            "reddit_subreddit": t.get("reddit_subreddit", ""),
            "reddit_score": t.get("reddit_score", 0),
            "reddit_title": t.get("reddit_title", "")[:120],
        })

    results.sort(key=lambda r: r["reddit_score"], reverse=True)
    return {"tokens": results, "count": len(results)}


@mcp.tool()
def scan_volatile_tokens(
    min_momentum_5m_pct: float = 4.0,
    min_volume_h1_usd: float = 8000,
    min_market_cap_usd: float = 75000,
    max_market_cap_usd: float = 5000000,
    limit: int = 10,
) -> dict:
    """
    Scan DexScreener for Solana tokens showing momentum pop patterns right now.
    Returns candidates sorted by 5-min price momentum, with full signal analysis.
    Use this to find trading opportunities. Adjust thresholds to widen/narrow results.
    """
    candidates = market.scan_volatile_solana_tokens()

    results = []
    for pair in candidates:
        addr = pair.get("baseToken", {}).get("address", "")
        if not addr:
            continue

        mcap = float(pair.get("marketCap") or 0)
        vol_h1 = float(pair.get("volume", {}).get("h1") or 0)
        m5_pct = float(pair.get("priceChange", {}).get("m5") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        h1_pct = float(pair.get("priceChange", {}).get("h1") or 0)
        vol_m5 = float(pair.get("volume", {}).get("m5") or 0)
        avg_5min_vol = vol_h1 / 12 if vol_h1 > 0 else 0
        vol_accel = vol_m5 / avg_5min_vol if avg_5min_vol > 0 else 0

        txns = pair.get("txns", {}).get("m5", {})
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        buy_sell_ratio = buys / max(sells, 1)

        if (m5_pct < min_momentum_5m_pct
                or vol_h1 < min_volume_h1_usd
                or mcap < min_market_cap_usd
                or mcap > max_market_cap_usd
                or liquidity < cfg.ENTRY_MIN_LIQUIDITY):
            continue

        # Full strategy signal check
        signal = check_entry(pair)

        results.append({
            "symbol": pair.get("baseToken", {}).get("symbol"),
            "address": addr,
            "price_usd": pair.get("priceUsd"),
            "momentum_5m_pct": round(m5_pct, 2),
            "momentum_h1_pct": round(h1_pct, 2),
            "volume_h1_usd": round(vol_h1, 0),
            "volume_acceleration": round(vol_accel, 2),
            "buy_sell_ratio_5m": round(buy_sell_ratio, 2),
            "liquidity_usd": round(liquidity, 0),
            "market_cap_usd": round(mcap, 0),
            "strong_signal": signal is not None,
            "signal_reason": signal.reason if signal else None,
            "dexscreener_url": f"https://dexscreener.com/solana/{pair.get('pairAddress', addr)}",
        })

    results.sort(key=lambda r: r["momentum_5m_pct"], reverse=True)
    return {
        "candidates": results[:limit],
        "total_found": len(results),
        "scanned": len(candidates),
        "strong_signals": sum(1 for r in results if r["strong_signal"]),
    }


# ---------------------------------------------------------------------------
# Trading tools
# ---------------------------------------------------------------------------

@mcp.tool()
def buy_token(
    token_address: str,
    sol_amount: float,
    token_symbol: str = "",
) -> dict:
    """
    Buy a Solana token using SOL via Jupiter aggregator.
    Executes a real on-chain transaction and records the position.

    Args:
        token_address: The Solana mint address of the token to buy
        sol_amount: Amount of SOL to spend (e.g. 0.05 = ~$7.50 at typical prices)
        token_symbol: Optional symbol for logging (will be looked up if empty)

    Returns transaction signature and position details on success.
    """
    _init()
    pubkey = _keypair.pubkey()

    sol_balance = wallet.get_sol_balance(_client, pubkey)
    if sol_amount > sol_balance - cfg.MIN_SOL_RESERVE:
        return {
            "success": False,
            "error": f"Insufficient SOL. Balance: {sol_balance:.4f}, requested: {sol_amount:.4f}, reserve: {cfg.MIN_SOL_RESERVE}",
        }

    # Look up token info if symbol not provided
    if not token_symbol:
        pair = market.get_pair_data(token_address)
        if pair:
            token_symbol = pair.get("baseToken", {}).get("symbol", token_address[:8])
            decimals = 6  # default; DexScreener doesn't always expose decimals
        else:
            token_symbol = token_address[:8]
            decimals = 6
    else:
        pair = market.get_pair_data(token_address)
        decimals = 6

    price_usd = float(pair.get("priceUsd") or 0) if pair else 0
    pair_address = pair.get("pairAddress", "") if pair else ""
    sig = None
    out_ui = 0.0
    route = "jupiter"

    # Try bonding curve first — earliest possible entry for new moonshot launches
    on_curve = is_on_bonding_curve(token_address, cfg.RPC_ENDPOINT)
    if on_curve:
        sig = buy_on_curve(_keypair, cfg.RPC_ENDPOINT, token_address, sol_amount, cfg.SLIPPAGE_BPS)
        if sig:
            route = "bonding_curve"
            decimals = 9  # moonshot tokens use 9 decimals on curve
            out_ui = (sol_amount * 1e9 / price_usd) if price_usd > 0 else 0

    if not sig:
        # Graduated token or curve failed — use Jupiter
        amount_lamports = jupiter.sol_to_lamports(sol_amount)
        quote = jupiter.get_quote(
            input_mint=cfg.SOL_MINT,
            output_mint=token_address,
            amount_lamports=amount_lamports,
            slippage_bps=cfg.SLIPPAGE_BPS,
        )
        if not quote:
            return {"success": False, "error": "Could not get Jupiter quote — token may not be tradeable yet"}

        out_raw = int(quote.get("outAmount", 0))
        out_ui = out_raw / (10 ** decimals)

        swap_tx = jupiter.get_swap_transaction(quote, str(pubkey))
        if not swap_tx:
            return {"success": False, "error": "Failed to build swap transaction"}

        sig = wallet.sign_and_send(_client, swap_tx, _keypair)
        if not sig:
            return {"success": False, "error": "Transaction failed to send"}

        confirmed = wallet.confirm_transaction(_client, sig)
        if not confirmed:
            return {
                "success": False,
                "error": "Transaction sent but not confirmed within timeout",
                "tx_signature": sig,
            }

    pos = Position(
        token_address=token_address,
        token_symbol=token_symbol,
        entry_price_usd=price_usd,
        entry_sol_spent=sol_amount,
        token_amount=out_ui,
        token_decimals=decimals,
        entry_time=time.time(),
        entry_tx=sig,
        pair_address=pair_address,
    )
    _book.add(pos)

    return {
        "success": True,
        "symbol": token_symbol,
        "route": route,
        "sol_spent": sol_amount,
        "tokens_received": round(out_ui, 4),
        "entry_price_usd": price_usd,
        "tx_signature": sig,
        "explorer_url": f"https://solscan.io/tx/{sig}",
    }


@mcp.tool()
def sell_token(
    token_address: str,
    sell_percent: float = 100.0,
) -> dict:
    """
    Sell a token back to SOL via Jupiter aggregator.
    Works on both bot-tracked positions and any token in the wallet.

    Args:
        token_address: The Solana mint address of the token to sell
        sell_percent: What percentage of holdings to sell (default 100 = full exit)

    Returns SOL received and transaction details.
    """
    _init()
    pubkey = _keypair.pubkey()

    # Get current token balance from wallet
    token_bals = wallet.get_token_balances(_client, pubkey)
    info = token_bals.get(token_address)
    if not info or info["ui_amount"] <= 0:
        return {"success": False, "error": f"No balance found for token {token_address}"}

    decimals = info["decimals"]
    sell_amount_ui = info["ui_amount"] * (sell_percent / 100.0)
    amount_raw = jupiter.token_units(sell_amount_ui, decimals)

    if amount_raw <= 0:
        return {"success": False, "error": "Sell amount too small"}

    quote = jupiter.get_quote(
        input_mint=token_address,
        output_mint=cfg.SOL_MINT,
        amount_lamports=amount_raw,
        slippage_bps=cfg.SLIPPAGE_BPS,
    )
    if not quote:
        return {"success": False, "error": "Could not get sell quote — may be no liquidity"}

    out_lamports = int(quote.get("outAmount", 0))
    out_sol = jupiter.lamports_to_sol(out_lamports)
    price_impact_pct = float(quote.get("priceImpactPct") or 0)

    swap_tx = jupiter.get_swap_transaction(quote, str(pubkey))
    if not swap_tx:
        return {"success": False, "error": "Failed to build sell transaction"}

    sig = wallet.sign_and_send(_client, swap_tx, _keypair)
    if not sig:
        return {"success": False, "error": "Sell transaction failed to send"}

    confirmed = wallet.confirm_transaction(_client, sig)
    if not confirmed:
        return {
            "success": False,
            "error": "Sell sent but not confirmed within timeout",
            "tx_signature": sig,
        }

    # Look up symbol and P&L if we have a tracked position
    symbol = token_address[:8]
    pnl_summary = None
    pos = _book.get(token_address)
    if pos:
        symbol = pos.token_symbol
        cur_price = market.get_price_usd(token_address) or pos.entry_price_usd
        pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100 if pos.entry_price_usd else 0
        pnl_summary = {
            "entry_price": pos.entry_price_usd,
            "exit_price": cur_price,
            "pnl_pct": round(pct, 2),
            "sol_spent": pos.entry_sol_spent,
            "sol_received": out_sol,
            "sol_pnl": round(out_sol - pos.entry_sol_spent, 4),
        }
        if sell_percent >= 99:
            _book.remove(token_address)

    return {
        "success": True,
        "symbol": symbol,
        "tokens_sold": sell_amount_ui,
        "sol_received": out_sol,
        "price_impact_pct": price_impact_pct,
        "pnl": pnl_summary,
        "tx_signature": sig,
        "explorer_url": f"https://solscan.io/tx/{sig}",
    }


@mcp.tool()
def check_exit_signals() -> dict:
    """
    Check all open bot positions for take-profit, stop-loss, or time-based exit signals.
    Returns which positions should be closed and why. Does NOT execute — lets Claude decide.
    """
    _init()
    positions = _book.all()
    if not positions:
        return {"signals": [], "message": "No open positions to check"}

    signals = []
    for p in positions:
        cur_price = market.get_price_usd(p.token_address)
        if not cur_price:
            signals.append({
                "symbol": p.token_symbol,
                "address": p.token_address,
                "action": "UNKNOWN",
                "reason": "Could not fetch current price",
            })
            continue

        pct = (cur_price - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd else 0
        exit_reason = check_exit(p.entry_price_usd, cur_price, p.entry_time)
        age_minutes = (time.time() - p.entry_time) / 60

        signals.append({
            "symbol": p.token_symbol,
            "address": p.token_address,
            "pnl_pct": round(pct, 2),
            "age_minutes": round(age_minutes, 1),
            "entry_price": p.entry_price_usd,
            "current_price": cur_price,
            "should_exit": exit_reason is not None,
            "exit_reason": exit_reason,
        })

    return {
        "signals": signals,
        "exits_recommended": sum(1 for s in signals if s["should_exit"]),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
