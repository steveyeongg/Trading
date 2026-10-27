"""Statistics over a trade table from :func:`swing_engine.backtest.trades.simulate_candidates`.

Only closed trades are results: ``status == "closed"``, a DELISTED exit included. A trade that
never filled, or is still open, has no R multiple yet and is left out. R is the exit against the
actual fill in units of the actual fill's risk, so every number here is in R.

**Distinct trades.** A stock that sits near its high (or stays oversold) is a candidate session
after session, and every triggered session is its own row. Expectancy and profit factor use all
of them (spec §7.8: every triggered idea, the same basis as the forward record), but a trade
count built on them overstates the evidence: ten overlapping trades on one stock are one bet.
``n_distinct`` counts only the closed trades entered while no earlier trade on the same symbol
was still open (see :func:`distinct_mask`), so no two counted trades ever overlap on a symbol.
The ship gate's trade floor reads ``n_distinct``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from backtest_service.metrics import expectancy, hit_rate, profit_factor

_OPEN_TO_THE_END = np.iinfo(np.int64).max


def _mean(values: npt.NDArray[np.float64]) -> float:
    return float(values.mean()) if values.size else math.nan


def _closed_mask(trades: pd.DataFrame) -> npt.NDArray[np.bool_]:
    closed: npt.NDArray[np.bool_] = (trades["status"] == "closed").to_numpy(dtype=bool)
    return closed


def distinct_mask(trades: pd.DataFrame) -> npt.NDArray[np.bool_]:
    """Per row of ``trades``: True for a trade entered while no earlier trade on its symbol was
    still open.

    "Earlier" is an earlier ``entry_date``, or the same date higher up the table. An earlier trade
    is still open at this one's entry unless its exit was known by then (``known_date <=
    entry_date``: exits are booked before entries on a session, as in the portfolio simulation),
    and one with no ``known_date`` (open at the end of the data) holds its symbol to the end.
    Every earlier trade counts, distinct or not. A row that never filled (no ``entry_date``) is
    False and blocks nothing. Otherwise the table's order does not matter. Needs ``symbol``,
    ``entry_date`` and ``known_date``.
    """
    entry = pd.to_datetime(trades["entry_date"]).to_numpy(dtype="datetime64[ns]")
    known = pd.to_datetime(trades["known_date"]).to_numpy(dtype="datetime64[ns]")
    out = np.zeros(len(trades), dtype=bool)
    rows = np.flatnonzero(~np.isnat(entry))
    if not rows.size:
        return out
    entered = entry[rows].astype("int64")
    held_until = np.where(np.isnat(known[rows]), _OPEN_TO_THE_END, known[rows].astype("int64"))
    symbols = trades["symbol"].to_numpy(dtype=object)[rows].astype(str)

    busy_symbol: str | None = None
    busy_until = 0
    for k in np.lexsort((rows, entered, symbols)).tolist():  # by symbol, then entry, then row
        if symbols[k] != busy_symbol:
            busy_symbol, busy_until = str(symbols[k]), int(entered[k])  # nothing earlier
        out[rows[k]] = int(entered[k]) >= busy_until
        busy_until = max(busy_until, int(held_until[k]))
    return out


def distinct_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """The rows of ``trades`` that :func:`distinct_mask` marks, in the table's order."""
    return trades.loc[distinct_mask(trades)]


def _stats(trades: pd.DataFrame, distinct: npt.NDArray[np.bool_]) -> dict[str, Any]:
    """:func:`trade_stats` with the distinct flags already worked out (on a wider table)."""
    closed = _closed_mask(trades)
    rows = trades.loc[closed]
    r = rows["r_multiple"].to_numpy(dtype="float64")
    return {
        "n": int(r.size),
        "n_distinct": int((closed & distinct).sum()),
        "win_rate": hit_rate(r),
        "avg_win_r": _mean(r[r > 0.0]),
        "avg_loss_r": _mean(r[r <= 0.0]),
        "expectancy_r": expectancy(r),
        "profit_factor": profit_factor(r) if r.size else math.nan,
        "avg_hold": _mean(rows["sessions_held"].to_numpy(dtype="float64")),
    }


def trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """Trade count, distinct trade count, win rate, average win and loss, expectancy, profit
    factor and holding period.

    ``n`` counts every closed trade and ``n_distinct`` the closed ones :func:`distinct_mask`
    marks (open trades still block later ones). ``avg_win_r`` is the mean R of trades above
    zero and ``avg_loss_r`` the mean of the rest, so a scratch trade counts as a loss.
    ``win_rate`` is the share above zero and ``profit_factor`` is gross gains over gross losses
    (``inf`` with no losing trade). ``avg_hold`` is the mean ``sessions_held``. With no closed
    trade ``n`` and ``n_distinct`` are 0 and everything else is NaN.
    """
    return _stats(trades, distinct_mask(trades))


def by_year(trades: pd.DataFrame) -> dict[int, dict[str, Any]]:
    """:func:`trade_stats` per calendar year of the entry, for years with a closed trade.

    Distinct trades are judged on the whole table, so a trade opened one year still blocks a
    re-entry on its symbol the next.
    """
    distinct = distinct_mask(trades)
    closed = _closed_mask(trades)
    years = pd.DatetimeIndex(pd.to_datetime(trades["entry_date"])).year.to_numpy()
    out: dict[int, dict[str, Any]] = {}
    for year in sorted({int(y) for y in years[closed]}):
        rows = closed & (years == year)
        out[year] = _stats(trades.loc[rows], distinct[rows])
    return out


def regime_on(trades: pd.DataFrame) -> pd.DataFrame:
    """The trades entered on a candidate whose market regime was not ``downtrend``."""
    return trades.loc[trades["regime"] != "downtrend"]
