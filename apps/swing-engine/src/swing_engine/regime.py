"""Market regime from SPY, and market breadth across the eligible universe.

Both are point-in-time: the value on a date uses only data at or before that date.
"""

from __future__ import annotations

from enum import StrEnum

import pandas as pd

from swing_engine.indicators import sma


class Regime(StrEnum):
    UPTREND = "uptrend"
    MIXED = "mixed"
    DOWNTREND = "downtrend"


def market_regime(spy_close: pd.Series) -> pd.Series:
    """Classify each session from SPY's close against its 50- and 200-bar averages.

    UPTREND needs close > SMA200 and SMA50 > SMA200; DOWNTREND needs close < SMA200 and
    SMA50 < SMA200. Anything else is MIXED: a split signal, an exact tie, a session with no
    SPY bar, and the first 199 bars while SMA200 warms up. The result has the input's index.
    """
    frame = spy_close.to_frame("close")
    sma50 = sma(frame, 50)["close"]
    sma200 = sma(frame, 200)["close"]
    up = (spy_close > sma200) & (sma50 > sma200)
    down = (spy_close < sma200) & (sma50 < sma200)
    out = pd.Series(Regime.MIXED, index=spy_close.index, dtype=object, name="regime")
    out.loc[up] = Regime.UPTREND
    out.loc[down] = Regime.DOWNTREND
    return out


def breadth(close: pd.DataFrame, sma50: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series:
    """Percent (0 to 100) of eligible symbols closing above their SMA50, per session.

    Only eligible symbols count, in both the numerator and the denominator. A session with
    no eligible symbol is NaN, not 0: there is no market to describe.
    """
    eligible = eligible.reindex(index=close.index, columns=close.columns, fill_value=False)
    above = eligible & (close > sma50.reindex_like(close))
    n_eligible = eligible.sum(axis=1)
    return 100.0 * above.sum(axis=1) / n_eligible.where(n_eligible > 0)
