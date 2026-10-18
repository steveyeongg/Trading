"""Point-in-time universe: which symbols may be scanned on each session.

A symbol is eligible at ``(d, s)`` only if every rule holds using data at or before ``d``:
it has a bar on ``d`` (a halted or not-yet-printed symbol is out, and yesterday's price is
never carried forward), it trades at or above the price floor, its 50-bar median dollar
volume clears the liquidity floor, its current segment is long enough, and its asset kind
is tradeable. Segments follow :func:`swing_engine.panel.segments`, so a reused ticker
restarts both the history count and the dollar-volume window, and a missing day is never
filled.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from swing_engine.config import UniverseFilter
from swing_engine.data.assets import TRADEABLE_KINDS, AssetKind
from swing_engine.panel import Panel, segments


def eligibility(panel: Panel, kinds: Mapping[str, AssetKind], f: UniverseFilter) -> pd.DataFrame:
    """Boolean frame shaped like ``panel.close``: True where the symbol may be scanned.

    ``kinds`` maps symbol to :class:`AssetKind`. A symbol with no entry (``SPY`` is a
    reference series, not a candidate) is never eligible.
    """
    close = panel.close
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
        & (close.to_numpy() >= f.min_price)
        & (median_dv >= f.min_median_dollar_volume)
        & (history >= f.min_history_sessions)
        & tradeable[np.newaxis, :]
    )
    return pd.DataFrame(ok, index=close.index, columns=close.columns)
