"""CLI: seed demo positions into the default portfolio.

Usage:
    uv run python -m portfolio_service.seed
    uv run python -m portfolio_service.seed --reset
"""

from __future__ import annotations

import argparse
import asyncio

from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger, setup_logging
from sqlalchemy import text

from portfolio_service.store import DEFAULT_USER, add_position

setup_logging()
log = get_logger("portfolio.seed")

# (symbol, qty, avg_cost, sector)
DEMO = [
    ("AAPL", 50, 205.00, "Information Technology"),
    ("MSFT", 30, 410.00, "Information Technology"),
    ("NVDA", 40, 120.00, "Information Technology"),
    ("XOM", 80, 105.00, "Energy"),
    ("JPM", 60, 195.00, "Financials"),
]


async def _seed(reset: bool) -> None:
    if reset:
        async with session_scope() as s:
            await s.execute(
                text(
                    """
                    DELETE FROM positions WHERE portfolio_id IN (
                        SELECT id FROM portfolios WHERE user_id = :uid
                    )
                    """
                ),
                {"uid": DEFAULT_USER},
            )
        log.info("portfolio.seed.cleared")
    for sym, qty, cost, sector in DEMO:
        await add_position(sym, qty, cost, sector=sector, user_id=DEFAULT_USER)
    log.info("portfolio.seed.done", n=len(DEMO))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="Clear existing positions first.")
    args = p.parse_args()
    asyncio.run(_seed(args.reset))


if __name__ == "__main__":
    main()
