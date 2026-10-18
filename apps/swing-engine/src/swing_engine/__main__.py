"""``python -m swing_engine <command>``: the swing engine's command line.

    uv run python -m swing_engine backfill [--start 2016-01-01] [--symbols AAPL,MSFT]
    uv run python -m swing_engine update
    uv run python -m swing_engine scan [--as-of 2026-10-16]

``backfill`` loads daily bars (active and delisted common stock plus SPY) up to the last
completed session. It is safe to interrupt and re-run, even a session later: a symbol with
no bars is loaded in full, a symbol that is behind is caught up from its last stored bar, a
delisted symbol that already has bars and a symbol already at the target are skipped, and
failed chunks are retried. ``update`` appends the newest sessions to everything stored.
Both replace the history of any symbol whose split/dividend adjustment moved. Both need
``ALPACA_API_KEY`` and ``ALPACA_API_SECRET`` (a ``.env`` is loaded) and a migrated database,
and both exit with status 1 when some symbols failed, so a re-run is needed.

``scan`` reads the stored bars only (no Alpaca keys) and prints tonight's regime and the top
ranked swing ideas for the latest SPY session, or for ``--as-of``. It exits with status 1 and a
message when there is no data or SPY has no bar on that date.

Importing this module has no side effects; everything starts in :func:`main`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
from atlas_shared.config import load_env
from atlas_shared.logging import get_logger, setup_logging

from swing_engine import store
from swing_engine.config import TOP_N_DEFAULT, RiskSettings
from swing_engine.data.alpaca import AlpacaData, AlpacaError
from swing_engine.data.assets import AssetKind
from swing_engine.ingest import HISTORY_START, IngestResult, backfill, incremental_update
from swing_engine.panel import Panel, build_panel
from swing_engine.scan import (
    BENCHMARK,
    Idea,
    ScanError,
    ScanResult,
    default_setups,
    run_scan,
)

log = get_logger("swing.cli")

_LISTED_LIMIT = 50
_SCAN_MIN_DOLLAR_VOLUME = 20_000_000
_SCAN_WINDOW_DAYS = 800  # calendar days of bars: about 550 sessions, enough for a 252-bar RS


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

    sc = commands.add_parser(
        "scan",
        help="print tonight's regime and ranked swing ideas from the stored bars",
        description=(
            "Scan the stored daily bars as of one session: SPY's market regime, then the "
            "ranked, sized swing ideas (the top "
            f"{TOP_N_DEFAULT}). A downtrend lists the gated ideas as watch-only. Needs a "
            "database loaded by backfill/update; no Alpaca keys."
        ),
    )
    sc.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="session to scan (default: the latest stored SPY session)",
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


def _kinds(assets: pd.DataFrame) -> dict[str, AssetKind]:
    return {str(s): AssetKind(k) for s, k in zip(assets["symbol"], assets["kind"], strict=True)}


async def _load_scan_inputs(as_of: date | None) -> tuple[Panel, dict[str, AssetKind], date]:
    """Stored bars for the liquid stocks plus SPY, adjusted and raw, ending on the scan session."""
    assets = await store.load_assets()
    if assets.empty:
        raise ScanError("no assets stored: run `python -m swing_engine backfill` first")
    if as_of is None:
        latest = await store.last_bar_dates()
        if BENCHMARK not in latest:
            raise ScanError(f"no {BENCHMARK} bars stored: run `python -m swing_engine backfill`")
        as_of = latest[BENCHMARK]
    symbols = sorted({*await store.liquid_prefilter(_SCAN_MIN_DOLLAR_VOLUME), BENCHMARK})
    start = as_of - timedelta(days=_SCAN_WINDOW_DAYS)
    bars = await store.load_daily_bars(symbols, start, as_of)
    if bars.empty:
        raise ScanError(
            f"no daily bars stored for {start} to {as_of}: run `python -m swing_engine backfill`"
        )
    raw = await store.load_daily_bars(symbols, start, as_of, resolution=store.RAW_DAILY)
    return build_panel(bars, raw_bars=raw), _kinds(assets), as_of


_TABLE = (
    ("#", ">"),
    ("Symbol", "<"),
    ("Setup", "<"),
    ("Buy above", ">"),
    ("Stop", ">"),
    ("Shares", ">"),
    ("$ risk", ">"),
    ("RS", ">"),
    ("Flags", "<"),
)


def _flags(idea: Idea) -> str:
    flags = ["UNPROVEN"] if not idea.live else []
    if idea.overlaps:
        flags.append(f"overlaps {','.join(idea.overlaps)}")
    if idea.regime_warning:
        flags.append("regime warning")
    return "; ".join(flags)


def _table(ideas: Sequence[Idea]) -> list[str]:
    rows = [
        [
            str(i.rank),
            i.symbol,
            i.setup,
            f"{i.trigger:.2f}",
            f"{i.stop:.2f}",
            str(i.sizing.shares),
            f"{i.sizing.risk_dollars:,.0f}",
            f"{i.rs_pct:.0f}",
            _flags(i),
        ]
        for i in ideas
    ]
    header = [name for name, _ in _TABLE]
    widths = [max(len(h), *(len(r[k]) for r in rows)) for k, h in enumerate(header)]
    return [
        "  ".join(
            f"{cell:{align}{w}}" for cell, (_, align), w in zip(row, _TABLE, widths, strict=True)
        ).rstrip()
        for row in (header, *rows)
    ]


def _section(ideas: Sequence[Idea], top_n: int) -> list[str]:
    shown = ideas[:top_n]
    lines = _table(shown)
    if len(ideas) > len(shown):
        lines.append(f"... {len(ideas) - len(shown)} more not shown (top {top_n} of {len(ideas)})")
    if any(not i.live for i in shown):
        lines.append("UNPROVEN = no out-of-sample validation for this setup yet")
    return lines


def format_scan(r: ScanResult, top_n: int = TOP_N_DEFAULT) -> str:
    """The regime line, the book, the top ``top_n`` ideas and any watch-only ideas, as text."""
    breadth = "n/a" if math.isnan(r.breadth) else f"{r.breadth:.0f}% above SMA50"
    c = r.capacity
    lines = [
        f"{r.session.isoformat()}  regime {r.regime.value.upper()} "
        f"(size factor {r.regime_factor:.1f})  breadth {breadth}  universe {r.universe_size} stocks",
        f"book: {c.open_positions} of {c.max_positions} positions, "
        f"${c.open_risk_dollars:,.0f} of ${c.max_risk_dollars:,.0f} open risk",
        "",
    ]
    lines += _section(r.ideas, top_n) if r.ideas else ["No ideas tonight."]
    if r.watch_only:
        lines += ["", f"Watch only ({r.regime.value}: gated setups are not sized)"]
        lines += _section(r.watch_only, top_n)
    return "\n".join(lines)


def _scan_report(as_of: date | None) -> str:
    try:
        panel, kinds, session = asyncio.run(_load_scan_inputs(as_of))
        result = run_scan(panel, kinds, session, RiskSettings(), default_setups(Path.cwd()))
    except ScanError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return format_scan(result)


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    load_env()  # so AlpacaData sees the keys from .env
    setup_logging()
    if args.cmd == "scan":
        print(_scan_report(args.as_of))
        return
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
