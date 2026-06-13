"""Persistent position tracking — survives restarts via JSON file."""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

import moonshot_trader.config as cfg

logger = logging.getLogger(__name__)


@dataclass
class Position:
    token_address: str
    token_symbol: str
    entry_price_usd: float
    entry_sol_spent: float       # SOL used to buy (pre-fees)
    token_amount: float          # Tokens received
    token_decimals: int
    entry_time: float            # Unix timestamp
    entry_tx: str
    pair_address: str            # DexScreener pair address (for price lookups)


class PositionBook:
    def __init__(self, filepath: str = cfg.POSITIONS_FILE):
        self._path = filepath
        self._positions: dict[str, Position] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            for item in data.get("positions", []):
                p = Position(**item)
                self._positions[p.token_address] = p
            logger.info("Loaded %d saved positions", len(self._positions))
        except Exception as e:
            logger.warning("Could not load positions file: %s", e)

    def _save(self) -> None:
        try:
            data = {"positions": [asdict(p) for p in self._positions.values()]}
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Could not save positions: %s", e)

    def add(self, position: Position) -> None:
        self._positions[position.token_address] = position
        self._save()
        logger.info("Position opened: %s  entry=$%.6f  spent=%.4f SOL",
                    position.token_symbol, position.entry_price_usd, position.entry_sol_spent)

    def remove(self, token_address: str) -> Optional[Position]:
        pos = self._positions.pop(token_address, None)
        if pos:
            self._save()
        return pos

    def get(self, token_address: str) -> Optional[Position]:
        return self._positions.get(token_address)

    def all(self) -> list[Position]:
        return list(self._positions.values())

    def count(self) -> int:
        return len(self._positions)

    def has(self, token_address: str) -> bool:
        return token_address in self._positions

    def pnl_summary(self, current_prices: dict[str, float]) -> str:
        if not self._positions:
            return "No open positions."
        lines = []
        for p in self._positions.values():
            cur = current_prices.get(p.token_address, p.entry_price_usd)
            pct = (cur - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd > 0 else 0
            age = (time.time() - p.entry_time) / 60
            lines.append(
                f"  {p.token_symbol:10s} {pct:+.1f}%  "
                f"entry=${p.entry_price_usd:.6f}  now=${cur:.6f}  "
                f"age:{age:.0f}min"
            )
        return "\n".join(lines)
