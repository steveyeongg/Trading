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

Nothing looks past the session being processed, and processing stops at the exit, so a trade
that closed on or before a session is identical whatever bars follow it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import pandas as pd

from swing_engine.config import ExecutionParams

_PRICES = ["open", "high", "low", "close"]
_REQUIRED = [*_PRICES, "atr14"]


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
    DELISTED = "delisted"  # the bars ended and the symbol is known to have stopped trading


@dataclass(frozen=True)
class TradeOutcome:
    """Where one candidate stands.

    ``PENDING``, ``NOT_TRIGGERED`` and ``SKIPPED_GAP`` carry no fill: every entry, exit and
    ``r_multiple`` field is ``None`` and ``current_stop`` / ``highest_close`` are ``None``.
    ``OPEN`` has the fill plus ``current_stop`` and ``highest_close`` as of the last bar.
    ``CLOSED`` has every field. There ``current_stop`` and ``highest_close`` are the values in
    force when the exit happened, so a stop-out (which comes before that session's close is
    used) does not include the exit session's close, and a stop-out on the entry session has
    no ``highest_close`` at all. ``sessions_held`` counts the sessions from the fill through
    the exit (or through the last bar while open), the entry session being 1.
    ``r_multiple`` is the exit against the actual fill, in units of the actual fill's risk:
    ``(exit - entry) / (entry - initial_stop)``.
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

    ``bars`` is one symbol's rows, indexed by tz-naive session timestamps in strictly ascending
    order, with ``open, high, low, close, atr14``, ending at the evaluation date (callers
    truncate; later bars are never needed and never read once the trade has exited). It must
    have a row on ``signal_date`` (a ``date`` or ``Timestamp``): the gap rule uses that day's
    ``atr14``. A row with no price is a missing session and is skipped, not filled, so it does
    not count toward ``max_hold``. ``delisted`` says the bars end because the symbol stopped
    trading: an open trade then closes at the last close instead of staying open forever.

    The sequence, with ``slip = ex.slippage_bps / 10_000``, is fixed. On the first session
    after the signal: skip if the open is above ``trigger + max_gap_atr * atr14[signal_date]``;
    no trade if the high stayed under ``trigger``; fill at ``max(open, trigger) * (1 + slip)``;
    exit at ``stop * (1 - slip)`` if the low reached the stop; move the trail from the close;
    exit at the close if ``max_hold`` is 1. On every later session: exit at
    ``min(open, current_stop) * (1 - slip)`` if the low reached the current stop; move the trail
    from the close; exit at ``close * (1 - slip)`` once ``max_hold`` sessions have been held.
    A NaN ``atr14`` on a later session leaves the trail where it was.
    """
    if max_hold < 1:
        raise ValueError(f"max_hold must be at least 1, got {max_hold}")
    if not (math.isfinite(trigger) and math.isfinite(stop)):
        raise ValueError(f"trigger and stop must be finite, got trigger={trigger}, stop={stop}")
    if not (math.isfinite(trail_atr) and trail_atr >= 0):
        raise ValueError(f"trail_atr must be a finite number >= 0, got {trail_atr}")
    missing = [c for c in _REQUIRED if c not in bars.columns]
    if missing:
        raise ValueError(f"bars is missing columns {missing}")
    index = bars.index
    if not (index.is_monotonic_increasing and index.is_unique):
        raise ValueError("bars must be indexed by strictly ascending session dates")

    sig = pd.Timestamp(signal_date).normalize()
    pos = int(index.searchsorted(sig))
    if pos >= len(index) or index[pos] != sig:
        raise ValueError(f"bars has no row on signal date {sig.date()}")
    signal_atr = float(bars["atr14"].iloc[pos])

    window = bars.iloc[pos + 1 :]
    window = window[window[_PRICES].notna().all(axis=1)]
    if window.empty:
        return _no_fill(TrackStatus.PENDING, stop)
    dates = [ts.date() for ts in window.index]
    opens = window["open"].to_numpy(dtype="float64").tolist()
    highs = window["high"].to_numpy(dtype="float64").tolist()
    lows = window["low"].to_numpy(dtype="float64").tolist()
    closes = window["close"].to_numpy(dtype="float64").tolist()
    atrs = window["atr14"].to_numpy(dtype="float64").tolist()
    slip = ex.slippage_bps / 10_000.0
    stop = float(stop)

    # The first session: gap rule, buy-stop trigger, fill.
    if math.isfinite(ex.max_gap_atr):
        if math.isnan(signal_atr):
            raise ValueError(
                f"atr14 is NaN on signal date {sig.date()}, so the gap rule cannot be applied"
            )
        if opens[0] > trigger + ex.max_gap_atr * signal_atr:
            return _no_fill(TrackStatus.SKIPPED_GAP, stop)
    if highs[0] < trigger:
        return _no_fill(TrackStatus.NOT_TRIGGERED, stop)
    entry = max(opens[0], trigger) * (1.0 + slip)
    if not entry > stop:
        raise ValueError(f"stop {stop} must be below the entry fill {entry}")
    entry_date = dates[0]

    def closed(
        i: int,
        raw_price: float,
        reason: ExitReason,
        held: int,
        cur: float,
        highest_close: float | None,
    ) -> TradeOutcome:
        exit_price = raw_price * (1.0 - slip)
        return TradeOutcome(
            status=TrackStatus.CLOSED,
            entry_date=entry_date,
            entry_price=entry,
            initial_stop=stop,
            exit_date=dates[i],
            exit_price=exit_price,
            exit_reason=reason,
            r_multiple=(exit_price - entry) / (entry - stop),
            sessions_held=held,
            current_stop=cur,
            highest_close=highest_close,
        )

    held = 1
    if lows[0] <= stop:
        return closed(0, stop, ExitReason.STOP, held, stop, None)
    highest_close = closes[0]
    cur = _ratchet(stop, highest_close, trail_atr, atrs[0])
    if held == max_hold:
        return closed(0, closes[0], ExitReason.MAX_HOLD, held, cur, highest_close)

    for i in range(1, len(closes)):
        held += 1
        if lows[i] <= cur:
            reason = ExitReason.STOP if cur == stop else ExitReason.TRAIL
            return closed(i, min(opens[i], cur), reason, held, cur, highest_close)
        highest_close = max(highest_close, closes[i])
        cur = _ratchet(cur, highest_close, trail_atr, atrs[i])
        if held == max_hold:
            return closed(i, closes[i], ExitReason.MAX_HOLD, held, cur, highest_close)

    if delisted:
        return closed(len(closes) - 1, closes[-1], ExitReason.DELISTED, held, cur, highest_close)
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
