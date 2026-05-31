"""CLI: run a backtest and auto-log its closed trades to the journal.

This is the demonstrable "auto-logging" path — the journal fills from real
simulated fills (entry/exit/MAE/MFE/R-multiple), not hand-typed rows.

Usage:
    uv run python -m journal_service.seed --strategy trend-follower --n-bars 4000
"""

from __future__ import annotations

import argparse
import asyncio

from atlas_shared.logging import get_logger, setup_logging
from backtest_service import BacktestConfig, TrendFollower, run_backtest
from feature_engine import gbm_bars

from journal_service.store import log_trades

setup_logging()
log = get_logger("journal.seed")


async def _go(symbol: str, strategy: str, n_bars: int, seed: int) -> None:
    bars = gbm_bars(n=n_bars, seed=seed, symbol=symbol.upper())
    # trend-follower is dependency-light and always produces trades on synthetic
    # data; atlas requires a trained model so we keep the seeder simple.
    strat = TrendFollower()
    result = run_backtest({symbol.upper(): bars}, strat, config=BacktestConfig())
    n = await log_trades(result.trades, strategy=strategy)
    log.info("journal.seed.done", logged=n, of=len(result.trades))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SYN")
    p.add_argument("--strategy", default="trend-follower")
    p.add_argument("--n-bars", type=int, default=4000)
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args()
    asyncio.run(_go(args.symbol, args.strategy, args.n_bars, args.seed))


if __name__ == "__main__":
    main()
