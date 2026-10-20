"""Swing engine: the after-close scheduler and its wiring into the API's lifespan.

``swing_scheduler`` runs inside the FastAPI process: on startup it catches up when the last stored
scan is older than the last session whose scan time (its close plus 45 minutes) has passed, then
each night it sleeps until that scan time and runs the nightly job. Starting between the settle
time (close + 16 minutes) and the scan time waits for the nightly, so the digest uses settled
bars. These tests pin what the review asked about:

* **Half days and the DST switch.** The next scan is the close + 45 minutes in New York: 13:45 on
  the half day Fri 27 Nov 2026, and 16:45 EST (21:45 UTC) on Mon 2 Nov, after DST ended.
* **The Mac slept.** macOS pauses the monotonic clock while asleep, so the scheduler sleeps in
  chunks of at most 5 minutes and re-reads the wall clock after each one: a laptop that wakes after
  the scan time runs within one chunk, and the job's own catch-up covers the missed sessions.
* **Nothing kills the loop.** Any exception in a pass of the loop (even one nobody foresaw, like a
  stored session that is not a date) is logged and waited out for 15 minutes. A job that raises, one that is already running (the "Run scan now"
  button) and a failed catch-up are logged and skipped. Only cancellation ends the task, and it
  propagates. A scheduler that cannot be built (no Alpaca keys), or cannot reach the database or
  the calendar, says why in ``scheduler_state()`` and never stops the API from starting.
* **Quiet by default in tests and when switched off.** ``scheduler_enabled()`` reads the environment
  when it is called (the lifespan calls it after ``load_env()``, so ``ATLAS_DISABLE_SCHEDULER=1``
  works from ``.env``), and under pytest ``TestClient(app)`` never starts it.

Nothing here reaches Alpaca, Telegram or the database: the clock, the sleep, the calendar, the
store and ``run_job`` are fakes, and the lifespan tests replace the scheduler or the Alpaca client.
"""

from __future__ import annotations

import asyncio
import os
import time as wall_time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from signal_service import main as api
from swing_engine import scheduler
from swing_engine.calendar import Session, TradingCalendar
from swing_engine.data.alpaca import AlpacaError
from swing_engine.job import JobBusy, JobDeps
from swing_engine.scheduler import (
    CALENDAR_RETRY,
    MAX_SLEEP_S,
    next_scan_at,
    scheduler_enabled,
    scheduler_state,
    swing_scheduler,
)

ET = ZoneInfo("America/New_York")
HOLIDAYS = {date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
HALF_DAYS = {date(2026, 11, 27), date(2026, 12, 24)}


def _sessions() -> list[Session]:
    """NYSE sessions from Aug 2026 to Jan 2027: weekdays minus holidays, 13:00 closes on half days."""
    sessions = []
    day = date(2026, 8, 3)
    while day <= date(2027, 1, 29):
        if day.weekday() < 5 and day not in HOLIDAYS:
            close = time(13, 0) if day in HALF_DAYS else time(16, 0)
            sessions.append(Session(day, time(9, 30), close))
        day += timedelta(days=1)
    return sessions


SESSIONS = _sessions()
CAL = TradingCalendar(SESSIONS)
THU, FRI, MON, TUE = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)
MON_1000_ET = datetime(2026, 10, 19, 10, 0, tzinfo=ET)
MON_1700_ET = datetime(2026, 10, 19, 17, 0, tzinfo=ET)
MON_SCAN = datetime(2026, 10, 19, 16, 45, tzinfo=ET)
QUARTER_HOUR = timedelta(minutes=15)
MON_1620_ET = datetime(2026, 10, 19, 16, 20, tzinfo=ET)  # Monday's scan is due at 16:45
# With Thursday scanned, Friday is owed at 16:20 Monday (its scan time has passed).
TUE_SCAN = datetime(2026, 10, 20, 16, 45, tzinfo=ET)


# ------------------------------------------------------------------------ the brief's tests


def test_next_scan_weekend_rolls_to_monday():
    friday_evening = datetime(2026, 10, 16, 17, 0, tzinfo=ET)
    assert next_scan_at(CAL, friday_evening) == datetime(2026, 10, 19, 16, 45, tzinfo=ET)


def test_next_scan_half_day():
    assert next_scan_at(CAL, datetime(2026, 11, 27, 9, 0, tzinfo=ET)) == datetime(
        2026, 11, 27, 13, 45, tzinfo=ET
    )


