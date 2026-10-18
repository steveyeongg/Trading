"""Wide daily-bar panel: one tz-naive session-date index, one column per symbol."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Panel:
    """OHLCV as five aligned frames.

    The index is a tz-naive ``DatetimeIndex`` of session dates and the columns are
    symbols. A symbol with no bar on a session holds NaN there; nothing is filled.
    """

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    def symbols(self) -> list[str]:
        return [str(c) for c in self.close.columns]

    def dates(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)


def build_panel(bars: pd.DataFrame) -> Panel:
    """Pivot a long frame (``ts, symbol, open, high, low, close, volume``) into a Panel.

    ``ts`` is the session date at 00:00 UTC (the storage convention); the panel index
    is that session date, tz-naive. A repeated ``(ts, symbol)`` keeps the last row.
    """
    df = bars.loc[:, ["ts", "symbol", *_FIELDS]].copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None).dt.normalize()
    df = df.drop_duplicates(subset=["ts", "symbol"], keep="last")
    frames = {
        f: df.pivot(index="ts", columns="symbol", values=f).sort_index().astype("float64")
        for f in _FIELDS
    }
    for frame in frames.values():
        frame.index.name = None
        frame.columns.name = None
    return Panel(**frames)


def truncate(panel: Panel, as_of: pd.Timestamp) -> Panel:
    """Drop every session after ``as_of`` so nothing downstream can see the future."""
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert(None)
    return Panel(**{f: getattr(panel, f).loc[:cutoff] for f in _FIELDS})


def select(panel: Panel, symbols: Sequence[str]) -> Panel:
    """Keep only ``symbols`` (in the given order). An unknown symbol raises ``KeyError``."""
    cols = list(symbols)
    return Panel(**{f: getattr(panel, f).loc[:, cols] for f in _FIELDS})


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
