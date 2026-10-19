"""``python -m swing_engine <command>``: the swing engine's command line.

    uv run python -m swing_engine backfill [--start 2016-01-01] [--symbols AAPL,MSFT]
    uv run python -m swing_engine backfill --refresh-assets
    uv run python -m swing_engine update
    uv run python -m swing_engine scan [--as-of 2026-10-16]
    uv run python -m swing_engine insample --family breakout [--hypothesis "..."]
    uv run python -m swing_engine oos --family breakout --version breakout-v1

``backfill`` loads daily bars (active and delisted common stock plus SPY) up to the last
completed session. It is safe to interrupt and re-run, even a session later: a symbol with
no bars is loaded in full, a symbol that is behind is caught up from its last stored bar, a
delisted symbol that already has bars and a symbol already at the target are skipped, and
failed chunks are retried. The asset list is fetched from Alpaca once, on the first run; a
re-run reuses the stored list, so new listings and delistings are picked up only with
``--refresh-assets``, which re-fetches the active and inactive lists first (run it
periodically, for example weekly). ``update`` appends the newest sessions to everything
stored; it never discovers new listings. Both replace the history of any symbol whose
split/dividend adjustment moved. Both need ``ALPACA_API_KEY`` and ``ALPACA_API_SECRET`` (a
``.env`` is loaded) and a migrated database, and both exit with status 1 when some symbols
failed, so a re-run is needed.

``scan`` reads the stored bars only (no Alpaca keys) and prints tonight's regime and the top
ranked swing ideas for the latest SPY session, or for ``--as-of``. It exits with status 1 and a
message when there is no data or SPY has no bar on that date. It ranks by relative strength
unless a validated setup's report found that RS ranking did not help.

``insample`` and ``oos`` are the validation protocol (spec §7.8), run from the repo root because
they read and write ``ml/validation/``. ``insample`` runs the declared grid on 2016-2022 only and
writes the chosen variant as a draft pre-registration; a revision (v2, v3) needs
``--hypothesis``. Commit that file, then ``oos`` runs the one out-of-sample test of that version
and writes its report, Live or Research only. Both read the stored bars only and exit with status
1 and the reason when the budget is spent, the pre-registration is not committed and unchanged,
or the data is missing.

Importing this module has no side effects; everything starts in :func:`main`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

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
from swing_engine.validation.prereg import PreregError
from swing_engine.validation.protocol import FAMILIES, IS_END, IS_START
from swing_engine.validation.run import (
    VALIDATION_DIR,
    BudgetError,
    ranking_choice,
    run_insample,
    run_oos,
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
            "repeated, even on a later day. The asset list is fetched on the first run only; "
            "pass --refresh-assets to pick up new listings and delistings."
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
    bf.add_argument(
        "--refresh-assets",
        dest="refresh_assets",
        action="store_true",
        help=(
            "re-fetch Alpaca's active and inactive asset lists first, so new listings and "
            "delistings are picked up (otherwise the stored list is reused after the first run)"
        ),
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

    ins = commands.add_parser(
        "insample",
        help="run a setup's in-sample grid and draft its pre-registration",
        description=(
            f"Run the declared parameter grid on {IS_START.year}-{IS_END.year} only, choose the "
            "variant with the highest expectancy among those with 100+ trades, and write it to "
            "ml/validation/<family>/<version>.prereg.json as a draft to review and commit. "
            "Needs a database loaded by backfill/update."
        ),
    )
    ins.add_argument("--family", required=True, choices=FAMILIES, help="setup family")
    ins.add_argument(
        "--hypothesis",
        default="",
        metavar="TEXT",
        help="why this revision should do better, from in-sample analysis (required for v2+)",
    )

    oos = commands.add_parser(
        "oos",
        help="run a pre-registered version's one out-of-sample test",
        description=(
            "Check the budget (3 runs per family) and that the version's pre-registration is "
            "committed and unchanged, freeze the window at the last stored session, run the "
            "out-of-sample test once and write its report with the Live / Research-only verdict."
        ),
    )
    oos.add_argument("--family", required=True, choices=FAMILIES, help="setup family")
    oos.add_argument(
        "--version", required=True, metavar="FAMILY-vN", help="pre-registered version to test"
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
            refresh_assets=args.refresh_assets,
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
        result = run_scan(
            panel,
            kinds,
            session,
            RiskSettings(),
            default_setups(Path.cwd()),
            rank_by=ranking_choice(Path.cwd()),
        )
    except ScanError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return format_scan(result)


def _float(value: object) -> float:
    """A stored number as a float: null is NaN, and ``"inf"``/``"-inf"`` are infinities."""
    if value is None:
        return math.nan
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _num(value: object, spec: str, suffix: str = "") -> str:
    """A stored number formatted, or ``n/a`` when it is missing (stored NaN is null)."""
    x = _float(value)
    return "n/a" if math.isnan(x) else f"{x:{spec}}{suffix}"


def _count(value: object) -> str:
    """A stored count, or ``n/a`` when it is missing (a record written before it existed)."""
    return _num(value, ",.0f")


def _aligned(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """``header`` and ``rows`` as right-aligned text columns."""
    widths = [max(len(h), *(len(r[k]) for r in rows)) for k, h in enumerate(header)]
    return [
        "  ".join(f"{cell:>{w}}" for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in (header, *rows)
    ]


def _stats_cells(stats: Mapping[str, Any]) -> list[str]:
    return [
        str(stats["n"]),
        _count(stats.get("n_distinct")),
        _num(stats["win_rate"], ".1%"),
        _num(stats["expectancy_r"], ".2f", "R"),
        _num(stats["profit_factor"], ".2f"),
    ]


def _data_lines(summary: Mapping[str, Any]) -> list[str]:
    """The data fingerprint, delisted coverage and universe size, when the record has them."""
    lines = []
    fp = summary.get("fingerprint")
    if fp:
        lines.append(
            f"data: {fp['symbols']:,} symbols, {fp['first_date']} to {fp['last_date']}, "
            f"{fp['rows']:,} bars (sha256 {str(fp['sha256'])[:12]}), "
            f"delisted coverage {_num(summary.get('coverage'), '.0%')}"
        )
    universe = summary.get("universe")
    if universe:
        lines.append(
            f"universe: median {_num(universe.get('median_eligible'), ',.0f')} eligible stocks "
            f"per session over {_count(universe.get('sessions'))} in-sample sessions"
        )
    return lines


def format_insample(summary: Mapping[str, Any]) -> str:
    """The data, the grid as a table (the chosen variant starred), the chosen variant by year,
    then the draft and what to do next."""
    grid, chosen = summary["grid"], summary["chosen"]
    keys = list(grid[0]["params"]) if grid else []
    header = ["", *keys, "Trades", "Distinct", "Win %", "Expectancy", "PF", "Avg hold"]
    rows = [
        [
            "*" if row["params"] == chosen["params"] else "",
            *(f"{row['params'][k]:g}" for k in keys),
            *_stats_cells(row),
            _num(row["avg_hold"], ".1f"),
        ]
        for row in grid
    ]
    family, version = summary["family"], summary["version"]
    lines = [
        f"{version} in sample ({IS_START} to {IS_END}): {len(grid)} variants",
        *_data_lines(summary),
        "",
        *_aligned(header, rows),
        "",
        "chosen (*): "
        + ", ".join(f"{k}={v:g}" for k, v in chosen["params"].items())
        + " (highest expectancy among variants with 100+ trades)",
    ]
    years = summary.get("chosen_by_year")
    if years:
        year_rows = [[str(year), *_stats_cells(stats)] for year, stats in years.items()]
        lines += [
            "",
            "chosen variant by year (in sample, regime on):",
            *_aligned(["Year", "Trades", "Distinct", "Win %", "Expectancy", "PF"], year_rows),
        ]
    lines += [
        "",
        f"draft pre-registration: {summary['prereg_path']}",
        f"next: review and commit it, then run "
        f"`python -m swing_engine oos --family {family} --version {version}`",
    ]
    return "\n".join(lines)


def format_oos(report: Mapping[str, Any]) -> str:
    """The verdict line, the numbers the gate read, every failure, and where the report is."""
    family, version = report["family"], report["version"]
    passed = report["verdict"]["passed"]
    start, end = report["oos_window"]
    oos = report["oos_trades"]
    dd, spy = report["portfolio_alone"]["max_dd"], report["spy"]["max_dd"]
    lines = [
        f"{version} out of sample {start} to {end}: "
        + ("PASSED, now Live" if passed else "FAILED, stays Research only"),
        f"  {oos['n']} closed ideas, {_count(oos.get('n_distinct'))} distinct trades, "
        f"expectancy {_num(oos['expectancy_r'], '.2f', 'R')}, "
        f"profit factor {_num(oos['profit_factor'], '.2f')}, "
        f"random entry median {_num(report['baseline']['median'], '.2f', 'R')}",
        f"  max drawdown {_num(abs(_float(dd)), '.0%')} (SPY {_num(abs(_float(spy)), '.0%')}), "
        f"delisted coverage {_num(report['coverage'], '.0%')}, "
        f"deflated Sharpe {_num(report['deflated_sharpe']['value'], '.2f')}",
    ]
    if report["verdict"]["failures"]:
        lines += ["failures:", *(f"  - {f}" for f in report["verdict"]["failures"])]
    path = VALIDATION_DIR / family / f"{version}.report.json"
    lines.append(f"report: {path.as_posix()} (commit it)")
    return "\n".join(lines)


def _validation_report(args: argparse.Namespace) -> str:
    repo = Path.cwd()
    try:
        if args.cmd == "insample":
            return format_insample(run_insample(args.family, repo, hypothesis=args.hypothesis))
        return format_oos(run_oos(args.family, args.version, repo, datetime.now(UTC)))
    except (BudgetError, PreregError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    load_env()  # so AlpacaData sees the keys from .env
    setup_logging()
    if args.cmd == "scan":
        print(_scan_report(args.as_of))
        return
    if args.cmd in ("insample", "oos"):
        print(_validation_report(args))
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
