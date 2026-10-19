"""Wide daily-bar panel: one tz-naive session-date index, one column per symbol.

When the reference series (SPY) is in the bars, the panel's rows are SPY's sessions and nothing
else: a stray bar dated off its calendar (a weekend or holiday print, or a stock updated before
SPY) would otherwise add a row on which every other symbol is missing, which reads as a missed
session for all of them and shifts every rolling window by a row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

_FIELDS = ("open", "high", "low", "close", "volume")
REFERENCE = "SPY"
"""The series whose sessions are the panel's rows when it is present (the market benchmark)."""


@dataclass(frozen=True)
class Panel:
    """OHLCV as five aligned frames, plus optionally the unadjusted close.

    The index is a tz-naive ``DatetimeIndex`` of session dates and the columns are
    symbols. A symbol with no bar on a session holds NaN there; nothing is filled.

    ``raw_close`` is the close as it printed (no split or dividend adjustment), on the
    same index and columns as ``close``, or None when no raw bars were given. It exists
    for the point-in-time price floor and nothing else: everything else is adjusted.
    """

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    raw_close: pd.DataFrame | None = None

    def symbols(self) -> list[str]:
        return [str(c) for c in self.close.columns]

    def dates(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)


def _session_dates(ts: pd.Series) -> pd.Series:
    """Session dates at 00:00 UTC to tz-naive midnights (the panel's index convention)."""
    return pd.to_datetime(ts, utc=True).dt.tz_convert(None).dt.normalize()


def _on_reference_sessions(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """``frames`` cut to the rows where :data:`REFERENCE` has a close, when it is a column."""
    close = frames["close"]
    if REFERENCE not in close.columns:
        return frames
    keep = close[REFERENCE].notna().to_numpy()
    if keep.all():
        return frames
    return {f: frame.loc[keep] for f, frame in frames.items()}


def build_panel(bars: pd.DataFrame, raw_bars: pd.DataFrame | None = None) -> Panel:
    """Pivot a long frame (``ts, symbol, open, high, low, close, volume``) into a Panel.

    ``ts`` is the session date at 00:00 UTC (the storage convention); the panel index
    is that session date, tz-naive. A repeated ``(ts, symbol)`` keeps the last row. When
    :data:`REFERENCE` is among the symbols, only the sessions on which it has a bar are kept
    (see the module docstring).

    ``raw_bars`` (``ts, symbol, close`` at least, from the ``1d_raw`` resolution) fills
    ``Panel.raw_close``, reindexed onto the adjusted panel's index and columns: a raw bar for a
    session or symbol the adjusted bars lack is dropped, and a missing raw bar is NaN, never
    filled.
    """
    df = bars.loc[:, ["ts", "symbol", *_FIELDS]].copy()
    df["ts"] = _session_dates(df["ts"])
    df = df.drop_duplicates(subset=["ts", "symbol"], keep="last")
    frames = {
        f: df.pivot(index="ts", columns="symbol", values=f).sort_index().astype("float64")
        for f in _FIELDS
    }
    for frame in frames.values():
        frame.index.name = None
        frame.columns.name = None
    frames = _on_reference_sessions(frames)
    raw_close = None
    if raw_bars is not None:
        raw = raw_bars.loc[:, ["ts", "symbol", "close"]].copy()
        raw["ts"] = _session_dates(raw["ts"])
        raw = raw.drop_duplicates(subset=["ts", "symbol"], keep="last")
        raw_close = (
            raw.pivot(index="ts", columns="symbol", values="close")
            .astype("float64")
            .reindex(index=frames["close"].index, columns=frames["close"].columns)
        )
    return Panel(**frames, raw_close=raw_close)


def concat_panels(panels: Sequence[Panel]) -> Panel:
    """Panels over disjoint symbol sets side by side, as one :func:`build_panel` would give.

    The rows are the union of their sessions (sorted), cut to :data:`REFERENCE`'s sessions when
    one of them holds it, and the columns are sorted. A symbol in two panels raises
    ``ValueError``. ``raw_close`` is kept where a panel has it and NaN where it has none.
    """
    if not panels:
        raise ValueError("no panels to join")
    symbols = [s for p in panels for s in p.symbols()]
    repeated = sorted({s for s in symbols if symbols.count(s) > 1})
    if repeated:
        raise ValueError(f"symbols appear in more than one panel: {repeated}")

    def joined(frames: list[pd.DataFrame]) -> pd.DataFrame:
        out = pd.concat(frames, axis=1, sort=False).sort_index().sort_index(axis=1)
        return out.astype("float64")

    frames = _on_reference_sessions({f: joined([getattr(p, f) for p in panels]) for f in _FIELDS})
    close = frames["close"]
    raw_close = None
    if any(p.raw_close is not None for p in panels):
        raw = [p.close.iloc[:0] if p.raw_close is None else p.raw_close for p in panels]
        raw_close = joined(raw).reindex(index=close.index, columns=close.columns)
    return Panel(**frames, raw_close=raw_close)


def truncate(panel: Panel, as_of: pd.Timestamp) -> Panel:
    """Drop every session after ``as_of`` so nothing downstream can see the future."""
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert(None)
    raw = panel.raw_close
    return Panel(
        **{f: getattr(panel, f).loc[:cutoff] for f in _FIELDS},
        raw_close=None if raw is None else raw.loc[:cutoff],
    )


def select(panel: Panel, symbols: Sequence[str]) -> Panel:
    """Keep only ``symbols`` (in the given order). An unknown symbol raises ``KeyError``."""
    cols = list(symbols)
    raw = panel.raw_close
    return Panel(
        **{f: getattr(panel, f).loc[:, cols] for f in _FIELDS},
        raw_close=None if raw is None else raw.loc[:, cols],
    )


def segments(series: pd.Series, max_gap: int) -> list[pd.Series]:
    """Split a symbol's non-NaN values wherever the bars are too far apart.

    The gap is the number of panel sessions (rows of the panel index) that lie between
    two consecutive non-NaN values. More than ``max_gap`` of them starts a new segment,
    which is how a reused ticker or a long halt is kept from blending old and new data.
    """
    valid = series.dropna()
    if valid.empty:
        return []
    rows = series.index.get_indexer(valid.index)
    breaks = np.flatnonzero(np.diff(rows) - 1 > max_gap) + 1
    bounds = [0, *breaks.tolist(), len(valid)]
    return [valid.iloc[a:b] for a, b in pairwise(bounds)]
