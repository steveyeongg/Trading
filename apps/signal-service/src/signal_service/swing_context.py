"""The API's scan context cache: one :class:`~swing_engine.scan.ScanContext` for the stock and
positions pages.

The nightly job hands its target session's context over (:func:`set_context` is
``JobDeps.on_context``); otherwise :func:`get_context` builds one lazily, once, off the event loop
and under a lock, from the stored bars of the latest scanned session and the open positions'
symbols. It is rebuilt when the universe filters in Settings change and when the store has a
scan of a later session than the cached context (a job that ran outside this process, the CLI or
``ATLAS_DISABLE_SCHEDULER=1``), except while a job runs here, which hands its own over. It never
goes back to an earlier session (see :class:`_Cache`). A request never builds a context itself.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from atlas_shared.logging import get_logger
from swing_engine import job, product_store
from swing_engine.config import UniverseFilter
from swing_engine.job import safe_reason
from swing_engine.loader import load_scan_inputs
from swing_engine.scan import ScanContext, build_context

log = get_logger("swing.api.context")

# What the cache reaches outside itself (module attributes, so tests can swap them).
store: Any = product_store
loader = load_scan_inputs

BUILD_RETRY_S = 60.0
"""After a failed context build, how long requests go without one before the next try."""


@dataclass
class _Cache:
    """The cached context and the universe filter it was built with.

    ``universe`` None marks a context the job handed over (``JobDeps.on_context`` passes the
    context alone); the job built it from the stored settings, so the first read takes the
    settings' filter as its own. ``drop_job_context`` is set when the universe changed while a
    job was running: that job's context was built with the old filters and is not kept.

    The cache never goes back in time: ``newest`` is the latest session any context was offered
    for (a dropped one included), and a context for an earlier session is never cached. Advice
    computed on an older session would be written over newer advice the job stored.
    ``failed`` is the filter, session, time and reason of the last failed build.
    """

    ctx: ScanContext | None = None
    universe: UniverseFilter | None = None
    failed: tuple[UniverseFilter, date | None, float, str] | None = None
    drop_job_context: bool = False
    newest: pd.Timestamp | None = None


_CACHE = _Cache()
_CACHE_LOCK = asyncio.Lock()


def set_context(ctx: ScanContext, universe: UniverseFilter | None = None) -> None:
    """Cache ``ctx`` (``JobDeps.on_context``); ``universe`` is the filter it was built with.

    A context for a session earlier than one already offered is ignored (see :class:`_Cache`).
    """
    _offer(ctx, universe)


def _offer(ctx: ScanContext, universe: UniverseFilter | None) -> bool:
    """Cache ``ctx`` unless it is older than the newest session seen; True when it was cached."""
    cache = _CACHE
    session = pd.Timestamp(ctx.session)
    if universe is None and cache.drop_job_context:
        # The job read the old filters. Its session is still the newest there is, so an older
        # cached context (a lazy build from the scan before) goes too: the next read rebuilds.
        cache.drop_job_context = False
        cache.newest = session if cache.newest is None else max(cache.newest, session)
        if cache.ctx is not None and pd.Timestamp(cache.ctx.session) < session:
            cache.ctx, cache.universe = None, None
        log.info("swing.api.context_dropped", session=str(session.date()))
        return False
    if cache.newest is not None and session < cache.newest:
        log.info(
            "swing.api.older_context_ignored",
            session=str(session.date()),
            newest=str(cache.newest.date()),
        )
        return False
    cache.ctx, cache.universe, cache.failed, cache.newest = ctx, universe, None, session
    return True


def last_failure() -> str | None:
    """Why the last context build failed (no secrets in it), or None."""
    return None if _CACHE.failed is None else _CACHE.failed[3]


def invalidate_context(*, during_job: bool = False) -> None:
    """Forget the cached context (the universe filters changed); the next read rebuilds it."""
    _CACHE.ctx, _CACHE.universe, _CACHE.failed = None, None, None
    _CACHE.drop_job_context = during_job


def _hit(wanted: UniverseFilter, latest: date | None) -> tuple[ScanContext, UniverseFilter] | None:
    """The cached context when it is for ``wanted`` and not older than the latest stored scan
    (a job running here is let finish: it hands its newer context over itself)."""
    cache = _CACHE
    if cache.ctx is None:
        return None
    if cache.universe is None:
        cache.universe = wanted  # the job's context, built from these settings
    if cache.universe != wanted:
        return None
    if latest is not None and cache.ctx.session.date() < latest and not job.is_running():
        return None
    return cache.ctx, wanted


def _build_session(latest: date | None) -> date | None:
    """The session to build: the latest stored scan's, or the newest one offered when that is
    later (a dropped job context), so the build is never refused as older. None: SPY's latest."""
    newest: date | None = None if _CACHE.newest is None else _CACHE.newest.date()
    if latest is None or newest is None:
        return latest if newest is None else newest
    return max(latest, newest)


async def cached_context() -> tuple[ScanContext, UniverseFilter] | None:
    """The context for the stored settings' universe and that filter, or None when there is none.

    A miss (nothing cached, another universe, or a stored scan later than the cached session)
    builds it once: the stored bars of :func:`_build_session` with the open positions' symbols,
    built off the event loop and under a lock, so concurrent requests share one build. A failed
    build is logged and not tried again for :data:`BUILD_RETRY_S` (for the same filter and
    session); meanwhile there is no context.
    """
    wanted = (await store.get_settings()).universe()
    latest = await store.latest_scan_session()
    hit = _hit(wanted, latest)
    if hit is not None:
        return hit
    async with _CACHE_LOCK:
        hit = _hit(wanted, latest)
        if hit is not None:
            return hit
        as_of = _build_session(latest)
        failed = _CACHE.failed
        if (
            failed is not None
            and failed[:2] == (wanted, as_of)
            and time.monotonic() - failed[2] < BUILD_RETRY_S
        ):
            return None
        try:
            ctx = await _build_context(wanted, as_of)
        except Exception as exc:
            reason = safe_reason(exc)
            log.warning("swing.api.context_unavailable", error=reason)
            _CACHE.failed = (wanted, as_of, time.monotonic(), reason)
            return None
        if _offer(ctx, wanted):
            return ctx, wanted
        return _hit(wanted, latest)  # a newer session arrived while this one was built


async def get_context() -> ScanContext | None:
    """The cached scan context (see :func:`cached_context`), or None."""
    cached = await cached_context()
    return None if cached is None else cached[0]


async def _build_context(universe: UniverseFilter, as_of: date | None) -> ScanContext:
    held = sorted({str(p["symbol"]) for p in await store.list_positions("open")})
    panel, kinds, session = await loader(as_of, universe, extra_symbols=held)
    return await asyncio.to_thread(build_context, panel, kinds, session, universe)


async def warm_context() -> None:
    """Fill the cache in the background at startup (when the scheduler is on). Never raises."""
    if job.is_running():
        return  # the running job hands its context over when it is done
    try:
        await get_context()
    except Exception as exc:
        log.warning("swing.api.warm_up_failed", error=safe_reason(exc))
