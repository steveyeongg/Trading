"""Swing engine: the next-session buy-stop fill and chandelier exit model.

Every case is a small hand-built frame of daily bars so the expected fill, exit and R are exact
numbers. The shared setup is trigger 100, stop 96, an ATR of 2 on the signal date, a 3-ATR
trail and the default 10 bps of slippage (a buy fills 0.1% above, a sell 0.1% below). The signal
session is always row 0 of the frame; the first fill session is row 1. A last block checks that
a closed trade never depends on a bar after its exit, and that a malformed call fails loudly.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from swing_engine.config import ExecutionParams
from swing_engine.execution import ExitReason, TrackStatus, TradeOutcome, simulate_trade

TRIGGER = 100.0
STOP = 96.0
ATR = 2.0
TRAIL = 3.0
SLIP = 0.001  # ExecutionParams() default: 10 bps
START = "2024-01-02"

# (open, high, low, close, atr14)
SIGNAL = (98.0, 99.0, 97.0, 98.5, ATR)
QUIET = (101.0, 102.0, 100.5, 101.0, ATR)  # sits far above the stop and a flat trail

Row = tuple[float, float, float, float, float]


def _bars(rows: list[Row], start: str = START) -> pd.DataFrame:
    """Bars on consecutive business days from ``start``; ``rows[0]`` is the signal session."""
    index = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close", "atr14"])


def _day(frame: pd.DataFrame, k: int) -> date:
    return frame.index[k].date()


def _sim(
    rows: list[Row],
    *,
    trigger: float = TRIGGER,
    stop: float = STOP,
    trail_atr: float = TRAIL,
    max_hold: int = 30,
    ex: ExecutionParams | None = None,
    delisted: bool = False,
    signal_k: int = 0,
) -> TradeOutcome:
    frame = _bars(rows)
    return simulate_trade(
        frame,
        frame.index[signal_k],
        trigger,
        stop,
        trail_atr,
        max_hold,
        ex or ExecutionParams(),
        delisted=delisted,
    )


def _assert_no_entry(out: TradeOutcome, status: TrackStatus) -> None:
    assert out.status == status
    assert out.entry_date is None and out.entry_price is None
    assert out.exit_date is None and out.exit_price is None and out.exit_reason is None
    assert out.r_multiple is None
    assert out.current_stop is None and out.highest_close is None
    assert out.sessions_held == 0
    assert out.initial_stop == STOP


# --------------------------------------------------------------------- statuses without a fill


def test_no_next_bar_is_pending():
    _assert_no_entry(_sim([SIGNAL]), TrackStatus.PENDING)


def test_high_below_trigger_not_triggered():
    out = _sim([SIGNAL, (97.0, 99.9, 96.5, 99.0, ATR)])
    _assert_no_entry(out, TrackStatus.NOT_TRIGGERED)


def test_high_exactly_at_trigger_does_trigger():
    out = _sim([SIGNAL, (99.0, 100.0, 98.0, 99.5, ATR)])
    assert out.status == TrackStatus.OPEN
    assert out.entry_price == pytest.approx(100.0 * 1.001)


def test_gap_above_trigger_plus_one_atr_is_skipped():
    out = _sim([SIGNAL, (TRIGGER + 1.01 * ATR, 110.0, 101.0, 105.0, ATR)])
    _assert_no_entry(out, TrackStatus.SKIPPED_GAP)


def test_gap_of_exactly_one_atr_is_still_filled():
    out = _sim([SIGNAL, (TRIGGER + 1.0 * ATR, 110.0, 101.0, 105.0, ATR)])
    assert out.status == TrackStatus.OPEN
    assert out.entry_price == pytest.approx(102.0 * 1.001)


def test_gap_check_uses_the_signal_date_atr_not_the_next_sessions():
    # open 102.02 is 1.01 signal-ATRs above the trigger: skipped, even though session 1's own
    # atr14 (10) would make it look small.
    skipped = _sim([SIGNAL, (102.02, 110.0, 101.0, 105.0, 10.0)])
    assert skipped.status == TrackStatus.SKIPPED_GAP
    # open 102.0 is exactly 1 signal-ATR above the trigger: filled, even though session 1's own
    # atr14 (0.5) would make it look huge.
    filled = _sim([SIGNAL, (102.0, 110.0, 101.0, 105.0, 0.5)])
    assert filled.status == TrackStatus.OPEN


def test_skipped_gap_wins_over_not_triggered_ordering():
    # A gap that big already has the open above the trigger, so the gap rule decides it.
    out = _sim([SIGNAL, (110.0, 111.0, 109.0, 110.0, ATR)])
    assert out.status == TrackStatus.SKIPPED_GAP


# ----------------------------------------------------------------------------------- the fill


@pytest.mark.parametrize(
    ("open1", "expected_base"),
    [
        (99.0, 100.0),  # opens under the trigger: the buy-stop fills at the trigger
        (100.0, 100.0),  # opens on the trigger
        (101.5, 101.5),  # gaps up through the trigger within one ATR: fills at the open
    ],
)
def test_fill_at_worse_of_open_and_trigger_plus_slippage(open1, expected_base):
    out = _sim([SIGNAL, (open1, 103.0, 99.0, 102.0, ATR)])
    assert out.status == TrackStatus.OPEN
    assert out.entry_price == pytest.approx(max(open1, TRIGGER) * 1.001)
    assert out.entry_price == pytest.approx(expected_base * 1.001)


def test_open_trade_carries_state_so_far():
    frame = _bars([SIGNAL, (99.0, 103.0, 98.0, 102.0, ATR)])
    out = simulate_trade(frame, frame.index[0], TRIGGER, STOP, TRAIL, 30, ExecutionParams())
    assert out.status == TrackStatus.OPEN
    assert out.entry_date == _day(frame, 1)
    assert out.exit_date is None and out.exit_price is None and out.exit_reason is None
    assert out.r_multiple is None
    assert out.sessions_held == 1
    assert out.initial_stop == STOP
    assert out.highest_close == 102.0
    assert out.current_stop == STOP  # 102 - 3 * 2 = 96: not above the initial stop


# --------------------------------------------------------------------------------- stop exits


def test_entry_day_low_through_stop_exits_same_day_at_stop():
    rows = [SIGNAL, (101.0, 103.0, 95.0, 102.0, ATR)]
    frame = _bars(rows)
    out = _sim(rows)
    assert out.status == TrackStatus.CLOSED
    assert out.entry_date == _day(frame, 1)
    assert out.exit_date == _day(frame, 1) and out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)
    assert out.entry_price == pytest.approx(101.0 * 1.001)
    assert out.sessions_held == 1
    assert out.current_stop == STOP
    assert out.highest_close == 102.0  # the entry session's close counts as a close seen
    assert out.r_multiple == pytest.approx((95.904 - 101.101) / (101.101 - 96.0))


def test_entry_day_low_exactly_at_stop_exits():
    out = _sim([SIGNAL, (101.0, 103.0, 96.0, 102.0, ATR)])
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)


def test_entry_day_stop_beats_max_hold_of_one():
    out = _sim([SIGNAL, (101.0, 103.0, 95.0, 102.0, ATR)], max_hold=1)
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)


def test_later_day_low_through_stop_fills_at_stop_when_open_is_above_it():
    # cur stays 96 (102 - 6); day 2 opens at 100 and trades down through 96.
    out = _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 100.5, 95.5, 97.0, ATR)])
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)
    assert out.sessions_held == 2
    assert out.current_stop == STOP
    assert out.highest_close == 102.0  # 97 on the stop-out day is below the 102 already seen


def test_stop_out_day_close_counts_toward_highest_close_but_does_not_lift_the_stop():
    # An outside day: it trades down through the 96 stop, then closes at a new high of 106.
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 107.0, 95.5, 106.0, ATR)]
    out = _sim(rows)
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)
    assert out.highest_close == 106.0  # every held session's close, the exit session included
    assert out.current_stop == STOP  # the level that was hit, not 106 - 6 = 100


def test_later_day_low_exactly_at_the_stop_exits():
    out = _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 100.5, 96.0, 97.0, ATR)])
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)
    assert out.sessions_held == 2


def test_later_day_low_just_above_the_stop_holds():
    out = _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 100.5, 96.01, 97.0, ATR)])
    assert out.status == TrackStatus.OPEN
    assert out.sessions_held == 2


def test_gap_down_through_stop_fills_at_open():
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (94.0, 95.0, 93.0, 94.5, ATR)]
    frame = _bars(rows)
    out = _sim(rows)
    assert out.exit_date == _day(frame, 2)
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(94.0 * 0.999)  # worse than the 96 stop
    assert out.exit_price < STOP * 0.999


def test_gap_down_through_a_trailed_stop_fills_at_open_and_is_a_trail_exit():
    # day 1 close 105 -> cur = 105 - 6 = 99; day 2 opens at 97, below it.
    out = _sim([SIGNAL, (101.0, 106.0, 100.5, 105.0, ATR), (97.0, 98.0, 95.0, 96.0, ATR)])
    assert out.exit_reason == ExitReason.TRAIL
    assert out.exit_price == pytest.approx(97.0 * 0.999)
    assert out.current_stop == pytest.approx(99.0)


# ----------------------------------------------------------------------------------- the trail


def test_trail_ratchets_from_highest_close_and_never_drops():
    rows = [
        SIGNAL,
        (101.0, 109.0, 100.5, 108.0, 2.0),  # hc 108 -> cur = max(96, 102) = 102
        (108.0, 113.0, 107.0, 112.0, 2.0),  # hc 112 -> cur = 106
        (112.0, 112.5, 107.0, 105.0, 5.0),  # close falls, atr jumps: 112 - 15 = 97 < 106
    ]
    after1 = _sim(rows[:2])
    assert after1.current_stop == pytest.approx(max(STOP, 108.0 - 3 * 2.0))
    assert after1.highest_close == 108.0
    after2 = _sim(rows[:3])
    assert after2.current_stop == pytest.approx(106.0)
    assert after2.highest_close == 112.0
    after3 = _sim(rows)
    assert after3.status == TrackStatus.OPEN
    assert after3.highest_close == 112.0  # the lower close does not lower it
    assert after3.current_stop == pytest.approx(max(STOP, after3.highest_close - 3 * 2.0))
    assert after3.current_stop == pytest.approx(106.0)  # and the wider ATR does not lower it
    assert after3.sessions_held == 3


def test_a_close_moves_the_trail_only_after_that_sessions_stop_check():
    # Entry day: close 109 would lift the stop to 103, but its 99 low is checked against 96.
    entry_day = (101.0, 110.0, 99.0, 109.0, ATR)
    out = _sim([SIGNAL, entry_day])
    assert out.status == TrackStatus.OPEN
    assert out.current_stop == pytest.approx(103.0)
    # Later day: the stop in force is still 103 at the open; the 112 close that would lift it
    # to 106 does not retroactively catch the 104 low.
    out = _sim([SIGNAL, entry_day, (108.0, 113.0, 104.0, 112.0, ATR)])
    assert out.status == TrackStatus.OPEN
    assert out.current_stop == pytest.approx(106.0)
    # ... but the next session does see 106.
    out = _sim(
        [SIGNAL, entry_day, (108.0, 113.0, 104.0, 112.0, ATR), (107.0, 108.0, 105.0, 106.0, ATR)]
    )
    assert out.exit_reason == ExitReason.TRAIL
    assert out.exit_price == pytest.approx(106.0 * 0.999)


def test_trailed_exit_reason_is_trail():
    rows = [
        SIGNAL,
        (101.0, 109.0, 100.5, 108.0, ATR),
        (108.0, 113.0, 107.0, 112.0, ATR),  # cur = 106
        (107.0, 107.5, 105.5, 106.5, ATR),  # opens above 106, low 105.5 <= 106
    ]
    out = _sim(rows)
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.TRAIL
    assert out.exit_price == pytest.approx(106.0 * 0.999)
    assert out.current_stop == pytest.approx(106.0)
    assert out.highest_close == 112.0
    assert out.sessions_held == 3


def test_stop_reason_when_the_trail_never_rose_above_the_initial_stop():
    out = _sim([SIGNAL, (101.0, 103.0, 100.5, 100.0, ATR), (99.0, 99.5, 95.0, 96.0, ATR)])
    assert out.exit_reason == ExitReason.STOP  # cur stayed 96 (100 - 6 = 94)
    assert out.exit_price == pytest.approx(STOP * 0.999)


def test_nan_atr_on_a_later_day_leaves_the_trail_where_it_was():
    nan = float("nan")
    rows = [
        SIGNAL,
        (101.0, 109.0, 100.5, 108.0, ATR),  # cur 102
        (108.0, 113.0, 107.0, 112.0, nan),  # no ATR: cur stays 102 (not NaN), hc still 112
    ]
    out = _sim(rows)
    assert out.status == TrackStatus.OPEN
    assert out.current_stop == pytest.approx(102.0)
    assert out.highest_close == 112.0
    # ... and the next day's stop check still works against the kept level.
    out = _sim([*rows, (103.0, 103.5, 101.0, 102.0, ATR)])
    assert out.exit_reason == ExitReason.TRAIL
    assert out.exit_price == pytest.approx(102.0 * 0.999)


def test_nan_atr_on_the_entry_day_leaves_the_stop_at_the_initial_stop():
    out = _sim([SIGNAL, (101.0, 109.0, 100.5, 108.0, float("nan"))])
    assert out.status == TrackStatus.OPEN
    assert out.current_stop == STOP
    assert out.highest_close == 108.0


# ------------------------------------------------------------------------------- the time stop


def _climb(n: int) -> list[Row]:
    """``n`` sessions that grind up 0.5 a day (close 0.25 over the open): no stop is ever near."""
    return [
        (101.0 + 0.5 * k, 101.75 + 0.5 * k, 100.5 + 0.5 * k, 101.25 + 0.5 * k, ATR)
        for k in range(n)
    ]


def test_max_hold_exits_at_close_of_session_30():
    rows = [SIGNAL, *_climb(35)]  # more bars than needed: the rest must be ignored
    frame = _bars(rows)
    out = _sim(rows, max_hold=30)
    assert out.status == TrackStatus.CLOSED
    assert out.sessions_held == 30
    assert out.exit_reason == ExitReason.MAX_HOLD
    assert out.exit_date == _day(frame, 30)
    assert out.exit_price == pytest.approx(frame["close"].iloc[30] * 0.999)
    assert out.entry_date == _day(frame, 1)
    assert out.highest_close == frame["close"].iloc[30]  # the exit-day close counts
    assert out.current_stop == pytest.approx(frame["close"].iloc[30] - 3 * ATR)


def test_one_session_short_of_max_hold_is_still_open():
    out = _sim([SIGNAL, *_climb(29)], max_hold=30)
    assert out.status == TrackStatus.OPEN
    assert out.sessions_held == 29


def test_max_hold_of_one_exits_at_the_entry_day_close_after_the_trail_update():
    out = _sim([SIGNAL, (99.0, 103.0, 98.0, 102.5, ATR), (50.0, 51.0, 49.0, 50.0, ATR)], max_hold=1)
    assert out.exit_reason == ExitReason.MAX_HOLD
    assert out.sessions_held == 1
    assert out.exit_price == pytest.approx(102.5 * 0.999)
    assert out.highest_close == 102.5
    assert out.current_stop == pytest.approx(max(STOP, 102.5 - 6.0))


def test_a_stop_on_the_final_hold_day_beats_the_time_stop():
    rows = [SIGNAL, *_climb(2)]
    # session 3 (the max_hold-th) opens near 102 but trades down through the 96 stop.
    rows.append((102.0, 102.5, 95.0, 101.5, ATR))
    out = _sim(rows, max_hold=3)
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)
    assert out.sessions_held == 3


# --------------------------------------------------------------------------------- delisting


def test_delisted_closes_at_last_close():
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (102.0, 104.0, 101.0, 103.5, ATR)]
    frame = _bars(rows)
    out = _sim(rows, delisted=True)
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.DELISTED
    assert out.exit_date == _day(frame, 2)
    assert out.exit_price == pytest.approx(103.5 * 0.999)
    assert out.sessions_held == 2
    assert out.highest_close == 103.5
    assert out.r_multiple == pytest.approx((103.5 * 0.999 - 101.0 * 1.001) / (101.0 * 1.001 - STOP))


def test_same_bars_without_the_delisted_flag_stay_open():
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (102.0, 104.0, 101.0, 103.5, ATR)]
    out = _sim(rows, delisted=False)
    assert out.status == TrackStatus.OPEN
    assert out.exit_reason is None and out.exit_price is None and out.r_multiple is None


def test_delisted_on_the_entry_day_closes_at_that_close():
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)]
    out = _sim(rows, delisted=True)
    assert out.exit_reason == ExitReason.DELISTED
    assert out.sessions_held == 1
    assert out.exit_price == pytest.approx(102.0 * 0.999)


def test_delisted_flag_does_not_override_a_real_exit():
    rows = [SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 100.5, 95.5, 97.0, ATR)]
    assert _sim(rows, delisted=True).exit_reason == ExitReason.STOP
    rows = [SIGNAL, *_climb(30)]
    assert _sim(rows, delisted=True).exit_reason == ExitReason.MAX_HOLD


def test_delisted_flag_does_not_create_a_fill():
    assert _sim([SIGNAL], delisted=True).status == TrackStatus.PENDING
    out = _sim([SIGNAL, (97.0, 99.9, 96.5, 99.0, ATR)], delisted=True)
    assert out.status == TrackStatus.NOT_TRIGGERED


# ----------------------------------------------------------------------------- R and baselines


def test_r_multiple_uses_fill_price():
    out = _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR), (100.0, 100.5, 95.5, 97.0, ATR)])
    assert out.entry_price is not None and out.exit_price is not None
    assert out.r_multiple == pytest.approx(
        (out.exit_price - out.entry_price) / (out.entry_price - STOP)
    )
    # 101.101 in, 95.904 out, risk 5.101 per share: a bit worse than -1R because of slippage.
    assert out.r_multiple == pytest.approx(-1.01883, abs=1e-4)


def test_a_winner_has_positive_r_from_the_trail():
    rows = [
        SIGNAL,
        (101.0, 109.0, 100.5, 108.0, ATR),
        (108.0, 113.0, 107.0, 112.0, ATR),
        (107.0, 107.5, 105.5, 106.5, ATR),
    ]
    out = _sim(rows)
    expected = (106.0 * 0.999 - 101.0 * 1.001) / (101.0 * 1.001 - STOP)
    assert out.r_multiple == pytest.approx(expected)
    assert out.r_multiple > 0


def test_random_entry_baseline_call_forces_a_next_open_entry():
    # trigger=0 and an infinite gap limit: a 30% gap-up still fills at the open plus slippage.
    ex = ExecutionParams(slippage_bps=10.0, max_gap_atr=float("inf"))
    rows = [SIGNAL, (130.0, 132.0, 128.0, 131.0, ATR)]
    out = _sim(rows, trigger=0.0, stop=124.0, ex=ex)
    assert out.status == TrackStatus.OPEN
    assert out.entry_price == pytest.approx(130.0 * 1.001)
    # ... and the default limit would have skipped the same bar for a real trigger.
    assert _sim(rows).status == TrackStatus.SKIPPED_GAP


def test_zero_slippage_fills_exactly_at_the_level():
    ex = ExecutionParams(slippage_bps=0.0)
    out = _sim([SIGNAL, (99.0, 103.0, 95.0, 102.0, ATR)], ex=ex)
    assert out.entry_price == pytest.approx(100.0)
    assert out.exit_price == pytest.approx(96.0)
    assert out.r_multiple == pytest.approx(-1.0)


# --------------------------------------------------------------------------- inputs and dates


def test_signal_date_may_be_a_python_date_or_a_timestamp():
    frame = _bars([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)])
    day = frame.index[0]
    ex = ExecutionParams()
    a = simulate_trade(frame, day, TRIGGER, STOP, TRAIL, 30, ex)
    b = simulate_trade(frame, day.date(), TRIGGER, STOP, TRAIL, 30, ex)
    c = simulate_trade(frame, day + pd.Timedelta(hours=9), TRIGGER, STOP, TRAIL, 30, ex)
    assert a == b == c
    assert a.entry_date == date(2024, 1, 3)
    assert type(a.entry_date) is date


def test_later_bars_beyond_the_signal_are_what_the_trade_runs_on():
    # The signal row is not row 0: bars before it are history and play no part.
    history = [(90.0, 91.0, 89.0, 90.0, 1.0)] * 3
    rows = [*history, SIGNAL, (101.0, 103.0, 95.0, 102.0, ATR)]
    out = _sim(rows, signal_k=3)
    assert out.exit_reason == ExitReason.STOP
    assert out.sessions_held == 1
    assert out.entry_price == pytest.approx(101.0 * 1.001)


def test_missing_signal_row_raises():
    frame = _bars([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)])
    with pytest.raises(ValueError, match="signal date"):
        simulate_trade(frame, date(2023, 12, 29), TRIGGER, STOP, TRAIL, 30, ExecutionParams())
    # a weekend between sessions is not a session either
    with pytest.raises(ValueError, match="signal date"):
        simulate_trade(frame, date(2024, 1, 6), TRIGGER, STOP, TRAIL, 30, ExecutionParams())


def test_missing_columns_and_unsorted_bars_raise():
    frame = _bars([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)])
    ex = ExecutionParams()
    with pytest.raises(ValueError, match="atr14"):
        simulate_trade(frame.drop(columns="atr14"), frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)
    backwards = frame.iloc[::-1]
    with pytest.raises(ValueError, match="ascending"):
        simulate_trade(backwards, backwards.index[0], TRIGGER, STOP, TRAIL, 30, ex)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_hold": 0},
        {"trigger": float("nan")},
        {"stop": float("nan")},
        {"trail_atr": float("nan")},
        {"trail_atr": -1.0},
    ],
)
def test_invalid_parameters_raise(kwargs):
    with pytest.raises(ValueError):
        _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)], **kwargs)


def test_stop_at_or_above_the_fill_raises():
    with pytest.raises(ValueError, match="below the entry"):
        _sim([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)], stop=105.0)


def test_nan_signal_atr_cannot_silently_disable_the_gap_check():
    rows = [(98.0, 99.0, 97.0, 98.5, float("nan")), (110.0, 111.0, 109.0, 110.0, ATR)]
    with pytest.raises(ValueError, match="atr14"):
        _sim(rows)
    # ... but with no gap limit the ATR is never consulted
    ex = ExecutionParams(max_gap_atr=float("inf"))
    assert _sim(rows, trigger=0.0, stop=100.0, ex=ex).status == TrackStatus.OPEN


# ----------------------------------------- sessions with no bar: lapse, skip, and segment breaks

NAN = float("nan")
NO_BAR: Row = (NAN, NAN, NAN, NAN, NAN)
D1: Row = (101.0, 103.0, 100.5, 102.0, ATR)  # fills at 101 and holds: far above the 96 stop
NEW_COMPANY: Row = (15.5, 16.0, 14.9, 15.0, 0.4)  # a reused ticker, trading at a tenth of the price


def _none(k: int) -> list[Row]:
    """``k`` consecutive sessions on which the symbol has no bar."""
    return [NO_BAR] * k


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_a_next_session_with_no_price_lets_the_buy_stop_lapse(field):
    row = {"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0}
    row[field] = NAN
    broken: Row = (row["open"], row["high"], row["low"], row["close"], ATR)
    _assert_no_entry(_sim([SIGNAL, broken, D1]), TrackStatus.NOT_TRIGGERED)


@pytest.mark.parametrize("missing", [1, 5, 10, 300])
def test_the_buy_stop_is_not_carried_over_a_missing_session_or_a_segment_break(missing):
    # The signal is the old company's last bar; the first bar after the break would have
    # triggered a one-session order, but the order was good for the very next session only.
    out = _sim([SIGNAL, *_none(missing), D1])
    _assert_no_entry(out, TrackStatus.NOT_TRIGGERED)


def test_unpriced_rows_inside_the_hold_are_skipped_and_not_counted():
    d2: Row = (100.0, 104.0, 99.0, 103.0, ATR)
    d3: Row = (103.0, 105.0, 102.0, 104.0, ATR)
    rows = [SIGNAL, D1, *_none(10), d2, d3]  # ten missing sessions: still the same segment
    frame = _bars(rows)
    out = _sim(rows, max_hold=3)
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.MAX_HOLD
    assert out.sessions_held == 3  # the ten missing sessions are not held sessions
    assert out.exit_date == _day(frame, 13)
    assert out.exit_price == pytest.approx(104.0 * 0.999)
    assert out.highest_close == 104.0
    assert out.current_stop == pytest.approx(98.0)  # 104 - 3 * 2: moved only by priced closes


def test_a_run_of_missing_sessions_does_not_stop_out_or_move_the_trail():
    with_gap = _sim([SIGNAL, D1, *_none(7), (102.5, 103.0, 101.0, 102.5, ATR)])
    without = _sim([SIGNAL, D1, (102.5, 103.0, 101.0, 102.5, ATR)])
    assert with_gap.status == TrackStatus.OPEN
    assert with_gap.sessions_held == without.sessions_held == 2
    assert with_gap.current_stop == without.current_stop
    assert with_gap.highest_close == without.highest_close == 102.5


def test_a_gap_down_after_a_skipped_run_fills_at_that_sessions_open():
    # day 1 close 105 lifts the stop to 99; after 4 missing sessions the stock reopens at 93.
    d1: Row = (101.0, 106.0, 100.5, 105.0, ATR)
    rows = [SIGNAL, d1, *_none(4), (93.0, 94.0, 90.0, 92.0, ATR)]
    frame = _bars(rows)
    out = _sim(rows)
    assert out.exit_reason == ExitReason.TRAIL
    assert out.exit_date == _day(frame, 6)
    assert out.exit_price == pytest.approx(93.0 * 0.999)
    assert out.sessions_held == 2
    assert out.current_stop == pytest.approx(99.0)


@pytest.mark.parametrize("delisted", [False, True])
def test_ten_missing_sessions_are_skipped_and_eleven_end_the_segment(delisted):
    ten_rows = [SIGNAL, D1, *_none(10), D1]
    ten = _sim(ten_rows, delisted=delisted)
    assert ten.sessions_held == 2  # both priced bars were held: the run did not end the trade
    if delisted:  # the flag says the bars end: at the last priced bar, after the skipped run
        assert ten.exit_reason == ExitReason.DELISTED
        assert ten.exit_date == _day(_bars(ten_rows), 12)
    else:
        assert ten.status == TrackStatus.OPEN

    frame = _bars([SIGNAL, D1, *_none(11), D1])
    eleven = _sim([SIGNAL, D1, *_none(11), D1], delisted=delisted)
    assert eleven.status == TrackStatus.CLOSED
    assert eleven.exit_reason == ExitReason.DELISTED
    assert eleven.exit_date == _day(frame, 1)  # the last priced bar before the break
    assert eleven.exit_price == pytest.approx(102.0 * 0.999)
    assert eleven.sessions_held == 1
    assert eleven.highest_close == 102.0


def test_only_consecutive_missing_sessions_make_a_segment_break():
    # 6 + 6 missing sessions, split by a priced bar: neither run is longer than ten.
    rows = [SIGNAL, D1, *_none(6), (102.0, 104.0, 101.5, 103.0, ATR), *_none(6), D1]
    out = _sim(rows)
    assert out.status == TrackStatus.OPEN
    assert out.sessions_held == 3


def test_a_reused_ticker_cannot_be_traded_into_across_a_segment_break():
    # Open at 101.1, 300 sessions with no bar, then a different company on the same symbol at 15.
    rows = [SIGNAL, D1, (102.0, 104.0, 101.5, 103.0, ATR), *_none(300), *[NEW_COMPANY] * 5]
    frame = _bars(rows)
    out = _sim(rows)
    assert out.status == TrackStatus.CLOSED
    assert out.exit_reason == ExitReason.DELISTED
    assert out.exit_date == _day(frame, 2)  # the old company's last bar, not the new one's
    assert out.exit_price == pytest.approx(103.0 * 0.999)
    assert out.sessions_held == 2
    assert out.r_multiple == pytest.approx((103.0 * 0.999 - 101.0 * 1.001) / (101.0 * 1.001 - STOP))
    assert out.r_multiple > 0  # not a -17R loss on the new company's prices
    assert _sim(rows, delisted=True) == out  # the flag changes nothing: the break is the end


@pytest.mark.parametrize(
    ("trailing", "flag", "status"), [(10, False, "open"), (11, False, "closed")]
)
def test_trailing_missing_sessions_close_only_once_they_are_a_segment_break(trailing, flag, status):
    rows = [SIGNAL, D1, (102.0, 104.0, 101.5, 103.0, ATR), *_none(trailing)]
    out = _sim(rows, delisted=flag)
    assert out.status.value == status
    if status == "closed":
        assert out.exit_reason == ExitReason.DELISTED
        assert out.exit_date == _day(_bars(rows), 2)


def test_trailing_missing_sessions_with_the_delisted_flag_exit_on_the_last_priced_bar():
    # The real shape of a delisted symbol's panel column: priced bars, then NaN to the end.
    rows = [SIGNAL, D1, (102.0, 104.0, 101.5, 103.0, ATR), *_none(3)]
    frame = _bars(rows)
    flagged = _sim(rows, delisted=True)
    assert flagged.status == TrackStatus.CLOSED
    assert flagged.exit_reason == ExitReason.DELISTED
    assert flagged.exit_date == _day(frame, 2)
    assert flagged.exit_price == pytest.approx(103.0 * 0.999)
    assert flagged.sessions_held == 2
    assert flagged.highest_close == 103.0
    assert flagged.r_multiple == pytest.approx(
        (103.0 * 0.999 - 101.0 * 1.001) / (101.0 * 1.001 - STOP)
    )
    unflagged = _sim(rows, delisted=False)
    assert unflagged.status == TrackStatus.OPEN
    assert unflagged.exit_date is None and unflagged.exit_price is None
    assert unflagged.sessions_held == 2
    assert unflagged.highest_close == 103.0


def test_trailing_missing_sessions_right_after_the_entry_session():
    rows = [SIGNAL, D1, *_none(2)]
    out = _sim(rows, delisted=True)
    assert out.exit_reason == ExitReason.DELISTED
    assert out.sessions_held == 1
    assert out.exit_date == _day(_bars(rows), 1)
    assert _sim(rows).status == TrackStatus.OPEN


def test_a_real_exit_before_the_break_stands():
    rows = [SIGNAL, D1, (100.0, 100.5, 95.5, 97.0, ATR), *_none(300), NEW_COMPANY]
    out = _sim(rows)
    assert out.exit_reason == ExitReason.STOP
    assert out.exit_price == pytest.approx(STOP * 0.999)


# ----------------------------------------------------------------- reading only what is needed


def test_rows_after_the_exit_are_never_read():
    stopped = _bars([SIGNAL, (101.0, 103.0, 95.0, 102.0, ATR)])
    # Append rows whose dates run backwards and whose prices are NaN or absurd: if the scan
    # touched anything past the exit it would raise on the ordering.
    junk = pd.DataFrame(
        [NO_BAR, (1e9, 1e9, -1e9, 1e9, 1e9)],
        index=pd.DatetimeIndex(["2023-01-03", "2023-01-02"]),
        columns=stopped.columns,
    )
    messy = pd.concat([stopped, junk])
    assert not messy.index.is_monotonic_increasing
    ex = ExecutionParams()
    clean = simulate_trade(stopped, stopped.index[0], TRIGGER, STOP, TRAIL, 30, ex)
    assert clean.exit_reason == ExitReason.STOP
    assert simulate_trade(messy, stopped.index[0], TRIGGER, STOP, TRAIL, 30, ex) == clean


def test_rows_out_of_order_inside_the_trade_raise():
    frame = _bars([SIGNAL, D1, (102.0, 104.0, 101.5, 103.0, ATR)])
    shuffled = frame.iloc[[0, 1, 2]].copy()
    shuffled.index = frame.index[[0, 2, 1]]
    with pytest.raises(ValueError, match="ascending"):
        simulate_trade(shuffled, shuffled.index[0], TRIGGER, STOP, TRAIL, 30, ExecutionParams())


def test_index_must_be_tz_naive_datetimes_with_one_row_per_signal_date():
    frame = _bars([SIGNAL, D1])
    ex = ExecutionParams()
    aware = frame.tz_localize("UTC")
    with pytest.raises(ValueError, match="tz-naive"):
        simulate_trade(aware, frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)
    plain = frame.reset_index(drop=True)
    with pytest.raises(ValueError, match="tz-naive"):
        simulate_trade(plain, frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)
    with pytest.raises(ValueError, match="tz-naive"):
        simulate_trade(frame, frame.index[0].tz_localize("UTC"), TRIGGER, STOP, TRAIL, 30, ex)
    doubled = pd.concat([frame, frame.iloc[[0]]])
    with pytest.raises(ValueError, match="more than one row"):
        simulate_trade(doubled, frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)


# ----------------------------------------------------------------------- no look-ahead property


def _wander(n: int, seed: int) -> list[Row]:
    """A seeded random walk of valid bars (high >= open, close >= low, ...) from price ~100."""
    rng = np.random.default_rng(seed)
    rows: list[Row] = []
    price = 99.0
    for _ in range(n):
        o = price * (1 + rng.normal(0, 0.008))
        c = o * (1 + rng.normal(0.002, 0.012))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.006)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        rows.append((o, h, low, c, price * 0.02))
        price = c
    return rows


def _with_missing_sessions(rows: list[Row], seed: int) -> list[Row]:
    """Blank two short runs of sessions, and on odd seeds one 11-session run (a segment break)."""
    rng = np.random.default_rng(2000 + seed)
    out = list(rows)
    runs = [int(rng.integers(1, 4)), int(rng.integers(1, 4))]
    if seed % 2:
        runs.append(11)
    for length in runs:
        start = int(rng.integers(3, 25))
        out[start : start + length] = [NO_BAR] * length
    return out


def _with_scrambled_tail(frame: pd.DataFrame, upto: int, seed: int) -> pd.DataFrame:
    """``frame`` with every row after position ``upto`` replaced by unrelated random bars."""
    out = frame.copy()
    rng = np.random.default_rng(1000 + seed)
    tail = out.iloc[upto + 1 :]
    if len(tail):
        base = rng.uniform(40, 200, size=len(tail))
        out.iloc[upto + 1 :, :] = np.column_stack(
            [base, base * 1.2, base * 0.8, base * 1.05, rng.uniform(0.5, 5, size=len(tail))]
        )
    return out


@pytest.mark.parametrize("with_missing", [False, True], ids=["dense", "missing-sessions"])
@pytest.mark.parametrize("mode", ["fills", "not_triggered", "skipped_gap"])
@pytest.mark.parametrize("seed", range(20))
def test_a_settled_trade_never_depends_on_bars_after_it_settled(seed, mode, with_missing):
    rows = _wander(45, seed)
    if with_missing:
        rows = _with_missing_sessions(rows, seed)
    frame = _bars(rows)
    sig = frame.index[0]
    ex = ExecutionParams()
    open1, high1, atr0 = rows[1][0], rows[1][1], rows[0][4]
    if mode == "fills":
        trigger = open1 * 0.999  # just under the next open: always fills, at that open
    elif mode == "not_triggered":
        trigger = high1 + 0.01  # one cent above the next session's high: never touched
    else:
        trigger = open1 - 2 * atr0 - 0.5  # the next open sits 2.5 ATR-ish above the trigger
    stop = trigger - 2 * atr0
    full = simulate_trade(frame, sig, trigger, stop, TRAIL, 20, ex)
    expected = {
        "fills": TrackStatus.CLOSED,  # 20 held sessions, a stop, or a segment break: all exits
        "not_triggered": TrackStatus.NOT_TRIGGERED,
        "skipped_gap": TrackStatus.SKIPPED_GAP,
    }[mode]
    assert full.status == expected

    for upto in range(len(frame)):
        prefix = simulate_trade(frame.iloc[: upto + 1], sig, trigger, stop, TRAIL, 20, ex)
        if prefix.status in {
            TrackStatus.CLOSED,
            TrackStatus.NOT_TRIGGERED,
            TrackStatus.SKIPPED_GAP,
        }:
            # settled with bars through ``upto``: every longer window must agree exactly
            assert prefix == full
            scrambled = _with_scrambled_tail(frame, upto, seed)
            assert simulate_trade(scrambled, sig, trigger, stop, TRAIL, 20, ex) == prefix
        else:
            assert prefix.status in {TrackStatus.PENDING, TrackStatus.OPEN}

    if mode == "fills":
        # highest_close is the best close over every held session, the exit session included,
        # and sessions_held counts the priced sessions in that span.
        assert full.entry_date is not None and full.exit_date is not None
        held = frame.loc[str(full.entry_date) : str(full.exit_date)].dropna(subset=["close"])
        assert full.highest_close == held["close"].max()
        assert full.sessions_held == len(held)


def test_open_trade_status_changes_only_when_a_later_bar_arrives():
    rows = [SIGNAL, *_climb(5)]
    frame = _bars(rows)
    outs = [
        simulate_trade(
            frame.iloc[: k + 1], frame.index[0], TRIGGER, STOP, TRAIL, 30, ExecutionParams()
        )
        for k in range(len(frame))
    ]
    assert outs[0].status == TrackStatus.PENDING
    assert [o.status for o in outs[1:]] == [TrackStatus.OPEN] * 5
    assert [o.sessions_held for o in outs[1:]] == [1, 2, 3, 4, 5]
    assert all(o.entry_price == outs[1].entry_price for o in outs[1:])


def test_outcome_is_immutable_and_values_are_plain_floats():
    out = _sim([SIGNAL, (101.0, 103.0, 95.0, 102.0, ATR)])
    with pytest.raises(AttributeError):
        out.status = TrackStatus.OPEN  # type: ignore[misc]
    for value in (out.entry_price, out.exit_price, out.r_multiple, out.current_stop):
        assert type(value) is float
        assert math.isfinite(value)


def test_enum_values_are_the_documented_lowercase_strings():
    assert [s.value for s in TrackStatus] == [
        "pending",
        "not_triggered",
        "skipped_gap",
        "open",
        "closed",
    ]
    assert [r.value for r in ExitReason] == ["stop", "trail", "max_hold", "delisted"]
