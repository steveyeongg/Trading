"""The stored bars a scan reads: one data path for the ``scan`` command, the nightly job and the API.

:func:`load_scan_inputs` reads the database only (never the network): the asset kinds, then the
adjusted and raw daily bars of the liquid stocks plus SPY for a window ending on the scan session,
built into a :class:`~swing_engine.panel.Panel`. The window is long enough for every rolling
calculation the scan makes (a 252-session relative strength needs about a year of rows). The
pivot into the panel is seconds of pandas work on a full universe, so it runs in a worker thread
and the event loop (the API's requests) stays free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, timedelta

import pandas as pd

from swing_engine import store
from swing_engine.config import UniverseFilter
from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel, build_panel
from swing_engine.scan import BENCHMARK, ScanError

SCAN_WINDOW_DAYS = 800
"""Calendar days of bars loaded before the scan session: about 550 sessions."""


def _kinds(assets: pd.DataFrame) -> dict[str, AssetKind]:
    return {str(s): AssetKind(k) for s, k in zip(assets["symbol"], assets["kind"], strict=True)}


async def load_scan_inputs(
    as_of: date | None, universe: UniverseFilter, extra_symbols: Sequence[str] = ()
) -> tuple[Panel, dict[str, AssetKind], date]:
    """Stored bars for the liquid stocks plus SPY, adjusted and raw, ending on the scan session.

    ``as_of`` None means the latest session SPY has a stored bar for. "Liquid" is the store's
    prefilter at ``universe``'s dollar-volume floor, a necessary condition for being eligible
    under that same filter. ``extra_symbols`` are loaded too whatever their liquidity (the nightly
    job passes the symbols the user holds, which need their bars for tonight's action); a stock
    below the prefilter fails the same floor in the scan, so it never becomes an idea. Returns the
    panel, each symbol's asset kind and the session.
    Raises :class:`~swing_engine.scan.ScanError` when there are no assets, no SPY bars or no bars
    in the window, each with what to run first.
    """
    assets = await store.load_assets()
    if assets.empty:
        raise ScanError("no assets stored: run `python -m swing_engine backfill` first")
    if as_of is None:
        latest = await store.last_bar_dates()
        if BENCHMARK not in latest:
            raise ScanError(f"no {BENCHMARK} bars stored: run `python -m swing_engine backfill`")
        as_of = latest[BENCHMARK]
    liquid = await store.liquid_prefilter(universe.min_median_dollar_volume)
    symbols = sorted({*liquid, *extra_symbols, BENCHMARK})
    start = as_of - timedelta(days=SCAN_WINDOW_DAYS)
    bars = await store.load_daily_bars(symbols, start, as_of)
    if bars.empty:
        raise ScanError(
            f"no daily bars stored for {start} to {as_of}: run `python -m swing_engine backfill`"
        )
    raw = await store.load_daily_bars(symbols, start, as_of, resolution=store.RAW_DAILY)
    panel = await asyncio.to_thread(build_panel, bars, raw_bars=raw)
    return panel, _kinds(assets), as_of
