"""The eligibility checklist: for one stock, which universe, leader and setup rules it passes.

Each of the 14 rules is judged with the same formula the engine uses, on the same frames the scan
built (``ScanContext``), and carries the actual number next to the limit it was held to. So a tick
here agrees with the engine: a symbol passes rules 1 to 6 exactly when ``ctx.eligible`` says it is
tradable, rules 7 to 11 as well exactly when ``ctx.leaders`` says it is a leader, and a leader that
passes rule 12 and 13 (breakout) or rule 14 (pullback) is that setup's candidate.

* Rules 1 to 6 are :func:`swing_engine.universe.eligibility`, split into its parts. The price floor
  reads the raw close where the panel has one, else the adjusted close (cell by cell, as the engine
  does); history and the dollar-volume median count the symbol's own bars in its current segment.
* Rules 7 to 11 are :func:`swing_engine.leaders.leader_mask`; the relative-strength percentile is
  read from ``ctx.rs_pct``, which ranks only eligible symbols, so an ineligible stock has none.
* Rules 12 to 14 are :func:`swing_engine.setups.breakout_candidates` and ``pullback_candidates``.

A value that is unknown or NaN (no bar that day, an indicator still warming up, a symbol the data
has never seen) reads ``—`` and the rule fails, as it does in the engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from swing_engine.config import BreakoutParams, LeaderParams, PullbackParams, UniverseFilter
from swing_engine.data.assets import TRADEABLE_KINDS
from swing_engine.indicators import per_segment
from swing_engine.narrate import day_label
from swing_engine.panel import segments
from swing_engine.scan import ScanContext

UNKNOWN = "—"
"""What a value that is not known (or is NaN) reads."""

_NAN = float("nan")


@dataclass(frozen=True)
class CheckItem:
    """One rule: its plain-English label, whether the stock passes, its value and the limit."""

    rule: str
    passed: bool
    value: str
    threshold: str


def eligibility_checklist(
    ctx: ScanContext,
    symbol: str,
    universe: UniverseFilter,
    *,
    breakout: BreakoutParams | None = None,
    pullback: PullbackParams | None = None,
) -> list[CheckItem]:
    """The 14 rules for ``symbol`` on ``ctx.session``, universe first, then leader, then setups.

    ``universe`` must be the filter the context was built with. The leader gate is always the
    default :class:`LeaderParams` (``build_context`` uses nothing else); ``breakout`` and
    ``pullback`` default to the code parameters, and a caller scanning with a pre-registered
    version passes that version's parameters so the last three rules match its ideas.
    """
    leaders = LeaderParams()
    bp = BreakoutParams() if breakout is None else breakout
    pp = PullbackParams() if pullback is None else pullback
    s = ctx.session
    ind = ctx.ind

    def at(frame: pd.DataFrame | None) -> float:
        return _at(frame, symbol, s)

    close = at(ctx.panel.close)
    raw = at(ctx.panel.raw_close)
    # Price floor: the same price the engine tests, the raw (unadjusted) close where the panel has
    # one for the day, else the adjusted close. ``adjustment=all`` rewrites history on a split, so
    # only the floor reads raw prices (see ``universe.eligibility``).
    price = close if math.isnan(raw) else raw
    history, median_dv = _segment_stats(ctx, symbol, universe)
    atr_pct = _ratio(at(ind.atr14), close)
    kind = ctx.kinds.get(symbol)

    sma50, sma200 = at(ind.sma50), at(ind.sma200)
    sma200_then = _sma200_then(ctx, symbol, leaders)
    rs = at(ctx.rs_pct)
    off_52w = _drop(close, at(ind.close_max252))
    off_50d = _drop(close, at(ind.close_max50))
    contraction = _ratio(at(ind.atr10), at(ind.atr50))
    rsi2, ema20 = at(ind.rsi2), at(ind.ema20)
    pct = f"{universe.min_atr_pct * 100.0:g}%"

    return [
        CheckItem(
            f"Has a bar for {day_label(s)}",
            not math.isnan(close),
            _bar_value(ctx, symbol),
            "Yes",
        ),
        CheckItem(
            f"Price at least ${universe.min_price:g}",
            price >= universe.min_price,
            _dollars(price),
            _dollars(universe.min_price),
        ),
        _dollar_volume(universe, median_dv),
        _daily_range(universe, atr_pct, pct),
        CheckItem(
            f"At least {universe.min_history_sessions} sessions of history",
            history >= universe.min_history_sessions,
            UNKNOWN if math.isnan(history) else f"{int(history)}",
            f"{universe.min_history_sessions}",
        ),
        CheckItem(
            "Common stock (not an ETF or fund)",
            kind in TRADEABLE_KINDS,
            UNKNOWN if kind is None else str(kind.value),
            ", ".join(sorted(k.value for k in TRADEABLE_KINDS)),
        ),
        CheckItem("Close above 50-day average", close > sma50, _dollars(close), _dollars(sma50)),
        CheckItem(
            "50-day average above 200-day average",
            sma50 > sma200,
            _dollars(sma50),
            _dollars(sma200),
        ),
        CheckItem(
            f"200-day average rising (vs {leaders.sma200_rising_lookback} sessions ago)",
            sma200 > sma200_then,
            _dollars(sma200),
            _dollars(sma200_then),
        ),
        CheckItem(
            f"Stronger than {leaders.rs_min_pct:g}% of US stocks (relative strength)",
            rs >= leaders.rs_min_pct,
            _percent(rs),
            _percent(leaders.rs_min_pct),
        ),
        CheckItem(
            f"Within {leaders.within_high_pct * 100.0:g}% of 52-week high",
            close >= (1.0 - leaders.within_high_pct) * at(ind.close_max252),
            _percent(off_52w),
            _percent(leaders.within_high_pct * 100.0),
        ),
        CheckItem(
            f"Breakout: close within {bp.near_high_pct * 100.0:g}% of 50-day closing high",
            close >= (1.0 - bp.near_high_pct) * at(ind.close_max50),
            _percent(off_50d),
            _percent(bp.near_high_pct * 100.0),
        ),
        CheckItem(
            f"Breakout: quiet stretch (ATR10 / ATR50 at most {bp.contraction_max:g})",
            contraction <= bp.contraction_max,
            UNKNOWN if math.isnan(contraction) else f"{contraction:.2f}",
            f"{bp.contraction_max:.2f}",
        ),
        _pullback(pp, close, rsi2, ema20),
    ]


def dollar_volume_rule(ctx: ScanContext, symbol: str, universe: UniverseFilter) -> CheckItem:
    """Rule 3 alone, the dollar-volume floor, judged on ``ctx.panel`` and ``ctx.session``.

    The stock page uses it for a stock the cached panel lacks (below the liquidity prefilter),
    with a context whose panel holds that stock's own stored bars on the market's sessions.
    """
    _, median_dv = _segment_stats(ctx, symbol, universe)
    return _dollar_volume(universe, median_dv)


def _dollar_volume(universe: UniverseFilter, median_dv: float) -> CheckItem:
    return CheckItem(
        f"{universe.dollar_volume_window}-day median dollar volume at least "
        f"${universe.min_median_dollar_volume / 1e6:g}M",
        median_dv >= universe.min_median_dollar_volume,
        _millions(median_dv),
        _millions(universe.min_median_dollar_volume),
    )


def _daily_range(universe: UniverseFilter, atr_pct: float, pct: str) -> CheckItem:
    """The pinned-stock rule: ATR14 as a share of the close. Off (always passes) at 0."""
    label = f"Daily range at least {pct} of price (not pinned)"
    value = UNKNOWN if math.isnan(atr_pct) else f"{atr_pct * 100.0:.2f}%"
    if universe.min_atr_pct <= 0.0:
        return CheckItem(label, True, value, "not checked")
    return CheckItem(
        label, atr_pct >= universe.min_atr_pct, value, f"{universe.min_atr_pct * 100.0:.2f}%"
    )


def _pullback(p: PullbackParams, close: float, rsi2: float, ema20: float) -> CheckItem:
    """RSI(2) at or under its limit, or the close at or under the EMA20; both must be known."""
    known = not (math.isnan(rsi2) or math.isnan(ema20) or math.isnan(close))
    oversold = rsi2 <= p.rsi2_max or close <= ema20
    rsi_text = UNKNOWN if math.isnan(rsi2) else f"{rsi2:.1f}"
    nothing = math.isnan(rsi2) and math.isnan(close) and math.isnan(ema20)
    return CheckItem(
        f"Pullback: 2-day RSI at most {p.rsi2_max:g} or close below {p.ema_window}-day average",
        known and oversold,
        UNKNOWN if nothing else f"RSI {rsi_text}, close {_dollars(close)} vs avg {_dollars(ema20)}",
        f"RSI {p.rsi2_max:g} or lower, or close at or below avg",
    )


# ------------------------------------------------------------------------------------- readers


def _at(frame: pd.DataFrame | None, symbol: str, session: pd.Timestamp) -> float:
    """``frame[symbol]`` on ``session``, or NaN when the frame, symbol or session is missing."""
    if frame is None or symbol not in frame.columns or session not in frame.index:
        return _NAN
    value = frame.at[session, symbol]
    return _NAN if pd.isna(value) else float(value)


def _segment_stats(ctx: ScanContext, symbol: str, universe: UniverseFilter) -> tuple[float, float]:
    """Bars in the symbol's current segment and their 50-day median dollar volume, as the universe
    rule counts them; both NaN when the symbol has no bar on the session (the engine reads 0 and
    NaN there, and rule 1 already fails)."""
    panel = ctx.panel
    if symbol not in panel.close.columns:
        return _NAN, _NAN
    segs = segments(panel.close[symbol], universe.max_gap_sessions)
    if not segs or segs[-1].index[-1] != ctx.session:
        return _NAN, _NAN
    seg = segs[-1]
    window = universe.dollar_volume_window
    if len(seg) < window:
        return float(len(seg)), _NAN
    dollar_volume = (seg * panel.volume[symbol].reindex(seg.index)).to_numpy()[-window:]
    median = pd.Series(dollar_volume).rolling(window, min_periods=window).median().iloc[-1]
    return float(len(seg)), float(median)


def _sma200_then(ctx: ScanContext, symbol: str, leaders: LeaderParams) -> float:
    """The 200-day average ``sma200_rising_lookback`` of the symbol's own bars ago."""
    sma200 = ctx.ind.sma200
    if symbol not in sma200.columns:
        return _NAN
    then = per_segment(sma200[[symbol]], lambda s: s.shift(leaders.sma200_rising_lookback))
    return _at(then, symbol, ctx.session)


