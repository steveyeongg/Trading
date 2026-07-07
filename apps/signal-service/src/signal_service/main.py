"""ATLAS signal service — Phase 1.

Endpoints:
  GET  /healthz, /readyz
  GET  /v1/signals/{symbol}     — pull latest bars, score, return Signal | null
  POST /v1/scan                 — multi-symbol filtered scan
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from atlas_shared import get_logger, get_settings, load_env, setup_logging
from atlas_shared import metrics as mx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

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
from signal_service.watchlist import router as watchlist_router

setup_logging()
log = get_logger("signal-service")

MONITOR_INTERVAL = 10.0  # seconds between position-monitor sweeps

# BLUEPRINT §22 #6 — non-negotiable: no auto-execution by default.
# The monitor loop auto-closes positions on stop / target / time, which is an
# execution action. Disabled unless `ATLAS_ENABLE_AUTO_EXECUTION=1` is set.
_AUTO_EXEC_ENABLED = os.environ.get("ATLAS_ENABLE_AUTO_EXECUTION", "0") == "1"


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
    load_trend_model()  # warning-only if missing

    tasks = [asyncio.create_task(broadcaster())]
    if _AUTO_EXEC_ENABLED:
        log.warning(
            "monitor.auto_execution_enabled",
            note="ATLAS_ENABLE_AUTO_EXECUTION=1 — positions will auto-close on stop/target/time",
        )
        tasks.append(asyncio.create_task(_monitor_loop()))
    else:
        log.info("monitor.disabled", reason="auto-execution opt-in; set ATLAS_ENABLE_AUTO_EXECUTION=1 to enable")
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


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
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
