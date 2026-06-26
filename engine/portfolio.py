"""
Portfolio state — positions, balances, realized PnL, and the equity curve.

This is mode-agnostic: the same object backs paper and live trading. The broker
tells it what filled; it tracks the consequences and persists to disk so a
restart resumes cleanly. All values are in SOL unless suffixed _usd.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from engine import config as cfg


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_usd: float
    peak_price_usd: float
    amount: float                 # token units held
    sol_in: float                 # SOL spent to enter (incl. simulated fees)
    entry_time: float
    score: float
    conviction: float
    pair_address: str = ""
    entry_liquidity_usd: float = 0.0
    last_price_usd: float = 0.0
    entry_tx: str = ""

    def pnl_pct(self, price: Optional[float] = None) -> float:
        p = price if price is not None else (self.last_price_usd or self.entry_price_usd)
        if self.entry_price_usd <= 0:
            return 0.0
        return (p - self.entry_price_usd) / self.entry_price_usd * 100.0

    def value_sol(self, price: Optional[float] = None) -> float:
        """Mark-to-market value in SOL (entry SOL scaled by price move)."""
        return self.sol_in * (1 + self.pnl_pct(price) / 100.0)

    def age_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pnl_pct"] = round(self.pnl_pct(), 2)
        d["value_sol"] = round(self.value_sol(), 5)
        d["age_minutes"] = round(self.age_minutes(), 1)
        return d


@dataclass
class ClosedTrade:
    mint: str
    symbol: str
    entry_price_usd: float
    exit_price_usd: float
    sol_in: float
    sol_out: float
    pnl_sol: float
    pnl_pct: float
    reason: str
    entry_time: float
    exit_time: float
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


class Portfolio:
    def __init__(self, starting_sol: float):
        self.cash_sol: float = starting_sol
        self.starting_sol: float = starting_sol
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.equity_curve: list[dict] = []   # [{t, equity}]
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        path = cfg.state_file()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.cash_sol = data.get("cash_sol", self.cash_sol)
            self.starting_sol = data.get("starting_sol", self.starting_sol)
            self.positions = {
                m: Position(**p) for m, p in data.get("positions", {}).items()
            }
            self.equity_curve = data.get("equity_curve", [])[-2000:]
        except Exception:  # noqa: BLE001
            pass
        # Closed trades live in their own append-only file.
        try:
            if os.path.exists(cfg.TRADES_FILE):
                with open(cfg.TRADES_FILE) as f:
                    self.closed = [ClosedTrade(**t) for t in json.load(f)]
        except Exception:  # noqa: BLE001
            self.closed = []

    def save(self) -> None:
        os.makedirs(cfg.STATE_DIR, exist_ok=True)
        with open(cfg.state_file(), "w") as f:
            json.dump({
                "cash_sol": self.cash_sol,
                "starting_sol": self.starting_sol,
                "positions": {m: asdict(p) for m, p in self.positions.items()},
                "equity_curve": self.equity_curve[-2000:],
            }, f, indent=2)
        with open(cfg.TRADES_FILE, "w") as f:
            json.dump([t.to_dict() for t in self.closed], f, indent=2)

    # ── mutations ──────────────────────────────────────────────────────────
    def open_position(self, pos: Position) -> None:
        self.cash_sol -= pos.sol_in
        self.positions[pos.mint] = pos
        self.save()

    def close_position(self, mint: str, exit_price_usd: float, sol_out: float,
                       reason: str) -> Optional[ClosedTrade]:
        pos = self.positions.pop(mint, None)
        if not pos:
            return None
        self.cash_sol += sol_out
        trade = ClosedTrade(
            mint=pos.mint, symbol=pos.symbol,
            entry_price_usd=pos.entry_price_usd, exit_price_usd=exit_price_usd,
            sol_in=round(pos.sol_in, 5), sol_out=round(sol_out, 5),
            pnl_sol=round(sol_out - pos.sol_in, 5),
            pnl_pct=round(pos.pnl_pct(exit_price_usd), 2),
            reason=reason, entry_time=pos.entry_time, exit_time=time.time(),
            score=pos.score,
        )
        self.closed.append(trade)
        self.save()
        return trade

    def mark(self, mint: str, price: float) -> None:
        pos = self.positions.get(mint)
        if pos and price > 0:
            pos.last_price_usd = price
            if price > pos.peak_price_usd:
                pos.peak_price_usd = price

    def sample_equity(self) -> None:
        self.equity_curve.append({"t": time.time(), "equity": round(self.equity_sol(), 5)})
        self.equity_curve = self.equity_curve[-2000:]

    # ── views ──────────────────────────────────────────────────────────────
    def positions_value_sol(self) -> float:
        return sum(p.value_sol() for p in self.positions.values())

    def equity_sol(self) -> float:
        return self.cash_sol + self.positions_value_sol()

    def realized_pnl_sol(self) -> float:
        return sum(t.pnl_sol for t in self.closed)

    def stats(self) -> dict:
        wins = [t for t in self.closed if t.pnl_sol > 0]
        losses = [t for t in self.closed if t.pnl_sol <= 0]
        n = len(self.closed)
        equity = self.equity_sol()
        gross_win = sum(t.pnl_sol for t in wins)
        gross_loss = abs(sum(t.pnl_sol for t in losses))
        return {
            "cash_sol": round(self.cash_sol, 4),
            "positions_value_sol": round(self.positions_value_sol(), 4),
            "equity_sol": round(equity, 4),
            "starting_sol": round(self.starting_sol, 4),
            "total_return_pct": round((equity / self.starting_sol - 1) * 100, 2)
            if self.starting_sol else 0.0,
            "realized_pnl_sol": round(self.realized_pnl_sol(), 4),
            "open_positions": len(self.positions),
            "closed_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "best_trade_pct": round(max((t.pnl_pct for t in self.closed), default=0.0), 1),
            "worst_trade_pct": round(min((t.pnl_pct for t in self.closed), default=0.0), 1),
        }

    def snapshot(self) -> dict:
        return {
            "stats": self.stats(),
            "positions": [p.to_dict() for p in sorted(
                self.positions.values(), key=lambda x: x.entry_time, reverse=True)],
            "closed": [t.to_dict() for t in self.closed[-40:]][::-1],
            "equity_curve": self.equity_curve[-400:],
        }
