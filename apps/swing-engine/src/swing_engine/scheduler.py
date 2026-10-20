"""The after-close scheduler: runs the nightly job inside the API process, once per session.

:func:`swing_scheduler` is a background task for the FastAPI lifespan. It does two things:

1. **Catch-up on start.** When nothing was ever scanned, or the latest scanned session is earlier
   than the last session whose scan time (close plus 45 minutes) has passed, it runs the job as
   ``catchup`` (the Mac was off or asleep at the close). Starting between a close plus 16
   minutes (the bars exist) and its scan time is not a catch-up: the nightly runs at the scan
   time, so the digest is built from settled bars. The job itself scans every missed session in order and sends one digest. A catch-up
   that fails (the job raised, or came back with a status other than ``ok``: the Mac often boots
   before Docker or Postgres is ready) is retried every :data:`CALENDAR_RETRY` until it works or
   the next scheduled scan is due, which then runs as the normal ``nightly``. A catch-up that
   finds another job running (:class:`~swing_engine.job.JobBusy`) is not run again blindly:
   15 minutes later the scheduler checks whether the last completed session has been scanned
   by then and only runs again if it has not.
2. **The nightly loop.** It sleeps until :func:`next_scan_at` (the close plus 45 minutes in New
   York, so 13:45 on a half day and 16:45 EST after DST ends), runs the job as ``nightly``, and
   starts over. A nightly that fails is not retried until the next night.

The schedule is built to survive a laptop. Sleeping is done in chunks of at most
:data:`MAX_SLEEP_S`, and the time left is re-read from the wall clock (``deps.now()``) after each
chunk, because macOS pauses the monotonic clock while the Mac sleeps: a machine that wakes after
the scan time runs the job within one chunk instead of waiting out a stale timer. The calendar is
fetched at most once per New York date and shared with the job.

Nothing here may stop the API from starting or the loop from going on. Every exception except
cancellation is logged (with secrets removed) and the loop continues: a job that raises, one that
finds another job running (:class:`~swing_engine.job.JobBusy`, e.g. the "Run scan now" button),
a calendar or database that cannot be reached, and any other error in a pass of the loop (each
waited out for :data:`CALENDAR_RETRY` before the loop tries again). The one
case that ends the task is deps that cannot be built (no Alpaca keys): that cannot fix itself
without a restart, so the scheduler records why in :func:`scheduler_state` and returns.
Cancellation (shutdown) propagates, and the Alpaca client the deps were built with is closed.

The scheduler starts only when :func:`scheduler_enabled`, which the lifespan evaluates after
``load_env()``. It places no orders; the job only reads data and tells the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from atlas_shared.logging import get_logger

from swing_engine.calendar import ET, TradingCalendar
from swing_engine.job import SCAN_DELAY_MINUTES as SCAN_DELAY_MINUTES  # one constant, shared
from swing_engine.job import JobBusy, JobDeps, JobKind, _reason, run_job

log = get_logger("swing.scheduler")

MAX_SLEEP_S = 300.0
"""The longest single sleep, so a clock jump (the Mac waking) is noticed within this long."""

CALENDAR_RETRY = timedelta(minutes=15)
"""How long to wait before trying the calendar or the database again after a failure."""

Sleep = Callable[[float], Awaitable[None]]


# --- the schedule ----------------------------------------------------------------------


def next_scan_at(
    cal: TradingCalendar, now: datetime, delay_minutes: int = SCAN_DELAY_MINUTES
) -> datetime:
    """The first ``cal.scan_time(d, delay_minutes)`` strictly after ``now`` (New York time).

    A scan time equal to ``now`` has passed, so the result is never the instant it is asked at.
    Raises ``ValueError`` for a naive ``now`` and ``LookupError`` when the calendar ends before
    the next session.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    instant = now.astimezone(UTC)
    # ``start`` is exclusive, and a delay pushes a session's scan time past midnight: step back
    # one day per 24 hours of delay, plus one for the close and one for the exclusive start. The
    # loop below skips every earlier session whose scan time has passed.
    start = now.astimezone(ET).date() - timedelta(days=delay_minutes // (24 * 60) + 2)
    day = cal.next_session(start)
    while (when := cal.scan_time(day, delay_minutes)).astimezone(UTC) <= instant:
        day = cal.next_session(day)
    return when


def scheduler_enabled() -> bool:
    """Whether the lifespan should start the scheduler.

    Off with ``ATLAS_DISABLE_SCHEDULER=1`` and under pytest. Read from the environment when
    called, never at import, so a flag set in ``.env`` (bridged by ``load_env()`` at startup)
    counts.
    """
    return disabled_reason() is None


def disabled_reason() -> str | None:
    """Why :func:`scheduler_enabled` is False, in words for the status page, or None."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return "Automatic scans are off: the API is running under pytest."
    if os.environ.get("ATLAS_DISABLE_SCHEDULER") == "1":
        return "Automatic scans are off: ATLAS_DISABLE_SCHEDULER=1 is set."
    return None


# --- state for the status endpoint ------------------------------------------------------


@dataclass
class _State:
    running: bool = False
    next_scan_at: datetime | None = None
    disabled_reason: str | None = "Automatic scans have not started."


_STATE = _State()


def scheduler_state() -> dict[str, Any]:
    """``{"running": bool, "next_scan_at": datetime | None, "disabled_reason": str | None}``.

    ``running`` is True while the loop is alive: waiting for the next scan or running a job.
    ``next_scan_at`` is then the next scheduled scan (None only while the nightly job itself
    runs). When ``running`` is False, ``next_scan_at`` is None and ``disabled_reason`` says in
    words why: switched off, no Alpaca keys, waiting for the database, calendar or after an
    unexpected error, or stopped.
    """
    return {
        "running": _STATE.running,
        "next_scan_at": _STATE.next_scan_at,
        "disabled_reason": _STATE.disabled_reason,
    }


def mark_disabled(reason: str) -> None:
    """Record that the scheduler is not running and why (the lifespan does this when it is off)."""
    _set_state(running=False, reason=reason)


def _set_state(
    *, running: bool, next_at: datetime | None = None, reason: str | None = None
) -> None:
    _STATE.running, _STATE.next_scan_at, _STATE.disabled_reason = running, next_at, reason


# --- the task --------------------------------------------------------------------------


async def swing_scheduler(
    make_deps: Callable[[], JobDeps], *, sleep: Sleep = asyncio.sleep
) -> None:
    """Catch up once, then run the nightly job after every close, until cancelled.

    ``make_deps`` is called once, here, so a failure to build the dependencies (no Alpaca keys)
    is recorded in :func:`scheduler_state` and returns instead of raising into the lifespan.
    ``sleep`` is injectable for tests.
    """
    try:
        built = make_deps()
    except Exception as exc:
        reason = _reason(exc)
        log.warning("swing.scheduler.disabled", error=reason)
        mark_disabled(f"Automatic scans are off: {reason}")
        return
    deps = dataclasses.replace(built, calendar=_DailyCalendar(built.calendar, built.now))
    try:
        # ``when`` is the next scheduled scan. ``retry_at`` is when the next catch-up attempt is
        # due (None: no catch-up pending), and ``recheck`` says the last attempt found another
        # job running, so the session may be scanned by now.
        when: datetime | None = None
        retry_at: datetime | None = None
        recheck = False
        checked = False  # the startup check has run
        while True:
            try:
                if not checked:
                    retry_at = deps.now() if await _needs_catch_up(deps, sleep) else None
                    checked = True
                if when is None:
                    when = next_scan_at(await deps.calendar(), deps.now())
                _set_state(running=True, next_at=when)
                log.info("swing.scheduler.waiting", next_scan_at=when.isoformat())
                wake = when if retry_at is None else min(retry_at, when)
                await _sleep_until(wake, deps.now, sleep)
                if deps.now() >= when:
                    # The scan is due (or a catch-up ran past it). The nightly job also catches
                    # up every missed session, so it ends the catch-up retries.
                    _set_state(running=True)
                    await _run(deps, "nightly")
                    when, retry_at, recheck = None, None, False
                    continue
                if recheck:
                    recheck = False
                    if not await _needs_catch_up(deps, sleep):  # the other job did it
                        retry_at = None
                        continue
                    if deps.now() >= when:  # the check itself outlasted the wait
                        continue
                outcome = await _run(deps, "catchup")
                retry_at = None if outcome == "ok" else deps.now() + CALENDAR_RETRY
                recheck = outcome == "busy"
                if retry_at is not None:
                    log.info(
                        "swing.scheduler.catch_up_retry", outcome=outcome, at=retry_at.isoformat()
                    )
            except Exception as exc:  # a calendar that ends too soon, or anything not foreseen
                # ``when`` is kept: a scan that came due during the wait still runs afterwards.
                await _wait_to_retry("an unexpected error", exc, deps, sleep)
    finally:
        _set_state(running=False, reason="Automatic scans stopped.")
        await _close(built.client)


class _DailyCalendar:
    """``fetch`` called at most once per New York date; the job is handed this, not ``fetch``.

    A failed fetch is not cached: it raises, and the next call tries again.
    """

    def __init__(
        self, fetch: Callable[[], Awaitable[TradingCalendar]], now: Callable[[], datetime]
    ) -> None:
        self._fetch = fetch
        self._now = now
        self._day: date | None = None
        self._calendar: TradingCalendar | None = None

    async def __call__(self) -> TradingCalendar:
        today = self._now().astimezone(ET).date()
        if self._calendar is None or self._day != today:
            self._calendar = await self._fetch()
            self._day = today
        return self._calendar


async def _needs_catch_up(deps: JobDeps, sleep: Sleep) -> bool:
    """True when nothing was ever scanned or the last scan is older than the last session whose
    scan time (the close plus :data:`SCAN_DELAY_MINUTES`) has passed. Before that time the
    nightly is about to run on settled bars, so there is nothing to catch up yet. A calendar or
    database that cannot be reached is retried, not given up on."""
    while True:
        try:
            target = (await deps.calendar()).last_completed_session(deps.now(), SCAN_DELAY_MINUTES)
        except Exception as exc:
            await _wait_to_retry("the trading calendar", exc, deps, sleep)
            continue
        try:
            latest = await deps.store.latest_scan()
        except Exception as exc:
            await _wait_to_retry("the database", exc, deps, sleep)
            continue
        behind = latest is None or latest["session"] < target
        log.info(
            "swing.scheduler.startup_check",
            last_scanned=None if latest is None else str(latest["session"]),
            last_due=str(target),
            catch_up=behind,
        )
        return bool(behind)


async def _run(deps: JobDeps, kind: JobKind) -> Literal["ok", "failed", "busy"]:
    """One job run and how it went. Nothing but cancellation escapes.

    ``failed`` is a run that raised, or that came back with a status other than ``ok``;
    ``busy`` is another job already running in this process.
    """
    try:
        run = await run_job(deps, kind)
    except JobBusy:
        log.info("swing.scheduler.skipped", kind=kind, reason="a scan is already running")
        return "busy"
    except Exception as exc:
        log.warning("swing.scheduler.run_failed", kind=kind, error=_reason(exc))
        log.debug("swing.scheduler.run_failed_trace", kind=kind, exc_info=True)
        return "failed"
    status = run.get("status")
    log.info("swing.scheduler.ran", kind=kind, status=status, session=str(run.get("session")))
    return "ok" if status == "ok" else "failed"


async def _wait_to_retry(what: str, exc: Exception, deps: JobDeps, sleep: Sleep) -> None:
    """Record that ``what`` went wrong (the database could not be reached, an unexpected error),
    then wait :data:`CALENDAR_RETRY`."""
    reason = _reason(exc)
    minutes = int(CALENDAR_RETRY.total_seconds() // 60)
    log.warning("swing.scheduler.paused", what=what, error=reason, retry_minutes=minutes)
    log.debug("swing.scheduler.paused_trace", what=what, exc_info=True)
    _set_state(
        running=False,
        reason=f"Automatic scans are paused ({what}): {reason}. Retrying in {minutes} minutes.",
    )
    await _sleep_until(deps.now() + CALENDAR_RETRY, deps.now, sleep)


async def _sleep_until(when: datetime, now: Callable[[], datetime], sleep: Sleep) -> None:
    """Sleep until the wall clock reaches ``when``, in chunks of at most :data:`MAX_SLEEP_S`.

    The time left is recomputed from ``now()`` after every chunk, so a clock that jumped (the
    Mac slept) ends the wait at the next check rather than after the original duration.
    """
    while (left := (when - now()).total_seconds()) > 0:
        await sleep(min(left, MAX_SLEEP_S))


async def _close(client: object) -> None:
    """Close the Alpaca client the deps were built with, when it has a close."""
    close = getattr(client, "aclose", None)
    if close is not None:
        with contextlib.suppress(Exception):
            await close()
