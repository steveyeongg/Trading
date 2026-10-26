"""ATLAS signal service — Phase 1.

Endpoints:
  GET  /healthz, /readyz
  GET  /v1/signals/{symbol}     — pull latest bars, score, return Signal | null
  POST /v1/scan                 — multi-symbol filtered scan
  /v1/today, /v1/stocks/{symbol}, /v1/positions, /v1/track-record, /v1/settings,
  /v1/scan/run, /v1/system/status — the swing engine's routes (signal_service.swing_routes)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from atlas_shared import get_logger, get_settings, load_env, setup_logging
from atlas_shared import metrics as mx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from swing_engine import product_store
from swing_engine.job import safe_reason
from swing_engine.scheduler import (
    disabled_reason,
    mark_disabled,
    scheduler_enabled,
    swing_scheduler,
)

from signal_service.alert_routes import router as alert_router
from signal_service.auth_dep import router as auth_router
from signal_service.backtest_routes import router as backtest_router
from signal_service.execution_routes import router as execution_router
from signal_service.journal_routes import router as journal_router
from signal_service.portfolio_routes import router as portfolio_router
from signal_service.routes import router
from signal_service.state import load_trend_model
from signal_service.stream import broadcaster
from signal_service.stream import router as stream_router
from signal_service.swing_context import warm_context as warm_swing_context
from signal_service.swing_deps import make_swing_deps
from signal_service.swing_routes import router as swing_router
from signal_service.watchlist import router as watchlist_router

setup_logging()
log = get_logger("signal-service")

MONITOR_INTERVAL = 10.0  # seconds between position-monitor sweeps

# BLUEPRINT §22 #6 — non-negotiable: no auto-execution by default.
# The monitor loop auto-closes positions on stop / target / time, which is an
# execution action. Disabled unless `ATLAS_ENABLE_AUTO_EXECUTION=1` is set.
_AUTO_EXEC_ENABLED = os.environ.get("ATLAS_ENABLE_AUTO_EXECUTION", "0") == "1"


swing_store: Any = product_store
"""The swing job-run store the lifespan closes interrupted runs in (a module attribute, so tests
swap it; under pytest the real one is never touched)."""

JOBS_INTERRUPTED = "interrupted: the API stopped during the run"
"""The error a run left ``running`` by an API that stopped mid-run is closed with at start-up."""

STARTUP_DB_WAIT_S = 5.0
"""How long start-up waits for the database to close interrupted runs before going on."""


async def _close_interrupted_jobs() -> None:
    """Mark every swing job run still ``running`` as failed: no job of this process has started
    yet, so such a row was left by an API that stopped mid-run, and it would keep "Run scan now"
    refused and Today's progress showing forever. Never raises and never blocks start-up for
    more than :data:`STARTUP_DB_WAIT_S`; under pytest only an injected store is used."""
    if swing_store is product_store and "pytest" in sys.modules:
        return
    try:
        closed = await asyncio.wait_for(
            swing_store.fail_running_jobs(JOBS_INTERRUPTED), STARTUP_DB_WAIT_S
        )
    except Exception as exc:
        log.warning("swing.interrupted_jobs_not_closed", error=safe_reason(exc))
        return
    if closed:
        log.warning("swing.interrupted_jobs_closed", count=closed)


async def _monitor_loop() -> None:
    """Periodically auto-exit open positions on stop / target / time."""
    from execution_service import ExecutionEngine, run_monitor_once

    engine = ExecutionEngine()
    log.info("monitor.start", interval=MONITOR_INTERVAL)
    try:
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            with contextlib.suppress(Exception):
                exits = await run_monitor_once(engine)
                if exits:
                    log.info("monitor.swept", exits=len(exits))
    except asyncio.CancelledError:
        log.info("monitor.stop")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bridge `.env` → os.environ before anything reads it. Fixes the provider
    # status panel, the DeepSeek writer, and the Telegram channel, which all
    # read os.environ directly (pydantic-settings only populates Settings).
    load_env()
    settings = get_settings()
    log.info("signal-service.start", env=settings.env)
    # Warns if the model is missing; also refuses legacy (leaky-label) models unless
    # ATLAS_ALLOW_LEGACY_MODEL=1. A missing or refused model never blocks startup.
    load_trend_model()

    tasks = [asyncio.create_task(broadcaster())]
    if _AUTO_EXEC_ENABLED:
        log.warning(
            "monitor.auto_execution_enabled",
            note="ATLAS_ENABLE_AUTO_EXECUTION=1 — positions will auto-close on stop/target/time",
        )
        tasks.append(asyncio.create_task(_monitor_loop()))
    else:
        log.info("monitor.disabled", reason="auto-execution opt-in; set ATLAS_ENABLE_AUTO_EXECUTION=1 to enable")
    # Before the scheduler can start a job: a run an earlier API left "running" is closed.
    await _close_interrupted_jobs()
    # Evaluated here, after load_env(), so ATLAS_DISABLE_SCHEDULER=1 works from `.env` too. The
    # deps are built inside the task: a failure there is the scheduler's, never the API's.
    if scheduler_enabled():
        tasks.append(asyncio.create_task(swing_scheduler(make_swing_deps)))
        log.info("swing.scheduler.enabled")
    else:
        reason = disabled_reason() or "Automatic scans are off."
        mark_disabled(reason)
        log.info("swing.scheduler.disabled", reason=reason)
    # The swing context cache is warmed in the background (it logs and gives up on a DB error),
    # so the first stock page does not wait out the build. Whether or not the scheduler runs
    # (ATLAS_DISABLE_SCHEDULER=1 still serves the pages); never under pytest.
    if "PYTEST_CURRENT_TEST" not in os.environ:
        tasks.append(asyncio.create_task(warm_swing_context()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        log.info("signal-service.stop")


app = FastAPI(
    title="ATLAS Signal Service",
    version="0.1.0",
    lifespan=lifespan,
)

DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,testserver"
"""The ``Host`` names the API answers by default (``testserver`` is the test client's)."""


def allowed_hosts() -> list[str]:
    """``ATLAS_ALLOWED_HOSTS`` (comma-separated), or :data:`DEFAULT_ALLOWED_HOSTS`. A request
    whose ``Host`` is not one of them gets 400, which stops a DNS-rebinding page from reading
    the API through a name it controls. Read when the app is built, from the shell environment."""
    raw = os.environ.get("ATLAS_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return hosts or DEFAULT_ALLOWED_HOSTS.split(",")


app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

# Dev CORS: Next.js dashboard at :3000 hits this API at :8000. Production
# would lock origins down via env config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(backtest_router)
app.include_router(portfolio_router)
app.include_router(journal_router)
app.include_router(stream_router)
app.include_router(watchlist_router)
app.include_router(alert_router)
app.include_router(auth_router)
app.include_router(execution_router)
app.include_router(swing_router)


@app.middleware("http")
async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Record per-route request count + latency. Uses the route *template*
    (e.g. /v1/signals/{symbol}) as the label to keep cardinality bounded."""
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if path != "/metrics":  # don't observe the scrape itself
        mx.HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path, status=str(response.status_code)
        ).inc()
        mx.HTTP_REQUEST_SECONDS.labels(method=request.method, path=path).observe(
            time.perf_counter() - start
        )
    return response


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = mx.render()
    return Response(content=body, media_type=content_type)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
