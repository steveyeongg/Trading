"""The after-close scheduler: runs the nightly job inside the API process, once per session.

:func:`swing_scheduler` is a background task for the FastAPI lifespan. It does two things:

1. **Catch-up on start.** When nothing was ever scanned, or the latest scanned session is earlier
   than the last completed one, it runs the job once as ``catchup`` (the Mac was off or asleep
   at the close). The job itself scans every missed session in order and sends one digest.
2. **The nightly loop.** It sleeps until :func:`next_scan_at` (the close plus 45 minutes in New
   York, so 13:45 on a half day and 16:45 EST after DST ends), runs the job as ``nightly``, and
   starts over.

The schedule is built to survive a laptop. Sleeping is done in chunks of at most
:data:`MAX_SLEEP_S`, and the time left is re-read from the wall clock (``deps.now()``) after each
chunk, because macOS pauses the monotonic clock while the Mac sleeps: a machine that wakes after
the scan time runs the job within one chunk instead of waiting out a stale timer. The calendar is
fetched at most once per New York date and shared with the job.

Nothing here may stop the API from starting or the loop from going on. Every exception except
cancellation is logged (with secrets removed) and the loop continues: a job that raises, one that
finds another job running (:class:`~swing_engine.job.JobBusy`, e.g. the "Run scan now" button),
a calendar or database that cannot be reached (retried every :data:`CALENDAR_RETRY`). The one
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
from typing import Any

from atlas_shared.logging import get_logger

from swing_engine.calendar import ET, TradingCalendar
from swing_engine.job import SETTLE_MINUTES, JobBusy, JobDeps, JobKind, _reason, run_job

log = get_logger("swing.scheduler")

SCAN_DELAY_MINUTES = 45
"""A session is scanned this long after its close (the job needs the final SIP bars)."""

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
    # Start far enough back that a long delay still finds an earlier session's scan time.
    start = now.astimezone(ET).date() - timedelta(days=delay_minutes // (24 * 60) + 1)
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

    ``running`` is True while the loop is alive and will fire at ``next_scan_at`` (None while a
    startup catch-up runs). Otherwise ``disabled_reason`` says in words why nothing is scheduled.
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
        if await _needs_catch_up(deps, sleep):
            _set_state(running=True)
            await _run(deps, "catchup")
        while True:
            try:
                when = next_scan_at(await deps.calendar(), deps.now())
            except Exception as exc:  # the calendar, or a calendar that ends too soon
                await _wait_to_retry("the trading calendar", exc, deps, sleep)
                continue
            _set_state(running=True, next_at=when)
            log.info("swing.scheduler.waiting", next_scan_at=when.isoformat())
            await _sleep_until(when, deps.now, sleep)
            await _run(deps, "nightly")
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
    """True when nothing was ever scanned or the last scan is older than the last completed
    session. A calendar or database that cannot be reached is retried, not given up on."""
    while True:
        try:
            target = (await deps.calendar()).last_completed_session(deps.now(), SETTLE_MINUTES)
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
            last_completed=str(target),
            catch_up=behind,
        )
        return bool(behind)


async def _run(deps: JobDeps, kind: JobKind) -> None:
    """One job run. Nothing but cancellation escapes."""
    try:
        run = await run_job(deps, kind)
    except JobBusy:
        log.info("swing.scheduler.skipped", kind=kind, reason="a scan is already running")
    except Exception as exc:
        log.warning("swing.scheduler.run_failed", kind=kind, error=_reason(exc))
        log.debug("swing.scheduler.run_failed_trace", kind=kind, exc_info=True)
    else:
        log.info(
            "swing.scheduler.ran",
            kind=kind,
            status=run.get("status"),
            session=str(run.get("session")),
        )


async def _wait_to_retry(what: str, exc: Exception, deps: JobDeps, sleep: Sleep) -> None:
    """Record that ``what`` cannot be reached, then wait :data:`CALENDAR_RETRY`."""
    reason = _reason(exc)
    minutes = int(CALENDAR_RETRY.total_seconds() // 60)
    log.warning("swing.scheduler.unreachable", what=what, error=reason, retry_minutes=minutes)
    _set_state(
        running=False,
        reason=f"Automatic scans are waiting for {what}: {reason}. Retrying in {minutes} minutes.",
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
