"""Statistics over a trade table from :func:`swing_engine.backtest.trades.simulate_candidates`.

Only closed trades are results: ``status == "closed"``, a DELISTED exit included. A trade that
never filled, or is still open, has no R multiple yet and is left out. R is the exit against the
actual fill in units of the actual fill's risk, so every number here is in R.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from backtest_service.metrics import expectancy, hit_rate, profit_factor


def _mean(values: npt.NDArray[np.float64]) -> float:
    return float(values.mean()) if values.size else math.nan


def _closed(trades: pd.DataFrame) -> pd.DataFrame:
    return trades.loc[trades["status"] == "closed"]


def trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """Win rate, average win and loss, expectancy, profit factor and holding period.

    ``avg_win_r`` is the mean R of trades above zero and ``avg_loss_r`` the mean of the rest,
    so a scratch trade counts as a loss. ``win_rate`` is the share above zero and
    ``profit_factor`` is gross gains over gross losses (``inf`` with no losing trade).
    ``avg_hold`` is the mean ``sessions_held``. With no closed trade ``n`` is 0 and everything
    else is NaN.
    """
    closed = _closed(trades)
    r = closed["r_multiple"].to_numpy(dtype="float64")
    return {
        "n": int(r.size),
        "win_rate": hit_rate(r),
        "avg_win_r": _mean(r[r > 0.0]),
        "avg_loss_r": _mean(r[r <= 0.0]),
        "expectancy_r": expectancy(r),
        "profit_factor": profit_factor(r) if r.size else math.nan,
        "avg_hold": _mean(closed["sessions_held"].to_numpy(dtype="float64")),
    }


def by_year(trades: pd.DataFrame) -> dict[int, dict[str, Any]]:
    """:func:`trade_stats` per calendar year of the entry, for years with a closed trade."""
    closed = _closed(trades)
    years = pd.DatetimeIndex(pd.to_datetime(closed["entry_date"])).year
    return {
        int(year): trade_stats(closed.loc[years == year]) for year in sorted(set(years.tolist()))
    }


def regime_on(trades: pd.DataFrame) -> pd.DataFrame:
    """The trades entered on a candidate whose market regime was not ``downtrend``."""
    return trades.loc[trades["regime"] != "downtrend"]
