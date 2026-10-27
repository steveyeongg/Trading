"""Relative strength and the leader gate shared by the breakout and pullback setups.

Everything is point-in-time: a value on date d uses only bars at or before d. Look-backs count
the symbol's own bars inside its current segment (see :func:`swing_engine.panel.segments`),
not panel rows, so a halted day does not shorten a window and a reused ticker never blends
the old company's history into the new one. A NaN in any input means "not a leader".
"""

from __future__ import annotations

import pandas as pd

from swing_engine.config import LeaderParams
from swing_engine.indicators import Indicators, per_segment

# (look-back in bars, weight): the most recent quarter counts double.
RS_HORIZONS = ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2))


def rs_score(close: pd.DataFrame) -> pd.DataFrame:
    """Weighted trailing return: ``0.4*r63 + 0.2*r126 + 0.2*r189 + 0.2*r252``.

    ``rN = close / close.shift(N) - 1`` over the symbol's own bars in its current segment.
    NaN until the segment holds 252 earlier bars, so a short history never ranks.
    """

    def one(s: pd.Series) -> pd.Series:
        score = pd.Series(0.0, index=s.index)
        for n, weight in RS_HORIZONS:
            score = score + weight * (s / s.shift(n) - 1.0)
        return score

    return per_segment(close, one)


def rs_percentile(score: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    """Per-date percentile rank (0 to 100) of ``score`` among the eligible symbols.

    ``rank(pct=True) * 100`` with average ranks for ties. A symbol that is not eligible, or
    whose score is NaN, gets NaN and is left out of everyone else's ranking.
    """
    ok = eligible.reindex(index=score.index, columns=score.columns, fill_value=False)
    return score.where(ok).rank(axis=1, pct=True) * 100.0


def leader_mask(
    close: pd.DataFrame,
    ind: Indicators,
    rs_pct: pd.DataFrame,
    eligible: pd.DataFrame,
    p: LeaderParams,
) -> pd.DataFrame:
    """True where a symbol is an eligible relative-strength leader in an established uptrend.

    All of: eligible; ``close > sma50 > sma200`` (strict); ``sma200`` above its value
    ``sma200_rising_lookback`` bars ago (strict); ``rs_pct >= rs_min_pct``; and
    ``close >= (1 - within_high_pct) * close_max252``.
    """
    ok = eligible.reindex(index=close.index, columns=close.columns, fill_value=False)
    rs = rs_pct.reindex(index=close.index, columns=close.columns)
    # SMA200 is warm at every bar of a segment after its 199th, so its own non-NaN values are
    # exactly those bars and the shift never reaches into another segment.
    sma200_then = per_segment(ind.sma200, lambda s: s.shift(p.sma200_rising_lookback))
    return (
        ok
        & (close > ind.sma50)
        & (ind.sma50 > ind.sma200)
        & (ind.sma200 > sma200_then)
        & (rs >= p.rs_min_pct)
        & (close >= (1.0 - p.within_high_pct) * ind.close_max252)
    )