def test_next_scan_after_dst_end_is_2145_utc():
    when = next_scan_at(CAL, datetime(2026, 11, 2, 12, 0, tzinfo=ET))
    assert when.astimezone(UTC) == datetime(2026, 11, 2, 21, 45, tzinfo=UTC)


def test_scheduler_disabled_under_pytest():
    assert scheduler_enabled() is False


# ------------------------------------------------------------------------ next_scan_at


def test_next_scan_is_strictly_after_now():
    """At exactly the scan time the next one is tomorrow's, so a run never fires twice."""
    assert next_scan_at(CAL, MON_SCAN) == TUE_SCAN
    assert next_scan_at(CAL, MON_SCAN - timedelta(seconds=1)) == MON_SCAN


def test_next_scan_before_the_open_is_that_days_scan():
    assert next_scan_at(CAL, datetime(2026, 10, 20, 3, 0, tzinfo=ET)) == TUE_SCAN


def test_next_scan_skips_a_holiday_and_takes_utc_input():
    """Thanksgiving is closed; Wed 25 Nov 22:00 UTC is 17:00 ET, past that day's scan."""
    wed_evening_utc = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    assert next_scan_at(CAL, wed_evening_utc) == datetime(2026, 11, 27, 13, 45, tzinfo=ET)


def test_next_scan_takes_the_delay_as_an_argument():
    assert next_scan_at(CAL, MON_1000_ET, delay_minutes=60) == datetime(
        2026, 10, 19, 17, 0, tzinfo=ET
    )


def test_next_scan_needs_an_aware_time():
    with pytest.raises(ValueError, match="timezone"):
        next_scan_at(CAL, datetime(2026, 10, 19, 10, 0))


def test_next_scan_past_the_end_of_the_calendar_is_a_lookup_error():
    with pytest.raises(LookupError):
        next_scan_at(CAL, datetime(2027, 2, 1, 12, 0, tzinfo=ET))


@pytest.mark.parametrize("delay", [45, 479, 480, 600, 1439, 1440, 1500])
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 20, 1, 0, tzinfo=ET),  # Tue 01:00: a long delay still owes Monday's
        datetime(2026, 10, 17, 12, 0, tzinfo=ET),  # Saturday
        datetime(2026, 10, 19, 23, 59, 59, tzinfo=ET),
        datetime(2026, 11, 2, 0, 30, tzinfo=ET),  # the night after DST ended
    ],
    ids=["tue-0100", "saturday", "mon-2359", "after-dst"],
)
def test_next_scan_equals_a_brute_force_search_for_any_delay(now, delay):
    """The search must start early enough that a session whose scan time is still ahead (a long
    delay pushes it past midnight) is never skipped, whatever the delay."""
    expected = min(
        when
        for s in SESSIONS
        if (when := CAL.scan_time(s.day, delay)).astimezone(UTC) > now.astimezone(UTC)
    )
    assert next_scan_at(CAL, now, delay) == expected


# ------------------------------------------------------------------------ scheduler_enabled


def test_scheduler_enabled_reads_the_environment_when_called(monkeypatch):
    """Not a module constant: ``.env`` is bridged into ``os.environ`` after import."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)
    assert scheduler_enabled() is True
    monkeypatch.setenv("ATLAS_DISABLE_SCHEDULER", "1")
    assert scheduler_enabled() is False
    monkeypatch.setenv("ATLAS_DISABLE_SCHEDULER", "0")
    assert scheduler_enabled() is True
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test (call)")
    assert scheduler_enabled() is False


# ------------------------------------------------------------------------ the fakes


class FakeLog:
    """A logger that records ``(level, event, fields)`` instead of printing."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(event: str, **fields: Any) -> None:
            self.records.append((level, event, fields))

        return record

    def text(self) -> str:
        return "\n".join(f"{lvl} {event} {fields}" for lvl, event, fields in self.records)


