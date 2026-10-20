"""Point-in-time universe: which symbols may be scanned on each session.

A symbol is eligible at ``(d, s)`` only if every rule holds using data at or before ``d``:
it has a bar on ``d`` (a halted or not-yet-printed symbol is out, and yesterday's price is
never carried forward), it trades at or above the price floor, its 50-bar median dollar
volume clears the liquidity floor, its current segment is long enough, it moves enough (its
14-session ATR is at least ``min_atr_pct`` of the close, which keeps out stocks pinned to a
buyout price) and its asset kind is tradeable. Segments follow :func:`swing_engine.panel.segments`, so a reused ticker
restarts both the history count and the dollar-volume window, and a missing day is never
filled.

The price floor is judged on the raw (unadjusted) close when the panel has one, because
``adjustment=all`` rewrites history on every split: a stock that later split 10:1 reads under
$10 in years it traded at $30-$250, and one that later reverse-split reads far above what it
traded at. Only the floor looks at raw prices; dollar volume and everything else stay adjusted.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from swing_engine.config import UniverseFilter
from swing_engine.data.assets import TRADEABLE_KINDS, AssetKind
from swing_engine.indicators import atr
from swing_engine.panel import Panel, segments

ATR_WINDOW = 14
"""The ATR the minimum-volatility rule reads (the same 14 as ``Indicators.atr14``)."""


def eligibility(
    panel: Panel,
    kinds: Mapping[str, AssetKind],
    f: UniverseFilter,
    atr14: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Boolean frame shaped like ``panel.close``: True where the symbol may be scanned.

    ``kinds`` maps symbol to :class:`AssetKind`. A symbol with no entry (``SPY`` is a
    reference series, not a candidate) is never eligible.

    ``atr14`` is the panel's 14-session ATR (``compute_indicators(panel).atr14``), passed by a
    caller that already has it; None computes it here. It is read only when ``f.min_atr_pct``
    is above 0.

    The price floor uses ``panel.raw_close`` where it has a value and the adjusted close
    where it does not (cell by cell); with no ``raw_close`` it uses the adjusted close.
    """
    close = panel.close
    raw = panel.raw_close
    price = close if raw is None else raw.where(raw.notna(), close)
    dollar_volume = (close * panel.volume).to_numpy()
    window = f.dollar_volume_window
    history = np.zeros(close.shape)  # bars in the current segment, up to and including the row
    median_dv = np.full(close.shape, np.nan)  # strict: NaN until ``window`` bars in the segment

    for j, symbol in enumerate(close.columns):
        for seg in segments(close[symbol], f.max_gap_sessions):
            rows = close.index.get_indexer(seg.index)
            history[rows, j] = np.arange(1, len(rows) + 1)
            median_dv[rows, j] = (
                pd.Series(dollar_volume[rows, j]).rolling(window, min_periods=window).median()
            ).to_numpy()

    tradeable = np.array([kinds.get(str(symbol)) in TRADEABLE_KINDS for symbol in close.columns])
    ok = (
        close.notna().to_numpy()
        & (price.to_numpy() >= f.min_price)
        & (median_dv >= f.min_median_dollar_volume)
        & (history >= f.min_history_sessions)
        & tradeable[np.newaxis, :]
    )
    if f.min_atr_pct > 0.0:
        a = atr(panel, ATR_WINDOW) if atr14 is None else atr14
        a = a.reindex(index=close.index, columns=close.columns)
        with np.errstate(divide="ignore", invalid="ignore"):
            moves = a.to_numpy() / close.to_numpy()
        ok &= moves >= f.min_atr_pct  # a NaN ratio (warm-up, no bar) compares False
    return pd.DataFrame(ok, index=close.index, columns=close.columns)
