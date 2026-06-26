#!/usr/bin/env python3
"""
Moonshot Engine — entrypoint.

  python run.py              # start the engine + real-time dashboard
  LIVE=true python run.py    # trade your real Solana wallet (default is paper)

Then open http://localhost:8000
"""

import uvicorn

from engine import config as cfg


def main() -> None:
    print("=" * 60)
    print("  MOONSHOT ENGINE")
    print(f"  mode      : {'LIVE 🔴' if cfg.LIVE else 'PAPER 🧪'}")
    print(f"  brain     : {cfg.BRAIN_MODEL if (cfg.BRAIN_ENABLED and cfg.ANTHROPIC_API_KEY) else 'disabled (pure quant)'}")
    print(f"  dashboard : http://localhost:{cfg.PORT}")
    print("=" * 60)
    uvicorn.run("server:app", host=cfg.HOST, port=cfg.PORT, log_level="warning")


if __name__ == "__main__":
    main()
