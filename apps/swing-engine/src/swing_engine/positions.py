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

**The nightly update** (:func:`nightly_update`) is what the nightly job stores for each open
position, built on :func:`position_action`:

* A position already advised for the session (``last_action_on`` on or after it) keeps its stored
  action, so a second run of the same session says the same thing. Re-testing tonight's low
  against the stop the first run raised would turn a raise into a stop hit.
* A position bought before a split is first rescaled to the adjusted prices the bars use
  (:func:`split_ratio`, :func:`rescaled`); the bars are restated after every split, the price the
  user paid is not, and without this a 4:1 split reads as a stop hit.
* The rules are then walked over every session since the position was last advised, oldest
  first, all from tonight's context (:func:`walk_action`). A stop hit on a day the job did not run
  (the Mac slept, or a run failed) is the action, led by that day, instead of tonight's hold; a
  stop raised on such a day carries into tonight and is told even when tonight would only hold.
  Raised stops are kept to the cent, the stop the user is told.
* A position never advised (``last_action_on`` None, a trade just recorded) is judged on
  tonight's session alone, with no walk back to ``opened_on``: the user records the stop as it is
  today, and testing it against the lows since the open would report stop hits from before ATLAS
  knew of the position.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

import pandas as pd

from swing_engine.config import BreakoutParams
from swing_engine.execution import _ratchet
from swing_engine.narrate import day_label
from swing_engine.scan import ScanContext

DEFAULT_TRAIL_ATR = BreakoutParams().trail_atr
"""The chandelier multiple for a position saved without one (the breakout setup's)."""

MIN_RAISE = 0.01
"""A trail must clear the current stop by at least a cent to be worth telling the user about."""

_CENT_TOLERANCE = 1e-9
"""Floating point slack: ``101.01 - 6`` is 95.00999999999999, which is still a cent over 95."""

SPLIT_BAND = (0.75, 1.33)
"""A position whose opening-day adjusted close is within this ratio of its entry was not split."""

SPLIT_RATIOS = (1 / 10, 1 / 8, 1 / 5, 1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 2, 2, 3, 4, 5, 8, 10)
"""The split ratios a position is snapped to (adjusted price / price paid)."""


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


def raise_stop_text(stop: float) -> str:
    """The instruction for a raised trailing stop, as the user is told it."""
    return f"raise stop to {stop:.2f} (trail)"


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


def position_action(
    pos: Mapping[str, Any], ctx: ScanContext, max_hold: int = 30, *, check_stop: bool = True
) -> Action:
    """The action for one position as of ``ctx.session``, by the rules in the module docstring.

    ``pos`` is a row from ``product_store.list_positions``: ``id, symbol, opened_on,
    initial_stop, current_stop`` and ``trail_atr`` (None means the default of 3.0). Prices are
    on the adjusted scale the panel uses. ``check_stop=False`` skips rule 3 for a stop the user
    set after the session's close (an edit tonight): it was not working during the session, so
    the session's low says nothing about it.
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
    if check_stop and low_d <= stop:
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
        return act(ActionKind.RAISE_STOP, new, raise_stop_text(new))

    # 6. Nothing to do.
    return act(ActionKind.HOLD, stop, f"hold, stop {stop:.2f}")


# --- the nightly update ----------------------------------------------------------------


def current_stop(pos: Mapping[str, Any]) -> float:
    """The stop in force: ``current_stop``, or the initial stop when none was saved."""
    stop = pos.get("current_stop")
    return float(pos["initial_stop"] if stop is None else stop)


def open_risk(pos: Mapping[str, Any]) -> float:
    """What a position still has at stake if it were stopped out now: ``shares x max(0, entry -
    current stop)``, never negative (a stop raised above the entry risks nothing)."""
    return float(pos["shares"]) * max(0.0, float(pos["entry_price"]) - current_stop(pos))


def split_ratio(pos: Mapping[str, Any], ctx: ScanContext) -> float:
    """The factor from the price the user paid to tonight's adjusted prices, 1.0 for no split.

    It is the adjusted close on ``opened_on`` over ``entry_price``. Inside :data:`SPLIT_BAND` it is
    an ordinary day's move from the fill and 1.0 is returned; outside it, it is snapped to the
    nearest of :data:`SPLIT_RATIOS` (nearest by ratio, so 7 snaps to 8 and 0.3 to 1/3). When the
    symbol has no bar on ``opened_on`` (or is not in the panel, or the entry is not positive)
    nothing can be measured: 1.0.
    """
    symbol = str(pos["symbol"])
    entry = float(pos["entry_price"])
    opened = pd.Timestamp(pos["opened_on"]).normalize()
    close = ctx.panel.close
    if symbol not in close.columns or opened not in close.index or not entry > 0:
        return 1.0
    adjusted = float(close.at[opened, symbol])
    if not (math.isfinite(adjusted) and adjusted > 0):
        return 1.0
    ratio = adjusted / entry
    low, high = SPLIT_BAND
    if low <= ratio <= high:
        return 1.0
    return float(min(SPLIT_RATIOS, key=lambda r: abs(math.log(ratio / r))))


def rescaled(pos: Mapping[str, Any], ratio: float) -> dict[str, float]:
    """The position's prices times ``ratio`` and its shares over it: the same money at risk."""
    fields = {
        "entry_price": float(pos["entry_price"]) * ratio,
        "initial_stop": float(pos["initial_stop"]) * ratio,
        "shares": float(pos["shares"]) / ratio,
    }
    if pos.get("current_stop") is not None:
        fields["current_stop"] = float(pos["current_stop"]) * ratio
    return fields