class FakeStore:
    """The one store call the scheduler makes, ``latest_scan``: the database can be down, or hand
    back a row the scheduler cannot compare."""

    def __init__(self, latest: date | None) -> None:
        self.latest = latest
        self.down_for = 0
        self.garbled = 0
        self.calls = 0

    async def latest_scan(self) -> dict[str, Any] | None:
        self.calls += 1
        if self.down_for > 0:
            self.down_for -= 1
            raise ConnectionRefusedError("connect call failed ('127.0.0.1', 5432)")
        if self.garbled > 0:  # a row the code did not expect: the session is not a date
            self.garbled -= 1
            return {"session": "2026-10-16T00:00:00 s3cr3t-alpaca-value"}
        return None if self.latest is None else {"session": self.latest}


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class Rig:
    """A fake clock, sleep, calendar, store and ``run_job`` wired to ``swing_scheduler``.

    ``stop_after_runs(n)`` makes the n-th ``run_job`` raise ``CancelledError`` (after recording it)
    so a test can drive the otherwise endless loop and see it end the way a real shutdown does.
    ``effects`` maps a 1-based ``run_job`` call number to an exception it raises instead of
    returning (or to a function that does something and returns the exception to raise, or None),
    ``statuses`` maps a call number to the status of the run it returns instead of ``ok``, and
    ``oversleep`` maps a 1-based sleep number to extra seconds the clock jumps (a laptop asleep).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = MON_1000_ET
        self.store = FakeStore(FRI)
        self.client = FakeClient()
        self.sleeps: list[float] = []
        self.states: list[dict[str, Any]] = []
        self.runs: list[tuple[str, datetime]] = []
        self.job_deps: list[JobDeps] = []
        self.effects: dict[int, BaseException | Callable[[], BaseException | None]] = {}
        self.statuses: dict[int, str] = {}
        self.oversleep: dict[int, float] = {}
        self.calendar_calls = 0
        self.calendar_down_for = 0
        self.stop_at: int | None = None
        self.hang: asyncio.Event | None = None
        self.log = FakeLog()
        monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
        monkeypatch.setattr(scheduler, "run_job", self.run_job)
        monkeypatch.setattr(scheduler, "log", self.log)

    def start(self, at: datetime, latest: date | None = FRI) -> None:
        self.clock, self.store.latest = at, latest

    def stop_after_runs(self, n: int) -> None:
        self.stop_at = n

    def now(self) -> datetime:
        return self.clock

    async def sleep(self, seconds: float) -> None:
        assert len(self.sleeps) < 5000, "the scheduler is spinning"
        self.sleeps.append(seconds)
        self.states.append(scheduler_state())
        extra = self.oversleep.get(len(self.sleeps), 0.0)
        self.clock += timedelta(seconds=seconds + extra)
        await asyncio.sleep(0)

    async def calendar(self) -> TradingCalendar:
        self.calendar_calls += 1
        if self.calendar_down_for > 0:
            self.calendar_down_for -= 1
            raise RuntimeError("network error on /v2/calendar")
        return CAL

    async def send(self, text: str) -> Any:
        raise AssertionError("the scheduler never sends anything itself")

    def make_deps(self) -> JobDeps:
        return JobDeps(
            client=self.client,  # type: ignore[arg-type]
            calendar=self.calendar,
            bars_store=object(),  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            send=self.send,
            repo=Path("."),
            now=self.now,
        )

    async def run_job(self, deps: JobDeps, kind: str) -> dict[str, Any]:
        """Stands in for ``swing_engine.job.run_job``; like the real one it reads the calendar."""
        number = len(self.runs) + 1
        self.runs.append((kind, self.clock))
        self.job_deps.append(deps)
        if self.hang is not None:
            await self.hang.wait()
        await deps.calendar()
        effect = self.effects.get(number)
        if callable(effect):
            effect = effect()
        if effect is not None:
            raise effect
        if self.stop_at is not None and number >= self.stop_at:
            raise asyncio.CancelledError
        return {"status": self.statuses.get(number, "ok"), "kind": kind, "session": None}

    async def drive(self) -> None:
        """Run the scheduler to the ``stop_after_runs`` cancellation, as a shutdown would end it."""
        with pytest.raises(asyncio.CancelledError):
            await swing_scheduler(self.make_deps, sleep=self.sleep)


@pytest.fixture
def rig(monkeypatch) -> Rig:
    return Rig(monkeypatch)


# ------------------------------------------------------------------------ startup catch-up


async def test_catch_up_runs_when_nothing_was_ever_scanned(rig):
    rig.start(MON_1000_ET, latest=None)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1000_ET)] and rig.sleeps == []


async def test_catch_up_runs_when_the_last_scan_is_behind_the_last_due_session(rig):
    """Mon 17:00 ET: Monday's scan time (16:45) has passed but only Friday's was scanned."""
    rig.start(MON_1700_ET, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1700_ET)]


async def test_no_catch_up_when_the_last_due_session_was_scanned(rig):
    rig.start(MON_1700_ET, latest=MON)
    rig.stop_after_runs(1)

    await rig.drive()

    # the first (and only) run is tomorrow's scheduled one, not a catch-up
    assert rig.runs == [("nightly", TUE_SCAN)]


async def test_no_catch_up_before_todays_scan_time(rig):
    """Mon 10:00 ET: the last session whose scan time has passed is Friday's, which was scanned."""
    rig.start(MON_1000_ET, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert [kind for kind, _ in rig.runs] == ["nightly"]


async def test_starting_between_the_settle_time_and_the_scan_time_waits_for_the_nightly(rig):
    """Mon 16:30 ET is after the close + 16 minutes (the bars exist) but before the 16:45 scan
    time: with Friday scanned there is nothing due, so no catch-up; the 16:45 nightly runs so
    tonight's digest uses settled bars."""
    at_1630 = datetime(2026, 10, 19, 16, 30, tzinfo=ET)
    rig.start(at_1630, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("nightly", MON_SCAN)]
    assert sum(rig.sleeps) == (MON_SCAN - at_1630).total_seconds()


async def test_starting_just_after_the_scan_time_catches_up(rig):
    at_1646 = datetime(2026, 10, 19, 16, 46, tzinfo=ET)
    rig.start(at_1646, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("catchup", at_1646)]


async def test_starting_before_the_scan_time_still_catches_up_an_older_gap(rig):
    """Mon 16:30 ET with Thursday scanned: Friday's scan time passed, so Friday is owed."""
    at_1630 = datetime(2026, 10, 19, 16, 30, tzinfo=ET)
    rig.start(at_1630, latest=THU)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("catchup", at_1630)]


