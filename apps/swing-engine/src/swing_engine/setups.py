"""Breakout and pullback setup candidates, one long frame per setup.

A candidate is a (date, symbol) where a leader meets a setup's entry-idea rule on that
session's close. It carries the buy-stop trigger and protective stop the next session would
use. Nothing here looks past the row's own date: the indicators already include today's bar
and are strict (NaN until their window is full), and a NaN in any input the rule or the
levels need means no candidate for that (date, symbol).

The windows behind ``Indicators`` (50-session highs, EMA20, 5-session low) are fixed, so a
setup whose params ask for a different window raises instead of quietly using the wrong one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from swing_engine.config import BreakoutParams, PullbackParams, SetupParams
from swing_engine.indicators import Indicators
from swing_engine.panel import Panel

CANDIDATE_COLUMNS = ["date", "symbol", "setup", "version", "trigger", "stop", "atr14", "rs_pct"]

_HIGH_WINDOW = 50  # Indicators.high_max50 / close_max50
_EMA_WINDOW = 20  # Indicators.ema20
_LOW_WINDOW = 5  # Indicators.low_min5


def _require_window(name: str, value: int, fixed: int) -> None:
    if value != fixed:
        raise ValueError(
            f"{name}={value} is unsupported: Indicators carry a fixed {fixed}-bar window"
        )


def _long(
    panel: Panel,
    mask: pd.DataFrame,
    *,
    setup: str,
    version: str,
    trigger: pd.DataFrame,
    stop: pd.DataFrame,
    atr14: pd.DataFrame,
    rs_pct: pd.DataFrame,
) -> pd.DataFrame:
    """Stack the True cells of ``mask`` into a candidate frame sorted by date, then symbol."""
    keep = mask & trigger.notna() & stop.notna() & atr14.notna() & rs_pct.notna()
    rows, cols = np.nonzero(keep.to_numpy())
    symbols = np.asarray(panel.close.columns, dtype=object)[cols]
    out = pd.DataFrame(
        {
            "date": panel.dates()[rows],
            "symbol": symbols,
            "setup": np.full(len(rows), setup, dtype=object),
            "version": np.full(len(rows), version, dtype=object),
            "trigger": trigger.to_numpy()[rows, cols],
            "stop": stop.to_numpy()[rows, cols],
            "atr14": atr14.to_numpy()[rows, cols],
            "rs_pct": rs_pct.to_numpy()[rows, cols],
        },
        columns=CANDIDATE_COLUMNS,
    )
    return out.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)


def _aligned(
    panel: Panel, leaders: pd.DataFrame, rs_pct: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``leaders`` (False where missing) and ``rs_pct`` (NaN where missing) on the panel's labels."""
    close = panel.close
    return (
        leaders.reindex(index=close.index, columns=close.columns, fill_value=False),
        rs_pct.reindex(index=close.index, columns=close.columns),
    )


def breakout_candidates(
    panel: Panel,
    ind: Indicators,
    leaders: pd.DataFrame,
    rs_pct: pd.DataFrame,
    p: BreakoutParams,
) -> pd.DataFrame:
    """Leaders within ``near_high_pct`` of their 50-close high with contracting volatility.

    Rule: leader, ``close >= (1 - near_high_pct) * close_max50`` and
    ``atr10 / atr50 <= contraction_max``. Trigger = ``high_max50 + trigger_atr * atr14`` and
    stop = ``trigger - stop_atr * atr14``.
    """
    _require_window("high_window", p.high_window, _HIGH_WINDOW)
    leaders, rs_pct = _aligned(panel, leaders, rs_pct)
    near_high = panel.close >= (1.0 - p.near_high_pct) * ind.close_max50
    contracted = ind.atr10 / ind.atr50 <= p.contraction_max
    trigger = ind.high_max50 + p.trigger_atr * ind.atr14
    stop = trigger - p.stop_atr * ind.atr14
    return _long(
        panel,
        leaders & near_high & contracted,
        setup="breakout",
        version=p.version,
        trigger=trigger,
        stop=stop,
        atr14=ind.atr14,
        rs_pct=rs_pct,
    )


def pullback_candidates(
    panel: Panel,
    ind: Indicators,
    leaders: pd.DataFrame,
    rs_pct: pd.DataFrame,
    p: PullbackParams,
) -> pd.DataFrame:
    """Leaders that are short-term oversold: RSI(2) low, or the close at or under the EMA20.

    Rule: leader and (``rsi2 <= rsi2_max`` or ``close <= ema20``); both indicators must be
    known. Trigger = ``high + trigger_atr * atr14``. The raw stop sits ``stop_buffer_atr``
    under the 5-session low, and its distance below the trigger is clamped to
    ``[min_stop_atr, max_stop_atr]`` ATR before the final stop is set.
    """
    _require_window("ema_window", p.ema_window, _EMA_WINDOW)
    _require_window("low_window", p.low_window, _LOW_WINDOW)
    leaders, rs_pct = _aligned(panel, leaders, rs_pct)
    known = ind.rsi2.notna() & ind.ema20.notna()
    oversold = (ind.rsi2 <= p.rsi2_max) | (panel.close <= ind.ema20)
    trigger = panel.high + p.trigger_atr * ind.atr14
    raw_stop = ind.low_min5 - p.stop_buffer_atr * ind.atr14
    # np.maximum / np.minimum carry a NaN through (DataFrame.clip would ignore a NaN bound).
    distance = np.minimum(
        np.maximum(trigger - raw_stop, p.min_stop_atr * ind.atr14), p.max_stop_atr * ind.atr14
    )
    return _long(
        panel,
        leaders & known & oversold,
        setup="pullback",
        version=p.version,
        trigger=trigger,
        stop=trigger - distance,
        atr14=ind.atr14,
        rs_pct=rs_pct,
    )


def candidates_for(
    panel: Panel,
    ind: Indicators,
    leaders: pd.DataFrame,
    rs_pct: pd.DataFrame,
    params: SetupParams,
) -> pd.DataFrame:
    """Candidates for whichever setup ``params`` belongs to (:data:`CANDIDATE_COLUMNS`)."""
    if isinstance(params, BreakoutParams):
        return breakout_candidates(panel, ind, leaders, rs_pct, params)
    if isinstance(params, PullbackParams):
        return pullback_candidates(panel, ind, leaders, rs_pct, params)
    raise TypeError(f"unsupported setup params: {type(params).__name__}")