def _bar_value(ctx: ScanContext, symbol: str) -> str:
    """``Yes`` with a bar on the session, else ``No, last bar <day>``; ``—`` if never seen."""
    close = ctx.panel.close
    if symbol not in close.columns:
        return UNKNOWN
    if not pd.isna(_at(close, symbol, ctx.session)):
        return "Yes"
    last = close[symbol].last_valid_index()
    return UNKNOWN if last is None else f"No, last bar {day_label(pd.Timestamp(last))}"


# ----------------------------------------------------------------------------------- formatting


def _ratio(num: float, den: float) -> float:
    """``num / den``, NaN when either is NaN or the denominator is zero."""
    if math.isnan(num) or math.isnan(den) or den == 0.0:
        return _NAN
    return num / den


def _drop(close: float, high: float) -> float:
    """How far ``close`` sits under ``high``, in percent (0 at the high)."""
    ratio = _ratio(high - close, high)
    return ratio * 100.0


def _dollars(x: float) -> str:
    return UNKNOWN if np.isnan(x) else f"${x:,.2f}"


def _millions(x: float) -> str:
    return UNKNOWN if np.isnan(x) else f"${x / 1e6:,.1f}M"


def _percent(x: float) -> str:
    return UNKNOWN if np.isnan(x) else f"{x:.1f}%"
