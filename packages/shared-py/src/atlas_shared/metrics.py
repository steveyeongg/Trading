"""Prometheus metrics — the single place ATLAS metric names are defined.

Uses the default global registry so every service shares one namespace.
Metric objects are module-level singletons (Prometheus forbids re-registration);
importing this module anywhere is safe and idempotent.

Render `/metrics` with `render()`.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# --- domain counters -------------------------------------------------------

SIGNALS_TOTAL = Counter(
    "atlas_signals_total", "Signal pipeline outcomes", ["result"]
)  # result: published | vetoed | gated | insufficient_bars

ALERTS_FIRED_TOTAL = Counter(
    "atlas_alerts_fired_total", "Alert channel deliveries", ["channel", "ok"]
)

ORDERS_TOTAL = Counter(
    "atlas_orders_total", "Broker orders", ["intent", "status"]
)  # intent: open|close ; status: filled|rejected

MONITOR_EXITS_TOTAL = Counter(
    "atlas_monitor_exits_total", "Position-monitor auto-exits", ["reason"]
)  # reason: stop|target|time

# --- latency ---------------------------------------------------------------

PIPELINE_SECONDS = Histogram(
    "atlas_pipeline_seconds", "Signal pipeline latency", ["stage"]
)

HTTP_REQUESTS_TOTAL = Counter(
    "atlas_http_requests_total", "HTTP requests", ["method", "path", "status"]
)

HTTP_REQUEST_SECONDS = Histogram(
    "atlas_http_request_seconds", "HTTP request latency", ["method", "path"]
)

# --- gauges ----------------------------------------------------------------

WS_CONNECTIONS = Gauge("atlas_ws_connections", "Active WebSocket connections")


@contextlib.contextmanager
def time_stage(stage: str) -> Iterator[None]:
    """Observe the duration of a pipeline stage into PIPELINE_SECONDS."""
    start = time.perf_counter()
    try:
        yield
    finally:
        PIPELINE_SECONDS.labels(stage=stage).observe(time.perf_counter() - start)


def render() -> tuple[bytes, str]:
    """(body, content_type) for a /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
