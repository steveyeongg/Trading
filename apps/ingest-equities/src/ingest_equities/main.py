"""CLI: backfill bars into Postgres.

Examples:
    # Real backfill via Polygon:
    uv run python -m ingest_equities backfill --source polygon --symbols AAPL,MSFT --days 30

    # Real backfill via Alpaca (free IEX feed if no paid plan):
    uv run python -m ingest_equities backfill --source alpaca --symbols AAPL,MSFT --days 30

    # Auto: picks polygon if POLYGON_API_KEY is set, else alpaca if Alpaca keys are set:
    uv run python -m ingest_equities backfill --symbols AAPL,MSFT --days 30

    # Offline synthetic backfill (no API key required):
    uv run python -m ingest_equities synthetic --symbols AAPL,MSFT --n-bars 1000
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from atlas_shared.config import get_settings
from atlas_shared.logging import get_logger, setup_logging
from feature_engine import gbm_bars

from ingest_equities.alpaca import AlpacaClient
from ingest_equities.polygon import PolygonClient
from ingest_equities.store import upsert_bars

setup_logging()
log = get_logger("ingest.cli")


def _resolve_source(requested: str) -> str:
    """Pick a real-data source.

    `auto` → polygon if its key is present, else alpaca if its pair is present,
    else raise (synthetic is a different subcommand on purpose).
    """
    if requested != "auto":
        return requested
    s = get_settings()
    if s.polygon_api_key:
        return "polygon"
    if s.alpaca_api_key and s.alpaca_api_secret:
        return "alpaca"
    raise SystemExit(
        "no real-data credentials configured. Set POLYGON_API_KEY or "
        "ALPACA_API_KEY+ALPACA_API_SECRET, or use the `synthetic` subcommand."
    )


async def _backfill(symbols: list[str], days: int, source: str) -> None:
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    client_cls = {"polygon": PolygonClient, "alpaca": AlpacaClient}[source]
    async with client_cls() as client:
        for sym in symbols:
            n = 0
            async for chunk in client.aggs_1m(sym, start, end):
                n += await upsert_bars(chunk)
            log.info("backfill.done", source=source, symbol=sym, rows=n)


async def _synthetic(symbols: list[str], n_bars: int) -> None:
    for sym in symbols:
        bars = gbm_bars(n=n_bars, symbol=sym, seed=hash(sym) & 0xFFFF)
        n = await upsert_bars(bars)
        log.info("synthetic.done", symbol=sym, rows=n)


def main() -> None:
    from atlas_shared import load_env

    load_env()  # so Polygon/Alpaca clients see keys from .env
    p = argparse.ArgumentParser(prog="ingest-equities")
    sub = p.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="Pull historical bars from a real-data vendor.")
    bf.add_argument("--symbols", required=True, help="Comma-separated symbols.")
    bf.add_argument("--days", type=int, default=30)
    bf.add_argument(
        "--source",
        choices=("auto", "polygon", "alpaca"),
        default="auto",
        help="Bar source. `auto` picks polygon if its key is set, else alpaca.",
    )

    syn = sub.add_parser("synthetic", help="Generate synthetic bars (no API needed).")
    syn.add_argument("--symbols", required=True)
    syn.add_argument("--n-bars", type=int, default=1000)

    args = p.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.cmd == "backfill":
        source = _resolve_source(args.source)
        asyncio.run(_backfill(symbols, args.days, source))
    elif args.cmd == "synthetic":
        asyncio.run(_synthetic(symbols, args.n_bars))


if __name__ == "__main__":
    main()