async def test_starting_before_the_scan_time_with_nothing_ever_scanned_catches_up(rig):
    at_1630 = datetime(2026, 10, 19, 16, 30, tzinfo=ET)
    rig.start(at_1630, latest=None)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("catchup", at_1630)]


async def test_no_catch_up_on_a_weekend_when_friday_was_scanned(rig):
    rig.start(datetime(2026, 10, 17, 12, 0, tzinfo=ET), latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.runs == [("nightly", MON_SCAN)]


# ------------------------------------------------------------------------ the nightly loop


async def test_the_loop_runs_nightly_at_the_next_scan_time_then_the_one_after(rig):
    rig.start(MON_1000_ET, latest=FRI)
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("nightly", MON_SCAN), ("nightly", TUE_SCAN)]


async def test_state_shows_the_next_scan_while_waiting(rig):
    rig.start(MON_1000_ET, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.states and all(s["running"] is True for s in rig.states)
    assert {s["next_scan_at"] for s in rig.states} == {MON_SCAN}
    assert all(s["disabled_reason"] is None for s in rig.states)
    assert set(rig.states[0]) == {"running", "next_scan_at", "disabled_reason"}


async def test_sleep_is_chunked_to_five_minutes_and_ends_at_the_scan_time(rig):
    rig.start(MON_1000_ET, latest=FRI)
    rig.stop_after_runs(1)

    await rig.drive()

    assert max(rig.sleeps) <= MAX_SLEEP_S == 300
    assert sum(rig.sleeps) == (MON_SCAN - MON_1000_ET).total_seconds()
    assert rig.runs == [("nightly", MON_SCAN)]


async def test_a_laptop_that_wakes_after_the_scan_time_runs_at_once(rig):
    """Asleep through the scan: the third chunk 'lasts' 9 hours of wall clock. The next loop
    re-reads the clock and runs the job then, not a chunk later and not tomorrow."""
    rig.start(MON_1000_ET, latest=FRI)
    rig.oversleep = {3: 9 * 3600}
    rig.stop_after_runs(1)

    await rig.drive()

    woke = MON_1000_ET + timedelta(seconds=3 * 300 + 9 * 3600)
    assert woke > MON_SCAN and len(rig.sleeps) == 3
    assert rig.runs == [("nightly", woke)]


# ------------------------------------------------------------------------ nothing kills the loop


async def test_an_exception_in_a_run_is_logged_and_the_next_night_still_runs(rig, monkeypatch):
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-alpaca-value")
    rig.effects = {1: RuntimeError("alpaca refused key s3cr3t-alpaca-value\n[SQL: SELECT 1]")}
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("nightly", MON_SCAN), ("nightly", TUE_SCAN)]
    warnings = [r for r in rig.log.records if r[0] == "warning"]
    assert len(warnings) == 1 and "alpaca refused key" in rig.log.text()
    assert "s3cr3t-alpaca-value" not in rig.log.text() and "SELECT 1" not in rig.log.text()


