"""CLI: backfill bars into Postgres.

Examples:
    # Real backfill via Polygon:
    uv run python -m ingest_equities backfill --symbols AAPL,MSFT --days 30

    # Offline synthetic backfill (no API key required):
    uv run python -m ingest_equities synthetic --symbols AAPL,MSFT --n-bars 1000
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from atlas_shared.logging import get_logger, setup_logging
from feature_engine import gbm_bars

from ingest_equities.polygon import PolygonClient
from ingest_equities.store import upsert_bars

setup_logging()
log = get_logger("ingest.cli")


async def _backfill(symbols: list[str], days: int) -> None:
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    async with PolygonClient() as client:
        for sym in symbols:
            n = 0
            async for chunk in client.aggs_1m(sym, start, end):
                n += await upsert_bars(chunk)
            log.info("backfill.done", symbol=sym, rows=n)


async def _synthetic(symbols: list[str], n_bars: int) -> None:
    for sym in symbols:
        bars = gbm_bars(n=n_bars, symbol=sym, seed=hash(sym) & 0xFFFF)
        n = await upsert_bars(bars)
        log.info("synthetic.done", symbol=sym, rows=n)


def main() -> None:
    p = argparse.ArgumentParser(prog="ingest-equities")
    sub = p.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="Pull historical bars from Polygon.")
    bf.add_argument("--symbols", required=True, help="Comma-separated symbols.")
    bf.add_argument("--days", type=int, default=30)

    syn = sub.add_parser("synthetic", help="Generate synthetic bars (no API needed).")
    syn.add_argument("--symbols", required=True)
    syn.add_argument("--n-bars", type=int, default=1000)

    args = p.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.cmd == "backfill":
        asyncio.run(_backfill(symbols, args.days))
    elif args.cmd == "synthetic":
        asyncio.run(_synthetic(symbols, args.n_bars))


if __name__ == "__main__":
    main()
