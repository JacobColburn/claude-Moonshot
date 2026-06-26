"""
The Moonshot Engine orchestration loop.

One async loop drives three cadences:

  • every POSITION_REFRESH_SECONDS — re-price open positions and run exits
  • every SCAN_INTERVAL_SECONDS     — scan, score, safety+brain gate, execute buys
  • every EQUITY_SAMPLE_SECONDS     — sample the equity curve

Everything it does is published to the EventBus so the dashboard reflects it live.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from engine import brain, config as cfg, datafeed, risk, safety, scoring
from engine.broker import make_broker
from engine.events import bus
from engine.portfolio import Portfolio, Position
from engine.signals import TokenSnapshot

logger = logging.getLogger("engine")


class MoonshotEngine:
    def __init__(self):
        self.portfolio = Portfolio(cfg.PAPER_START_SOL)
        self.broker = None                       # built on start()
        self.leaderboard: list[dict] = []
        self.brain_read: str = ""
        self.started_at = time.time()
        self.last_scan_at = 0.0
        self.scan_count = 0
        self._running = False
        self._t_positions = 0.0
        self._t_scan = 0.0
        self._t_equity = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        self._running = True
        try:
            self.broker = make_broker()
        except Exception as e:  # noqa: BLE001
            bus.log("error", f"Broker init failed, falling back to PAPER: {e}")
            cfg.LIVE = False
            self.broker = make_broker()

        mode = "LIVE" if self.broker.live else "PAPER"
        bus.log("info", f"Engine online — {mode} mode | brain={'on' if brain.available() else 'off'}")

        # If live, sync the real wallet balance into the portfolio cash view.
        if self.broker.live:
            try:
                self.portfolio.cash_sol = self.broker.sol_balance()
            except Exception:  # noqa: BLE001
                pass

        await self._publish_state()
        while self._running:
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                logger.exception("tick error")
                bus.log("error", f"tick error: {e}")
            await asyncio.sleep(1.0)

    def stop(self):
        self._running = False

    # ── the tick ─────────────────────────────────────────────────────────────
    async def _tick(self):
        now = time.time()

        if now - self._t_positions >= cfg.POSITION_REFRESH_SECONDS:
            self._t_positions = now
            await self._manage_positions()

        if now - self._t_scan >= cfg.SCAN_INTERVAL_SECONDS:
            self._t_scan = now
            await self._scan_and_trade()

        if now - self._t_equity >= cfg.EQUITY_SAMPLE_SECONDS:
            self._t_equity = now
            self.portfolio.sample_equity()
            await self._publish_state()

    # ── position management ──────────────────────────────────────────────────
    async def _manage_positions(self):
        if not self.portfolio.positions:
            return
        for mint in list(self.portfolio.positions.keys()):
            snap = await datafeed.refresh(mint)
            if snap:
                self.portfolio.mark(mint, snap.price_usd)
            pos = self.portfolio.positions.get(mint)
            if not pos:
                continue
            reason = risk.evaluate_exit(pos, snap)
            if reason:
                await self._sell(pos, snap.price_usd if snap else pos.last_price_usd, reason)
        self.portfolio.save()
        await self._publish_state()

    async def _sell(self, pos: Position, price: float, reason: str):
        fill = await asyncio.to_thread(self.broker.sell, pos, price)
        if not fill:
            bus.log("warn", f"Sell failed for {pos.symbol} ({reason}) — will retry")
            return
        trade = self.portfolio.close_position(pos.mint, fill.price_usd, fill.sol_delta, reason)
        if trade:
            sign = "🟢" if trade.pnl_sol >= 0 else "🔴"
            bus.log("trade", f"{sign} SELL {pos.symbol}  {trade.pnl_pct:+.1f}%  "
                             f"{trade.pnl_sol:+.4f} SOL  [{reason}]")
            bus.publish_event("sell", {**trade.to_dict()})

    # ── scan + buy ───────────────────────────────────────────────────────────
    async def _scan_and_trade(self):
        snaps = await datafeed.scan_candidates()
        if not snaps:
            bus.log("warn", "Scan returned no candidates")
            return
        ranked = scoring.rank(snaps)
        self.leaderboard = [s.to_dict() for s in ranked[:25]]
        self.scan_count += 1
        self.last_scan_at = time.time()

        # Capacity / capital checks.
        held = set(self.portfolio.positions.keys())
        slots = cfg.MAX_POSITIONS - len(held)
        available = self.portfolio.cash_sol
        if slots <= 0 or available <= cfg.MIN_SOL_RESERVE + cfg.MIN_POSITION_SOL:
            await self._publish_state()
            return

        # Candidates that clear the entry score and aren't already held.
        finalists = [s for s in ranked
                     if s.score >= cfg.ENTRY_SCORE_THRESHOLD and s.mint not in held][:cfg.BRAIN_REVIEW_TOP_N]
        if not finalists:
            await self._publish_state()
            return

        # Safety gate (RugCheck) on finalists only.
        safe_finalists: list[TokenSnapshot] = []
        safety_map: dict[str, dict] = {}
        for s in finalists:
            rep = await asyncio.to_thread(safety.check, s.mint)
            safety_map[s.mint] = rep.to_dict()
            if rep.ok:
                safe_finalists.append(s)
            else:
                bus.log("info", f"⛔ {s.symbol} blocked by safety: {rep.reason}")
        if not safe_finalists:
            await self._publish_state()
            return

        # Claude conviction review.
        review_payload = [{
            "mint": s.mint, "symbol": s.symbol, "score": s.score,
            "signals": s.signals, "market": {
                "price_usd": s.price_usd, "liquidity_usd": s.liquidity_usd,
                "market_cap": s.market_cap, "age_minutes": round(s.age_minutes, 1),
                "change_m5": s.change_m5, "change_h1": s.change_h1,
                "vol_h1": s.vol_h1,
            },
            "safety": safety_map.get(s.mint, {}),
        } for s in safe_finalists]
        reviews = await asyncio.to_thread(brain.review, review_payload)
        self.brain_read = reviews.get("_read", "")

        # Execute buys in score order.
        for s in safe_finalists:
            if len(self.portfolio.positions) >= cfg.MAX_POSITIONS:
                break
            rv = reviews.get(s.mint, {"conviction": 1.0, "note": ""})
            conviction = rv.get("conviction", 1.0)
            if conviction < cfg.BRAIN_MIN_CONVICTION:
                bus.log("info", f"🧠 {s.symbol} vetoed by brain "
                                f"(conv {conviction:.2f}) {rv.get('note','')}")
                continue
            size = scoring.position_size_sol(
                s.score, self.portfolio.equity_sol(), self.portfolio.cash_sol, conviction)
            if size <= 0:
                continue
            await self._buy(s, size, conviction, rv.get("note", ""), safety_map.get(s.mint, {}))

        await self._publish_state()

    async def _buy(self, s: TokenSnapshot, size_sol: float, conviction: float,
                   note: str, safety_info: dict):
        fill = await asyncio.to_thread(self.broker.buy, s, size_sol)
        if not fill:
            bus.log("warn", f"Buy failed for {s.symbol}")
            return
        pos = Position(
            mint=s.mint, symbol=s.symbol,
            entry_price_usd=fill.price_usd, peak_price_usd=fill.price_usd,
            amount=fill.amount, sol_in=fill.sol_delta, entry_time=time.time(),
            score=s.score, conviction=conviction, pair_address=s.pair_address,
            entry_liquidity_usd=s.liquidity_usd, last_price_usd=fill.price_usd,
            entry_tx=fill.tx,
        )
        self.portfolio.open_position(pos)
        bus.log("trade", f"🚀 BUY {s.symbol}  {size_sol:.4f} SOL  "
                         f"score {s.score:.0f}  conv {conviction:.2f}  {note}")
        bus.publish_event("buy", {
            "symbol": s.symbol, "mint": s.mint, "sol": round(size_sol, 4),
            "score": s.score, "conviction": conviction,
            "price_usd": fill.price_usd, "safety": safety_info,
        })

    # ── state out ────────────────────────────────────────────────────────────
    async def _publish_state(self):
        bus.publish_state(self.state())

    def state(self) -> dict:
        return {
            "config": cfg.summary(),
            "mode": "LIVE" if (self.broker and self.broker.live) else "PAPER",
            "uptime_seconds": int(time.time() - self.started_at),
            "scan_count": self.scan_count,
            "last_scan_at": self.last_scan_at,
            "brain_read": self.brain_read,
            "portfolio": self.portfolio.snapshot(),
            "leaderboard": self.leaderboard,
            "server_time": time.time(),
        }
