"""
Moonshot Engine — a fast, multi-signal Solana crypto trading system.

Architecture:
  datafeed  → pulls live market data (DexScreener) for candidates + held positions
  signals   → computes normalized 0-100 sub-signals per token
  scoring   → blends sub-signals into a composite conviction score + sizing
  brain     → optional Claude Opus 4.8 review of the top-ranked candidates
  risk      → trailing stops + dynamic exit management
  broker    → execution abstraction (PaperBroker | LiveBroker over Jupiter+wallet)
  portfolio → balances, open positions, realized PnL, equity curve
  engine    → the async orchestration loop tying it all together
  events    → in-memory pub/sub bus that the dashboard subscribes to
"""

__all__ = ["config"]
