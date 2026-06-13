#!/usr/bin/env python3
"""
Moonshot Trader Dashboard
--------------------------
Real-time view of your Solana portfolio, open positions, and trade history.

Run:
  streamlit run dashboard.py

Reads from the same positions.json and .env as the trader/monitor.
No API keys needed beyond what's already in .env.
"""

import json
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import moonshot_trader.config as cfg
from moonshot_trader import market, wallet
from moonshot_trader.positions import PositionBook

# ── Page config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Moonshot Trader",
    page_icon="🌙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Styling ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .metric-card {
    background: #1a1a2e;
    border: 1px solid #16213e;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 12px;
  }
  .profit { color: #00ff88; font-weight: 700; }
  .loss   { color: #ff4d6d; font-weight: 700; }
  .neutral { color: #aaaaaa; }
  .tag-curve { background:#7c3aed; color:white; padding:2px 8px; border-radius:6px; font-size:11px; }
  .tag-jup   { background:#0ea5e9; color:white; padding:2px 8px; border-radius:6px; font-size:11px; }
  div[data-testid="stMetricValue"] { font-size: 1.6rem !important; }
  header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

REFRESH_SECONDS = 30
DEXSCREENER_EMBED = "https://dexscreener.com/solana/{pair}?embed=1&theme=dark&trades=1&info=0"
SOLSCAN_TX = "https://solscan.io/tx/{sig}"
SOLSCAN_TOKEN = "https://solscan.io/token/{mint}"


# ── Cached data fetchers (cache busts on each refresh) ─────────────────────

@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_wallet():
    if not cfg.PRIVATE_KEY:
        return None, None, {}
    try:
        kp = wallet.load_keypair(cfg.PRIVATE_KEY)
        client = wallet.make_client(cfg.RPC_ENDPOINT)
        sol = wallet.get_sol_balance(client, kp.pubkey())
        tokens = wallet.get_token_balances(client, kp.pubkey())
        return kp, client, {"sol": sol, "tokens": tokens, "pubkey": str(kp.pubkey())}
    except Exception as e:
        return None, None, {"error": str(e)}


@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_positions_with_prices():
    book = PositionBook()
    positions = book.all()
    result = []
    for p in positions:
        price = market.get_price_usd(p.token_address) or p.entry_price_usd
        pct = (price - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd else 0
        age_min = (time.time() - p.entry_time) / 60
        pair = market.get_pair_data(p.token_address)
        pair_addr = pair.get("pairAddress", p.pair_address) if pair else p.pair_address
        h1 = float((pair.get("priceChange") or {}).get("h1") or 0) if pair else 0
        liq = float((pair.get("liquidity") or {}).get("usd") or 0) if pair else 0
        result.append({
            "symbol": p.token_symbol,
            "mint": p.token_address,
            "pair_address": pair_addr,
            "entry_price": p.entry_price_usd,
            "current_price": price,
            "pnl_pct": round(pct, 2),
            "sol_spent": p.entry_sol_spent,
            "token_amount": p.token_amount,
            "age_min": round(age_min, 1),
            "entry_tx": p.entry_tx,
            "h1_pct": h1,
            "liquidity_usd": liq,
        })
    return result


@st.cache_data(ttl=60)
def fetch_wallet_tokens(token_mints: tuple):
    """Get prices for non-position wallet tokens."""
    results = []
    for mint in token_mints:
        pair = market.get_pair_data(mint)
        if pair:
            results.append({
                "mint": mint,
                "symbol": pair.get("baseToken", {}).get("symbol", mint[:8]),
                "price_usd": float(pair.get("priceUsd") or 0),
                "h1_pct": float((pair.get("priceChange") or {}).get("h1") or 0),
                "h24_pct": float((pair.get("priceChange") or {}).get("h24") or 0),
                "pair_address": pair.get("pairAddress", ""),
            })
    return results


def parse_log_trades(log_path: str = "agent.log", limit: int = 50) -> list[dict]:
    """Parse BUY/SELL lines from the agent log for trade history."""
    trades = []
    try:
        lines = Path(log_path).read_text().splitlines()
        for line in reversed(lines):
            if " BUY " in line or " SELL " in line or "Entered " in line or "Exited " in line:
                trades.append({"raw": line.strip()})
                if len(trades) >= limit:
                    break
    except FileNotFoundError:
        pass
    return trades


def pnl_color(pct: float) -> str:
    if pct > 0:
        return f'<span class="profit">+{pct:.2f}%</span>'
    elif pct < 0:
        return f'<span class="loss">{pct:.2f}%</span>'
    return f'<span class="neutral">{pct:.2f}%</span>'


# ── Header ─────────────────────────────────────────────────────────────────

st.markdown("## 🌙 Moonshot Trader")

kp, client, wallet_data = fetch_wallet()

if "error" in wallet_data:
    st.error(f"Wallet error: {wallet_data['error']}")
    st.stop()

if not cfg.PRIVATE_KEY:
    st.warning("SOLANA_PRIVATE_KEY not set in .env — showing demo layout")
    st.stop()

# ── Top metrics ────────────────────────────────────────────────────────────

positions = fetch_positions_with_prices()
sol_bal = wallet_data.get("sol", 0.0)

# Estimate SOL price for USD conversion
sol_pair = market.get_pair_data("So11111111111111111111111111111111111111112")
sol_price_usd = 150.0  # fallback
if sol_pair:
    sol_price_usd = float(sol_pair.get("priceUsd") or 150.0)

total_position_value_sol = sum(
    p["sol_spent"] * (1 + p["pnl_pct"] / 100) for p in positions
)
portfolio_sol = sol_bal + total_position_value_sol
portfolio_usd = portfolio_sol * sol_price_usd

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("SOL Balance", f"{sol_bal:.4f} SOL", f"${sol_bal * sol_price_usd:.2f}")
col2.metric("Open Positions", len(positions), f"Max: {cfg.MAX_POSITIONS}")
col3.metric("Portfolio Value", f"${portfolio_usd:.2f}", f"{portfolio_sol:.4f} SOL")
col4.metric("SOL Price", f"${sol_price_usd:.2f}")
col5.metric("Wallet", f"{wallet_data.get('pubkey','')[:8]}...")

st.divider()

# ── Open positions ─────────────────────────────────────────────────────────

st.markdown("### Open Positions")

if not positions:
    st.info("No open bot-managed positions. Use the MCP tools in Claude Code to enter trades.")
else:
    # Chart selector
    selected_sym = st.selectbox(
        "View chart for:",
        options=[p["symbol"] for p in positions],
        index=0,
    )
    selected = next((p for p in positions if p["symbol"] == selected_sym), None)

    chart_col, table_col = st.columns([3, 2])

    with chart_col:
        if selected and selected.get("pair_address"):
            embed_url = DEXSCREENER_EMBED.format(pair=selected["pair_address"])
            st.markdown(f'<iframe src="{embed_url}" width="100%" height="420" frameborder="0" allowfullscreen></iframe>',
                        unsafe_allow_html=True)
        else:
            st.info("No DexScreener pair data available for this token yet.")

    with table_col:
        st.markdown("**Position Details**")
        for p in positions:
            is_selected = p["symbol"] == selected_sym
            border = "2px solid #7c3aed" if is_selected else "1px solid #333"
            pnl_html = pnl_color(p["pnl_pct"])
            h1_html = pnl_color(p["h1_pct"])
            tx_link = SOLSCAN_TX.format(sig=p["entry_tx"]) if p["entry_tx"] else "#"
            token_link = SOLSCAN_TOKEN.format(mint=p["mint"])

            st.markdown(f"""
<div style="border:{border}; border-radius:10px; padding:12px; margin-bottom:10px; background:#111;">
  <b><a href="{token_link}" target="_blank" style="color:white;text-decoration:none;">{p['symbol']}</a></b>
  &nbsp; P&L: {pnl_html} &nbsp; 1h: {h1_html}<br>
  <small>
    Entry: <code>${p['entry_price']:.6f}</code> &nbsp;
    Now: <code>${p['current_price']:.6f}</code><br>
    SOL in: <code>{p['sol_spent']:.4f}</code> &nbsp;
    Age: <code>{p['age_min']:.0f}min</code><br>
    Liq: <code>${p['liquidity_usd']:,.0f}</code> &nbsp;
    <a href="{tx_link}" target="_blank" style="color:#7c3aed;">Entry tx ↗</a>
  </small>
</div>
""", unsafe_allow_html=True)

st.divider()

# ── Wallet holdings (non-bot tokens) ───────────────────────────────────────

st.markdown("### Wallet Holdings")

position_mints = {p["mint"] for p in positions}
wallet_token_mints = tuple(
    mint for mint in wallet_data.get("tokens", {}).keys()
    if mint != cfg.SOL_MINT and mint not in position_mints
)

if wallet_token_mints:
    wallet_tokens = fetch_wallet_tokens(wallet_token_mints)
    if wallet_tokens:
        token_cols = st.columns(min(len(wallet_tokens), 4))
        for i, tok in enumerate(wallet_tokens[:8]):
            col = token_cols[i % len(token_cols)]
            amount = wallet_data["tokens"].get(tok["mint"], {}).get("ui_amount", 0)
            usd_val = amount * tok["price_usd"]
            h1_str = f"+{tok['h1_pct']:.1f}%" if tok["h1_pct"] >= 0 else f"{tok['h1_pct']:.1f}%"
            link = SOLSCAN_TOKEN.format(mint=tok["mint"])
            col.markdown(f"""
<div class="metric-card">
  <b><a href="{link}" target="_blank" style="color:white;">{tok['symbol']}</a></b><br>
  <span style="font-size:1.1rem;">${usd_val:.2f}</span><br>
  <small class="neutral">{amount:.2f} tokens · 1h: {h1_str}</small>
</div>
""", unsafe_allow_html=True)
    else:
        st.info("Fetching token prices...")
else:
    st.info("No other SPL tokens in wallet.")

st.divider()

# ── Trade log ──────────────────────────────────────────────────────────────

st.markdown("### Recent Activity")

log_col1, log_col2 = st.columns(2)

with log_col1:
    st.markdown("**Agent Log** (agent.log)")
    trades = parse_log_trades("agent.log", limit=30)
    if trades:
        log_text = "\n".join(t["raw"] for t in trades)
        st.code(log_text, language=None)
    else:
        st.info("No agent.log yet — start the MCP server and run a cycle.")

with log_col2:
    st.markdown("**Monitor Log** (monitor.log)")
    monitor_trades = parse_log_trades("monitor.log", limit=30)
    if monitor_trades:
        log_text = "\n".join(t["raw"] for t in monitor_trades)
        st.code(log_text, language=None)
    else:
        st.info("No monitor.log yet — start monitor.py to protect positions.")

# ── Auto-refresh ───────────────────────────────────────────────────────────

st.divider()
refresh_col, status_col = st.columns([1, 4])
with refresh_col:
    if st.button("🔄 Refresh Now"):
        st.cache_data.clear()
        st.rerun()
with status_col:
    st.caption(f"Auto-refreshes every {REFRESH_SECONDS}s · Last update: {time.strftime('%H:%M:%S')}")

# Auto-refresh via meta tag
st.markdown(f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">', unsafe_allow_html=True)
