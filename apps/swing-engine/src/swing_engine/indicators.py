"""Strict daily indicators, computed per symbol and per contiguous segment.

Every function returns a frame shaped like its input panel. A value is NaN until the
window is full (``min_periods=n``), and a gap of more than ``MAX_GAP`` sessions in a
symbol's bars starts a new segment, so a reused ticker never blends old and new
prices. A missing day is never filled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_engine.panel import Panel, segments

MAX_GAP = 10


def _per_segment(x: pd.DataFrame, fn: Callable[[pd.Series], pd.Series]) -> pd.DataFrame:
    """Apply ``fn`` to each contiguous segment of each column, then realign to ``x``."""
    cols: dict[str, pd.Series] = {}
    for col in x.columns:
        parts = [fn(seg) for seg in segments(x[col], MAX_GAP)]
        cols[col] = (
            pd.concat(parts).reindex(x.index)
            if parts
            else pd.Series(np.nan, index=x.index, dtype="float64")
        )
    return pd.DataFrame(cols, index=x.index, columns=x.columns, dtype="float64")


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def sma(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return _per_segment(x, lambda s: s.rolling(n, min_periods=n).mean())


def ema(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return _per_segment(x, lambda s: s.ewm(span=n, adjust=False, min_periods=n).mean())


def wilder(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """Wilder's smoothing (an EMA with ``alpha = 1/n``)."""
    return _per_segment(x, lambda s: _wilder(s, n))


def rolling_max(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return _per_segment(x, lambda s: s.rolling(n, min_periods=n).max())


def rolling_min(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return _per_segment(x, lambda s: s.rolling(n, min_periods=n).min())


def true_range(p: Panel) -> pd.DataFrame:
    """Wilder's true range. The first bar of a segment has no prior close, so it is high - low."""
    cols: dict[str, pd.Series] = {}
    for col in p.close.columns:
        parts = []
        for seg in segments(p.close[col], MAX_GAP):
            high = p.high[col].reindex(seg.index)
            low = p.low[col].reindex(seg.index)
            prev = seg.shift(1)
            parts.append(
                pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1)
                .max(axis=1, skipna=True)
                .where(high.notna() & low.notna())
            )
        cols[col] = (
            pd.concat(parts).reindex(p.close.index)
            if parts
            else pd.Series(np.nan, index=p.close.index, dtype="float64")
        )
    return pd.DataFrame(cols, index=p.close.index, columns=p.close.columns, dtype="float64")


def atr(p: Panel, n: int) -> pd.DataFrame:
    return wilder(true_range(p), n)


def rsi(close: pd.DataFrame, n: int) -> pd.DataFrame:
    """Wilder's RSI. A window with no movement at all reads 50 (neutral)."""

    def one(s: pd.Series) -> pd.Series:
        delta = s.diff()
        gain = _wilder(delta.clip(lower=0.0), n)
        loss = _wilder((-delta).clip(lower=0.0), n)
        total = gain + loss
        out = 100.0 * gain / total.where(total > 0.0)
        return out.where(~(total == 0.0), 50.0)

    return _per_segment(close, one)


@dataclass(frozen=True)
class Indicators:
    sma50: pd.DataFrame
    sma200: pd.DataFrame
    ema20: pd.DataFrame
    atr10: pd.DataFrame
    atr14: pd.DataFrame
    atr50: pd.DataFrame
    rsi2: pd.DataFrame
    high_max50: pd.DataFrame
    close_max50: pd.DataFrame
    close_max252: pd.DataFrame
    low_min5: pd.DataFrame


def compute_indicators(p: Panel) -> Indicators:
    tr = true_range(p)
    return Indicators(
        sma50=sma(p.close, 50),
        sma200=sma(p.close, 200),
        ema20=ema(p.close, 20),
        atr10=wilder(tr, 10),
        atr14=wilder(tr, 14),
        atr50=wilder(tr, 50),
        rsi2=rsi(p.close, 2),
        high_max50=rolling_max(p.high, 50),
        close_max50=rolling_max(p.close, 50),
        close_max252=rolling_max(p.close, 252),
        low_min5=rolling_min(p.low, 5),
    )