async def test_a_job_that_is_already_running_is_skipped_and_logged(rig):
    rig.effects = {1: JobBusy("a scan is already running")}
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("nightly", MON_SCAN), ("nightly", TUE_SCAN)]
    assert "already running" in rig.log.text()
    assert not [r for r in rig.log.records if r[0] in ("warning", "error")]


async def test_a_failed_catch_up_does_not_stop_the_loop(rig):
    rig.start(MON_1700_ET, latest=FRI)
    rig.effects = {1: RuntimeError("alpaca 503")}
    rig.stop_after_runs(3)

    await rig.drive()

    # the failure is retried 15 minutes later, then the nightly schedule carries on
    assert rig.runs == [
        ("catchup", MON_1700_ET),
        ("catchup", MON_1700_ET + CALENDAR_RETRY),
        ("nightly", TUE_SCAN),
    ]


async def test_an_unexpected_error_in_the_loop_is_logged_and_waited_out(rig, monkeypatch):
    """The loop catches everything but cancellation. Here the store hands back a session that is
    not a date, so the startup comparison raises ``TypeError`` once; the scheduler logs it (no
    secret in it), says so in its state, waits 15 minutes and goes on to run the nightly."""
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-alpaca-value")
    rig.store.garbled = 1
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.store.calls == 2 and rig.runs == [("nightly", MON_SCAN)]
    first = rig.states[0]
    assert first["running"] is False and first["next_scan_at"] is None
    assert "unexpected error" in first["disabled_reason"]
    assert "15 minutes" in first["disabled_reason"]
    assert rig.sleeps[:3] == [300.0, 300.0, 300.0]
    assert any(r[0] == "warning" and "unexpected error" in str(r) for r in rig.log.records)
    assert "s3cr3t-alpaca-value" not in rig.log.text()
    assert "s3cr3t-alpaca-value" not in str(first["disabled_reason"])
    assert rig.states[-1]["running"] is True and rig.states[-1]["disabled_reason"] is None


async def test_a_scan_that_falls_due_while_waiting_out_an_error_still_runs(rig):
    """Mon 16:20: the catch-up finds a job running, and the re-check at 16:35 hits an unexpected
    error. The 15-minute wait ends at 16:50, after the 16:45 scan time: that night's scan runs
    then, it is not pushed to tomorrow's."""
    rig.start(MON_1620_ET, latest=THU)

    def busy_then_garbled() -> BaseException:
        rig.store.garbled = 1  # the next store read (the re-check) raises TypeError
        return JobBusy("a scan is already running")

    rig.effects = {1: busy_then_garbled}
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1620_ET), ("nightly", MON_1620_ET + 2 * QUARTER_HOUR)]
    assert MON_SCAN < rig.runs[1][1] < TUE_SCAN


# ------------------------------------------------------------------------ catch-up retries


async def test_a_catch_up_that_fails_twice_is_retried_every_15_minutes_until_it_works(rig):
    """The Mac boots before Postgres or Docker is ready: Today must not stay stale until tomorrow."""
    rig.start(MON_1700_ET, latest=FRI)
    rig.effects = {1: RuntimeError("connection refused"), 2: RuntimeError("connection refused")}
    rig.stop_after_runs(4)

    await rig.drive()

    assert rig.runs == [
        ("catchup", MON_1700_ET),
        ("catchup", MON_1700_ET + QUARTER_HOUR),
        ("catchup", MON_1700_ET + 2 * QUARTER_HOUR),
        ("nightly", TUE_SCAN),  # the third attempt worked: no more catch-ups
    ]
    assert max(rig.sleeps) <= MAX_SLEEP_S  # the retry waits are chunked like every other wait
    assert len([r for r in rig.log.records if r[0] == "warning"]) == 2


async def test_a_run_that_comes_back_not_ok_counts_as_failed_and_is_retried(rig):
    rig.start(MON_1700_ET, latest=FRI)
    rig.statuses = {1: "failed"}  # run_job records step failures in the row instead of raising
    rig.stop_after_runs(3)

    await rig.drive()

    assert rig.runs == [
        ("catchup", MON_1700_ET),
        ("catchup", MON_1700_ET + QUARTER_HOUR),
        ("nightly", TUE_SCAN),
    ]


