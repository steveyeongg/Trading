"""Swing engine trading calendar: session lookups, the settle rule and scan times."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from swing_engine.calendar import ET, Session, TradingCalendar


def _s(d: date, close: time = time(16)) -> Session:
    return Session(d, time(9, 30), close)


# Thu 15, Fri 16, then Mon 19 (the weekend 17-18 has no session).
CAL = TradingCalendar([_s(date(2026, 10, 15)), _s(date(2026, 10, 16)), _s(date(2026, 10, 19))])


def test_et_is_new_york():
    assert str(ET) == "America/New_York"


def test_is_session():
    assert CAL.is_session(date(2026, 10, 16))
    assert not CAL.is_session(date(2026, 10, 17))


def test_next_and_previous_session_skip_the_weekend():
    assert CAL.next_session(date(2026, 10, 16)) == date(2026, 10, 19)
    assert CAL.previous_session(date(2026, 10, 19)) == date(2026, 10, 16)


def test_next_and_previous_accept_non_session_days():
    assert CAL.next_session(date(2026, 10, 17)) == date(2026, 10, 19)
    assert CAL.previous_session(date(2026, 10, 18)) == date(2026, 10, 16)


def test_next_and_previous_are_strict_and_raise_off_the_ends():
    assert CAL.next_session(date(2026, 10, 15)) == date(2026, 10, 16)
    with pytest.raises(LookupError):
        CAL.next_session(date(2026, 10, 19))
    with pytest.raises(LookupError):
        CAL.previous_session(date(2026, 10, 15))


def test_sessions_between_is_exclusive_after_and_inclusive_through():
    assert CAL.sessions_between(date(2026, 10, 15), date(2026, 10, 19)) == [
        date(2026, 10, 16),
        date(2026, 10, 19),
    ]
    assert CAL.sessions_between(date(2026, 10, 16), date(2026, 10, 16)) == []
    assert CAL.sessions_between(date(2026, 10, 14), date(2026, 10, 16)) == [
        date(2026, 10, 15),
        date(2026, 10, 16),
    ]


def test_sessions_are_sorted_whatever_order_they_arrive_in():
    cal = TradingCalendar([_s(date(2026, 10, 19)), _s(date(2026, 10, 15)), _s(date(2026, 10, 16))])
    assert cal.sessions_between(date(2026, 10, 1), date(2026, 10, 30)) == [
        date(2026, 10, 15),
        date(2026, 10, 16),
        date(2026, 10, 19),
    ]


def test_last_completed_session_skips_weekend_and_waits_16_minutes():
    cal = TradingCalendar(
        [
            Session(date(2026, 10, 15), time(9, 30), time(16)),
            Session(date(2026, 10, 16), time(9, 30), time(16)),
            Session(date(2026, 10, 19), time(9, 30), time(16)),
        ]
    )
    assert cal.last_completed_session(datetime(2026, 10, 18, 12, tzinfo=ET)) == date(2026, 10, 16)
    assert cal.last_completed_session(datetime(2026, 10, 16, 16, 10, tzinfo=ET)) == date(
        2026, 10, 15
    )


def test_last_completed_session_boundary_is_close_plus_settle_inclusive():
    just_before = datetime(2026, 10, 16, 16, 15, 59, tzinfo=ET)
    exactly = datetime(2026, 10, 16, 16, 16, tzinfo=ET)
    assert CAL.last_completed_session(just_before) == date(2026, 10, 15)
    assert CAL.last_completed_session(exactly) == date(2026, 10, 16)
    assert CAL.last_completed_session(just_before, settle_minutes=0) == date(2026, 10, 16)


def test_last_completed_session_compares_instants_across_timezones():
    # 20:16 UTC on Oct 16 is 16:16 EDT, the first instant the Oct 16 bar is settled.
    assert CAL.last_completed_session(datetime(2026, 10, 16, 20, 15, tzinfo=UTC)) == date(
        2026, 10, 15
    )
    assert CAL.last_completed_session(datetime(2026, 10, 16, 20, 16, tzinfo=UTC)) == date(
        2026, 10, 16
    )


def test_last_completed_session_uses_the_early_close_on_half_days():
    cal = TradingCalendar([_s(date(2026, 11, 25)), _s(date(2026, 11, 27), close=time(13))])
    assert cal.last_completed_session(datetime(2026, 11, 27, 13, 15, tzinfo=ET)) == date(
        2026, 11, 25
    )
    assert cal.last_completed_session(datetime(2026, 11, 27, 13, 16, tzinfo=ET)) == date(
        2026, 11, 27
    )


def test_last_completed_session_before_the_first_session_raises():
    with pytest.raises(LookupError):
        CAL.last_completed_session(datetime(2026, 10, 15, 12, tzinfo=ET))


def test_last_completed_session_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        CAL.last_completed_session(datetime(2026, 10, 16, 20))


def test_scan_time_on_half_day_is_close_plus_45():
    cal = TradingCalendar([Session(date(2026, 11, 27), time(9, 30), time(13))])
    assert cal.scan_time(date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 45, tzinfo=ET)


def test_scan_time_is_tz_aware_and_honours_the_delay():
    t = CAL.scan_time(date(2026, 10, 16), delay_minutes=60)
    assert t == datetime(2026, 10, 16, 17, 0, tzinfo=ET)
    assert t.tzinfo is not None and t.utcoffset().total_seconds() == -4 * 3600


def test_scan_time_for_a_non_session_raises():
    with pytest.raises(LookupError):
        CAL.scan_time(date(2026, 10, 17))
