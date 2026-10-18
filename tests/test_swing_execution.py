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
    assert out.highest_close is None  # the trail never started: the stop hit before the close
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
    assert out.highest_close == 102.0  # the stop-day close is not part of the trade's state


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


def test_a_session_with_no_prices_is_not_a_session():
    nan = float("nan")
    rows = [
        SIGNAL,
        (nan, nan, nan, nan, nan),  # halted: no bar
        (101.0, 103.0, 100.5, 102.0, ATR),
    ]
    frame = _bars(rows)
    out = _sim(rows)
    assert out.status == TrackStatus.OPEN
    assert out.entry_date == _day(frame, 2)
    assert out.sessions_held == 1


def test_missing_columns_and_unsorted_bars_raise():
    frame = _bars([SIGNAL, (101.0, 103.0, 100.5, 102.0, ATR)])
    ex = ExecutionParams()
    with pytest.raises(ValueError, match="atr14"):
        simulate_trade(frame.drop(columns="atr14"), frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)
    with pytest.raises(ValueError, match="ascending"):
        simulate_trade(frame.iloc[::-1], frame.index[0], TRIGGER, STOP, TRAIL, 30, ex)


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


@pytest.mark.parametrize("seed", range(40))
def test_a_closed_trade_never_depends_on_bars_after_its_exit(seed):
    rows = _wander(45, seed)
    frame = _bars(rows)
    sig = frame.index[0]
    ex = ExecutionParams()
    # trigger just above the first session's open so most seeds fill; stop 2 ATR under it
    trigger = rows[1][0] * 0.999
    stop = trigger - 2 * rows[0][4]
    full = simulate_trade(frame, sig, trigger, stop, TRAIL, 20, ex)

    def run(upto: int) -> TradeOutcome:
        return simulate_trade(frame.iloc[: upto + 1], sig, trigger, stop, TRAIL, 20, ex)

    for upto in range(len(frame)):
        prefix = run(upto)
        if prefix.status in {
            TrackStatus.CLOSED,
            TrackStatus.NOT_TRIGGERED,
            TrackStatus.SKIPPED_GAP,
        }:
            # settled with bars through ``upto``: every longer window must agree exactly
            assert prefix == full
            assert (
                simulate_trade(
                    _with_scrambled_tail(frame, upto, seed), sig, trigger, stop, TRAIL, 20, ex
                )
                == prefix
            )
        else:
            assert prefix.status in {TrackStatus.PENDING, TrackStatus.OPEN}


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
