"""Plain-English copy for a trade idea: why it was picked and what cancels it.

Both strings come from fixed templates filled with the idea's own numbers and the scan's frames
(``ScanContext``), never from a model, and they describe the rule that fired, not an outcome.
They promise nothing: no "will", no "guaranteed", no expected profit.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from swing_engine.config import PullbackParams, SetupParams
from swing_engine.scan import Idea, ScanContext

SETUP_LABEL = {"breakout": "Breakout", "pullback": "Pullback"}
"""How a setup family is named on screen."""


def why(idea: Idea, ctx: ScanContext, params: SetupParams | None = None) -> str:
    """Why this stock is on the list, in two or three sentences.

    ``params`` is the idea's setup parameters when they are not the code defaults; a pullback
    names its oversold 2-day RSI only when that RSI is within the setup's ``rsi2_max``, and
    otherwise says the close is under its 20-day average.
    """
    if idea.setup == "breakout":
        return _why_breakout(idea, ctx)
    if idea.setup == "pullback":
        return _why_pullback(idea, ctx, params if isinstance(params, PullbackParams) else None)
    raise ValueError(f"no narration for setup {idea.setup!r}")


def cancels(idea: Idea, target_session: date, params: SetupParams) -> str:
    """What cancels the idea before entry and what ends the trade after it.

    ``target_session`` is the session the buy-stop is live for. ``params`` supplies the trail and
    the longest hold, which differ between setup versions.
    """
    return (
        f"Cancelled if it doesn't trade above {idea.trigger:.2f} on {day_label(target_session)}, "
        "or if it opens more than one ATR above that. "
        f"After entry, the trade is wrong below {idea.stop:.2f}; "
        f"the stop then trails {params.trail_atr:.1f} ATR under the highest close, "
        f"and the trade ends after {params.max_hold} sessions."
    )


def _why_breakout(idea: Idea, ctx: ScanContext) -> str:
    gap = _gap_pct(ctx.panel.close, ctx.ind.close_max50, idea.symbol, ctx.session)
    quiet = _cell(ctx.ind.atr10, idea.symbol, ctx.session) / _cell(
        ctx.ind.atr50, idea.symbol, ctx.session
    )
    return (
        f"{idea.symbol} is in a long-term uptrend (above its 50- and 200-day averages) and "
        f"stronger than {idea.rs_pct:.0f}% of US stocks over the past year. "
        f"It closed within {gap:.1f}% of its 50-day high after a quiet stretch "
        f"(recent daily range {quiet:.0%} of normal). "
        f"A move above {idea.trigger:.2f} next session would be a breakout."
    )


def _why_pullback(idea: Idea, ctx: ScanContext, params: PullbackParams | None) -> str:
    rsi2_max = (PullbackParams() if params is None else params).rsi2_max
    rsi2 = _cell(ctx.ind.rsi2, idea.symbol, ctx.session)
    detail = f"2-day RSI {rsi2:.0f}" if rsi2 <= rsi2_max else "closed below its 20-day average"
    return (
        f"{idea.symbol} is in a long-term uptrend and stronger than {idea.rs_pct:.0f}% of US "
        f"stocks, but has dipped over the last few days ({detail}). "
        f"Buying only above {idea.trigger:.2f}, today's high, waits for buyers to step back in."
    )


def _cell(frame: pd.DataFrame, symbol: str, session: pd.Timestamp) -> float:
    return float(frame.at[session, symbol])


def _gap_pct(close: pd.DataFrame, high: pd.DataFrame, symbol: str, session: pd.Timestamp) -> float:
    """How far the close sits under the 50-day closing high, in percent."""
    top = _cell(high, symbol, session)
    return (top - _cell(close, symbol, session)) / top * 100.0


def day_label(day: date) -> str:
    """``Mon 19 Oct`` (no zero padding, so portable)."""
    return f"{day:%a} {day.day} {day:%b}"
