"""NYSE trading calendar: sessions with their real open/close, and the scan clock.

Built from Alpaca's ``/v2/calendar`` rows, so holidays and early closes (half days) are
whatever the exchange published rather than rules re-implemented here.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Session:
    """One trading day. ``open`` and ``close`` are wall-clock times in ``ET``."""

    day: date
    open: time
    close: time


class TradingCalendar:
    """Lookups over an ordered set of sessions.

    Dates outside the supplied range are unknown, not "closed": asking for a neighbour
    that the calendar does not cover raises ``LookupError`` instead of guessing.
    """

    def __init__(self, sessions: Sequence[Session]) -> None:
        by_day = {s.day: s for s in sessions}
        self._days = sorted(by_day)
        self._by_day = by_day
        # Close instants in UTC, ascending: comparing in one zone sidesteps the wall-clock
        # comparison Python applies to two datetimes that share a tzinfo object.
        self._closes_utc = [self._close_at(d).astimezone(UTC) for d in self._days]

    def _close_at(self, d: date) -> datetime:
        return datetime.combine(d, self._by_day[d].close, ET)

    def is_session(self, d: date) -> bool:
        return d in self._by_day

    def next_session(self, d: date) -> date:
        """The first session strictly after ``d`` (``d`` need not be a session)."""
        i = bisect_right(self._days, d)
        if i == len(self._days):
            raise LookupError(f"no session after {d} in the calendar")
        return self._days[i]

    def previous_session(self, d: date) -> date:
        """The last session strictly before ``d`` (``d`` need not be a session)."""
        i = bisect_left(self._days, d)
        if i == 0:
            raise LookupError(f"no session before {d} in the calendar")
        return self._days[i - 1]

    def last_completed_session(self, now: datetime, settle_minutes: int = 16) -> date:
        """The latest session whose close plus ``settle_minutes`` is at or before ``now``.

        The settle wait matches the 16 minutes Alpaca's free plan holds back SIP data, so
        the returned session's daily bar is available and final.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now.astimezone(UTC) - timedelta(minutes=settle_minutes)
        i = bisect_right(self._closes_utc, cutoff)
        if i == 0:
            raise LookupError(f"no session in the calendar has completed by {now.isoformat()}")
        return self._days[i - 1]

    def sessions_between(self, after: date, through: date) -> list[date]:
        """Sessions with ``after < day <= through``."""
        return self._days[bisect_right(self._days, after) : bisect_right(self._days, through)]

    def scan_time(self, d: date, delay_minutes: int = 45) -> datetime:
        """When to scan session ``d``: its close plus ``delay_minutes``, tz-aware in ET."""
        if d not in self._by_day:
            raise LookupError(f"{d} is not a session in the calendar")
        return self._close_at(d) + timedelta(minutes=delay_minutes)