async def test_a_catch_up_that_keeps_failing_gives_way_to_the_nightly_when_it_is_due(rig):
    """Mon 16:20 ET: 16:20 and 16:35 fail; 16:50 would be after the 16:45 scan, so that runs."""
    rig.start(MON_1620_ET, latest=THU)
    rig.effects = {1: RuntimeError("alpaca 503"), 2: RuntimeError("alpaca 503")}
    rig.stop_after_runs(3)

    await rig.drive()

    assert rig.runs == [
        ("catchup", MON_1620_ET),
        ("catchup", MON_1620_ET + QUARTER_HOUR),
        ("nightly", MON_SCAN),
    ]


async def test_the_nightly_ends_the_retries_even_when_it_fails_too(rig):
    rig.start(MON_1620_ET, latest=THU)
    rig.effects = {i: RuntimeError("alpaca 503") for i in (1, 2, 3)}
    rig.stop_after_runs(4)

    await rig.drive()

    # 16:20, 16:35 catch-ups, the 16:45 nightly, then tomorrow's nightly: no catch-up after it
    assert [(kind, at) for kind, at in rig.runs] == [
        ("catchup", MON_1620_ET),
        ("catchup", MON_1620_ET + QUARTER_HOUR),
        ("nightly", MON_SCAN),
        ("nightly", TUE_SCAN),
    ]


async def test_a_catch_up_that_succeeds_is_not_retried(rig):
    rig.start(MON_1700_ET, latest=FRI)
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1700_ET), ("nightly", TUE_SCAN)]
    assert set(rig.sleeps) == {MAX_SLEEP_S}  # straight on to waiting for tomorrow's scan
    assert sum(rig.sleeps) == (TUE_SCAN - MON_1700_ET).total_seconds()


async def test_a_job_running_elsewhere_is_rechecked_after_15_minutes_and_not_rerun_if_done(rig):
    """JobBusy: someone else (the Run scan now button) is scanning. If the last due session is
    stored 15 minutes later, there is nothing left to catch up."""
    rig.start(MON_1700_ET, latest=FRI)

    def the_other_job_finishes() -> BaseException:
        rig.store.latest = MON
        return JobBusy("a scan is already running")

    rig.effects = {1: the_other_job_finishes}
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1700_ET), ("nightly", TUE_SCAN)]
    assert rig.store.calls == 2  # the startup check and the re-check
    assert "already running" in rig.log.text()


async def test_a_job_running_elsewhere_that_has_not_finished_the_session_is_caught_up_later(rig):
    rig.start(MON_1700_ET, latest=FRI)
    rig.effects = {1: JobBusy("a scan is already running")}  # and the session is still unscanned
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1700_ET), ("catchup", MON_1700_ET + QUARTER_HOUR)]
    assert rig.store.calls == 2


async def test_a_busy_catch_up_gives_way_to_the_nightly_when_it_is_due(rig):
    rig.start(MON_1620_ET, latest=THU)
    rig.effects = {1: JobBusy("a scan is already running"), 2: JobBusy("a scan is already running")}
    rig.stop_after_runs(3)

    await rig.drive()

    assert [kind for kind, _ in rig.runs] == ["catchup", "catchup", "nightly"]
    assert rig.runs[-1][1] == MON_SCAN


async def test_a_catch_up_that_runs_past_the_scan_time_is_followed_by_the_nightly_at_once(rig):
    """A slow first run (a long backfill) ends after tonight's scan time: that scan is not skipped."""
    rig.start(MON_1620_ET, latest=THU)

    def takes_an_hour() -> BaseException | None:
        rig.clock += timedelta(hours=1)
        return None

    rig.effects = {1: takes_an_hour}
    rig.stop_after_runs(2)

    await rig.drive()

    assert rig.runs == [("catchup", MON_1620_ET), ("nightly", MON_1620_ET + timedelta(hours=1))]


# ------------------------------------------------------------------------ cancellation


async def test_cancelling_while_waiting_propagates_and_stops_the_scheduler(rig):
    gate = asyncio.Event()

    async def hang(seconds: float) -> None:
        await gate.wait()

    task = asyncio.create_task(swing_scheduler(rig.make_deps, sleep=hang))
    for _ in range(50):
        if scheduler_state()["running"]:
            break
        await asyncio.sleep(0)
    assert scheduler_state() == {
        "running": True,
        "next_scan_at": MON_SCAN,
        "disabled_reason": None,
    }

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = scheduler_state()
    assert state["running"] is False and state["next_scan_at"] is None
    assert state["disabled_reason"]
    assert rig.client.closed is True  # the scheduler owns the client it was built with


