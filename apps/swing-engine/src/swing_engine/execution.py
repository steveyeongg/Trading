"""The one fill and exit model for a next-session buy-stop swing trade.

A candidate that closes on ``signal_date`` becomes a buy-stop order for the next session at its
``trigger``, with a protective ``stop`` and a chandelier trail. The backtests, the random-entry
baseline (``trigger=0.0`` and an infinite ``max_gap_atr`` force a next-open entry) and nightly
forward tracking all call ``simulate_trade``, so the three can never disagree on a fill.

The model is daily-bar only and deliberately pessimistic where a bar cannot say what happened
first:

* Entry fills at ``max(open, trigger)`` plus slippage: the worse of a gap through the trigger
  and the trigger itself. A session that opens more than ``max_gap_atr`` ATRs (measured on the
  signal date) above the trigger is skipped instead of chased.
* On the entry session, a low at or through the initial stop is a stop-out at the stop. A bar
  cannot say whether the low came before or after the trigger was hit, so it assumes the worse.
* On later sessions the stop in force at the open is checked first. A gap down through it
  fills at the open, not at the stop.
* Only then does the close move the trail (``highest close - trail_atr * atr14``, never
  lowered), and only then does the time stop fire at that session's close.

``bars`` is a symbol's column of the panel: one row per panel session, with NaN prices on a
session where the symbol has no bar. That alignment is what keeps a trade inside one company's
history. The buy-stop is good for the next row only, so a NaN next row expires it. A held trade
skips a short run of NaN rows (a halt or a missing print: no stop check, no trail move, not a
held session) but a run longer than ``indicators.MAX_GAP`` is a segment break, where the old
history ends and a reused ticker may begin, so the trade closes at the last priced close.

The scan runs forward from the signal row and returns at the first exit, so the work is
proportional to the sessions the trade is alive, not to the length of ``bars``. Nothing looks
past the session being processed: a trade that closed on or before a session is identical
whatever bars follow it, and those later rows are never read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np
import pandas as pd

from swing_engine.config import ExecutionParams
from swing_engine.indicators import MAX_GAP

_REQUIRED = frozenset({"open", "high", "low", "close", "atr14"})


class TrackStatus(StrEnum):
    PENDING = "pending"  # no session after the signal date yet
    NOT_TRIGGERED = "not_triggered"  # the next session never reached the trigger
    SKIPPED_GAP = "skipped_gap"  # the next session opened too far above the trigger
    OPEN = "open"  # filled, no exit yet
    CLOSED = "closed"  # filled and exited


class ExitReason(StrEnum):
    STOP = "stop"  # the initial stop, never raised by the trail
    TRAIL = "trail"  # the stop after the trail had ratcheted above the initial stop
    MAX_HOLD = "max_hold"
    DELISTED = "delisted"  # the symbol's bars ended (flagged by the caller, or a segment break)


@dataclass(frozen=True)
class TradeOutcome:
    """Where one candidate stands.

    ``PENDING``, ``NOT_TRIGGERED`` and ``SKIPPED_GAP`` carry no fill: every entry, exit and
    ``r_multiple`` field is ``None`` and ``current_stop`` / ``highest_close`` are ``None``.
    ``OPEN`` has the fill plus ``current_stop`` and ``highest_close`` as of the last priced bar.
    ``CLOSED`` has every field.

    ``highest_close`` is the highest close over every session held, the exit session included,
    for ``OPEN`` and ``CLOSED`` alike, so a stop-out on the entry session reports that
    session's close. ``current_stop`` is the stop in force when the trade ended, which is
    the level that was hit for a ``STOP`` or ``TRAIL`` exit: a stop-out does not let that
    session's close lift the stop it was already below. ``sessions_held`` counts the sessions
    with a bar from the fill through the exit (or through the last priced bar while open), the
    entry session being 1. ``r_multiple`` is the exit against the actual fill, in units of the
    actual fill's risk: ``(exit - entry) / (entry - initial_stop)``.
    """

    status: TrackStatus
    entry_date: date | None
    entry_price: float | None
    initial_stop: float
    exit_date: date | None
    exit_price: float | None
    exit_reason: ExitReason | None
    r_multiple: float | None
    sessions_held: int
    current_stop: float | None
    highest_close: float | None


def _no_fill(status: TrackStatus, stop: float) -> TradeOutcome:
    return TradeOutcome(
        status=status,
        entry_date=None,
        entry_price=None,
        initial_stop=stop,
        exit_date=None,
        exit_price=None,
        exit_reason=None,
        r_multiple=None,
        sessions_held=0,
        current_stop=None,
        highest_close=None,
    )


def _ratchet(cur: float, highest_close: float, trail_atr: float, atr: float) -> float:
    """The trail after a close: never lower, and unchanged when there is no ATR to size it."""
    if math.isnan(atr):
        return cur
    return max(cur, highest_close - trail_atr * atr)


def simulate_trade(
    bars: pd.DataFrame,
    signal_date: date,
    trigger: float,
    stop: float,
    trail_atr: float,
    max_hold: int,
    ex: ExecutionParams,
    delisted: bool = False,
) -> TradeOutcome:
    """Run one candidate through the sessions after ``signal_date`` and report where it stands.

    ``bars`` is one symbol's rows, indexed by tz-naive session timestamps in ascending order,
    with ``open, high, low, close, atr14``, ending at the evaluation date (callers truncate).
    It must have a row on ``signal_date`` (a ``date`` or ``Timestamp``): the gap rule uses that
    day's ``atr14``. A session with no bar is a row of NaN prices, never an absent row. A row
    counts as priced when open, high, low and close are all present.

    The sequence, with ``slip = ex.slippage_bps / 10_000``, is fixed. On the row right after the
    signal: no trade (``NOT_TRIGGERED``) if it is unpriced; skip if its open is above
    ``trigger + max_gap_atr * atr14[signal_date]``; no trade if its high stayed under
    ``trigger``; fill at ``max(open, trigger) * (1 + slip)``; exit at ``stop * (1 - slip)`` if
    its low reached the stop; move the trail from its close; exit at the close if ``max_hold``
    is 1. On every later priced row: exit at ``min(open, current_stop) * (1 - slip)`` if the low
    reached the current stop; move the trail from the close; exit at ``close * (1 - slip)`` once
    ``max_hold`` sessions have been held. A NaN ``atr14`` on a later session leaves the trail
    where it was.

    Unpriced rows after the fill are skipped, up to ``MAX_GAP`` in a row. A longer run is a
    segment break and closes the trade as ``DELISTED`` at the last priced close, whatever
    ``delisted`` says. ``delisted=True`` says the bars end because the symbol stopped trading:
    an open trade at the end of ``bars`` then closes the same way instead of staying open.
    """
    if max_hold < 1:
        raise ValueError(f"max_hold must be at least 1, got {max_hold}")
    if not (math.isfinite(trigger) and math.isfinite(stop)):
        raise ValueError(f"trigger and stop must be finite, got trigger={trigger}, stop={stop}")
    if not (math.isfinite(trail_atr) and trail_atr >= 0):
        raise ValueError(f"trail_atr must be a finite number >= 0, got {trail_atr}")
    if not _REQUIRED.issubset(bars.columns):
        raise ValueError(f"bars is missing columns {sorted(_REQUIRED - set(bars.columns))}")
    index = bars.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None:
        raise ValueError("bars must be indexed by tz-naive session timestamps")
    sig = pd.Timestamp(signal_date).normalize()
    if sig.tzinfo is not None:
        raise ValueError("signal_date must be tz-naive")
    try:
        pos = index.get_loc(sig)
    except KeyError:
        raise ValueError(f"bars has no row on signal date {sig.date()}") from None
    if not isinstance(pos, int | np.integer):
        raise ValueError(f"bars has more than one row on signal date {sig.date()}")
    first = pos + 1
    if first >= len(index):
        return _no_fill(TrackStatus.PENDING, stop)

    stamps = index.asi8
    opens = bars["open"].to_numpy()
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    closes = bars["close"].to_numpy()
    atrs = bars["atr14"].to_numpy()
    slip = ex.slippage_bps / 10_000.0
    stop = float(stop)

    if stamps[first] <= stamps[pos]:
        raise ValueError("bars must be indexed by strictly ascending session dates")
    open1, high1, low1, close1 = (
        float(opens[first]),
        float(highs[first]),
        float(lows[first]),
        float(closes[first]),
    )
    # The buy-stop lives for one session: a session with no bar lets it lapse.
    if not (open1 == open1 and high1 == high1 and low1 == low1 and close1 == close1):
        return _no_fill(TrackStatus.NOT_TRIGGERED, stop)
    if math.isfinite(ex.max_gap_atr):
        signal_atr = float(atrs[pos])
        if math.isnan(signal_atr):
            raise ValueError(
                f"atr14 is NaN on signal date {sig.date()}, so the gap rule cannot be applied"
            )
        if open1 > trigger + ex.max_gap_atr * signal_atr:
            return _no_fill(TrackStatus.SKIPPED_GAP, stop)
    if high1 < trigger:
        return _no_fill(TrackStatus.NOT_TRIGGERED, stop)
    entry = max(open1, trigger) * (1.0 + slip)
    if not entry > stop:
        raise ValueError(f"stop {stop} must be below the entry fill {entry}")
    entry_date = index[first].date()

    def closed(
        at: int, raw_price: float, reason: ExitReason, held: int, cur: float, highest: float
    ) -> TradeOutcome:
        exit_price = raw_price * (1.0 - slip)
        return TradeOutcome(
            status=TrackStatus.CLOSED,
            entry_date=entry_date,
            entry_price=entry,
            initial_stop=stop,
            exit_date=index[at].date(),
            exit_price=exit_price,
            exit_reason=reason,
            r_multiple=(exit_price - entry) / (entry - stop),
            sessions_held=held,
            current_stop=cur,
            highest_close=highest,
        )

    held = 1
    highest_close = close1
    if low1 <= stop:
        return closed(first, stop, ExitReason.STOP, held, stop, highest_close)
    cur = _ratchet(stop, highest_close, trail_atr, float(atrs[first]))
    if held == max_hold:
        return closed(first, close1, ExitReason.MAX_HOLD, held, cur, highest_close)

    last = first  # the latest priced row
    unpriced = 0  # consecutive unpriced rows since ``last``
    for i in range(first + 1, len(index)):
        if stamps[i] <= stamps[i - 1]:
            raise ValueError("bars must be indexed by strictly ascending session dates")
        open_i, high_i, low_i, close_i = (
            float(opens[i]),
            float(highs[i]),
            float(lows[i]),
            float(closes[i]),
        )
        if not (open_i == open_i and high_i == high_i and low_i == low_i and close_i == close_i):
            unpriced += 1
            if unpriced > MAX_GAP:  # a segment break: nothing past it is this company's trade
                return closed(
                    last, float(closes[last]), ExitReason.DELISTED, held, cur, highest_close
                )
            continue
        unpriced = 0
        last = i
        held += 1
        highest_close = max(highest_close, close_i)
        if low_i <= cur:
            reason = ExitReason.STOP if cur == stop else ExitReason.TRAIL
            return closed(i, min(open_i, cur), reason, held, cur, highest_close)
        cur = _ratchet(cur, highest_close, trail_atr, float(atrs[i]))
        if held == max_hold:
            return closed(i, close_i, ExitReason.MAX_HOLD, held, cur, highest_close)

    if delisted:
        return closed(last, float(closes[last]), ExitReason.DELISTED, held, cur, highest_close)
    return TradeOutcome(
        status=TrackStatus.OPEN,
        entry_date=entry_date,
        entry_price=entry,
        initial_stop=stop,
        exit_date=None,
        exit_price=None,
        exit_reason=None,
        r_multiple=None,
        sessions_held=held,
        current_stop=cur,
        highest_close=highest_close,
    )
