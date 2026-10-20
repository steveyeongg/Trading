"""Nightly actions for the positions the user actually took.

The user records a trade they placed themselves (``swing_positions``). After each close this
module reads the stored bars, never the network, and tells them what to do with it: hold, raise the
stop, exit because the stop was hit, consider exiting because the time limit is up, or look at the
broker because there is no price. It is a pure function of one position row and the scan context,
so running it twice gives the same answer, and it places no orders.

The rules run in this order at ``d = ctx.session`` and the first that applies wins:

1. **No bar for the symbol on ``d``** (delisted, halted, a mistyped symbol, or absent from the
   panel) is ``NO_DATA``. It comes first so a dead symbol is never reported as a quiet hold with
   a stop nobody is checking. The text names the last day the symbol had a close, or the day the
   position was opened if it never had one.
2. **Opened on ``d``** is ``HOLD`` at the current stop. The day's low may have printed before the
   fill, so it says nothing about the stop, and the trail starts moving the next night. A position
   dated after ``d`` (a back-dated scan) is held the same way, since the trade did not exist yet.
3. **The day's low reached the stop** (``low <= current_stop``) is ``STOP_HIT``. The stop in force
   at the open is checked before the day's close is allowed to move it, the order the one exit
   model uses (:func:`~swing_engine.execution.simulate_trade`).
4. **``max_hold`` sessions with a bar** from the open through ``d`` inclusive is ``MAX_HOLD``. A
   day without a bar is not a session held, and an open date that is not a session counts from the
   first session after it.
5. **The chandelier trail**: the highest close since the open (the open day and ``d`` included)
   minus ``trail_atr`` times the ATR14 on ``d``, never lower than the current stop. It is the
   formula execution applies each night (:func:`~swing_engine.execution._ratchet`), so a user who
   follows every raise holds the stop the backtests assume, to the cent, while the ATR is steady.
   It reads tonight's ATR only, where the backtest also keeps every earlier night's candidate and
   moves the trail on the entry session, so after the ATR has risen the user's trail can sit under
   the backtest's, never over it. A raise of at least one cent is ``RAISE_STOP``; a missing ATR
   leaves the trail where it was.
6. Otherwise ``HOLD`` at the current stop.

``Action.stop`` is the stop to have working after tonight: the new trail for ``RAISE_STOP``, the
current stop for ``HOLD``, ``STOP_HIT`` and ``MAX_HOLD``, and ``None`` for ``NO_DATA``, where no
stop can be vouched for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pandas as pd

from swing_engine.config import BreakoutParams
from swing_engine.execution import _ratchet
from swing_engine.scan import ScanContext

DEFAULT_TRAIL_ATR = BreakoutParams().trail_atr
"""The chandelier multiple for a position saved without one (the breakout setup's)."""

MIN_RAISE = 0.01
"""A trail must clear the current stop by at least a cent to be worth telling the user about."""

_CENT_TOLERANCE = 1e-9
"""Floating point slack: ``101.01 - 6`` is 95.00999999999999, which is still a cent over 95."""


class ActionKind(StrEnum):
    HOLD = "hold"
    RAISE_STOP = "raise_stop"
    STOP_HIT = "stop_hit"
    MAX_HOLD = "max_hold"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class Action:
    """What to do with one position tonight. ``stop`` is the stop to have working afterwards."""

    position_id: str
    symbol: str
    kind: ActionKind
    stop: float | None
    text: str


def _column(frame: pd.DataFrame, symbol: str, d: pd.Timestamp) -> pd.Series | None:
    """The symbol's column up to and including ``d``, or None when the panel lacks the symbol."""
    if symbol not in frame.columns:
        return None
    return frame[symbol].loc[:d]


def _value_on(series: pd.Series | None, d: pd.Timestamp) -> float:
    """The value on ``d``, or NaN when there is no such row (or no series at all)."""
    if series is None or d not in series.index:
        return float("nan")
    return float(series.at[d])


def position_action(pos: Mapping[str, Any], ctx: ScanContext, max_hold: int = 30) -> Action:
    """The action for one position as of ``ctx.session``, by the rules in the module docstring.

    ``pos`` is a row from ``product_store.list_positions``: ``id, symbol, opened_on,
    initial_stop, current_stop`` and ``trail_atr`` (None means the default of 3.0). Prices are
    on the adjusted scale the panel uses.
    """
    position_id = str(pos["id"])
    symbol = str(pos["symbol"])
    d = pd.Timestamp(ctx.session).normalize()
    opened = pd.Timestamp(pos["opened_on"]).normalize()
    current = pos.get("current_stop")
    stop = float(pos["initial_stop"] if current is None else current)
    trail_atr = pos.get("trail_atr")
    trail = DEFAULT_TRAIL_ATR if trail_atr is None else float(trail_atr)

    def act(kind: ActionKind, at: float | None, text: str) -> Action:
        return Action(position_id, symbol, kind, at, text)

    closes = _column(ctx.panel.close, symbol, d)
    lows = _column(ctx.panel.low, symbol, d)
    close_d = _value_on(closes, d)
    low_d = _value_on(lows, d)

    # 1. No bar on the scan day.
    if closes is None or close_d != close_d or low_d != low_d:
        priced = closes.dropna() if closes is not None else None
        last = priced.index[-1] if priced is not None and len(priced) else opened
        return act(
            ActionKind.NO_DATA, None, f"no price data for {symbol} since {last.date().isoformat()}"
        )

    # 2. Opened today (or, for a back-dated scan, not yet).
    if opened >= d:
        return act(ActionKind.HOLD, stop, f"hold, stop {stop:.2f}")

    # 3. The stop in force at the open was reached.
    if low_d <= stop:
        return act(ActionKind.STOP_HIT, stop, f"stop hit, exit (stop {stop:.2f})")

    # 4. The time limit. Only sessions with a bar count.
    held = closes.loc[opened:].dropna()
    if len(held) >= max_hold:
        return act(
            ActionKind.MAX_HOLD, stop, f"max hold reached ({max_hold} sessions), consider exiting"
        )

    # 5. The chandelier trail, exactly as execution moves it.
    atr = _value_on(_column(ctx.ind.atr14, symbol, d), d)
    new = _ratchet(stop, float(held.max()), trail, atr)
    if new - stop >= MIN_RAISE - _CENT_TOLERANCE:
        return act(ActionKind.RAISE_STOP, new, f"raise stop to {new:.2f} (trail)")

    # 6. Nothing to do.
    return act(ActionKind.HOLD, stop, f"hold, stop {stop:.2f}")
