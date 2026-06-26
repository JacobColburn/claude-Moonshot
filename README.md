# 🌙 Moonshot Engine

A fast, multi-signal Solana crypto trading system with a real-time dashboard.

It scores the whole memecoin market every few seconds, gates the best candidates
through a **rug-safety check** and a **Claude Opus 4.8 conviction review**, sizes
positions by conviction, and manages exits with **trailing stops** that let
winners run — all streamed live to a gorgeous browser dashboard.

It **paper-trades by default** (virtual balance, simulated fills) so you can
watch the equity curve prove itself before risking a single lamport. Flip one
flag to trade your real Solana wallet.

---

## Quick start

```bash
pip install -r requirements.txt          # paper mode needs only the core deps
cp .env.example .env                      # optional: add ANTHROPIC_API_KEY
python run.py
```

Open **http://localhost:8000**.

That's it — the engine starts scanning immediately and the dashboard updates in
real time over a WebSocket (no refresh, sub-second).

---

## How it decides (the strategy)

**Safe Momentum Breakout with trailing exits.** Each cycle:

1. **Scan** — pull trending/volatile Solana pairs from DexScreener.
2. **Score** — every token gets six normalized 0-100 sub-signals, blended into a
   composite conviction score:
   | signal | what it measures |
   |---|---|
   | **momentum** | fresh 5-min pop confirmed by the 1-hour trend (over-extension penalized) |
   | **volume** | 5-min volume vs the hourly baseline — is activity heating up *now*? |
   | **pressure** | buy/sell ratio over 5-min and 1-hour windows |
   | **liquidity** | healthy-range band — deep enough to exit, not too deep to move |
   | **turnover** | hourly volume ÷ market cap |
   | **freshness** | newer = more upside, with a floor for rug-prone first minutes |

   A US-market-hours multiplier and hard sanity floors (min liquidity / volume /
   market-cap) are applied on top.
3. **Safety gate** — finalists are checked against [RugCheck](https://rugcheck.xyz):
   live mint/freeze authority, unlocked liquidity, or extreme holder
   concentration get the token **rejected**. This is the piece the old bot lacked.
4. **Brain review** — the top finalists go to **Claude Opus 4.8**, which returns a
   conviction (0-1) per candidate. Low conviction vetoes the trade. (No API key →
   pure-quant mode, every finalist passes.)
5. **Size & buy** — stake scales from min→max as conviction rises above the entry
   threshold.
6. **Manage** — open positions are re-priced continuously and exited on:
   - **HARD_STOP** — fast downside cap
   - **TRAILING_STOP** — once up ~22%, ride the move and exit ~14% off the peak
     (this is what lets a moonshot run to +200% instead of capping at +40%)
   - **LIQUIDITY_DRAIN** — bail if the pool collapses (rug in progress)
   - **MOMENTUM_FADE** — bail on a sharp 1-hour reversal
   - **TIME_STOP** — recycle capital out of stale bags

Everything is tunable via `.env` — see the comments there.

---

## Paper → Live

```bash
# .env
LIVE=true
SOLANA_PRIVATE_KEY=<your base58 key>
SOLANA_RPC=<a private RPC endpoint>
```

```bash
pip install -r requirements.txt   # installs the Solana stack for real swaps
python run.py
```

Live mode executes real swaps through [Jupiter](https://jup.ag) using the same
signals, sizing, and exits you watched succeed in paper mode. **Start small.**

---

## Dashboard

- **Equity curve** — live, color-shifts green/red with your PnL
- **Stat cards** — equity, realized PnL, win rate, profit factor, cash vs deployed
- **Open positions** — per-bag PnL, peak gain, age, conviction
- **Signal leaderboard** — every candidate ranked, with a per-signal heatmap
- **Live feed** — buys, sells, vetoes, and safety blocks as they happen

---

## Architecture

```
engine/
  datafeed   market data (DexScreener)        broker     paper | live (Jupiter)
  signals    6 normalized sub-signals          portfolio  balances, PnL, equity curve
  scoring    composite score + sizing          risk       trailing/dynamic exits
  safety     RugCheck rug gate                 events     pub/sub bus -> dashboard
  brain      Claude Opus 4.8 conviction        engine     async orchestration loop
server.py    FastAPI + WebSocket               web/       real-time dashboard
run.py       entrypoint
```

State persists to `state/` so restarts resume cleanly. Closed trades are logged
to `state/trades.json`.

> ⚠️ Memecoin trading is extremely high risk. This software is for research and
> education. Paper-trade first; never risk more than you can afford to lose.
