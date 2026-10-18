"""The random-entry baseline: what the same exits earn on entries picked at random.

A setup only matters if its entries beat random entries that are managed the same way. For each
candidate date with k real candidates, a seed draws k symbols uniformly (without replacement) from
that date's leaders, enters each at the next session's open, and exits with the setup's own trail
and time stop. The initial stop sits ``D`` ATRs under the entry, where ``D`` is the median
``(trigger - stop) / atr14`` of that date's real candidates, so a random trade risks about what a
real one does. There is no gap filter: the entry is the next open, whatever it is.

A seed's value is the mean R over its closed trades. The caller decides which candidates (and so
which dates and how many draws) to compare against, for example only the non-downtrend ones.

Every draw is a deterministic function of its (date, symbol), so each distinct pair is simulated
once however many seeds pick it. The seeds are drawn twice, once to see which pairs any of them
needs and once to total each seed from the results, which keeps memory flat in the seed count.
"""

from __future__ import annotations

import math
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from backtest_service.metrics import expectancy

from swing_engine.backtest.trades import PanelBars, positions_by_key
from swing_engine.config import ExecutionParams, SetupParams
from swing_engine.execution import TrackStatus, simulate_trade
from swing_engine.indicators import Indicators
from swing_engine.panel import Panel


@dataclass(frozen=True)
class _Day:
    """One candidate date: ``k`` draws from ``n`` leaders, whose cells start at ``offset``."""

    offset: int
    n: int
    k: int


def random_entry_baseline(
    panel: Panel,
    ind: Indicators,
    leaders: pd.DataFrame,
    candidates: pd.DataFrame,
    params: SetupParams,
    ex: ExecutionParams,
    seeds: int = 100,
    delisted: AbstractSet[str] = frozenset(),
) -> npt.NDArray[np.float64]:
    """Expectancy (mean R) of random entries, one value per seed: an array of shape ``(seeds,)``.

    Seed ``s`` uses ``np.random.default_rng(s)`` and visits the candidate dates in order. On a
    date with k candidates it draws k of that date's ``leaders`` columns (in panel column order)
    with ``rng.choice(n, k, replace=False)``, or takes all n without touching the generator when
    n <= k. A draw is skipped when the next session has no open, or the stop is not a finite
    price below the entry. A seed with no closed trade is NaN.
    """
    bars = PanelBars(panel, ind)
    index = bars.index
    slip = ex.slippage_bps / 10_000.0
    entry_ex = ExecutionParams(slippage_bps=ex.slippage_bps, max_gap_atr=math.inf)
    lead = leaders.reindex(index=index, columns=bars.columns, fill_value=False).to_numpy(dtype=bool)

    rows = index.get_indexer(pd.DatetimeIndex(candidates["date"]))
    if (rows < 0).any():
        raise ValueError("candidates have dates that are not panel sessions")
    ratio = ((candidates["trigger"] - candidates["stop"]) / candidates["atr14"]).to_numpy(
        dtype="float64"
    )

    days: list[_Day] = []
    cell_row: list[npt.NDArray[np.intp]] = []
    cell_col: list[npt.NDArray[np.intp]] = []
    cell_stop: list[npt.NDArray[np.float64]] = []
    offset = 0
    for group in positions_by_key(rows):
        row = int(rows[group[0]])
        spread = ratio[group]
        spread = spread[np.isfinite(spread)]
        if row + 1 >= len(index) or spread.size == 0:
            continue  # no next session to enter on, or no usable stop distance
        cols = np.flatnonzero(lead[row])
        if cols.size == 0:
            continue
        entry = bars.open[row + 1, cols] * (1.0 + slip)
        stop = entry - float(np.median(spread)) * bars.atr[row, cols]
        usable = np.isfinite(entry) & (entry > 0.0) & np.isfinite(stop) & (stop < entry)
        days.append(_Day(offset, int(cols.size), len(group)))
        cell_row.append(np.full(cols.size, row))
        cell_col.append(cols)
        cell_stop.append(np.where(usable, stop, np.nan))
        offset += int(cols.size)

    out = np.full(seeds, np.nan)
    if not days:
        return out
    row_of = np.concatenate(cell_row)
    col_of = np.concatenate(cell_col)
    stop_of = np.concatenate(cell_stop)

    def draw(seed: int) -> npt.NDArray[np.intp]:
        """The cells seed ``seed`` picks, over all dates (ids into the arrays above)."""
        rng = np.random.default_rng(seed)
        picked = [
            d.offset + (np.arange(d.n) if d.n <= d.k else rng.choice(d.n, d.k, replace=False))
            for d in days
        ]
        return np.concatenate(picked)

    needed = np.zeros(offset, dtype=bool)
    for seed in range(seeds):
        needed[draw(seed)] = True
    needed &= np.isfinite(stop_of)

    r_of = np.full(offset, np.nan)
    cells = np.flatnonzero(needed)
    sessions = index.tolist()
    for group in positions_by_key(col_of[cells]):  # one frame per symbol
        col = int(col_of[cells[group[0]]])
        frame = bars.frame(col)
        is_delisted = str(bars.columns[col]) in delisted
        for cell in cells[group].tolist():
            trade = simulate_trade(
                frame,
                sessions[row_of[cell]],
                0.0,
                float(stop_of[cell]),
                params.trail_atr,
                params.max_hold,
                entry_ex,
                delisted=is_delisted,
            )
            if trade.status == TrackStatus.CLOSED and trade.r_multiple is not None:
                r_of[cell] = trade.r_multiple

    for seed in range(seeds):
        r = r_of[draw(seed)]
        out[seed] = expectancy(r[~np.isnan(r)])
    return out
