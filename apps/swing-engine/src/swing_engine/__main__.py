"""``python -m swing_engine <command>``: the swing engine's command line.

    uv run python -m swing_engine backfill [--start 2016-01-01] [--symbols AAPL,MSFT]
    uv run python -m swing_engine update

``backfill`` loads daily bars (active and delisted common stock plus SPY) up to the last
completed session. It is safe to interrupt and re-run, even a session later: a symbol with
no bars is loaded in full, a symbol that is behind is caught up from its last stored bar, a
delisted symbol that already has bars and a symbol already at the target are skipped, and
failed chunks are retried. ``update`` appends the newest sessions to everything stored.
Both replace the history of any symbol whose split/dividend adjustment moved. Both need
``ALPACA_API_KEY`` and ``ALPACA_API_SECRET`` (a ``.env`` is loaded) and a migrated database,
and both exit with status 1 when some symbols failed, so a re-run is needed.

Importing this module has no side effects; everything starts in :func:`main`.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime

from atlas_shared.config import load_env
from atlas_shared.logging import get_logger, setup_logging

from swing_engine import store
from swing_engine.data.alpaca import AlpacaData, AlpacaError
from swing_engine.ingest import HISTORY_START, IngestResult, backfill, incremental_update

log = get_logger("swing.cli")

_LISTED_LIMIT = 50


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _symbols(value: str) -> list[str]:
    symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("expected a comma-separated list of tickers")
    return symbols


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m swing_engine",
        description="ATLAS swing engine: daily-bar data jobs for US stocks.",
    )
    commands = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    bf = commands.add_parser(
        "backfill",
        help="load daily bars up to the last completed session (resumable)",
        description=(
            "Load SIP daily bars for every candidate stock, active and delisted, plus SPY. "
            "Only missing or outdated symbols are fetched, so an interrupted run can be "
            "repeated, even on a later day."
        ),
    )
    bf.add_argument(
        "--start",
        type=_iso_date,
        default=HISTORY_START,
        metavar="YYYY-MM-DD",
        help=f"first session to load (default {HISTORY_START.isoformat()})",
    )
    bf.add_argument(
        "--symbols",
        type=_symbols,
        default=None,
        metavar="A,B",
        help="only these tickers instead of the whole candidate universe",
    )

    commands.add_parser(
        "update",
        help="append the newest sessions; refetch a symbol's history after a split",
        description=(
            "Append the newest sessions to every stored symbol. A symbol whose adjusted "
            "history changed (split or dividend) has its full history refetched."
        ),
    )
    return parser


def _log_progress(handled: int, total: int) -> None:
    log.info("backfill.progress", handled=handled, total=total)


async def _run_backfill(args: argparse.Namespace) -> IngestResult:
    async with AlpacaData() as client:
        return await backfill(
            client,
            store,
            start=args.start,
            now=datetime.now(UTC),
            symbols=args.symbols,
            progress=_log_progress,
        )


async def _run_update() -> IngestResult:
    async with AlpacaData() as client:
        return await incremental_update(client, store, now=datetime.now(UTC))


def _names(symbols: list[str]) -> str:
    shown = ", ".join(symbols[:_LISTED_LIMIT])
    extra = len(symbols) - _LISTED_LIMIT
    return f"{shown} ... and {extra} more" if extra > 0 else shown


def _report(cmd: str, result: IngestResult) -> str:
    lines = [
        f"{cmd}: {result.symbols_done}/{result.symbols_total} symbols current, "
        f"{result.rows:,} rows written, {len(result.refetched)} refetched, "
        f"{len(result.failed)} failed"
    ]
    if result.refetched:
        lines.append(f"refetched full history (adjustment moved): {_names(result.refetched)}")
    if result.failed:
        lines.append(f"failed, re-run to resume: {_names(result.failed)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    load_env()  # so AlpacaData sees the keys from .env
    setup_logging()
    job = _run_backfill(args) if args.cmd == "backfill" else _run_update()
    try:
        result = asyncio.run(job)
    except AlpacaError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(_report(args.cmd, result))
    if result.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