def _at(ctx: ScanContext, session: pd.Timestamp) -> ScanContext:
    """``ctx`` read as of an earlier ``session``.

    :func:`position_action` reads the bars up to its session only and indicators are causal, so
    tonight's context answers for any session it contains, exactly as that night's would.
    """
    return dataclasses.replace(ctx, session=session)


def _to_the_cent(action: Action) -> Action:
    if action.kind is ActionKind.RAISE_STOP and action.stop is not None:
        return dataclasses.replace(action, stop=round(action.stop, 2))
    return action


def _unadvised_sessions(pos: Mapping[str, Any], ctx: ScanContext) -> list[pd.Timestamp]:
    """The context's sessions before tonight that the position has not been advised for yet.

    None for a position never advised: it is judged on tonight alone (see the module docstring).
    """
    last = pos.get("last_action_on")
    if last is None:
        return []
    d = pd.Timestamp(ctx.session).normalize()
    days = pd.DatetimeIndex(ctx.panel.close.index)
    after = days > pd.Timestamp(last).normalize()
    return [s for s in days[after] if s < d]


def walk_action(
    pos: Mapping[str, Any], ctx: ScanContext, max_hold: int = 30, *, check_stop: bool = True
) -> tuple[Action, float]:
    """Tonight's action and the stop to keep, after every session since the last advice.

    The rules of :func:`position_action` run on each session after ``last_action_on`` (none when
    the position was never advised) and then on ``ctx.session``, all read from ``ctx``. A stop
    hit on an earlier session ends the walk and is the action, its text led by the day
    (``Fri 16 Oct: stop hit, exit (stop 95.00)``). A stop raised on an earlier session carries
    forward: if tonight would only hold, the action is to raise the stop to it, and a max-hold or
    no-data action names it too. With nothing to walk this is :func:`position_action` at the
    session. A raised stop is kept to the cent; the returned stop is the one to save (the stop
    in force, also for ``NO_DATA``). ``check_stop`` applies to tonight's session only.
    """
    state = dict(pos)
    for session in _unadvised_sessions(pos, ctx):
        earlier = _to_the_cent(position_action(state, _at(ctx, session), max_hold))
        if earlier.kind is ActionKind.STOP_HIT:
            text = f"{day_label(session.date())}: {earlier.text}"
            return dataclasses.replace(earlier, text=text), current_stop(state)
        if earlier.kind is ActionKind.RAISE_STOP and earlier.stop is not None:
            state["current_stop"] = earlier.stop
    action = _to_the_cent(position_action(state, ctx, max_hold, check_stop=check_stop))
    if action.kind is ActionKind.RAISE_STOP and action.stop is not None:
        return action, action.stop
    carried = current_stop(state)
    if carried > current_stop(pos):
        told = raise_stop_text(carried)
        if action.kind is ActionKind.HOLD:
            return dataclasses.replace(action, kind=ActionKind.RAISE_STOP, text=told), carried
        if action.kind in (ActionKind.MAX_HOLD, ActionKind.NO_DATA):
            return dataclasses.replace(action, text=f"{action.text}; {told}"), carried
    return action, carried


def stored_action(pos: Mapping[str, Any]) -> Action | None:
    """The action saved with the position (``last_action*`` and the stop), or None if it has none.

    The stop is the saved ``current_stop``, except for ``NO_DATA``, which vouches for none.
    """
    kind, text = pos.get("last_action"), pos.get("last_action_text")
    if kind not in {k.value for k in ActionKind} or text is None:
        return None
    action_kind = ActionKind(kind)
    stop = None if action_kind is ActionKind.NO_DATA else current_stop(pos)
    return Action(str(pos["id"]), str(pos["symbol"]), action_kind, stop, str(text))


@dataclass(frozen=True)
class PositionUpdate:
    """What the nightly job does with one position.

    ``action`` is tonight's advice and ``fields`` the columns to save (empty when the position was
    already advised for the session, which ``reused`` says). ``ratio`` is the split factor applied
    (1.0 for none).
    """

    action: Action
    fields: dict[str, Any] = field(default_factory=dict)
    ratio: float = 1.0
    reused: bool = False


def nightly_update(
    pos: Mapping[str, Any], ctx: ScanContext, max_hold: int = 30, *, check_stop: bool = True
) -> PositionUpdate:
    """The advice to store for one open position after ``ctx.session``'s close.

    See "The nightly update" in the module docstring. ``fields`` holds, when the position is
    advised now: the rescaled ``entry_price, initial_stop, current_stop, shares`` after a split,
    ``last_action, last_action_text, last_action_on`` (the session) and ``current_stop`` when the
    stop to keep changed. A split's action text starts ``adjusted for a split (xR); `` (with a
    multiplication sign). ``check_stop=False`` is for a stop edited after the close (see
    :func:`position_action`); the nightly job always checks it.
    """
    d = pd.Timestamp(ctx.session).date()
    last: date | None = pos.get("last_action_on")
    if last is not None and pd.Timestamp(last).date() >= d:
        kept = stored_action(pos)
        if kept is not None:
            return PositionUpdate(kept, reused=True)
    ratio = split_ratio(pos, ctx)
    fields: dict[str, Any] = {}
    if ratio != 1.0:
        fields = dict(rescaled(pos, ratio))
        pos = {**pos, **fields}
    action, stop = walk_action(pos, ctx, max_hold, check_stop=check_stop)
    if ratio != 1.0:
        text = f"adjusted for a split (\u00d7{ratio:g}); {action.text}"
        action = dataclasses.replace(action, text=text)
    fields.update(last_action=action.kind.value, last_action_text=action.text, last_action_on=d)
    if stop != current_stop(pos):
        fields["current_stop"] = stop
    return PositionUpdate(action, fields, ratio)