async def test_cancelling_during_a_run_propagates_and_is_not_swallowed(rig):
    rig.hang = asyncio.Event()  # the job never finishes

    task = asyncio.create_task(swing_scheduler(rig.make_deps, sleep=rig.sleep))
    for _ in range(5000):
        if rig.runs:
            break
        await asyncio.sleep(0)
    assert rig.runs, "the job never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert scheduler_state()["running"] is False and rig.client.closed is True
    assert not [r for r in rig.log.records if r[0] in ("warning", "error")]


# ------------------------------------------------------------------------ fail-soft start


async def test_a_build_failure_disables_the_scheduler_with_a_reason_and_does_not_raise(
    rig, monkeypatch
):
    monkeypatch.setenv("ALPACA_API_SECRET", "s3cr3t-alpaca-value")

    def no_keys() -> JobDeps:
        raise AlpacaError(
            "ALPACA_API_KEY and ALPACA_API_SECRET are not configured: s3cr3t-alpaca-value"
        )

    await swing_scheduler(no_keys, sleep=rig.sleep)  # returns; the API goes on without it

    state = scheduler_state()
    assert state["running"] is False and state["next_scan_at"] is None
    assert "ALPACA_API_KEY" in state["disabled_reason"]
    assert "s3cr3t-alpaca-value" not in state["disabled_reason"]
    assert rig.runs == [] and rig.sleeps == []
    assert any(r[0] == "warning" for r in rig.log.records)


async def test_the_deps_are_built_once_when_the_task_starts_not_before(rig):
    built: list[JobDeps] = []

    def make() -> JobDeps:
        built.append(rig.make_deps())
        return built[-1]

    rig.stop_after_runs(1)
    assert built == []  # nothing is built just by creating the function
    with pytest.raises(asyncio.CancelledError):
        await swing_scheduler(make, sleep=rig.sleep)
    assert len(built) == 1


async def test_an_unreachable_database_waits_and_retries_instead_of_giving_up(rig):
    rig.store.down_for = 2
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.store.calls == 3
    waiting = [s for s in rig.states if s["running"] is False]
    assert waiting and "database" in waiting[0]["disabled_reason"]
    assert "connect call failed" in waiting[0]["disabled_reason"]
    assert "15 minutes" in waiting[0]["disabled_reason"]
    retry = int(CALENDAR_RETRY.total_seconds())
    assert sum(rig.sleeps[: retry // 300]) == retry  # the first retry waits 15 minutes
    assert rig.runs == [("nightly", MON_SCAN)]
    assert all(s["disabled_reason"] is None for s in rig.states if s["running"])


# ------------------------------------------------------------------------ the calendar


async def test_the_calendar_is_fetched_once_per_new_york_date(rig):
    """The startup check, the loop and both jobs share one fetch per ET date: two nights, two."""
    rig.start(MON_1000_ET, latest=FRI)
    rig.stop_after_runs(2)

    await rig.drive()

    assert len(rig.runs) == 2
    assert rig.calendar_calls == 2  # Monday's and Tuesday's


async def test_the_calendar_the_job_gets_is_the_cached_one(rig):
    """``run_job`` reads ``deps.calendar()`` itself: it must not cost another fetch."""
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.calendar_calls == 1  # the startup check and the job's own read share the fetch
    assert rig.job_deps[0].calendar != rig.calendar  # the job was handed the caching wrapper


async def test_a_failed_calendar_fetch_is_logged_and_retried_in_15_minutes(rig):
    rig.calendar_down_for = 1
    rig.stop_after_runs(1)

    await rig.drive()

    assert rig.calendar_calls >= 2
    first = rig.states[0]
    assert first["running"] is False and first["next_scan_at"] is None
    assert "calendar" in first["disabled_reason"] and "15 minutes" in first["disabled_reason"]
    assert rig.sleeps[:3] == [300.0, 300.0, 300.0]  # 15 minutes, in 5-minute chunks
    assert any(r[0] == "warning" and "calendar" in str(r) for r in rig.log.records)
    assert rig.runs == [("nightly", MON_SCAN)]  # then the schedule carried on
    last = rig.states[-1]  # after the retry the scheduler is waiting for Monday's scan again
    assert last["running"] is True and last["next_scan_at"] == MON_SCAN
    assert last["disabled_reason"] is None


# ------------------------------------------------------------------------ the lifespan


@pytest.fixture
def api_keys_off(monkeypatch):
    """The lifespan must never read the real ``.env`` or reach Alpaca in these tests."""
    monkeypatch.setattr(api, "load_env", lambda: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)


def test_testclient_startup_under_pytest_does_not_start_the_scheduler(monkeypatch, api_keys_off):
    started: list[object] = []

    async def must_not_run(make_deps: object) -> None:
        started.append(make_deps)

    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.setattr(api, "swing_scheduler", must_not_run)
    assert "PYTEST_CURRENT_TEST" in os.environ  # the premise: this is what disables it

    with TestClient(api.app) as client:
        assert client.get("/healthz").status_code == 200
        state = scheduler_state()

    assert started == []
    assert state["running"] is False and state["next_scan_at"] is None
    assert state["disabled_reason"] and "off" in state["disabled_reason"].lower()


def _run_lifespan_with_fake_scheduler(monkeypatch) -> tuple[list[object], list[bool]]:
    """Start and stop the app with the scheduler enabled and replaced by a task that waits."""
    started: list[object] = []
    cancelled: list[bool] = []

    async def forever(make_deps: object) -> None:
        started.append(make_deps)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(api, "swing_scheduler", forever)
    with TestClient(api.app) as client:
        assert client.get("/healthz").status_code == 200
        for _ in range(100):
            if started:
                break
            wall_time.sleep(0.01)
    return started, cancelled


def test_lifespan_starts_the_scheduler_when_enabled_and_cancels_it_on_shutdown(
    monkeypatch, api_keys_off
):
    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)

    started, cancelled = _run_lifespan_with_fake_scheduler(monkeypatch)

    assert started == [api._make_swing_deps]
    assert cancelled == [True]


def test_disable_flag_set_by_load_env_is_honoured(monkeypatch):
    """``scheduler_enabled()`` runs after ``load_env()``: a flag that only ``.env`` sets counts."""
    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)
    # What load_env() does with `.env`; monkeypatch undoes it, whatever conftest.py is around.
    monkeypatch.setattr(api, "load_env", lambda: monkeypatch.setenv("ATLAS_DISABLE_SCHEDULER", "1"))

    started, _ = _run_lifespan_with_fake_scheduler(monkeypatch)

    assert started == []
    assert "ATLAS_DISABLE_SCHEDULER" in scheduler_state()["disabled_reason"]


def test_enabled_by_default_when_load_env_sets_nothing(monkeypatch, api_keys_off):
    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)

    started, _ = _run_lifespan_with_fake_scheduler(monkeypatch)

    assert len(started) == 1


def test_missing_alpaca_keys_leave_the_api_up_and_say_why(monkeypatch, api_keys_off):
    """The deps are built inside the task, so a failure there is the scheduler's, not the API's."""
    monkeypatch.setattr(scheduler, "_STATE", scheduler._State())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ATLAS_DISABLE_SCHEDULER", raising=False)

    def no_client() -> object:
        raise AlpacaError("ALPACA_API_KEY and ALPACA_API_SECRET are not configured")

    monkeypatch.setattr(api, "AlpacaData", no_client)

    with TestClient(api.app) as client:
        for _ in range(300):
            if scheduler_state()["disabled_reason"] and "ALPACA" in str(
                scheduler_state()["disabled_reason"]
            ):
                break
            wall_time.sleep(0.01)
        assert client.get("/healthz").status_code == 200
        state = scheduler_state()

    assert state["running"] is False
    assert "ALPACA_API_KEY" in state["disabled_reason"]


def test_make_deps_builds_the_live_deps_for_this_checkout(monkeypatch):
    made: dict[str, Any] = {}
    client = object()

    monkeypatch.setattr(api, "AlpacaData", lambda: client)

    def fake_live_deps(c: object, repo: Path) -> str:
        made["client"], made["repo"] = c, repo
        return "deps"

    monkeypatch.setattr(api, "live_deps", fake_live_deps)

    assert api._make_swing_deps() == "deps"
    assert made["client"] is client
    assert (made["repo"] / "ml" / "validation").is_dir()  # where the setups' reports live
    assert made["repo"] == Path(__file__).resolve().parents[1]
